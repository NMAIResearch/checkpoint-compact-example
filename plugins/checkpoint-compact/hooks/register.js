// checkpoint-compact: compacts a Claude Code session at a checkpoint the session chose, only after checkpoint.py has
// accepted the resume file the session wrote to the records folder. The session asks with the checkpoint_compact tool;
// the compaction runs when a turn of the main loop ends with no other turn of it under way, after a second check. Right
// after it, a note naming the resume file is added to the conversation, where the next turn is given it whichever message
// starts that turn, and a prompt to read the file is queued to start a turn of its own once the session is idle. Nothing
// is kept beyond the one pending request of the running session, the main loop's turns under way, a count of its turn
// starts and ends, queued resume prompts and a count of the changes that make a request out of date: no store, no files, no network.

const PLUGIN = 'checkpoint-compact'
const TOOL = 'checkpoint_compact'
// The settings, each an environment variable read whenever the checker is run; an empty value counts as unset.
// CHECKPOINT_COMPACT_PYTHON: the Python that runs the checker, by default python3 found on the PATH.
// CHECKPOINT_COMPACT_CHECKER: the checker's absolute path, by default checker/checkpoint.py in this plugin's own folder.
// CHECKPOINT_COMPACT_ROOT: the records folder resume files must sit in, passed to the checker as --root; by default the
// checker's own (the same variable, or ~/.local/state/checkpoint-compact).
// Claude Code's loader wants each variable's name written out where $.env.get reads it.
const PYTHON = 'python3'
const CHECKER_IN_PLUGIN = 'checker/checkpoint.py'
const DIGEST = /^[0-9a-f]{64}$/
// How many turn ends of the main loop a request may wait through before it is dropped.
const MAX_WAITS = 4
const BUSY = 'another turn of the main loop is under way'

const DESCRIPTION =
  'Compact this conversation at a checkpoint you chose. Call it only at a clean stop, after you have written your resume ' +
  "file in the checkpoint records folder (it must name this session's id). Pass the resume " +
  "file's absolute path and its SHA-256, computed with sha256sum, never typed from memory. A refused call says why and " +
  'changes nothing. After an accepted call, end your turn: the compaction runs when the turn ends, and you are then asked to ' +
  'read the resume file first.'

const SCHEMA = {
  type: 'object',
  properties: {
    resume_path: { type: 'string', description: 'Absolute path of the resume file written for this checkpoint' },
    resume_sha256: { type: 'string', description: "The resume file's SHA-256, 64 lowercase hex characters" },
    reason: { type: 'string', description: 'One line: why this is a clean point' },
    keep: { type: 'string', description: 'Optional: anything the summary must keep that the resume file does not hold' },
  },
  required: ['resume_path', 'resume_sha256', 'reason'],
}

// The one pending request of this session, or null, and a count that the session's end and every accepted request move
// on. A request carries the count it was accepted under, and work on it stops at its next step once the count has moved,
// so neither a session that ended nor a request a newer one replaced is acted on.
let pending = null
let generation = 0
// The main loop's turns that have started and not yet ended, by id. The engine compacts only between turns, so a request
// whose turn end finds another turn of the main loop under way waits for that turn's end.
const running = new Set()
// How many turns of the main loop have ended. A request is out of pending while a turn end tries it and while the note
// that it waits is stored, so a turn end in that time finds nothing pending; the one holding the request compares this
// count before it puts the request back, and tries it again at once if a turn has ended since it was decided that it waits.
let ends = 0
// A queued resume is needed until another main-loop turn can read its stored note.
let starts = 0
const queuedResumes = new Set()

function message(error) {
  try {
    return String(error && error.message ? error.message : error)
  } catch (_error) {
    return 'an error that cannot be shown'
  }
}

// A setting's value as read, or undefined when it is not set or is empty.
function usable(value) {
  return typeof value === 'string' && value !== '' ? value : undefined
}

// The checker's command up to its subcommand: the Python, its flags and the checker's absolute path. It throws when the
// checker's place is not an absolute path, so that the call is refused before anything runs.
async function checker($) {
  const python = usable(await $.env.get('CHECKPOINT_COMPACT_PYTHON')) ?? PYTHON
  let path = usable(await $.env.get('CHECKPOINT_COMPACT_CHECKER'))
  if (path === undefined) {
    const folder = $.plugin.root
    if (typeof folder !== 'string' || !folder.startsWith('/')) {
      throw new Error("this plugin's folder is not known; set CHECKPOINT_COMPACT_CHECKER to the checker's absolute path")
    }
    path = folder.replace(/\/+$/, '') + '/' + CHECKER_IN_PLUGIN
  }
  if (!path.startsWith('/')) throw new Error('CHECKPOINT_COMPACT_CHECKER is not an absolute path: ' + path)
  return [python, '-I', '-B', path]
}

async function check($, request) {
  try {
    const records = usable(await $.env.get('CHECKPOINT_COMPACT_ROOT'))
    const argv = [...await checker($), 'check', '--resume', request.resume, '--sha256', request.sha256,
      '--session', request.session, ...(records === undefined ? [] : ['--root', records])]
    const done = await $.process.run(argv, { timeoutMs: 30000 })
    const report = JSON.parse(done.stdout)
    if (done.exitCode === 0 && report.ok === true) return { ok: true, reasons: [] }
    return { ok: false, reasons: Array.isArray(report.reasons) && report.reasons.length ? report.reasons : ['the checker refused it'] }
  } catch (error) {
    return { ok: false, reasons: ['the checker could not be run: ' + message(error)] }
  }
}

function instructions(request) {
  const keep = request.keep ? ' Also keep: ' + request.keep : ''
  return 'This conversation is being compacted at a checkpoint the session chose. Begin the summary with this line exactly: ' +
    'Checkpoint: ' + request.resume + ' (SHA-256 ' + request.sha256 + '). Then keep the task in progress and its next step ' +
    'as the checkpoint file states them, decisions still open, anything said after the file was written, and who is ' +
    'waiting on what. The checkpoint file is the authoritative record; do not restate it at length.' + keep
}

// The note added to the conversation right after the compaction, which the next turn is given whoever starts it.
function resumeNote(request) {
  return PLUGIN + ': compaction done at your checkpoint. Read ' + request.resume + ' in full first and confirm that its ' +
    'SHA-256 is ' + request.sha256 + ', then continue with the next task it names.'
}

// The prompt queued right after the compaction. A plugin's prompt starts a turn of its own once the session is idle and is
// never folded into a running turn, so it can reach the model after other turns; it says when it is out of date.
function resumePrompt(request) {
  return 'Compaction done at your checkpoint ' + request.resume + ' (SHA-256 ' + request.sha256 + '). If you have not ' +
    'read that file since the compaction, read it in full now and confirm its SHA-256, then continue with the next task it ' +
    'names. If you have, or you have written a newer checkpoint since, this message is ' +
    'out of date: carry on with your work.'
}

// The note added when an accepted request has to wait for the end of a turn that holds the session. It is stored before
// the request is put back to wait, so nothing compacts that request while the note is on its way; a newer request or the
// session's end can still overtake it, so it says when it is out of date.
function waitNote(request) {
  return PLUGIN + ': the checkpoint ' + request.resume + ' is accepted but not compacted yet, because the session was not ' +
    'between turns when its turn ended. It is compacted when the next turn of the main loop ends: do what that turn needs, ' +
    'then end it. If the conversation has been compacted at that checkpoint since, or you have called the tool again ' +
    'since, this note is out of date: carry on with your work.'
}

function note($, text) {
  try {
    const written = $.ui.log(PLUGIN + ': ' + text)
    if (written && typeof written.then === 'function') written.then(undefined, () => {})
  } catch (_error) {
    // A transcript line that cannot be written changes nothing about the compaction.
  }
}

// What Claude Code 2.1.301 resolves $.session.compact with, read from its code rather than seen in a live run: a finished
// compaction gives the summary's rows as { messages: [...] }, with token counts beside them; one another hook vetoed
// gives { skip: reason }; one that fails rejects. Only the first counts as compacted.
function finished(result) {
  return result !== null && typeof result === 'object' && result.skip === undefined && Array.isArray(result.messages)
}

function shape(value) {
  try {
    const text = JSON.stringify(value)
    if (text === undefined) return String(value)
    return text.length > 120 ? text.slice(0, 117) + '...' : text
  } catch (_error) {
    return 'a ' + typeof value
  }
}

// Why the request is no longer the one to act on, or '' while it still is, given the session id just read. It does not
// wait: each caller reads the session id, then calls this and its action with no wait between, so nothing can put the
// request out of date after this check and before the action has been called.
function stale(request, current, session) {
  if (!current()) return 'the session ended or a newer request replaced this one'
  if (session !== request.session) return 'the conversation changed since the request'
  return ''
}

// Adds a row for the model to the main conversation, listed at once: a turn under way is given it in its next request.
// Returns '' once it is stored, or why it was not; never throws.
async function tell($, text, what, onStored = () => {}) {
  const failed = (why) => {
    note($, what + ' ' + why)
    return what + ' ' + why
  }
  try {
    const stored = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    if (stored !== null && typeof stored === 'object' && stored.deny !== undefined) return failed('was refused: ' + message(stored.deny))
    onStored()
    return ''
  } catch (error) {
    return failed('could not be added: ' + message(error))
  }
}

// Sends the prompt to read the resume file. Returns '' once it is on its way, or why it could not be sent; a refusal that
// comes later is written to the transcript when it comes.
function sendPrompt($, request, notedAt) {
  const queued = { text: resumePrompt(request), notedAt }
  queuedResumes.add(queued)
  const failed = (error) => {
    queuedResumes.delete(queued)
    const why = 'the prompt to read the resume file could not be sent: ' + message(error)
    note($, why)
    return why
  }
  try {
    const sent = $.prompt.submit({ text: queued.text })
    if (sent && typeof sent.then === 'function') sent.then(() => queuedResumes.delete(queued), failed)
    return ''
  } catch (error) {
    return failed(error)
  }
}

// One try at compacting on a request taken from the queue: check it again, compact, then add the note naming the resume
// file and queue the prompt to read it. current() says whether the request is still the live one. Before the checker, the
// compaction, the note and the prompt, the session id is read and the request checked with stale(), and the action is
// called with no wait after that check. Returns { said, wait, mark }: what happened, also written to the transcript when
// it can be, whether the request is to wait for the next turn end of the main loop, and, when it is, the count ended()
// gave (the main loop's turn ends) at the point the decision rests on, so that the caller can see a turn end it missed.
// With mayWait it waits while busy() says another turn of the main loop is under way, and after a compaction call that
// the engine refused (it compacts only between turns): a refusal while busy() says a turn is under way waits for that
// turn's end; one after a turn of the main loop ended during the call is explained by that turn, which has gone, so its
// mark is the count when the call was made and the caller tries again at once; one with no turn seen waits once, since a
// turn may have started that busy() has not yet seen. At most MAX_WAITS waits in all. Never throws.
export async function attempt($, request, current = () => true, busy = () => false, mayWait = false, ended = () => 0) {
  let compacted = false
  const refuse = (why) => {
    note($, 'not compacted: ' + why)
    return { said: 'not compacted: ' + why, wait: false }
  }
  const hold = (why, mark) => {
    request.waits = (request.waits || 0) + 1
    if (request.waits > MAX_WAITS) {
      return refuse(why + ', after waiting through ' + MAX_WAITS + ' turn ends; call the tool again if this is still the checkpoint')
    }
    const said = 'not compacted yet: ' + why + (ended() === mark
      ? '; it is tried again when the next turn of the main loop ends'
      : '; that turn has ended, so it is tried again now')
    note($, said)
    return { said, wait: true, mark }
  }
  try {
    if (mayWait && busy()) return hold(BUSY, ended())
    const before = stale(request, current, await $.session.id())
    if (before) return refuse(before)
    const verdict = await check($, request)
    if (!verdict.ok) return refuse(verdict.reasons.join('; '))
    const checked = stale(request, current, await $.session.id())
    if (checked) return refuse(checked)
    if (mayWait && busy()) return hold(BUSY, ended())
    const called = ended()
    let result
    try {
      result = await $.session.compact({ instructions: instructions(request) })
    } catch (error) {
      if (!mayWait) return refuse(message(error))
      const why = 'the compaction was refused (' + message(error) + ')'
      const later = stale(request, current, await $.session.id())
      if (later) return refuse(why + '; ' + later)
      if (busy()) return hold(why + ' while ' + BUSY, ended())
      if (ended() !== called) return hold(why + ' while another turn of the main loop ran', called)
      if (request.refused) return refuse(why + ' again')
      request.refused = true
      return hold(why, called)
    }
    if (result !== null && typeof result === 'object' && result.skip !== undefined) {
      return refuse('skipped (' + message(result.skip) + ')')
    }
    if (!finished(result)) return refuse('the compaction answered ' + shape(result) + ', which is not a finished compaction')
    compacted = true
    const done = 'compacted at ' + request.resume
    note($, done)
    const after = stale(request, current, await $.session.id())
    if (after) {
      const why = 'the note and the prompt to read the resume file were not sent: ' + after
      note($, why)
      return { said: done + '; ' + why, wait: false }
    }
    let notedAt
    const unnoted = await tell($, resumeNote(request), 'the note naming the resume file', () => { notedAt = starts })
    const said = done + (unnoted ? '; ' + unnoted : '')
    const last = stale(request, current, await $.session.id())
    if (last) {
      const why = 'the prompt to read the resume file was not sent: ' + last
      note($, why)
      return { said: said + '; ' + why, wait: false }
    }
    const unsent = sendPrompt($, request, notedAt)
    return { said: unsent ? said + '; ' + unsent : said, wait: false }
  } catch (error) {
    if (!compacted) return refuse(message(error))
    const why = 'the prompt to read the resume file was not sent: ' + message(error)
    note($, why)
    return { said: 'compacted at ' + request.resume + '; ' + why, wait: false }
  }
}

// One try that never waits: where attempt with mayWait would hold the request, this refuses it. Returns what happened, as
// attempt says it.
export async function compactAt($, request, current = () => true) {
  return (await attempt($, request, current)).said
}

// Holds a request taken from pending at a turn end of the main loop: tries it, and tries it again at once while a turn of
// the main loop has ended since the point a wait was decided on (a turn end in that time found nothing pending). Otherwise
// it tells the session once that the checkpoint waits, then puts the request back to wait for the next turn end, unless a
// newer request has been accepted or the session has ended meanwhile; the note is stored before the request is put back,
// so nothing compacts the request while the note is on its way. attempt never throws, and tell never throws.
async function drive($, request) {
  const live = () => request.generation === generation
  for (;;) {
    const outcome = await attempt($, request, live, () => running.size > 0, true, () => ends)
    if (!outcome.wait || !live()) return
    if (ends === outcome.mark && !request.told) {
      request.told = true
      await tell($, waitNote(request), 'the note that the compaction waits')
      if (!live()) return
    }
    if (ends !== outcome.mark) continue
    if (pending === null) pending = request
    return
  }
}

export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.tool.register({ name: TOOL, description: DESCRIPTION, inputSchema: SCHEMA, isDeferred: false })
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    pending = null
    generation += 1
    running.clear()
    queuedResumes.clear()
    return next(e)
  })

  on('turn.start', ($, e, next) => {
    if (e.agentId === undefined) {
      starts += 1
      running.add(e.turnId)
    }
    return next(e)
  })

  // Plugin prompts reach this hook when the idle queue drains, before their own turn starts.
  on('prompt.submit', ($, e, next) => {
    if (e.origin.kind !== 'plugin' || e.origin.name !== PLUGIN) return next(e)
    const queued = [...queuedResumes].find((item) => item.text === e.text)
    if (!queued) return next(e)
    queuedResumes.delete(queued)
    if (queued.notedAt !== undefined && starts > queued.notedAt) {
      return { drop: 'The checkpoint note was available to another turn; the queued resume prompt is out of date.' }
    }
    return next(e)
  })

  // The filter is the tool's full name (mcp__, the plugin's name, two underscores, the tool's name), written out so that
  // `claude plugin validate` can read it.
  on('tool.call', { tool: 'mcp__checkpoint-compact__checkpoint_compact' }, async ($, e) => {
    const resume = typeof e.resume_path === 'string' ? e.resume_path : ''
    const sha256 = typeof e.resume_sha256 === 'string' ? e.resume_sha256.toLowerCase() : ''
    if (!resume.startsWith('/') || !DIGEST.test(sha256)) {
      return { result: 'refused: give resume_path as an absolute path and resume_sha256 as 64 hex characters. Nothing changed.' }
    }
    const asked = generation
    const session = await $.session.id()
    const request = { session, resume, sha256, keep: typeof e.keep === 'string' ? e.keep : '' }
    const verdict = await check($, request)
    if (!verdict.ok) {
      return { result: 'refused: ' + verdict.reasons.join('; ') + '. Nothing changed; fix the checkpoint and call again.' }
    }
    if (generation !== asked) {
      return { result: 'refused: the session ended or another request was accepted while this one was being checked. ' +
        'Nothing changed; call again if this is still the checkpoint.' }
    }
    generation += 1
    pending = { ...request, generation }
    return { result: 'accepted: end your turn now; the conversation is compacted when the turn ends, and you will then be ' +
      'asked to read ' + resume + '.' }
  }).catch(async ($, e, next) => {
    // The hook threw or timed out before it queued anything, so the call fails closed: nothing is compacted.
    const kind = next.error && next.error.kind ? next.error.kind : 'error'
    return { result: 'refused: the checkpoint tool failed (' + kind + '). Nothing changed; tell the user.' }
  })

  // A turn end of the main loop is counted, then takes the pending request, if there is one, and holds it (drive). drive
  // never throws.
  on('turn.complete', async ($, e, next) => {
    const answered = await next(e)
    if (e.agentId !== undefined) return answered
    running.delete(e.turnId)
    ends += 1
    if (pending === null) return answered
    const request = pending
    pending = null
    await drive($, request)
    return answered
  })
}
