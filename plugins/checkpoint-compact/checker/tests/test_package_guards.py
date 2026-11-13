"""Three guards no earlier test held, the future-date refusal's wording, and the pointer's records folder when it is a
symlink. A file dated more than 60 seconds ahead of the clock is refused, with the seconds rounded up in the reason,
and one dated less is accepted; the reminder's state folder must be writable by its owner alone, and one that others
may read is accepted; the reminder's window is at most 1,000,000,000 tokens; the pointer opens a records folder that
is itself a symlink through it, and follows no symlink below it. The checker runs as its own process with a temporary
home; the reminder's two functions are called in-process, as tests/test_context_nudge.py calls them. The margins (62
and 90 seconds ahead, 58 accepted) leave time for the process to start; the bound itself is tested with the checker's
time_reasons() called in-process and the clock fixed: a file 60 seconds ahead is accepted, one 60.001 seconds ahead
refused."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
CHECKER = HERE / "checkpoint.py"
SPEC = importlib.util.spec_from_file_location("context_nudge_guards", HERE / "context_nudge.py")
nudge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nudge)
CHECKER_SPEC = importlib.util.spec_from_file_location("checkpoint_guards", CHECKER)
checkpoint = importlib.util.module_from_spec(CHECKER_SPEC)
CHECKER_SPEC.loader.exec_module(checkpoint)
NOW = 1_800_000_000
SID = "22222222-3333-4444-8555-666666666666"
SETTINGS = ("CHECKPOINT_COMPACT_ROOT", "CONTEXT_NUDGE_WINDOW_TOKENS", "CONTEXT_NUDGE_STATE_DIR")


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="checkpoint-compact-guards-")
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        self.root = self.tmp / "records"
        self.root.mkdir(mode=0o700)
        self.env = {key: value for key, value in os.environ.items() if key not in SETTINGS}
        self.env["HOME"] = str(self.tmp / "home")
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def resume(self, folder, name="RESUME_example.md", ahead=0.0):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(f"# Resume\n\nSession `{SID}`.\n", encoding="utf-8")
        when = time.time() + ahead
        os.utime(path, (when, when))
        return path

    def check(self, path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        r = subprocess.run([sys.executable, "-I", "-B", str(CHECKER), "check", "--resume", str(path), "--sha256", digest,
                            "--session", SID, "--root", str(self.root)], capture_output=True, text=True, env=self.env,
                           timeout=60)
        return r, json.loads(r.stdout)

    def pointer(self, root):
        return subprocess.run([sys.executable, "-I", "-B", str(CHECKER), "pointer", "--hook", "--root", str(root)],
                              input=json.dumps({"session_id": SID}), capture_output=True, text=True, env=self.env,
                              timeout=60)


class FutureDates(Scratch):
    def test_a_file_62_seconds_ahead_is_refused(self):
        r, report = self.check(self.resume(self.root / "lead", ahead=62))
        self.assertEqual(r.returncode, 1, report)
        self.assertFalse(report["ok"])
        self.assertTrue(any("in the future" in reason for reason in report["reasons"]), report["reasons"])

    def test_a_file_90_seconds_ahead_is_refused_with_the_seconds_in_the_reason(self):
        r, report = self.check(self.resume(self.root / "lead", ahead=90))
        self.assertEqual(r.returncode, 1, report)
        self.assertEqual(len(report["reasons"]), 1, report["reasons"])
        self.assertRegex(report["reasons"][0], r"^the resume file is dated (89|90) seconds in the future, more than 60 "
                                               r"seconds ahead of the clock$")

    def test_a_file_58_seconds_ahead_is_accepted(self):
        r, report = self.check(self.resume(self.root / "lead", ahead=58))
        self.assertEqual(r.returncode, 0, report)
        self.assertTrue(report["ok"])


class FutureBound(unittest.TestCase):
    def reasons(self, ahead_ns):
        clock = types.SimpleNamespace(time=lambda: float(NOW))
        with mock.patch.object(checkpoint, "time", clock):
            return checkpoint.time_reasons(NOW * 1_000_000_000 + ahead_ns, 60)

    def test_a_file_exactly_60_seconds_ahead_is_accepted_with_the_clock_fixed(self):
        self.assertEqual(self.reasons(60_000_000_000), [])

    def test_a_file_60_001_seconds_ahead_is_refused_with_61_seconds_in_the_reason(self):
        self.assertEqual(self.reasons(60_001_000_000),
                         ["the resume file is dated 61 seconds in the future, more than 60 seconds ahead of the clock"])


class ReminderState(Scratch):
    def mode_inside(self, folder):
        with nudge._directory(folder) as fd:
            return os.fstat(fd).st_mode & 0o777

    def folder(self, name, mode):
        path = self.tmp / name
        path.mkdir()
        path.chmod(mode)
        return path

    def test_a_group_writable_state_folder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not private to its owner"):
            self.mode_inside(self.folder("state-group", 0o770))

    def test_an_others_writable_state_folder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not private to its owner"):
            self.mode_inside(self.folder("state-others", 0o707))

    def test_a_state_folder_writable_by_its_owner_alone_is_accepted(self):
        self.assertEqual(self.mode_inside(self.folder("state-private", 0o700)), 0o700)

    def test_a_state_folder_others_may_read_but_not_write_is_accepted(self):
        self.assertEqual(self.mode_inside(self.folder("state-readable", 0o755)), 0o755)


class WindowBound(unittest.TestCase):
    def test_one_token_above_the_bound_is_refused(self):
        with self.assertRaisesRegex(ValueError, "exceeds bound"):
            nudge.window_size("claude-example", {"CONTEXT_NUDGE_WINDOW_TOKENS": "1000000001"})

    def test_the_bound_itself_is_accepted(self):
        self.assertEqual(nudge.window_size("claude-example", {"CONTEXT_NUDGE_WINDOW_TOKENS": "1000000000"}),
                         1_000_000_000)


class RecordsFolderLink(Scratch):
    def test_a_records_folder_that_is_a_symlink_is_opened_through_it_and_a_child_symlink_is_not_followed(self):
        real = self.tmp / "records-real"
        older = self.resume(real / "a", "RESUME_a.md", ahead=-60)
        self.resume(self.tmp / "outside", "RESUME_b.md")
        (real / "b").symlink_to(self.tmp / "outside", target_is_directory=True)
        link = self.tmp / "records-link"
        link.symlink_to(real, target_is_directory=True)
        r = self.pointer(link)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(link / "a" / older.name), r.stdout)
        self.assertNotIn("RESUME_b.md", r.stdout)


if __name__ == "__main__":
    unittest.main()
