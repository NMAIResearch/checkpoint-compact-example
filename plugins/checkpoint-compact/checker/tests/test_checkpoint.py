#!/usr/bin/env python3
"""Tests for checkpoint.py.

What matters: `check` passes a resume file only when it is an absolute path to a regular .md file inside the root (after
following symlinks), at most 1 MiB, with the declared SHA-256, modified within the maximum age and naming the session as
its writer;
every failing condition is reported; exit 0 for a pass, 1 for a fail, 2 for a usage error; nothing is written. `pointer`
reads a hook event on stdin, names the newest RESUME_*.md one level below the root naming the event's session as writer,
with its SHA-256, ignores other sessions' files and deeper files, prints a no-file text for none, bad input or a missing
root, can wrap the text as SessionStart additionalContext JSON, and always exits 0. Temporary folders only.

Both entry points require a literal whole id within the first 10 UTF-8 replacement-decoded text lines, with no earlier
whole UUID-shaped id. CRLF counts as one line; a sentence-ending full stop is accepted. Later mentions never displace
the writer's own file. The synthetic header forms, window edges, token boundaries and decoding cases exercise
the same rule through both entry points.

Added after the first independent review: the verdict holds for one file identity, so a path retargeted outside the root,
a file that grows before it is read, a file rewritten or replaced after it is read and a symlink repointed after it is
read are all refused (the interleavings are scheduled at the moment the file is opened or read, through pathname and
descriptor calls alike, so the same tests apply to either way of reading); an unreadable file is refused in JSON, and so
is any unexpected error; age limits must be finite, positive and at most a day; a file dated in the future is refused; an
oversized file still lists its age; a FIFO or a folder is refused without blocking; a symlink inside the root still
passes. The pointer names only bytes it read and re-checked, ignores symlinks (to files or to folders), files over 1 MiB
and names that are not printable UTF-8, skips files dated in the future, does not block on a FIFO, whether there from the
start or swapped in as a file is opened or read, says when the session id is unusable, and exits 0 whatever Unicode its
input, root or file names hold, after an unexpected error and when its output cannot be written.

Added after the re-check of that repair: the pointer neither names a file whose folder became a symlink while the file
was read, nor reads a file through a folder replaced by a symlink just before the open; and a real process whose output
pipe has no reader still exits 0.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
TOOL = HERE.parent / "checkpoint.py"
SID = "11111111-2222-4333-8444-555555555555"
OTHER = "66666666-7777-4888-9999-000000000000"


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_tool():
    spec = importlib.util.spec_from_file_location("checkpoint_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def at_read(target: pathlib.Path, action, when: str):
    """Run action once when the file at target is read: just before the read starts ("before"), or once all of it has
    been read ("after"). It hooks pathname reads (Path.read_bytes, Path.read_text) and descriptor reads (os.read on a
    descriptor of the same file), so it applies whichever way the code under test reads; that code is not mocked."""
    st = target.stat()
    ident = (st.st_dev, st.st_ino)
    fired = []
    real_bytes, real_text, real_read = pathlib.Path.read_bytes, pathlib.Path.read_text, os.read

    def fire():
        if not fired:
            fired.append(True)
            action()

    def read_bytes(path):
        if path == target and when == "before":
            fire()
        data = real_bytes(path)
        if path == target and when == "after":
            fire()
        return data

    def read_text(path, *args, **kwargs):
        if path == target and when == "before":
            fire()
        data = real_text(path, *args, **kwargs)
        if path == target and when == "after":
            fire()
        return data

    def read(fd, count):
        try:
            fst = os.fstat(fd)
            mine = (fst.st_dev, fst.st_ino) == ident
        except OSError:
            mine = False
        if mine and when == "before":
            fire()
        data = real_read(fd, count)
        if mine and when == "after" and not data:
            fire()
        return data

    with mock.patch.object(pathlib.Path, "read_bytes", read_bytes), \
            mock.patch.object(pathlib.Path, "read_text", read_text), mock.patch.object(os, "read", read):
        yield fired


@contextlib.contextmanager
def at_open(target: pathlib.Path, action):
    """Run action once just before the file at target is opened, through os.open (by its path, or by its name relative
    to a folder descriptor) or io.open (which pathlib's reads use), so it applies whichever way the code under test
    opens; that code is not mocked."""
    want = os.fspath(target)
    fired = []
    real_os_open, real_io_open = os.open, io.open

    def fire(path, relative=False):
        if fired or not isinstance(path, (str, bytes, os.PathLike)):
            return
        name = os.fsdecode(os.fspath(path))
        if name == want or (relative and name == target.name):
            fired.append(True)
            action()

    def os_open(path, *args, **kwargs):
        fire(path, kwargs.get("dir_fd") is not None)
        return real_os_open(path, *args, **kwargs)

    def io_open(file, *args, **kwargs):
        fire(file)
        return real_io_open(file, *args, **kwargs)

    with mock.patch.object(os, "open", os_open), mock.patch.object(io, "open", io_open):
        yield fired


def hook_stdin(event):
    """A stdin carrying the event, with both a text and a byte interface."""
    data = event if isinstance(event, bytes) else (event if isinstance(event, str) else json.dumps(event)).encode("utf-8")
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "records"
        self.lead = self.root / "lead"
        self.lead.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resume(self, name="RESUME_2000-01-01_1100.md", text=None, folder=None, age_minutes=0.0):
        path = (folder or self.lead) / name
        path.write_text(text if text is not None else f"# Resume\n\nSession `{SID}`.\n", encoding="utf-8")
        if age_minutes:
            when = time.time() - age_minutes * 60
            os.utime(path, (when, when))
        return path

    def run_tool(self, *args, stdin=""):
        return subprocess.run([sys.executable, "-I", "-B", str(TOOL), *args], input=stdin, capture_output=True, text=True,
                              timeout=60)


class CheckBase(Base):
    def check(self, path, digest=None, session=SID, *extra):
        args = ["check", "--resume", str(path), "--sha256", digest if digest is not None else sha(path), "--session", session,
                "--root", str(self.root), *extra]
        r = self.run_tool(*args)
        return r, (json.loads(r.stdout) if r.stdout.strip() else None)

    def assert_refused(self, r, report, fragment):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertFalse(report["ok"])
        self.assertTrue(any(fragment in reason for reason in report["reasons"]), report["reasons"])


class Check(CheckBase):
    def test_a_good_checkpoint_passes(self):
        path = self.resume()
        r, report = self.check(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(report, {"ok": True, "reasons": [], "resume": str(path), "sha256": sha(path)})

    def test_a_relative_path_is_refused(self):
        self.resume()
        r = self.run_tool("check", "--resume", "lead/RESUME_2000-01-01_1100.md", "--sha256", "0" * 64, "--session", SID,
                          "--root", str(self.root))
        self.assert_refused(r, json.loads(r.stdout), "absolute")

    def test_a_file_outside_the_root_is_refused(self):
        outside = self.resume(folder=self.tmp)
        r, report = self.check(outside)
        self.assert_refused(r, report, "inside")

    def test_a_symlink_out_of_the_root_is_refused(self):
        outside = self.resume(folder=self.tmp)
        link = self.lead / "RESUME_2000-01-01_1200.md"
        link.symlink_to(outside)
        r, report = self.check(link, sha(outside))
        self.assert_refused(r, report, "inside")

    def test_a_file_that_is_not_markdown_is_refused(self):
        r, report = self.check(self.resume(name="RESUME_2000-01-01_1100.txt"))
        self.assert_refused(r, report, ".md")

    def test_a_large_file_is_refused(self):
        r, report = self.check(self.resume(text=f"Session {SID}\n" + "x" * (1 << 20)))
        self.assert_refused(r, report, "bytes, more than 1 MiB")

    def test_a_wrong_digest_is_refused(self):
        r, report = self.check(self.resume(), "0" * 64)
        self.assert_refused(r, report, "SHA-256")

    def test_an_old_file_is_refused(self):
        r, report = self.check(self.resume(age_minutes=90), None, SID, "--max-age-minutes", "60")
        self.assert_refused(r, report, "minutes")

    def test_a_file_without_the_session_id_is_refused(self):
        r, report = self.check(self.resume(text="# Resume\n\nSession unknown.\n"))
        self.assert_refused(r, report, "session")

    def test_a_missing_file_is_refused(self):
        r, report = self.check(self.lead / "RESUME_missing.md", "0" * 64)
        self.assert_refused(r, report, "regular file")

    def test_every_failed_condition_is_listed(self):
        path = self.resume(text="no id here\n", age_minutes=90)
        r, report = self.check(path, "0" * 64)
        self.assertEqual(r.returncode, 1)
        joined = " ".join(report["reasons"])
        for fragment in ("SHA-256", "minutes", "session"):
            self.assertIn(fragment, joined)

    def test_a_usage_error_exits_2(self):
        r = self.run_tool("check", "--resume", "/x.md", "--root", str(self.root))
        self.assertEqual(r.returncode, 2)

    def test_check_writes_nothing(self):
        path = self.resume()
        before = sorted(p.name for p in self.tmp.rglob("*"))
        self.check(path)
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)


class Pointer(Base):
    def pointer(self, event, *extra):
        stdin = event if isinstance(event, str) else json.dumps(event)
        return self.run_tool("pointer", "--hook", "--root", str(self.root), *extra, stdin=stdin)

    def test_names_the_newest_resume_file_of_the_session(self):
        older = self.resume("RESUME_2000-01-01_0900.md", age_minutes=30)
        newer = self.resume("RESUME_2000-01-01_1000.md", age_minutes=5)
        r = self.pointer({"session_id": SID, "hook_event_name": "SessionStart", "source": "compact"})
        self.assertEqual(r.returncode, 0)
        self.assertIn(str(newer), r.stdout)
        self.assertIn(sha(newer), r.stdout)
        self.assertNotIn(str(older), r.stdout)
        self.assertIn("Read it in full before you continue", r.stdout)

    def test_ignores_other_sessions_and_deeper_files(self):
        mine = self.resume("RESUME_2000-01-01_0900.md", age_minutes=30)
        other = self.root / "other-lead"
        other.mkdir()
        self.resume("RESUME_2000-01-01_1000.md", text=f"Session `{OTHER}`.\n", folder=other, age_minutes=1)
        deep = self.lead / "archive"
        deep.mkdir()
        self.resume("RESUME_2000-01-01_1030.md", folder=deep, age_minutes=1)
        r = self.pointer({"session_id": SID})
        self.assertIn(str(mine), r.stdout)
        self.assertNotIn("other-lead", r.stdout)
        self.assertNotIn("archive", r.stdout)

    def test_codex_shaped_events_work(self):
        mine = self.resume()
        r = self.pointer({"session_id": SID, "turn_id": "t1", "source": "compact", "cwd": str(self.lead), "model": "gpt-6"})
        self.assertIn(str(mine), r.stdout)

    def test_no_file_bad_input_or_no_root_still_exit_0(self):
        cases = [({"session_id": OTHER}, ()), ("not json", ()), ({"hook_event_name": "SessionStart"}, ()),
                 ({"session_id": SID}, ("--root", str(self.tmp / "missing")))]
        self.resume()
        for event, extra in cases:
            with self.subTest(event=event, extra=extra):
                r = self.pointer(event, *extra)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("no resume file", r.stdout)

    def test_json_output_for_hooks_that_take_additional_context(self):
        mine = self.resume()
        r = self.pointer({"session_id": SID}, "--json")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(str(mine), out["hookSpecificOutput"]["additionalContext"])


# Synthetic opening lines in the forms that resume files took in use: each keeps the line its writer's id stood on and
# what stood before it on that line and the lines above; missing lines are blank fixture lines.
HEADER_FORMS = (("The lead's form: the id on line 1",
  ((1,
    '# Resume: lead, Claude Code session a1b2c3d4-0001-4aaa-8bbb-111111111111, 2000-01-01, written after '
    'the 2000-01-01T18:48:04Z clock read; supersedes RESUME_2000-01-01_175650Z.md'),)),
 ("A reviewer's form: a short id on line 1, the full id on line 3",
  ((1,
    "# Resume file: reviewer, session b2c3d4e5, after the second study, the first task's reconciliation and "
    "the next task's first return"),
   (3,
    'Written after the 2000-01-01T18:46:08Z clock read. Session: Claude Code 2.1.0, Claude Opus at '
    "max, b2c3d4e5-0002-4bbb-8ccc-222222222222, agent name `reviewer`, role given at the start (this session's "
    'transcript, line 15, 17:19:04Z), ownership noted at the top'))),
 ("A reviewer's earlier form: no id on line 1, the full id on line 3",
  ((1, '# Resume file: reviewer, after tasks 1 to 4 and the second recheck (checkpoint 2)'),
   (3,
    'Written after the 2000-01-01T04:50:49Z clock read. Session: Claude Code '
    'c3d4e5f6-0003-4ccc-8ddd-333333333333, Claude Opus 5.5'),
   (4,
    '(`claude-opus-5-5`, effort max), agent name `reviewer`. The owner gave this session the reviewer role the '
    'day before.'))),
 ('A form whose first full id is on line 4',
  ((1,
    '# Resume file: reviewer, the second edition finished and handed over; next, the stage 3 '
    'plan (task 12)'),
   (2, ''),
   (3, 'Written after the 12:34:38 UTC clock read on 1 January 2000. Session: Claude Code, session id'),
   (4,
    'd4e5f6a7-0004-4ddd-8eee-444444444444, Opus 5.5 (`claude-opus-5-5`), session name "Reviewer". Read this '
    'file first, then'))),
 ('A form whose first full id is on line 5',
  ((1, '# Reviewer: resume file (1 January 2000, after the 10:34:17 UTC clock read)'),
   (2, ''),
   (3,
    'Supersedes `RESUME_2000-01-01_0958.md` (kept; it still holds the round-2 inputs and design '
    'notes in full).'),
   (4,
    '`RESUME_2000-01-01_0632.md` still holds the standing rules from before the first compaction. Written by '
    'the reviewer,'),
   (5,
    'Claude Code 2.1.0, session `e5f6a7b8-0005-4eee-8fff-555555555555`, model `claude-opus-5-5`, effort '
    'max. The owner compacted this'))),
 ('A form whose first full id is on line 6',
  ((1,
    "# Resume file: reviewer, the second result delivered and the third task next "
    '(checkpoint 72)'),
   (2, ''),
   (3,
    'Supersedes `RESUME_reviewer_c3d4e5f6_2000-01-01_0955.md` (01234567...89abcdef, kept unchanged), '
    'which with the 0850, 0420,'),
   (4,
    '0250, 0220, 0138, 0106, 1903, 1805, 1723, 1558, 1505, 1425, 1319, 1139 and 1137 files holds with the '
    'changes below; read'),
   (5,
    'those sixteen files, then this. Written after the 2000-01-01T10:47:24Z clock read. Session: Claude '
    'Code'),
   (6,
    'c3d4e5f6-0003-4ccc-8ddd-333333333333, Claude Opus 5.5, agent name `reviewer`. Paths are '
    'relative to'))),
 ('A form whose first full id is on line 7',
  ((1,
    "# Resume file: reviewer, the third result verified and reconciled, its lineage placed (checkpoint 53)"),
   (2, ''),
   (3,
    'Supersedes `RESUME_reviewer_c3d4e5f6_2000-01-01_0311.md` (89abcdef...01234567, kept unchanged), '
    'which with its'),
   (4,
    'predecessors 0119, 0049, 0017 and 0015 holds with the changes below. Written after the '
    '2000-01-01T03:46:36Z clock read'),
   (5,
    'and completed after the 08:07:01Z one: between the 03:46:36Z and 08:06:50Z reads more than four hours '
    'passed with no'),
   (6,
    "action of the reviewer's (cause unknown); the inbox was empty at 08:07:01Z and the message wait had not "
    'woken.'),
   (7,
    'Session: Claude Code c3d4e5f6-0003-4ccc-8ddd-333333333333, Claude Opus 5.5 (`claude-opus-5-5`, effort '
    'max), agent name'))),
 ('The reproducer: its writer on line 1, three other sessions named in full on lines 21 and 22',
  ((1,
    '# Resume: lead, Claude Code session a1b2c3d4-0001-4aaa-8bbb-111111111111, clock read '
    '2000-01-01T17:03:03Z'),
   (21,
    "- helper: Claude Code session 0d0e0f10-0008-4bbb-8ccc-888888888888, the owner's window, "
    '`work/` under a folder claim, brief '
    '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef. Do not interfere. It talks to the owner in '
    "its own window; the owner's go after t"),
   (22,
    '- Builder (Codex 019a0000-0000-7000-8000-000000000001) and writer (Codex '
    '019a0000-0000-7000-8000-000000000002): closed at 16:50:01Z (`ps`). The owner reopens them later; two '
    "messages wait for them. They are not the owner's to be chased: one ask is enough."))))

WRITER_IDS = ('a1b2c3d4-0001-4aaa-8bbb-111111111111',
 'b2c3d4e5-0002-4bbb-8ccc-222222222222',
 'c3d4e5f6-0003-4ccc-8ddd-333333333333',
 'd4e5f6a7-0004-4ddd-8eee-444444444444',
 'e5f6a7b8-0005-4eee-8fff-555555555555',
 'c3d4e5f6-0003-4ccc-8ddd-333333333333',
 'c3d4e5f6-0003-4ccc-8ddd-333333333333',
 'a1b2c3d4-0001-4aaa-8bbb-111111111111')
WRITER_HEADERS = tuple((name, session, lines) for (name, lines), session
                       in zip(HEADER_FORMS, WRITER_IDS, strict=True))

class WriterIdentity(CheckBase):
    """Both entry points recognise the opening writer declaration, using the synthetic header forms."""

    @staticmethod
    def header_text(numbered):
        lines = [""] * max(number for number, _text in numbered)
        for number, text in numbered:
            lines[number - 1] = text
        return "\n".join(lines) + "\n"

    def pointer_for(self, session):
        return self.run_tool("pointer", "--hook", "--root", str(self.root), stdin=json.dumps({"session_id": session}))

    def assert_repair_reason(self, report):
        joined = " ".join(report["reasons"])
        for fragment in ("session", "add", "whole token", "first full session id", "first 10 lines"):
            self.assertIn(fragment, joined)

    def assert_agrees(self, data, expected, session=SID):
        path = self.resume()
        path.write_bytes(data)
        checked, report = self.check(path, session=session)
        pointed = self.pointer_for(session)
        self.assertEqual(pointed.returncode, 0, pointed.stderr)
        self.assertEqual(pointed.stderr, "")
        self.assertEqual(checked.stderr, "")
        self.assertEqual(checked.returncode, 0 if expected else 1, report)
        self.assertEqual(report["ok"], expected, report)
        found = str(path) in pointed.stdout
        self.assertEqual(found, expected, pointed.stdout)
        self.assertEqual(report["ok"], found)
        if expected:
            self.assertEqual(report["reasons"], [])
            self.assertIn(sha(path), pointed.stdout)
            self.assertIn("your newest checkpoint", pointed.stdout)
        else:
            self.assert_repair_reason(report)
            self.assertEqual(
                pointed.stdout,
                f"Compaction: no resume file naming this session ({session}) was found one level "
                f"below {self.root}. Check your own records before you continue.\n",
            )

    def test_reproducer_pointer_refuses_line_21_mention(self):
        path = self.resume(text=self.header_text(HEADER_FORMS[-1][1]))
        result = self.pointer_for("0d0e0f10-0008-4bbb-8ccc-888888888888")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no resume file", result.stdout)
        self.assertNotIn(str(path), result.stdout)

    def test_reproducer_check_refuses_line_21_mention_with_repair_reason(self):
        path = self.resume(text=self.header_text(HEADER_FORMS[-1][1]))
        result, report = self.check(path, session="0d0e0f10-0008-4bbb-8ccc-888888888888")
        self.assert_refused(result, report, "session")
        self.assert_repair_reason(report)

    def test_reproducer_still_accepts_its_writer(self):
        self.assert_agrees(self.header_text(HEADER_FORMS[-1][1]).encode("utf-8"), True,
                           session="a1b2c3d4-0001-4aaa-8bbb-111111111111")

    def test_a_true_match_is_found(self):
        self.assert_agrees(f"# Resume\n\nSession [{SID}]\n".encode("utf-8"), True)

    def test_every_header_form(self):
        for name, session, numbered in WRITER_HEADERS:
            with self.subTest(header=name):
                self.assert_agrees(self.header_text(numbered).encode("utf-8"), True, session=session)

    def test_a_newer_mention_never_beats_the_writers_older_file(self):
        mine = self.resume("RESUME_older.md", age_minutes=5)
        self.resume("RESUME_newer.md", text=f"Session {OTHER}\n" + "\n" * 19 + f"Mentions {SID}\n")
        result = self.pointer_for(SID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(mine), result.stdout)
        self.assertNotIn("RESUME_newer.md", result.stdout)

    def test_another_full_id_first_is_refused_for_s_and_accepted_for_its_writer(self):
        data = f"Session {OTHER}, then session {SID}\n".encode("utf-8")
        self.assert_agrees(data, False)
        self.assert_agrees(data, True, session=OTHER)

    def test_line_10_is_inside_the_window(self):
        self.assert_agrees(("\n" * 9 + SID).encode("utf-8"), True)

    def test_line_11_is_outside_the_window(self):
        self.assert_agrees(("\n" * 10 + SID).encode("utf-8"), False)

    def test_a_late_first_id_is_outside_the_window(self):
        self.assert_agrees(("\n" * 20 + SID).encode("utf-8"), False)

    def test_ids_inside_longer_tokens_are_refused(self):
        for prefix, suffix in (("x", ""), ("_", ""), (".", ""), (":", ""), ("-", ""),
                               ("", "x"), ("", "_"), ("", ":"), ("", "-"), ("", ".tail"), ("", "..")):
            with self.subTest(prefix=prefix, suffix=suffix):
                self.assert_agrees(f"{prefix}{SID}{suffix}\n".encode("utf-8"), False)

    def test_header_punctuation_and_end_of_line_are_accepted(self):
        for text in (f"{SID}, text", f"{SID}.", f"{SID}. More", f"\x60{SID}\x60",
                     f"[{SID}]", f"({SID})", SID):
            with self.subTest(text=text):
                self.assert_agrees((text + "\n").encode("utf-8"), True)

    def test_custom_ids_are_matched_literally(self):
        session = "worker.582:run"
        for text, expected in ((session, True), ("workerX582:run", False), ("worker.582Xrun", False),
                               ("worker.582:run.extra", False), (session + ".", True)):
            with self.subTest(text=text):
                self.assert_agrees(text.encode("utf-8"), expected, session=session)

    def test_an_id_in_another_case_is_not_the_writer(self):
        session = "Worker.582:Run"
        for text, expected in ((session, True), (session.lower(), False), (session.upper(), False)):
            with self.subTest(text=text):
                self.assert_agrees(text.encode("utf-8"), expected, session=session)

    def test_an_empty_id_is_refused(self):
        path = self.resume()
        r, report = self.check(path, session="")
        self.assert_refused(r, report, "no id given")

    def test_custom_ids_require_no_earlier_uuid(self):
        session = "worker-582"
        for text, expected in ((session, True), (f"{SID}\n{session}", False),
                               (f"{session}\n{SID}", True), (f"{SID}, {session}", False),
                               (f"{session}, {SID}", True), ("\n" * 10 + session, False)):
            with self.subTest(text=text):
                self.assert_agrees(text.encode("utf-8"), expected, session=session)

    def test_uuid_tokens_must_be_whole_and_uuid_shape_includes_uppercase(self):
        upper = "ABCDEF01-2345-6789-ABCD-EF0123456789"
        for text, expected in ((f"{upper}\n{SID}", False), (f"x{OTHER}\n{SID}", True),
                               (f"{OTHER}.tail\n{SID}", True), (f"{OTHER}.\n{SID}", False)):
            with self.subTest(text=text):
                self.assert_agrees(text.encode("utf-8"), expected)

    def test_crlf_counts_as_one_line(self):
        self.assert_agrees(("\r\n" * 9 + SID + "\r\n").encode("utf-8"), True)
        self.assert_agrees(("\r\n" * 10 + SID + "\r\n").encode("utf-8"), False)

    def test_no_newline_and_empty_file_have_stated_results(self):
        self.assert_agrees(SID.encode("utf-8"), True)
        self.assert_agrees(b"", False)

    def test_undecodable_bytes_are_replaced_without_blocking_pointer(self):
        self.assert_agrees(b"\xff\nSession " + SID.encode("utf-8") + b"\xfe\n", True)
        self.assert_agrees(b"\xff\n" + OTHER.encode("utf-8") + b"\n" + SID.encode("utf-8"), False)
        self.assert_agrees(SID[:8].encode("utf-8") + b"\xff" + SID[8:].encode("utf-8"), False)

    def test_standard_splitlines_separators_count_as_lines(self):
        for separator in ("\r", "\u0085", "\u2028"):
            with self.subTest(separator=repr(separator)):
                self.assert_agrees((separator * 9 + SID).encode("utf-8"), True)
                self.assert_agrees((separator * 10 + SID).encode("utf-8"), False)

    def test_check_and_pointer_agree_for_a_table_of_file_shapes(self):
        cases = (
            (f"Session {SID}\n", True), (f"Session {OTHER}\nMentions {SID}\n", False),
            ("\n" * 9 + SID, True), ("\n" * 10 + SID, False),
            (f"prefix{SID}", False), (f"{SID}.", True), ("", False),
            (f"{SID}\nLater {OTHER}", True), (f"{OTHER}, {SID}", False),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assert_agrees(text.encode("utf-8"), expected)

    def test_an_oversized_file_still_reports_the_session_id_not_checked(self):
        path = self.resume(text=f"Session {SID}\n" + "x" * (1 << 20))
        result, report = self.check(path)
        self.assert_refused(result, report, "the session id were not checked")
        self.assertNotIn("add", " ".join(report["reasons"]))


class Races(Base):
    """A check's verdict must hold for one file identity, whatever happens to the path while it runs."""

    def setUp(self):
        super().setUp()
        self.tool = load_tool()

    def run_check(self, path, digest):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = self.tool.main(["check", "--resume", str(path), "--sha256", digest, "--session", SID,
                                   "--root", str(self.root)])
        return code, json.loads(out.getvalue())

    def assert_refused_after(self, fired, code, report):
        self.assertTrue(fired, "the interleaving never happened")
        self.assertEqual(code, 1, report)
        self.assertFalse(report["ok"])

    def test_a_path_retargeted_outside_the_root_during_the_check_is_refused(self):
        path = self.resume()
        digest = sha(path)
        outside = self.tmp / "outside.md"
        outside.write_bytes(path.read_bytes())

        def retarget():
            path.rename(path.with_name("preserved.md"))
            path.symlink_to(outside)

        with at_read(path, retarget, "before") as fired:
            code, report = self.run_check(path, digest)
        self.assert_refused_after(fired, code, report)

    def test_a_file_that_grows_before_it_is_read_is_refused(self):
        path = self.resume()
        big = path.read_bytes() + b"x" * (1 << 20)
        with at_read(path, lambda: path.write_bytes(big), "before") as fired:
            code, report = self.run_check(path, hashlib.sha256(big).hexdigest())
        self.assert_refused_after(fired, code, report)
        self.assertTrue(any("1 MiB" in reason for reason in report["reasons"]), report["reasons"])

    def test_a_file_rewritten_in_place_after_it_was_read_is_refused(self):
        path = self.resume(age_minutes=2)
        digest = sha(path)
        with at_read(path, lambda: path.write_text(f"# Resume\n\nSession `{OTHER}`.\n", encoding="utf-8"), "after") as fired:
            code, report = self.run_check(path, digest)
        self.assert_refused_after(fired, code, report)

    def test_a_file_replaced_after_it_was_read_is_refused(self):
        path = self.resume()
        digest = sha(path)

        def replace():
            incoming = path.with_name("incoming.md")
            incoming.write_text(f"Session `{OTHER}`.\n", encoding="utf-8")
            incoming.replace(path)

        with at_read(path, replace, "after") as fired:
            code, report = self.run_check(path, digest)
        self.assert_refused_after(fired, code, report)

    def test_a_symlink_repointed_after_it_was_read_is_refused(self):
        first = self.resume("RESUME_2000-01-01_1000.md")
        second = self.resume("RESUME_2000-01-01_1001.md", text=f"# Resume\n\nSession `{OTHER}`.\n")
        link = self.lead / "RESUME_2000-01-01_1100.md"
        link.symlink_to(first)
        digest = sha(first)

        def repoint():
            incoming = self.lead / "incoming-link"
            incoming.symlink_to(second)
            incoming.replace(link)

        with at_read(first, repoint, "after") as fired:
            code, report = self.run_check(link, digest)
        self.assert_refused_after(fired, code, report)


class Edges(CheckBase):
    def test_an_unreadable_file_is_refused_in_json(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a file whatever its mode")
        path = self.resume()
        digest = sha(path)
        path.chmod(0)
        try:
            r, report = self.check(path, digest)
        finally:
            path.chmod(0o600)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIsNotNone(report, r.stderr)
        self.assertFalse(report["ok"])
        self.assertIn("Permission denied", " ".join(report["reasons"]))
        self.assertNotIn("Traceback", r.stderr)

    def test_an_unexpected_error_is_still_a_json_refusal(self):
        tool = load_tool()
        path = self.resume()
        out = io.StringIO()
        with mock.patch.object(tool, "check", side_effect=RuntimeError("unexpected")), contextlib.redirect_stdout(out):
            code = tool.main(["check", "--resume", str(path), "--sha256", sha(path), "--session", SID,
                              "--root", str(self.root)])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertIn("RuntimeError", " ".join(report["reasons"]))

    def test_age_limits_must_be_finite_positive_and_at_most_a_day(self):
        path = self.resume(age_minutes=90)
        for value in ("nan", "inf", "-inf", "0", "-5", "1441", "1e9"):
            with self.subTest(value=value):
                r = self.run_tool("check", "--resume", str(path), "--sha256", sha(path), "--session", SID,
                                  "--root", str(self.root), "--max-age-minutes", value)
                self.assertEqual(r.returncode, 2, r.stdout)
        r, report = self.check(path, None, SID, "--max-age-minutes", "1440")
        self.assertEqual(r.returncode, 0, report)

    def test_a_symlink_inside_the_root_passes(self):
        target = self.resume()
        link = self.lead / "RESUME_2000-01-01_1200.md"
        link.symlink_to(target)
        r, report = self.check(link, sha(target))
        self.assertEqual(r.returncode, 0, report)

    def test_a_file_dated_in_the_future_is_refused(self):
        r, report = self.check(self.resume(age_minutes=-24 * 60))
        self.assert_refused(r, report, "future")

    def test_an_oversized_file_still_lists_its_age(self):
        r, report = self.check(self.resume(text=f"Session {SID}\n" + "x" * (1 << 20), age_minutes=90), "0" * 64)
        self.assertEqual(r.returncode, 1)
        joined = " ".join(report["reasons"])
        for fragment in ("MiB", "minutes", "not checked"):
            self.assertIn(fragment, joined)

    def test_a_fifo_or_a_folder_is_refused_without_blocking(self):
        fifo = self.lead / "RESUME_fifo.md"
        os.mkfifo(fifo)
        folder = self.lead / "RESUME_folder.md"
        folder.mkdir()
        for path in (fifo, folder):
            with self.subTest(path=path.name):
                r, report = self.check(path, "0" * 64)
                self.assert_refused(r, report, "regular file")


class PointerSafety(Base):
    def pointer(self, event, *extra):
        stdin = event if isinstance(event, bytes) else (event if isinstance(event, str) else json.dumps(event)).encode("utf-8")
        return subprocess.run([sys.executable, "-I", "-B", str(TOOL), "pointer", "--hook", "--root", str(self.root), *extra],
                              input=stdin, capture_output=True, timeout=60)

    def test_symlinks_and_large_files_are_not_named(self):
        outside = self.tmp / "outside.md"
        outside.write_text(f"Session `{SID}`.\n", encoding="utf-8")
        (self.lead / "RESUME_2000-01-01_1200.md").symlink_to(outside)
        self.resume("RESUME_2000-01-01_1300.md", text=f"Session `{SID}`.\n" + "x" * (1 << 20))
        r = self.pointer({"session_id": SID})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"no resume file", r.stdout)

    def test_a_file_dated_in_the_future_is_not_named(self):
        mine = self.resume("RESUME_2000-01-01_0900.md", age_minutes=30)
        self.resume("RESUME_2000-01-01_1000.md", age_minutes=-24 * 60)
        r = self.pointer({"session_id": SID})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.fsencode(str(mine)), r.stdout)
        self.assertNotIn(b"RESUME_2000-01-01_1000.md", r.stdout)

    def test_a_folder_linked_from_outside_the_root_is_not_searched(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "RESUME_2000-01-01_1200.md").write_text(f"Session `{SID}`.\n", encoding="utf-8")
        (self.root / "linked-lead").symlink_to(outside, target_is_directory=True)
        r = self.pointer({"session_id": SID})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"no resume file", r.stdout)

    def test_a_fifo_among_the_resume_files_does_not_block(self):
        mine = self.resume(age_minutes=5)
        os.mkfifo(self.lead / "RESUME_2000-01-01_1300.md")
        try:
            r = self.pointer({"session_id": SID})
        except subprocess.TimeoutExpired:
            self.fail("the pointer blocked on a FIFO")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(os.fsencode(str(mine)), r.stdout)

    def test_malformed_unicode_never_fails_the_hook(self):
        regular = self.resume(age_minutes=5)
        odd = os.fsencode(str(self.lead)) + b"/RESUME_\xff.md"
        with open(odd, "wb") as handle:
            handle.write(f"Session `{SID}`.\n".encode("utf-8"))
        for stdin in (b'{"session_id":"\\ud800"}', b'\xff\xfe{"session_id": 1}', json.dumps({"session_id": SID}).encode("utf-8")):
            with self.subTest(stdin=stdin):
                r = self.pointer(stdin)
                self.assertEqual(r.returncode, 0, r.stderr)
                out = r.stdout.decode("utf-8")
                if b"ud800" in stdin:
                    self.assertIn("no usable session id", out)
                if SID.encode("ascii") in stdin:
                    self.assertIn(str(regular), out)
                    self.assertNotIn("RESUME_\\xff", out)
        odd_root = os.fsencode(str(self.tmp)) + b"/root-\xff"
        os.mkdir(odd_root)
        with self.subTest(root=odd_root):
            r = subprocess.run([sys.executable, "-I", "-B", str(TOOL), "pointer", "--hook", "--root", odd_root],
                               input=json.dumps({"session_id": SID}).encode("utf-8"), capture_output=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no resume file", r.stdout.decode("utf-8"))

    def pointer_here(self, tool):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", hook_stdin({"session_id": SID})), contextlib.redirect_stdout(out):
            code = tool.main(["pointer", "--hook", "--root", str(self.root)])
        return code, out.getvalue()

    def test_an_unexpected_error_still_exits_0(self):
        tool = load_tool()
        self.resume()
        with mock.patch.object(tool, "pointer_text", side_effect=RuntimeError("unexpected")):
            code, text = self.pointer_here(tool)
        self.assertEqual(code, 0)
        self.assertIn("could not be worked out", text)

    def test_a_failed_write_still_exits_0(self):
        tool = load_tool()
        self.resume()

        class Broken(io.TextIOBase):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        with mock.patch.object(sys, "stdin", hook_stdin({"session_id": SID})), mock.patch.object(sys, "stdout", Broken()):
            code = tool.main(["pointer", "--hook", "--root", str(self.root)])
        self.assertEqual(code, 0)

    def test_an_output_pipe_without_a_reader_still_exits_0(self):
        """A real process whose stdout is a pipe nobody reads: the write fails, and so would the flush Python makes as it
        exits, unless the pointer has dealt with it."""
        self.resume()
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            r = subprocess.run([sys.executable, "-I", "-B", str(TOOL), "pointer", "--hook", "--root", str(self.root)],
                               input=json.dumps({"session_id": SID}).encode("utf-8"), stdout=write_end,
                               stderr=subprocess.PIPE, timeout=60)
        finally:
            os.close(write_end)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_folder_swapped_for_a_symlink_while_its_file_is_read_is_not_named(self):
        """The folder is renamed while its resume file is being read and a symlink to it takes its old name: the path
        the pointer would print now goes through a symlinked folder, so the file is not named."""
        path = self.resume()

        def swap():
            self.lead.rename(self.root / "moved")
            self.lead.symlink_to(self.root / "moved", target_is_directory=True)

        tool = load_tool()
        out = io.StringIO()
        with at_read(path, swap, "before") as fired, mock.patch.object(sys, "stdin", hook_stdin({"session_id": SID})), \
                contextlib.redirect_stdout(out):
            code = tool.main(["pointer", "--hook", "--root", str(self.root)])
        self.assertTrue(fired, "the interleaving never happened")
        self.assertEqual(code, 0)
        self.assertIn("no resume file", out.getvalue())
        self.assertNotIn("RESUME_2000-01-01_1100.md", out.getvalue())

    def test_a_folder_swapped_for_a_symlink_out_of_the_root_is_never_read(self):
        """Just before the listed resume file is opened, its folder is replaced by a symlink to a folder outside the root
        that holds a file of the same name: that outside file must never be read."""
        path = self.resume()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / path.name).write_text(f"Session `{SID}`, outside the records folder.\n", encoding="utf-8")

        def swap():
            self.lead.rename(self.root / "moved")
            self.lead.symlink_to(outside, target_is_directory=True)

        tool = load_tool()
        out = io.StringIO()
        with at_read(outside / path.name, lambda: None, "before") as read_outside, at_open(path, swap) as fired, \
                mock.patch.object(sys, "stdin", hook_stdin({"session_id": SID})), contextlib.redirect_stdout(out):
            code = tool.main(["pointer", "--hook", "--root", str(self.root)])
        self.assertTrue(fired, "the interleaving never happened")
        self.assertEqual(code, 0)
        self.assertFalse(read_outside, "the pointer read a file outside the root")
        self.assertIn("no resume file", out.getvalue())  # the path now leads outside, so it is not named either

    def test_a_file_replaced_after_its_scan_is_not_named(self):
        tool = load_tool()
        path = self.resume()

        def replace():
            incoming = path.with_name("incoming.md")
            incoming.write_text(f"Session `{OTHER}`.\n", encoding="utf-8")
            incoming.replace(path)

        out = io.StringIO()
        with at_read(path, replace, "after") as fired, mock.patch.object(sys, "stdin", hook_stdin({"session_id": SID})), \
                contextlib.redirect_stdout(out):
            code = tool.main(["pointer", "--hook", "--root", str(self.root)])
        self.assertTrue(fired, "the interleaving never happened")
        self.assertEqual(code, 0)
        self.assertNotIn(sha(path), out.getvalue())
        self.assertIn("no resume file", out.getvalue())

    def test_a_file_swapped_for_a_fifo_during_the_scan_does_not_block(self):
        path = self.resume()
        child = textwrap.dedent(f"""
            import json, os, pathlib, sys
            sys.path.insert(0, {str(HERE)!r})
            import test_checkpoint as t
            tool = t.load_tool()
            target = pathlib.Path({str(path)!r})

            def swap():
                target.rename(target.with_name("preserved.md"))
                os.mkfifo(target)

            with t.at_read(target, swap, "before"):
                sys.stdin = t.hook_stdin({{"session_id": {SID!r}}})
                sys.exit(tool.main(["pointer", "--hook", "--root", {str(self.root)!r}]))
        """)
        try:
            r = subprocess.run([sys.executable, "-I", "-B", "-c", child], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the pointer blocked on a FIFO")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_file_swapped_for_a_fifo_as_it_is_opened_does_not_block(self):
        path = self.resume()
        child = textwrap.dedent(f"""
            import os, pathlib, sys
            sys.path.insert(0, {str(HERE)!r})
            import test_checkpoint as t
            tool = t.load_tool()
            target = pathlib.Path({str(path)!r})

            def swap():
                target.rename(target.with_name("preserved.md"))
                os.mkfifo(target)

            with t.at_open(target, swap) as fired:
                sys.stdin = t.hook_stdin({{"session_id": {SID!r}}})
                code = tool.main(["pointer", "--hook", "--root", {str(self.root)!r}])
            sys.exit(code if fired else 3)
        """)
        try:
            r = subprocess.run([sys.executable, "-I", "-B", "-c", child], capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the pointer blocked on a FIFO")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no resume file", r.stdout)


if __name__ == "__main__":
    unittest.main()
