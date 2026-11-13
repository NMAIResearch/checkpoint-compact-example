"""The package's settings for the checker and the reminder: each default, each override, and that a default creates
nothing by itself. Every run has a temporary home folder, and the tools run as their own processes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent.parent
CHECKER = HERE / "checkpoint.py"
NUDGE = HERE / "context_nudge.py"
SID = "11111111-2222-4333-8444-555555555555"
SETTINGS = ("CHECKPOINT_COMPACT_ROOT", "CONTEXT_NUDGE_WINDOW_TOKENS", "CONTEXT_NUDGE_STATE_DIR")


class Home(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="checkpoint-compact-")
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items() if key not in SETTINGS}
        self.env["HOME"] = str(self.home)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def run_tool(self, tool, *args, stdin="", env=None):
        return subprocess.run([sys.executable, "-I", "-B", str(tool), *args], input=stdin, capture_output=True,
                              text=True, env=self.env if env is None else env, timeout=60)


class CheckerRoot(Home):
    def default(self):
        return self.home / ".local" / "state" / "checkpoint-compact"

    def resume(self, root):
        folder = root / "lead"
        folder.mkdir(parents=True)
        path = folder / "RESUME_example.md"
        path.write_text(f"# Resume\n\nSession `{SID}`.\n", encoding="utf-8")
        return path

    def check(self, path, *extra, env=None):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        r = self.run_tool(CHECKER, "check", "--resume", str(path), "--sha256", digest, "--session", SID, *extra, env=env)
        return r, json.loads(r.stdout)

    def pointer(self, env=None):
        return self.run_tool(CHECKER, "pointer", "--hook", stdin=json.dumps({"session_id": SID}), env=env)

    def test_the_default_records_folder_is_in_the_home_folders_local_state(self):
        path = self.resume(self.default())
        r, report = self.check(path)
        self.assertEqual(r.returncode, 0, report)
        self.assertEqual(report["reasons"], [])
        self.assertIn(str(path), self.pointer().stdout)

    def test_a_missing_default_folder_is_named_and_nothing_creates_it(self):
        path = self.resume(self.tmp / "elsewhere")
        r, report = self.check(path)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"the records folder {self.default()} cannot be found", " ".join(report["reasons"]))
        self.pointer()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_the_setting_names_the_records_folder(self):
        records = self.tmp / "records"
        mine = self.resume(records)
        env = dict(self.env, CHECKPOINT_COMPACT_ROOT=str(records))
        r, report = self.check(mine, env=env)
        self.assertEqual(r.returncode, 0, report)
        self.assertIn(str(mine), self.pointer(env=env).stdout)
        at_default = self.resume(self.default())
        r, report = self.check(at_default, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"must be inside {records}", " ".join(report["reasons"]))

    def test_an_empty_setting_counts_as_unset(self):
        path = self.resume(self.default())
        r, report = self.check(path, env=dict(self.env, CHECKPOINT_COMPACT_ROOT=""))
        self.assertEqual(r.returncode, 0, report)

    def test_the_root_argument_wins_over_the_setting(self):
        other = self.tmp / "other"
        path = self.resume(other)
        env = dict(self.env, CHECKPOINT_COMPACT_ROOT=str(self.tmp / "records"))
        r, report = self.check(path, "--root", str(other), env=env)
        self.assertEqual(r.returncode, 0, report)


class ReminderSettings(Home):
    def setUp(self):
        super().setUp()
        self.transcript = self.tmp / (SID + ".jsonl")
        self.event = json.dumps({"session_id": SID, "transcript_path": str(self.transcript),
                                 "hook_event_name": "PostToolUse"})

    def remind(self, used, env=None):
        row = {"type": "assistant", "sessionId": SID, "isSidechain": False,
               "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": used}}}
        self.transcript.write_text(json.dumps(row) + "\n", encoding="utf-8")
        r = self.run_tool(NUDGE, stdin=self.event, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        return r.stdout

    def state_file(self, folder):
        return folder / (hashlib.sha256(SID.encode()).hexdigest() + ".json")

    def test_the_default_state_folder_is_in_the_home_folders_local_state(self):
        self.assertIn("Recorded context 60%", self.remind(600_000))
        self.assertTrue(self.state_file(self.home / ".local" / "state" / "context-nudge").is_file())

    def test_the_state_setting_names_the_state_folder(self):
        state = self.tmp / "state"
        self.assertIn("Recorded context 60%", self.remind(600_000, env=dict(self.env, CONTEXT_NUDGE_STATE_DIR=str(state))))
        self.assertTrue(self.state_file(state).is_file())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_the_window_setting_names_the_window(self):
        env = dict(self.env, CONTEXT_NUDGE_STATE_DIR=str(self.tmp / "state"))
        self.assertEqual(self.remind(600_000, env=dict(env, CONTEXT_NUDGE_WINDOW_TOKENS="2000000")), "")
        self.assertIn("600000/1000000", self.remind(600_000, env=env))


if __name__ == "__main__":
    unittest.main()
