# Checkpoint compaction for Claude Code (worked example)

A Claude Code plugin that lets a session compact its own conversation at a point it chooses, after it has written down
where it stands.

When a long session reaches a clean point in its work, a step finished and nothing in flight, it writes a resume file:
what it was doing, what is done and what comes next. It then calls the plugin's tool, `checkpoint_compact`, with the
file's path and SHA-256. The plugin's checker accepts the file only if the path is absolute and leads to a Markdown file
inside the records folder, the SHA-256 matches, the file was last changed within the past hour, and the session's own id
is in its first 10 lines. When the session's turn ends, the checker runs again on the same file. If it still accepts it,
the plugin asks Claude Code to compact the conversation; if not, the request is refused. Afterwards a note in the
conversation names the file, and a queued prompt asks the session to read it before it goes on. The moment is the
model's own judgement: the plugin cannot tell a clean point from any other.

The plugin does not switch off or delay Claude Code's own automatic compaction. If the conversation reaches Claude
Code's compaction threshold before the session has called the tool, Claude Code compacts it in the usual way, with no
resume file. A session therefore compacts at a moment it chose only when it reaches its checkpoint first, and that
depends on how it is told to checkpoint and on Claude Code's compaction settings. In the paired test below, the
plugin's runs had Claude Code's compaction window raised to 700,000 tokens with `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, so
that automatic compaction would not come first.

It is shared as a worked example of one workstation's practice, not as a general product. Read the code and adapt it
to your own workflow before relying on it.

## Why this workstation uses it

A tie to know first: Claude sessions built this plugin, ran the paired test below and wrote this page, and Anthropic
makes both Claude Code and the Claude models those sessions ran on.

On the workstation this comes from, several Claude Code sessions run long pieces of work side by side, and each keeps
its state in shared records that the other sessions and the person running them can read. The plugin was built so that
a session could compact at a clean point, with its state saved to those records first, without anyone typing
`/compact` in each window.

What it gives is the timing and the file, not a better summary. A session compacts after a step is finished rather than
in the middle of one, and the resume file it leaves is checked, kept and can be read by anyone, so the work can be taken
up from it, by the same session or another. The paired test below found no difference in what a session remembered
afterwards; the plugin is kept for these two reasons.

It is in daily use there. Counted on 12 November 2026 in 48 session files of the workstation's main project folder:
711 calls of the tool, 664 of them accepted. The closing step, from the call to the compaction, cost 0.34 dollars at
list prices at the median, and all closing steps together 1.52 per cent of those sessions' use at list. One model had
no row of its own in the price table, and its requests were priced at the table's highest Claude row.

## The paired test

One small test, reported on 12 November 2026, and it is exploratory. The measured runs used Claude Opus 5.5. Every run
started from one saved session state of about 400,000 tokens, built from the workstation's own records, and after one
compaction answered 20 questions about the earlier work.

- **Two probes came first.** With nothing compacted, a run answered 20 of 20 right; after one normal automatic
  compaction, 17 of 20. The planned, larger test needed these two at least 4 apart to have room to show a gain, so it
  stopped there, and the three pairs below were run as a smaller follow-up.
- **Three pairs.** In each, one run used this plugin with its pointer, its reminder and its resume turn, and the other
  Claude Code's normal automatic compaction. The plugin's runs answered 18, 20 and 19 of 20 right, and the automatic
  runs 19, 19 and 19. No run gave a stale or a wrong answer; the rest were left unanswered.
- **Time and cost.** The plugin's runs took longer (21.5 to 26.1 minutes, against 14.4 to 18.8) and their recorded
  requests cost more at list prices, $2.61 to $6.34 more a pair, without the compaction calls, which the session files
  do not hold; part of the difference may come from caching, an inference the test did not check.

Only runs in which the session reached its checkpoint would have counted, and repeated compactions in one session were
not tested. The test does not show that the plugin keeps more of a session than automatic compaction does.

## Requirements

- Claude Code with plugin hooks modules. The plugin's tests and Claude Code's validation passed on Claude Code 2.1.323,
  and its install and tests on 2.1.325; other versions have not been tested here, and no live session has run this
  package.
- Linux. The checker reads where an open file is from `/proc/self/fd`, and the reminder locks its state with `fcntl`.
- Python 3, standard library only, for the checker and the reminder. Their tests were run with Python 3.14 and 3.15;
  older versions have not been tested.

## Install

The repository is a plugin marketplace holding the one plugin, `checkpoint-compact`. From a clone, or the folder a
release ZIP unzips to:

```sh
claude plugin marketplace add /path/to/checkpoint-compact-example
claude plugin install checkpoint-compact@checkpoint-compact-example
```

These are Claude Code's own plugin commands; this package's install was tried once here, from its release ZIP in a fresh
profile, by a Claude session that did not build it. Claude Code reads the plugin from that folder afterwards, so keep
it. Create the records folder before the first checkpoint, private to you, since the checker refuses while it does not
exist:

```sh
mkdir -p ~/.local/state/checkpoint-compact && chmod 700 ~/.local/state/checkpoint-compact
```

Then tell your sessions how to checkpoint, for example in your `CLAUDE.md`: when to call the tool, where to write the
resume file and what it must hold. The file's rules are under "Resume files" below.

## How it works

1. When a session starts, the plugin registers the tool `checkpoint_compact`. It takes `resume_path`,
   `resume_sha256`, a one-line `reason` and, optionally, `keep`: anything the summary must keep that the resume file
   does not hold.
2. The session writes its resume file and calls the tool. The plugin runs its checker, `checker/checkpoint.py check` in
   the plugin's folder, as a local process from an argument list, with no shell. A refused call says why and changes
   nothing; the session can fix the file and call again.
3. An accepted call waits for the end of the session's turn. The checker then runs again, and a file changed since the
   call is not compacted on. While another turn of the main loop is under way, whether before that check or after it,
   the request waits for the next turn end, four times at most in all. A subagent's turn end does nothing. A call
   accepted after an earlier one replaces it; when two calls are checked at the same time, the first to be accepted
   stands and the other is refused. The session's end drops the request, but a compaction already asked of Claude Code
   is not called back.
4. The plugin asks Claude Code to compact the conversation, with instructions that the summary begin with a line
   naming the resume file and its SHA-256, and keep what the session asked to keep.
5. After the compaction, the plugin adds a note naming the resume file to the conversation and queues a prompt asking
   the session to read the file in full and confirm its SHA-256 before it continues. The prompt says when it is out of
   date, and it is dropped once a turn of the main loop has started since the note was added; the plugin cannot tell
   whether that turn read it.

The checker judges every condition on one open file: its place with symlinks followed, its size (1 MiB at most), its
times, its digest and its text. It refuses a file that changes while it is read, and it writes nothing.
`python3 plugins/checkpoint-compact/checker/checkpoint.py --help` describes both of its commands.

### Resume files

- Put each one in a folder one level inside the records folder, with a name that starts `RESUME_` and ends `.md`, for
  example `~/.local/state/checkpoint-compact/project-a/RESUME_first-checkpoint.md`. The checker accepts any Markdown
  file inside the records folder; the optional pointer below looks one level down, at names of that form only.
- Write the session's own id as a whole token in the first 10 lines, before any other session id. In the Claude Code
  version tested here, commands a session runs with its Bash tool see that id in `CLAUDE_CODE_SESSION_ID`. A file
  without it is refused, and the reason names the id the checker expected.
- Write a new file for each checkpoint. A verdict holds for the bytes that were checked, and a file changed afterwards
  needs a new check. The session should take the SHA-256 from `sha256sum`, not type it from memory.
- Resume files can hold private details of your work. Keep the records folder to yourself: owned by you, with mode
  700 (`stat -c '%U %a'` on the folder shows both).

## Settings

Each setting is an environment variable. Set them in the environment Claude Code starts in; whether Claude Code's own
`env` settings reach the plugin has not been tested here.

| Variable | Default | What it sets |
|---|---|---|
| `CHECKPOINT_COMPACT_ROOT` | `~/.local/state/checkpoint-compact` | The records folder. The checker and the pointer read it, and the plugin passes it to the checker as `--root` when it is set. Give an absolute path. |
| `CHECKPOINT_COMPACT_PYTHON` | `python3`, found on the `PATH` | The Python the plugin runs the checker with. |
| `CHECKPOINT_COMPACT_CHECKER` | `checker/checkpoint.py` in the plugin's own folder | The checker the plugin runs, as an absolute path. |
| `CONTEXT_NUDGE_WINDOW_TOKENS` | 1,000,000 for a model name starting `claude-`, 200,000 for any other | The context window the reminder measures against, a whole number up to 1,000,000,000. |
| `CONTEXT_NUDGE_STATE_DIR` | `~/.local/state/context-nudge` | Where the reminder keeps one small state file per session. It must be owned by the user who runs the reminder, and no one else may write to it. |

The plugin and the checker treat an empty value as unset. The reminder treats a value it cannot use, an empty one
included, as an error, and then stays silent.

## Optional hooks

Two scripts in `plugins/checkpoint-compact/checker/` can run as ordinary Claude Code hooks. Wire them in your
`settings.json`, with the path of your clone or of the installed plugin:

```json
{
  "hooks": {
    "SessionStart": [
      {"matcher": "compact", "hooks": [{"type": "command", "timeout": 10,
        "command": "python3 -I -B /path/to/checker/checkpoint.py pointer --hook"}]}
    ],
    "PostToolUse": [
      {"matcher": ".*", "hooks": [{"type": "command", "timeout": 5,
        "command": "python3 -I -B /path/to/checker/context_nudge.py"}]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "timeout": 5,
        "command": "python3 -I -B /path/to/checker/context_nudge.py"}]}
    ]
  }
}
```

- **The pointer** runs after any compaction, the plugin's, `/compact` or automatic. It names the newest `RESUME_*.md`
  one level below the records folder that names the session as its writer, with its SHA-256, and asks the session to
  read it before it continues. It follows no symlink below the records folder; a records folder that is itself a
  symlink is opened through it. It always exits 0.
- **The reminder** reads the latest request's input tokens from the session's own transcript, after the last
  compaction, and adds one line suggesting a checkpoint at 50 and 60 per cent of the window and at every 5 points from
  80, once for each band. The window is configured, not read from the model, so set `CONTEXT_NUDGE_WINDOW_TOKENS` to
  your session's real window. It makes no model, checkpoint or network call.

## What it does not do

- It does not stop Claude Code compacting by itself; see the top of this file.
- A checkpoint the tool accepted but has not yet compacted on survives a compaction the plugin did not ask for
  (Claude Code's automatic one, or `/compact`): at the next turn end the plugin can compact again, on the resume file
  written before that compaction. This is read from the code; it has not been seen in a live session.
- In a headless session it has not compacted where that was tried (see the known behaviours below).
- It checks a resume file's place, size, digest, age and opening lines, not whether what the file says is right or
  enough.
- It does not show that a compaction keeps more of the session; see the paired test above.
- It keeps nothing between sessions and calls no network service. The checker writes nothing; the optional reminder
  writes one small state file per session.
- It assumes the records folder is not renamed or replaced while sessions run. The pointer checks the files it names
  through the folder it opened, not through the path it prints, so a folder put in the records folder's place during a
  read could make the printed path lead to a different file from the one whose SHA-256 it gives. An independent
  re-check found this, and it was accepted as a written limit.

## Known behaviours

Version numbers are the Claude Code versions each was seen on.

- In a headless session (`claude -p`) the plugin loads and its tool accepts a checkpoint, but Claude Code refuses the
  compaction, so nothing compacts; in an interactive session the compaction runs. Seen on Claude Code 2.1.317 and
  2.1.318, with a local stand-in for the API.
- When an interactive session loads the plugin from a folder (`--plugin-dir`), Claude Code writes type files into
  `.claude-plugin/types/` in that folder, which ignores itself in git (its `.gitignore` holds `*`). Their bytes can
  change with no change to the plugin, and a headless start does not write them. Seen on 2.1.318. The plugin's
  `tsconfig.json` points at them, for authoring only.
- The optional reminder can, once and straight after a compaction, show the usage figure from before it; the cause is
  not established. Seen once in 22 compactions of one session, and in 2 of 3 runs on the stand-in, those on 2.1.318.

## Tests

From the repository's top folder:

```sh
cd plugins/checkpoint-compact
claude plugin test .
cd checker
python3 -B -m unittest discover -s tests -v
```

The plugin's tests run against stubs in place of Claude Code: no model, process, file or network. One fake host has no
`session.append`, so the tests that use it take the plugin's fallback path for the note. The checker's and the
reminder's tests work in temporary folders only; some change a file at the moment it is opened or read, and some make
a FIFO. They do not show how the plugin behaves in a live session.

## Review status and AI assistance

**AI disclosure.** Written with AI assistance under the author's direction. Anthropic's Claude Opus 5.5, in Claude Code
sessions, wrote the plugin, the checker and their tests, and made this package. OpenAI Codex wrote the first version of
the optional reminder and its tests. Claude sessions set up and ran the paired test above: Claude Opus 5.5 sessions
built its tools and made the measured runs, and a Claude Fable 5.2 session drew their order and launched them. A model
cannot independently accept its own work. Models are not authors.

Reviewed across its versions by OpenAI's GPT-6 Astra and GPT-6.1 Sol, whose lab also wrote the reminder's first version,
and by Google's Gemini 4 Argon. Still open: what "What it does not do" lists, two narrow test gaps and one error
message's wording; the last three wording mends were not read again. No general claim about agent quality or security is
made.

## Licence

Code under the MIT licence, documentation under CC BY 4.0; see `LICENSE.md`. Shared by NMAI Research.
