#!/usr/bin/env python3
"""Checkpoints for compacting an agent session: the one rule every harness's trigger applies, and the pointer read after
a compaction.

A checkpoint is a resume file the session wrote to the records folder before asking to be compacted. Only a checkpoint
that passes `check` may be compacted on; after any compaction, `pointer` tells the session which resume file to read.
Resume files are written once: a verdict holds for the bytes that were checked, and a file changed afterwards needs a
new check.

A file names its writer when the session's literal id is a whole token within the first 10 text lines, with no earlier
UUID-shaped token. For a UUID-shaped session id, it must therefore be the first full session id in the file. UTF-8 is
decoded with replacement; splitlines treats CRLF as one line. A sentence-ending full stop may follow the id.

  checkpoint.py check --resume PATH --sha256 HEX --session ID [--root DIR] [--max-age-minutes N]
      Prints {"ok": ..., "reasons": [...], "resume": PATH, "sha256": HEX} as ASCII JSON and exits 0 when the checkpoint
      passes, 1 when it does not, 2 on a usage error. It passes only when PATH is absolute and leads, symlinks followed,
      to a regular .md file inside DIR, at most 1 MiB, whose SHA-256 is HEX, modified within N minutes (60 by default; N
      must be more than 0 and at most 1440) and not more than a minute in the future, naming ID as its writer. Every
      condition is judged on one open file: its place, size, times, digest and text all come from one descriptor, the
      file must not change while it is read, and PATH must still lead to it when the check ends. Every failed condition
      is listed; for a file over 1 MiB the digest and the session id are listed as not checked. A file that cannot be
      opened or read is refused in the same JSON. Writes nothing.

  checkpoint.py pointer --hook [--root DIR] [--json]
      Reads a hook event as JSON on stdin (Claude Code's and Codex's carry session_id) and prints, for the model, the
      newest RESUME_*.md one level below DIR naming that session as its writer, with its SHA-256, and the instruction
      to read it before continuing; with no such file, or bad input, a text saying so. It
      reads only regular files of at most 1 MiB. It opens DIR by its path, so a DIR that is itself a symlink is opened
      through it, and each folder and file below DIR through the descriptor of the one above it, without following a
      symlink, each file without waiting on a FIFO; it skips files dated more than a minute in the future and names
      that are not printable UTF-8. The SHA-256 it gives is that of the bytes it read, and just before naming a file it
      checks, from DIR as it opened it, that the file's path below DIR still leads, through no symlink, to that same
      unchanged file. A session id must be 1 to 128 letters, digits and . _ : - characters, starting with a letter or
      digit. --json wraps the text as SessionStart additionalContext. The output is UTF-8 whatever the input holds, and
      the exit status is 0 for any input, also when nothing reads the output.

DIR is the records folder: CHECKPOINT_COMPACT_ROOT when it is set and not empty, otherwise
~/.local/state/checkpoint-compact. Linux only: where an open file is comes from /proc/self/fd.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

ROOT_SETTING = "CHECKPOINT_COMPACT_ROOT"
DEFAULT_ROOT = Path.home() / ".local" / "state" / "checkpoint-compact"
MAX_BYTES = 1 << 20
MAX_AGE_MINUTES = 24 * 60
FUTURE_SECONDS = 60
MAX_EVENT_BYTES = 1 << 20
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
OPEN_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def why(error: OSError) -> str:
    return error.strerror or type(error).__name__


def identity(st: os.stat_result) -> tuple:
    """What must stay the same for a file to count as unchanged: the file itself, its size and its times."""
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def read_all(fd: int) -> bytes | None:
    """Every byte from fd to its end, or None once there are more than MAX_BYTES."""
    chunks, total = [], 0
    while True:
        block = os.read(fd, min(1 << 16, MAX_BYTES + 1 - total))
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        total += len(block)
        if total > MAX_BYTES:
            return None


def located(fd: int) -> Path | None:
    """Where the open file is, as the kernel names it; None if that cannot be read or the file has been deleted."""
    try:
        where = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    if not where.startswith("/") or where.endswith(" (deleted)"):
        return None
    return Path(where)


def place_reasons(fd: int, base: Path, root: Path) -> list[str]:
    where = located(fd)
    if where is None:
        return ["where the resume file is could not be confirmed"]
    reasons = []
    if base not in where.parents:
        reasons.append(f"the resume file must be inside {root} (it resolves to {where})")
    if where.suffix != ".md":
        reasons.append("the resume file must be a .md file")
    return reasons


def time_reasons(mtime_ns: int, max_age_minutes: float) -> list[str]:
    age = time.time() - mtime_ns / 1e9
    if age < -FUTURE_SECONDS:
        return [f"the resume file is dated {math.ceil(-age)} seconds in the future, more than {FUTURE_SECONDS} seconds "
                f"ahead of the clock"]
    if age / 60 > max_age_minutes:
        return [f"the resume file was last changed {age / 60:.0f} minutes ago, more than {max_age_minutes:g} minutes"]
    return []


def names_writer(data: bytes, session: str) -> bool:
    """A literal whole id in the first 10 decoded text lines, with no earlier whole UUID-shaped id.

    Id characters are ASCII letters, digits, dots, underscores, colons and hyphens. One trailing dot before a delimiter
    or the end of the text is sentence punctuation. Undecodable UTF-8 bytes become replacement characters.
    """
    if not session:
        return False
    opening = "\n".join(data.decode("utf-8", errors="replace").splitlines()[:10])
    start = r"(?<![A-Za-z0-9._:-])"
    end = r"(?![A-Za-z0-9_:-]|\.[A-Za-z0-9._:-])"
    named = re.search(start + re.escape(session) + end, opening)
    first = re.search(start + r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}" + end, opening)
    return named is not None and (first is None or named.start() <= first.start())


def examine(fd: int, path: Path, digest: str, session: str, base: Path, root: Path, max_age_minutes: float) -> list[str]:
    first = os.fstat(fd)
    if not stat.S_ISREG(first.st_mode):
        return [f"the resume file is not a regular file that can be read: {path}"]
    if first.st_size > MAX_BYTES:
        reasons = place_reasons(fd, base, root)
        reasons.append(f"the resume file is {first.st_size} bytes, more than 1 MiB")
        reasons += time_reasons(first.st_mtime_ns, max_age_minutes)
        reasons.append("its SHA-256 and the session id were not checked, because the file is too large to read")
        return reasons
    data = read_all(fd)
    last = os.fstat(fd)
    if data is None:
        reasons = place_reasons(fd, base, root)
        reasons.append("the resume file grew past 1 MiB while it was being read, so its SHA-256 and the session id were "
                       "not checked")
        return reasons + time_reasons(last.st_mtime_ns, max_age_minutes)
    reasons = []
    if identity(last) != identity(first) or len(data) != last.st_size:
        reasons.append("the resume file changed while it was being checked")
    reasons += place_reasons(fd, base, root)
    try:
        now_at_path = identity(os.stat(path))
    except OSError:
        now_at_path = None
    if now_at_path != identity(last):
        reasons.append(f"{path} no longer leads to the file that was checked")
    actual = hashlib.sha256(data).hexdigest()
    if actual != digest.lower():
        reasons.append(f"the resume file's SHA-256 is {actual}, not the declared {digest}")
    reasons += time_reasons(last.st_mtime_ns, max_age_minutes)
    if not names_writer(data, session):
        reasons.append(f"the resume file must name this session ({session or 'no id given'}) as its writer: add its own "
                       "full id as a whole token, as the first full session id in the file, within the first 10 lines")
    return reasons


def check(resume: str, digest: str, session: str, root: Path, max_age_minutes: float) -> list[str]:
    """Every reason the checkpoint fails; an empty list when it passes."""
    path = Path(resume)
    if not path.is_absolute():
        return [f"the resume path must be absolute: {resume}"]
    try:
        base = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        return [f"the records folder {root} cannot be found ({type(error).__name__})"]
    try:
        fd = os.open(path, OPEN_FLAGS)
    except OSError as error:
        return [f"the resume file is not a regular file that can be read: {resume} ({why(error)})"]
    try:
        return examine(fd, path, digest, session, base, root, max_age_minutes)
    except OSError as error:
        return [f"the resume file could not be read: {resume} ({why(error)})"]
    finally:
        os.close(fd)


def printable(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return name.isprintable()


def open_folder(root_fd: int, folder: str) -> int:
    """A descriptor of the folder of that name in the root, opened without following a symlink: a symlink fails to open."""
    return os.open(folder, DIR_FLAGS, dir_fd=root_fd)


def snapshot(root_fd: int, folder: str, name: str) -> tuple[bytes, os.stat_result] | None:
    """The whole content of the file root/folder/name and its status, when it is a regular file of at most 1 MiB that did
    not change while it was read; otherwise None. The folder and the file are each opened through the descriptor of the
    one above, without following a symlink, so the bytes come from a file one level below the root and through no
    symlink below the root. The file is opened without waiting on a FIFO."""
    try:
        dir_fd = open_folder(root_fd, folder)
    except OSError:
        return None
    try:
        fd = os.open(name, OPEN_FLAGS | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    try:
        first = os.fstat(fd)
        if not stat.S_ISREG(first.st_mode) or first.st_size > MAX_BYTES:
            return None
        data = read_all(fd)
        last = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if data is None or identity(last) != identity(first) or len(data) != last.st_size:
        return None
    return data, last


def leads_to(root_fd: int, folder: str, name: str) -> tuple | None:
    """The identity of the file root/folder/name, reached from root_fd without following a symlink below it; None when
    it cannot be reached."""
    try:
        dir_fd = open_folder(root_fd, folder)
    except OSError:
        return None
    try:
        return identity(os.stat(name, dir_fd=dir_fd, follow_symlinks=False))
    except OSError:
        return None
    finally:
        os.close(dir_fd)


def candidates(root_fd: int) -> list[tuple[int, str, str, tuple]]:
    """Every RESUME_*.md in a folder one level below the root that is listed as a regular file of at most 1 MiB and not
    dated in the future: (modification time, folder, name, identity when listed), newest first. A folder that is a
    symlink cannot be opened, so it is not searched."""
    found, latest = [], (time.time() + FUTURE_SECONDS) * 1e9
    with os.scandir(root_fd) as folders:
        for folder in folders:
            if not printable(folder.name):
                continue
            try:
                dir_fd = open_folder(root_fd, folder.name)
            except OSError:
                continue
            try:
                with os.scandir(dir_fd) as files:
                    for entry in files:
                        name = entry.name
                        if not (name.startswith("RESUME_") and name.endswith(".md") and printable(name)):
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISREG(st.st_mode) and st.st_size <= MAX_BYTES and st.st_mtime_ns <= latest:
                            found.append((st.st_mtime_ns, folder.name, name, identity(st)))
            except OSError:
                continue
            finally:
                os.close(dir_fd)
    return sorted(found, reverse=True)


def newest_resume(session: str, root: Path) -> tuple[Path, bytes, os.stat_result] | None:
    """The newest RESUME_*.md one level below root naming the session as its writer: its path, the bytes read and their
    status. Files are tried newest first; one that changed since it was listed, or whose path no longer leads to it
    through no symlink below the root when it would be named, is passed over."""
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for _mtime, folder, name, listed in candidates(root_fd):
            taken = snapshot(root_fd, folder, name)
            if taken is None or identity(taken[1]) != listed:
                continue
            data, st = taken
            if not names_writer(data, session):
                continue
            if leads_to(root_fd, folder, name) == listed:
                return root / folder / name, data, st
        return None
    finally:
        os.close(root_fd)


def session_from(raw: bytes) -> str | None:
    """The hook event's session id, when the event is a JSON object whose session_id is a usable id; otherwise None."""
    if len(raw) > MAX_EVENT_BYTES:
        return None
    try:
        event = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    session = event.get("session_id") if isinstance(event, dict) else None
    return session if isinstance(session, str) and SESSION_ID.fullmatch(session) else None


def saved(st: os.stat_result) -> str:
    try:
        return dt.datetime.fromtimestamp(st.st_mtime_ns / 1e9).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return "at an unknown time"


def pointer_text(raw: bytes, root: Path) -> str:
    session = session_from(raw)
    found = None
    if session is not None:
        try:
            found = newest_resume(session, root)
        except OSError:
            found = None
    if found is None:
        return (f"Compaction: no resume file naming this session ({session or 'no usable session id'}) was found one level "
                f"below {root}. Check your own records before you continue.")
    path, data, st = found
    return (f"Compaction: your newest checkpoint in the records folder is {path} (SHA-256 "
            f"{hashlib.sha256(data).hexdigest()}, saved {saved(st)}). Read it in full "
            f"before you continue; the summary above may leave things out.")


def read_event() -> bytes:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return sys.stdin.read(MAX_EVENT_BYTES + 1).encode("utf-8", "surrogateescape")
    return stream.read(MAX_EVENT_BYTES + 1)


def emit(text: str) -> None:
    """Writes text and a newline to stdout as UTF-8, escaping anything that is not valid Unicode. A write that fails is
    dropped, and stdout is then pointed at the null device, so that the flush Python makes as it exits cannot fail again
    (with nothing reading the output it would, and Python would exit 120): the pointer must still exit 0."""
    data = text.encode("utf-8", "backslashreplace") + b"\n"
    try:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is None:
            sys.stdout.write(data.decode("utf-8"))
        else:
            sys.stdout.flush()
            stream.write(data)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            sink = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(sink, sys.stdout.fileno())
            finally:
                os.close(sink)


def default_root() -> Path:
    """CHECKPOINT_COMPACT_ROOT when it is set and not empty, otherwise ~/.local/state/checkpoint-compact."""
    configured = os.environ.get(ROOT_SETTING)
    return Path(configured) if configured else DEFAULT_ROOT


def age_limit(text: str) -> float:
    """--max-age-minutes: a number of minutes, more than 0 and at most a day."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not math.isfinite(value) or value <= 0 or value > MAX_AGE_MINUTES:
        raise argparse.ArgumentTypeError(f"must be more than 0 and at most {MAX_AGE_MINUTES}: {text}")
    return value


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="checkpoint.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--resume", required=True)
    c.add_argument("--sha256", required=True)
    c.add_argument("--session", required=True)
    c.add_argument("--max-age-minutes", type=age_limit, default=60.0)
    p = sub.add_parser("pointer")
    p.add_argument("--hook", action="store_true", required=True, help="read the hook event as JSON on stdin")
    p.add_argument("--json", action="store_true", help="print SessionStart additionalContext JSON instead of plain text")
    for q in (c, p):
        q.add_argument("--root", type=Path, default=default_root())
    a = ap.parse_args(argv)
    if a.cmd == "check":
        try:
            reasons = check(a.resume, a.sha256, a.session, a.root, a.max_age_minutes)
        except Exception as error:  # the verdict still comes as JSON, and it is a refusal
            reasons = [f"the checkpoint could not be checked ({type(error).__name__})"]
        print(json.dumps({"ok": not reasons, "reasons": reasons, "resume": a.resume, "sha256": a.sha256}))
        return 0 if not reasons else 1
    try:
        text = pointer_text(read_event(), a.root)
    except Exception as error:  # a pointer hook must never fail the session start
        text = f"Compaction: the checkpoint pointer could not be worked out ({type(error).__name__}); check your own records."
    if a.json:
        clean = text.encode("utf-8", "backslashreplace").decode("utf-8")
        text = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": clean}},
                          ensure_ascii=False)
    emit(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
