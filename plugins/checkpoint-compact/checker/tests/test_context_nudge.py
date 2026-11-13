"""Synthetic behaviour and late-path checks for the quiet context reminder."""

import concurrent.futures
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOL = Path(__file__).resolve().parents[1] / "context_nudge.py"
SPEC = importlib.util.spec_from_file_location("context_nudge", TOOL)
NUDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NUDGE)


def request(session="session-a", used=0, model="claude-opus-5-5", **extra):
    row = {
        "type": "assistant", "sessionId": session, "isSidechain": False,
        "message": {"model": model, "usage": {"input_tokens": used}},
    }
    row.update(extra)
    return row


class ContextNudgeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="context-nudge-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.sid = "session-a"
        self.transcript = self.root / (self.sid + ".jsonl")
        self.state = self.root / "state"
        self.env = dict(os.environ)
        self.env.pop("CONTEXT_NUDGE_WINDOW_TOKENS", None)
        self.env["CONTEXT_NUDGE_STATE_DIR"] = str(self.state)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.event = {
            "session_id": self.sid, "transcript_path": str(self.transcript),
            "hook_event_name": "PostToolUse",
        }

    def write(self, rows):
        self.transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def invoke(self, event=None, raw=None, env=None):
        payload = raw if raw is not None else json.dumps(self.event if event is None else event)
        process = subprocess.run(
            [sys.executable, "-I", "-B", "-W", "error::ResourceWarning", str(TOOL)], input=payload, text=True,
            capture_output=True, env=self.env if env is None else env, timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return process.stdout

    def text(self, output):
        payload = json.loads(output)
        self.assertEqual(set(payload), {"hookSpecificOutput"})
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], self.event["hook_event_name"])
        return payload["hookSpecificOutput"]["additionalContext"]

    def scaled(self):
        self.env["CONTEXT_NUDGE_WINDOW_TOKENS"] = "1000"

    def test_additive_input_cache_fields_not_output_or_nested_cache_totals(self):
        self.scaled()
        row = request(used=100)
        row["message"]["usage"].update(
            cache_creation_input_tokens=200, cache_read_input_tokens=300,
            output_tokens=9000, cache_creation={"ephemeral_5m_input_tokens": 200},
        )
        self.write([row])
        text = self.text(self.invoke())
        self.assertIn("60%", text)
        self.assertIn("600/1000 input tokens; N=1000", text)
        self.assertIn("next clean point", text)

    def test_all_thresholds_once_and_only_five_point_steps_after_red(self):
        self.scaled()
        for used, expected in ((499, None), (500, "Plan a clean"), (599, None),
                               (600, "next clean point"), (799, None), (800, "Checkpoint now"),
                               (849, None), (850, "Checkpoint now"), (899, None),
                               (900, "Checkpoint now"), (950, "Checkpoint now"),
                               (1000, "Checkpoint now"), (1050, "Checkpoint now")):
            with self.subTest(used=used):
                self.write([request(used=used)])
                output = self.invoke()
                if expected is None:
                    self.assertEqual(output, "")
                else:
                    self.assertIn(expected, self.text(output))
                    self.assertEqual(self.invoke(), "")

    def test_jump_emits_highest_band_and_never_later_lower_advice(self):
        self.scaled()
        self.write([request(used=850)])
        self.assertIn("Checkpoint now", self.text(self.invoke()))
        for used in (800, 600, 500):
            self.write([request(used=used)])
            self.assertEqual(self.invoke(), "")

    def test_rearm_strictly_below_thirty_percent(self):
        self.scaled()
        for used, emitted in ((800, True), (300, False), (800, False),
                               (299, False), (800, True)):
            self.write([request(used=used)])
            self.assertEqual(bool(self.invoke()), emitted)

    def test_both_hook_events_share_one_session_state(self):
        self.scaled()
        self.write([request(used=600)])
        self.assertTrue(self.invoke())
        event = dict(self.event, hook_event_name="UserPromptSubmit")
        self.assertEqual(self.invoke(event), "")

    def test_prompt_hook_emits_the_prompt_event_name(self):
        self.scaled()
        self.write([request(used=600)])
        self.event["hook_event_name"] = "UserPromptSubmit"
        self.assertIn("next clean point", self.text(self.invoke()))

    def test_known_and_unknown_model_windows_and_override(self):
        self.write([request(used=100_000)])
        self.assertEqual(self.invoke(), "")
        self.write([request(used=100_000, model="unknown-model")])
        self.assertIn("100000/200000", self.text(self.invoke()))
        self.env["CONTEXT_NUDGE_WINDOW_TOKENS"] = "400000"
        self.assertEqual(self.invoke(), "")

    def test_every_claude_model_counts_one_million_and_other_names_do_not(self):
        claude = ("claude-opus-5-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5", "claude-haiku-5-5")
        for model in claude:
            with self.subTest(model=model):
                self.assertEqual(NUDGE.window_size(model, {}), 1_000_000)
                self.write([request(used=187_267, model=model)])
                self.assertEqual(self.invoke(), "")
                self.write([request(used=600_000, model=model)])
                text = self.text(self.invoke())
                self.assertIn("600000/1000000", text)
                self.assertIn("next clean point", text)
                self.write([request(used=100_000, model=model)])
                self.assertEqual(self.invoke(), "")
        for model in ("", "unknown-model", "claude", "xclaude-fable-5-2", "gpt-6.1-sol", None):
            with self.subTest(model=model):
                self.assertEqual(NUDGE.window_size(model, {}), 200_000)
        self.assertEqual(NUDGE.window_size("claude-fable-5-2", {"CONTEXT_NUDGE_WINDOW_TOKENS": "300000"}), 300_000)

    def test_invalid_window_override_is_quiet(self):
        self.write([request(used=900_000)])
        for value in ("0", "-1", "1.5", "abc", "1000000001", " "):
            with self.subTest(value=value):
                env = dict(self.env, CONTEXT_NUDGE_WINDOW_TOKENS=value)
                self.assertEqual(self.invoke(env=env), "")

    def test_wrong_session_and_sidechains_do_not_replace_latest_main_request(self):
        self.scaled()
        self.write([request(used=500), request(session="foreign", used=950),
                    request(used=950, isSidechain=True), request(used=950, agentId="child")])
        self.assertIn("50%", self.text(self.invoke()))

    def test_hook_agent_identifier_and_sidechain_are_ignored(self):
        self.write([request(used=900_000)])
        for event in (dict(self.event, agent_id="child"), dict(self.event, agent_id=None),
                      dict(self.event, isSidechain=True)):
            self.assertEqual(self.invoke(event), "")

    def test_missing_transcript_session_identity_is_not_assumed(self):
        row = request(used=900_000)
        del row["sessionId"]
        self.write([row])
        self.assertEqual(self.invoke(), "")

    def test_compaction_boundary_invalidates_old_usage_until_a_new_request(self):
        boundary = {"type": "system", "subtype": "compact_boundary", "sessionId": self.sid}
        self.write([request(used=900_000), boundary])
        self.assertEqual(self.invoke(), "")
        self.write([request(used=900_000), boundary, request(used=200_000)])
        self.assertEqual(self.invoke(), "")

    def test_foreign_compaction_does_not_clear_main_usage(self):
        self.write([request(used=600_000),
                    {"type": "system", "subtype": "compact_boundary", "sessionId": "foreign"}])
        self.assertIn("60%", self.text(self.invoke()))

    def test_latest_request_replaces_previous_usage_without_summing_requests(self):
        self.write([request(used=900_000), request(used=100_000)])
        self.assertEqual(self.invoke(), "")

    def test_invalid_latest_main_usage_does_not_reuse_stale_high_usage(self):
        row = request(used=900_000)
        row["message"]["usage"]["input_tokens"] = "invalid"
        self.write([request(used=900_000), row])
        self.assertEqual(self.invoke(), "")

    def test_bad_token_fields_are_not_numbers_or_zero_defaults(self):
        for field in NUDGE.TOKEN_FIELDS:
            for value in (-1, True, 1.5, "500000", None, 1_000_000_001):
                with self.subTest(field=field, value=value):
                    row = request(used=900_000)
                    row["message"]["usage"][field] = value
                    self.write([row])
                    self.assertEqual(self.invoke(), "")

    def test_missing_input_usage_is_not_inferred_from_cache(self):
        row = request()
        row["message"]["usage"] = {"cache_read_input_tokens": 900_000}
        self.write([row])
        self.assertEqual(self.invoke(), "")

    def test_broken_lines_and_partial_last_write_do_not_hide_a_complete_request(self):
        line = json.dumps(request(used=600_000)).encode()
        self.transcript.write_bytes(b"not json\n" + line + b'\n{"partial":')
        self.assertIn("60%", self.text(self.invoke()))

    def test_duplicate_fields_and_nonfinite_values_are_rejected(self):
        self.transcript.write_text(
            '{"type":"assistant","sessionId":"session-a","message":{"usage":'
            '{"input_tokens":900000,"input_tokens":1}}}\n', encoding="utf-8")
        self.assertEqual(self.invoke(), "")
        self.transcript.write_text(json.dumps(request(used=float("nan"))) + "\n", encoding="utf-8")
        self.assertEqual(self.invoke(), "")

    def test_large_transcript_only_reads_tail_and_ignores_old_usage(self):
        first = (json.dumps(request(used=900_000)) + "\n").encode()
        self.transcript.write_bytes(first + b"x" * (NUDGE.TAIL_LIMIT + 8) + b"\n")
        self.assertEqual(self.invoke(), "")
        with self.transcript.open("ab") as handle:
            handle.write((json.dumps(request(used=600_000)) + "\n").encode())
        self.assertIn("60%", self.text(self.invoke()))

    def test_oversized_json_record_is_skipped(self):
        row = request(used=900_000)
        row["message"]["content"] = "x" * NUDGE.LINE_LIMIT
        self.write([row])
        self.assertEqual(self.invoke(), "")

    def test_input_bounds_and_malformed_event_are_quiet(self):
        self.write([request(used=900_000)])
        oversized_valid = json.dumps(dict(self.event, padding="x" * 70_000))
        for raw in ("", "{", "[]", '{"x":NaN}', '{"session_id":"a","session_id":"b"}',
                    " " * (NUDGE.EVENT_LIMIT + 1), oversized_valid):
            with self.subTest(raw_length=len(raw)):
                self.assertEqual(self.invoke(raw=raw), "")

    def test_invalid_session_ids_and_unsupported_event_are_quiet(self):
        self.write([request(used=900_000)])
        for value in ("../escape", "", "x" * 129, None, False):
            self.assertEqual(self.invoke(dict(self.event, session_id=value)), "")
        self.assertEqual(self.invoke(dict(self.event, hook_event_name="Stop")), "")

    def test_transcript_wrong_name_relative_missing_symlink_directory_and_fifo(self):
        self.write([request(used=900_000)])
        wrong = self.root / "wrong.jsonl"
        wrong.write_bytes(self.transcript.read_bytes())
        link = self.root / "links"
        link.mkdir()
        (link / (self.sid + ".jsonl")).symlink_to(self.transcript)
        directory = self.root / "directory" / (self.sid + ".jsonl")
        directory.mkdir(parents=True)
        fifo = self.root / "fifo" / (self.sid + ".jsonl")
        fifo.parent.mkdir()
        os.mkfifo(fifo)
        for path in (wrong, Path("session-a.jsonl"), self.root / "missing.jsonl",
                     link / (self.sid + ".jsonl"), directory, fifo):
            with self.subTest(path=path):
                self.assertEqual(self.invoke(dict(self.event, transcript_path=str(path))), "")

    def test_corrupt_or_oversized_state_is_preserved_and_quiet(self):
        self.write([request(used=600_000)])
        self.assertTrue(self.invoke())
        state = next(self.state.glob("*.json"))
        valid = state.read_bytes()
        self.write([request(used=900_000)])
        for data in (b"{bad", b"x" * (NUDGE.STATE_LIMIT + 1), valid + b" " * NUDGE.STATE_LIMIT):
            state.write_bytes(data)
            self.assertEqual(self.invoke(), "")
            self.assertEqual(state.read_bytes(), data)

    def test_foreign_or_invalid_state_is_quiet(self):
        self.write([request(used=600_000)])
        self.assertTrue(self.invoke())
        state = next(self.state.glob("*.json"))
        valid = json.loads(state.read_text())
        self.write([request(used=900_000)])
        for change in ({"session_id": "foreign"}, {"version": True}, {"band": True}, {"band": 65}):
            value = dict(valid, **change)
            state.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(self.invoke(), "")
            self.assertEqual(json.loads(state.read_text()), value)

    def test_state_directory_symlink_does_not_write_to_its_target(self):
        self.write([request(used=600_000)])
        outside = self.root / "outside"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.invoke(), "")
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_or_hardlinked_state_does_not_change_victim(self):
        self.write([request(used=600_000)])
        self.state.mkdir()
        victim = self.root / "victim"
        victim.write_text("retain", encoding="utf-8")
        name = hashlib.sha256(self.sid.encode()).hexdigest() + ".json"
        target = self.state / name
        target.symlink_to(victim)
        self.assertEqual(self.invoke(), "")
        self.assertEqual(victim.read_text(), "retain")
        target.unlink()
        os.link(victim, target)
        self.assertEqual(self.invoke(), "")
        self.assertEqual(victim.read_text(), "retain")

    def test_lock_contention_is_quiet_and_nonblocking(self):
        self.write([request(used=600_000)])
        self.state.mkdir()
        name = hashlib.sha256(self.sid.encode()).hexdigest() + ".lock"
        with (self.state / name).open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.invoke(), "")

    def test_concurrent_hooks_emit_one_reminder_and_leave_valid_state(self):
        self.write([request(used=600_000)])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: self.invoke(), range(12)))
        self.assertEqual(sum(bool(output) for output in outputs), 1)
        self.assertEqual(self.invoke(), "")
        state = json.loads(next(self.state.glob("*.json")).read_text())
        self.assertEqual(state["session_id"], self.sid)
        self.assertEqual(state["band"], 60)
        self.assertEqual(list(self.state.glob("*.tmp")), [])

    def test_atomic_replace_failure_is_quiet_and_preserves_previous_state(self):
        self.write([request(used=500_000)])
        self.assertTrue(self.invoke())
        state = next(self.state.glob("*.json"))
        before = state.read_bytes()
        self.write([request(used=600_000)])
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(self.event).encode()))
        stdout = io.StringIO()
        with stdin, mock.patch.dict(os.environ, self.env), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(NUDGE.os, "replace", side_effect=OSError("synthetic replacement failure")):
            code = NUDGE.main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(list(self.state.glob("*.tmp")), [])
        self.assertTrue(self.invoke())

    def test_closed_stdout_is_quiet_and_returns_zero(self):
        self.write([request(used=600_000)])
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        with os.fdopen(write_fd, "wb") as sink:
            with subprocess.Popen(
                [sys.executable, "-I", "-B", "-W", "error::ResourceWarning", str(TOOL)], stdin=subprocess.PIPE,
                stdout=sink, stderr=subprocess.PIPE, env=self.env,
            ) as process:
                _, error = process.communicate(json.dumps(self.event).encode(), timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(error, b"")


if __name__ == "__main__":
    unittest.main()
