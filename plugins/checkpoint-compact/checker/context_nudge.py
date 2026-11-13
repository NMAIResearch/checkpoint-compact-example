#!/usr/bin/env python3
"""Quiet Linux context reminders for Claude PostToolUse and UserPromptSubmit.

Input is one bounded hook JSON object. Only the named session's regular main
transcript is read, at most its final 12 MiB. Token usage is the latest complete
eligible request after any compaction boundary, not cumulative billing usage.
The window is a configured figure, not one read from the model.

State contains only a session identifier and the highest reminded band. It is
locked without waiting and atomically replaced. Errors produce no reminder.
CONTEXT_NUDGE_WINDOW_TOKENS overrides the window; CONTEXT_NUDGE_STATE_DIR
overrides the state directory, ~/.local/state/context-nudge by default. No
model, checkpoint or network call is made.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

EVENT_LIMIT = 64 * 1024
TAIL_LIMIT = 12 * 1024 * 1024
LINE_LIMIT = 1024 * 1024
STATE_LIMIT = 4096
TOKEN_LIMIT = 1_000_000_000
EVENTS = frozenset({"PostToolUse", "UserPromptSubmit"})
TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON value")


def _json(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("expected a singly linked regular file owned by this user")


def read_tail(path, session_id):
    """Read a bounded snapshot; reject a wrong basename, final symlink or FIFO."""
    path = Path(path)
    if not path.is_absolute() or path.name != session_id + ".jsonl":
        raise ValueError("transcript must be an absolute session-named JSONL path")
    before = path.lstat()
    _regular(before)
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        _regular(info)
        if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("transcript identity changed")
        offset = max(0, info.st_size - TAIL_LIMIT)
        handle.seek(offset)
        data = handle.read(min(TAIL_LIMIT, info.st_size - offset))
    if offset:
        _, separator, data = data.partition(b"\n")
        if not separator:
            return b""
    return data


def latest_usage(data, session_id):
    """Ignore side chains, foreign sessions and usage before compaction."""
    latest = None
    for line in data.splitlines():
        if not line or len(line) > LINE_LIMIT:
            continue
        try:
            row = _json(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("sessionId") != session_id:
            continue
        if row.get("isSidechain", False) is not False or row.get("agentId"):
            continue
        if row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            latest = None
            continue
        if row.get("type") != "assistant":
            continue
        latest = None
        message = row.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        usage = message["usage"]
        values = [usage.get("input_tokens")] + [usage.get(key, 0) for key in TOKEN_FIELDS[1:]]
        if any(type(value) is not int or not 0 <= value <= TOKEN_LIMIT for value in values):
            continue
        model = message.get("model")
        latest = (sum(values), model if isinstance(model, str) else "")
    return latest


def window_size(model, env):
    override = env.get("CONTEXT_NUDGE_WINDOW_TOKENS")
    if override is not None:
        if not isinstance(override, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", override):
            raise ValueError("invalid window override")
        window = int(override)
        if window > TOKEN_LIMIT:
            raise ValueError("window override exceeds bound")
        return window
    # A model name that starts claude- counts a 1,000,000-token window, and any other name 200,000. Neither figure is
    # read from the model: set CONTEXT_NUDGE_WINDOW_TOKENS to the session's own window.
    return 1_000_000 if isinstance(model, str) and model.startswith("claude-") else 200_000


def current_band(used, window):
    percent = used * 100 // window
    if percent >= 80:
        return 80 + ((percent - 80) // 5) * 5
    if percent >= 60:
        return 60
    if percent >= 50:
        return 50
    return 0


@contextlib.contextmanager
def _directory(path):
    """Create/open the state directory through descriptors without symlinks."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("state directory must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("state directory is not private to its owner")
        yield fd
    finally:
        os.close(fd)


def _read_state(directory, name, session_id):
    try:
        fd = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=directory)
    except FileNotFoundError:
        return {"version": 1, "session_id": session_id, "band": 0}
    with os.fdopen(fd, "rb") as handle:
        _regular(os.fstat(handle.fileno()))
        data = handle.read(STATE_LIMIT + 1)
    if len(data) > STATE_LIMIT:
        raise ValueError("oversized state")
    state = _json(data)
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "session_id", "band"}
        or type(state["version"]) is not int
        or state["version"] != 1
        or state["session_id"] != session_id
        or type(state["band"]) is not int
        or not (
            state["band"] in (0, 50, 60)
            or 80 <= state["band"] <= 1_000_000_000_000 and state["band"] % 5 == 0
        )
    ):
        raise ValueError("invalid or foreign state")
    return state


def _write_state(directory, name, state):
    temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(state, sort_keys=True) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def handle(event, env):
    """Return a reminder object or None; main supplies the quiet-error boundary."""
    if not isinstance(event, dict) or event.get("hook_event_name") not in EVENTS:
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not SESSION.fullmatch(session_id):
        return None
    if "agent_id" in event or event.get("isSidechain", False) is not False:
        return None
    transcript = event.get("transcript_path")
    if not isinstance(transcript, str):
        return None
    usage = latest_usage(read_tail(transcript, session_id), session_id)
    if usage is None:
        return None
    used, model = usage
    window = window_size(model, env)
    root = env.get("CONTEXT_NUDGE_STATE_DIR")
    if root is None:
        root = Path.home().resolve() / ".local" / "state" / "context-nudge"
    key = hashlib.sha256(session_id.encode()).hexdigest()
    band = current_band(used, window)
    with _directory(root) as directory:
        fd = os.open(key + ".lock", os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "a+b") as lock:
            _regular(os.fstat(lock.fileno()))
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = _read_state(directory, key + ".json", session_id)
            if used * 100 < 30 * window:
                if state["band"]:
                    state["band"] = 0
                    _write_state(directory, key + ".json", state)
                return None
            if not band or band <= state["band"]:
                return None
            state["band"] = band
            _write_state(directory, key + ".json", state)
    if band >= 80:
        action = "Checkpoint now, before starting anything new."
    elif band >= 60:
        action = "At the next clean point write a resume file, then call checkpoint_compact."
    else:
        action = "Plan a clean point for a checkpoint."
    text = (
        f"Recorded context {used * 100 // window}% "
        f"({used}/{window} input tokens; N={window} token window). {action}"
    )
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext": text}}


def main():
    try:
        data = sys.stdin.buffer.read(EVENT_LIMIT + 1)
        if len(data) > EVENT_LIMIT:
            return 0
        result = handle(_json(data), os.environ)
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except BrokenPipeError:
        sys.stdout = None
        return 0
    except (Exception, KeyboardInterrupt):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
