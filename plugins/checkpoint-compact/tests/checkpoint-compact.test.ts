// Tests for the checkpoint-compact mod, run with `claude plugin test`. No model, process, file or network runs: every
// call the mod makes is answered by a stub. What matters: the tool is registered at session start; a checkpoint
// checkpoint.py refuses queues nothing; an accepted one compacts when the main loop's turn ends, with instructions that
// carry the resume path and SHA-256, and then asks for the resume file; the checkpoint is checked again at turn end and a
// changed file is not compacted on; a subagent's turn end does nothing; a refused or failed compaction submits nothing and
// leaves one transcript line; a request is used once, a second call replaces the first, and the session's end drops it;
// missing fields are refused without running the checker; the checker runs as an argument list.
//
// Added after the first independent review: only a finished compaction ({ messages: [...] } with no skip, the shape
// Claude Code 2.1.301 resolves with) counts, and any other answer submits nothing; a request that goes out of date while
// the turn-end check runs (the session ends, another session answers, or a new request comes in) is not compacted, and a
// session that changes after the compaction gets no prompt; a session that ends while the tool call's own check runs
// leaves nothing queued; a turn end whose session id cannot be read, a prompt that throws or is refused, a transcript line
// that cannot be written and an error that cannot be shown never end in an escaped error, and each ends in a transcript
// line wherever one can be written (a transcript write that fails is dropped).
//
// Added after the re-check of that repair: the last check sits right before each action with no wait between, so a
// request that goes out of date just after that check, while the mod waits to resume, is never compacted on and never
// gets the prompt.
import { expect, mock, test } from 'claude-code/testing'
import { compactAt } from '../hooks/register.js'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const OTHER = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1130.md'
const DIGEST = 'a'.repeat(64)
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'
// The plugin's own folder: this file is in its tests/ folder.
const PLUGIN_DIR = decodeURIComponent(new URL('..', import.meta.url).pathname).replace(/\/+$/, '')
const CHECKER = PLUGIN_DIR + '/checker/checkpoint.py'

type Calls = { registered: any[]; runs: string[][]; compacts: (string | undefined)[]; submits: string[]; logs: string[] }

// How the compaction answers: 'done' with the rows of a summary; 'skip', as when another hook vetoes it (the mod's call
// then resolves with { skip }). A call that rejects, as under DISABLE_COMPACT, cannot be stubbed this way; the tests at
// the end check it, and answers of other shapes, by calling compactAt with a fake $.
type Compaction = 'done' | 'skip'

type Options = {
  checks?: boolean[]
  compaction?: Compaction
  noSession?: boolean
  env?: Record<string, string>
  // The id the n-th $.session.id call answers (counting from 1); null makes that call fail.
  sessionAt?: (n: number) => string | null
  // The at-th checker run answers only once the test has moved the clock on by 1000 ms; reached() is called as it starts.
  slowCheck?: { clock: any; at: number; reached: () => void }
}

function stubs(on: any, opts: Options = {}): Calls {
  const checks = opts.checks ?? [true]
  const calls: Calls = { registered: [], runs: [], compacts: [], submits: [], logs: [] }
  let runs = 0
  let ids = 0
  on('tool.register', ($: any, e: any) => { calls.registered.push(e); return { value: undefined } })
  on('session.id', () => {
    ids += 1
    if (opts.noSession) return { deny: 'no session id here' }
    const id = opts.sessionAt ? opts.sessionAt(ids) : SID
    return id === null ? { deny: 'the session id cannot be read now' } : { value: id }
  })
  on('env.get', ($: any, e: any) => ({ value: (opts.env ?? {})[e.name] }))
  on('process.run', async ($: any, e: any) => {
    calls.runs.push(e.argv)
    runs += 1
    const run = runs
    if (opts.slowCheck && run === opts.slowCheck.at) {
      opts.slowCheck.reached()
      await opts.slowCheck.clock.sleep(1000)
    }
    const ok = checks[Math.min(run - 1, checks.length - 1)]
    const reasons = ok ? [] : ["the resume file's SHA-256 is b, not the declared a"]
    return { value: { exitCode: ok ? 0 : 1, stdout: JSON.stringify({ ok, reasons, resume: RESUME, sha256: DIGEST }), stderr: '' } }
  })
  on('session.compact', ($: any, e: any) => {
    calls.compacts.push(e.instructions)
    const how = opts.compaction ?? 'done'
    if (how === 'skip') return { skip: 'vetoed by another hook' }
    return { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
  })
  on('prompt.submit', ($: any, e: any) => { calls.submits.push(e.text); return { text: e.text } })
  on('ui.log', ($: any, e: any) => { calls.logs.push(e.text); return { value: undefined } })
  on('session.start', () => ({ cwd: '/work' }))
  on('turn.complete', () => ({ text: '' }))
  on('session.end', ($: any, e: any) => ({ sessionId: e.sessionId ?? SID }))
  return calls
}

// A promise that resolves when the slow checker run starts, and the function that resolves it.
function signal(): { reached: () => void; started: Promise<void> } {
  let reached = () => {}
  const started = new Promise<void>((resolve) => { reached = resolve })
  return { reached: () => reached(), started }
}

const start = ($: any) => $.session.start({ cwd: '/work' })

const turnEnd = ($: any, extra: object = {}) =>
  $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: false, usage: null, ...extra })

const ask = ($: any, fields: object = {}) =>
  $.tool.call({ tool: TOOL, resume_path: RESUME, resume_sha256: DIGEST, reason: 'task finished', ...fields })

test('registers the checkpoint tool at session start', async ($, on) => {
  const calls = stubs(on)
  await start($)
  expect(calls.registered.length).toBe(1)
  expect(calls.registered[0].name).toBe('checkpoint_compact')
  expect(calls.registered[0].isDeferred).toBe(false)
})

test('a refused checkpoint queues nothing', async ($, on) => {
  const calls = stubs(on, { checks: [false] })
  await start($)
  const r = await ask($)
  expect(String(r.result)).toContain('refused')
  expect(String(r.result)).toContain('SHA-256')
  await turnEnd($)
  expect(calls.compacts.length).toBe(0)
  expect(calls.submits.length).toBe(0)
})

test('an accepted checkpoint compacts when the turn ends, then asks for the resume file', async ($, on) => {
  const calls = stubs(on)
  await start($)
  const r = await ask($, { keep: 'the open question about the window' })
  expect(String(r.result)).toContain('accepted')
  expect(calls.compacts.length).toBe(0)
  await turnEnd($)
  expect(calls.runs.length).toBe(2)
  expect(calls.compacts.length).toBe(1)
  expect(String(calls.compacts[0])).toContain(RESUME)
  expect(String(calls.compacts[0])).toContain(DIGEST)
  expect(String(calls.compacts[0])).toContain('the open question about the window')
  expect(calls.submits.length).toBe(1)
  expect(calls.submits[0]).toContain(RESUME)
  expect(calls.submits[0]).toContain(DIGEST)
  expect(calls.submits[0]).toContain('then continue with the next task it names')
})

test('a resume file changed after the call is not compacted on', async ($, on) => {
  const calls = stubs(on, { checks: [true, false] })
  await start($)
  await ask($)
  await turnEnd($)
  expect(calls.compacts.length).toBe(0)
  expect(calls.submits.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('not compacted')
})

test("a subagent's turn end does nothing", async ($, on) => {
  const calls = stubs(on)
  await start($)
  await ask($)
  await turnEnd($, { agentId: 'agent-1' })
  expect(calls.compacts.length).toBe(0)
  await turnEnd($)
  expect(calls.compacts.length).toBe(1)
})

for (const how of ['skip'] as Compaction[]) {
  test(`a compaction that is ${how === 'skip' ? 'skipped' : 'refused'} submits nothing and leaves a transcript line`, async ($, on) => {
    const calls = stubs(on, { compaction: how })
    await start($)
    await ask($)
    await turnEnd($)
    expect(calls.compacts.length).toBe(1)
    expect(calls.submits.length).toBe(0)
    expect(calls.logs.join(' ')).toContain('not compacted')
    expect(calls.logs.join(' ')).toContain('skipped')
    expect(calls.logs.join(' ')).not.toContain('compacted at')
  })
}

test('with nothing pending, a turn end does nothing', async ($, on) => {
  const calls = stubs(on)
  await start($)
  await turnEnd($)
  expect(calls.runs.length).toBe(0)
  expect(calls.compacts.length).toBe(0)
})

test('a request is used once', async ($, on) => {
  const calls = stubs(on)
  await start($)
  await ask($)
  await turnEnd($)
  await turnEnd($)
  expect(calls.compacts.length).toBe(1)
})

test('a second call replaces the first', async ($, on) => {
  const calls = stubs(on)
  await start($)
  await ask($)
  await ask($, { resume_path: OTHER })
  await turnEnd($)
  expect(calls.compacts.length).toBe(1)
  expect(String(calls.compacts[0])).toContain(OTHER)
  expect(String(calls.compacts[0])).not.toContain(RESUME)
})

test("the session's end drops a pending request", async ($, on) => {
  const calls = stubs(on)
  await start($)
  await ask($)
  await $.session.end({ reason: 'clear', sessionId: SID })
  await turnEnd($)
  expect(calls.compacts.length).toBe(0)
})

test('a checkpoint tool that fails answers refused and queues nothing', async ($, on) => {
  const calls = stubs(on, { noSession: true })
  await start($)
  const r = await ask($)
  expect(String(r.result)).toContain('refused')
  expect(String(r.result)).toContain('failed')
  await turnEnd($)
  expect(calls.compacts.length).toBe(0)
})

test('a checker setting that is not an absolute path refuses the call, and the checker is not run', async ($, on) => {
  const calls = stubs(on, { env: { CHECKPOINT_COMPACT_CHECKER: 'checkpoint.py' } })
  await start($)
  const r = await ask($)
  expect(String(r.result)).toContain('refused')
  expect(String(r.result)).toContain('CHECKPOINT_COMPACT_CHECKER')
  expect(calls.runs.length).toBe(0)
})

test('missing fields are refused without running the checker', async ($, on) => {
  const calls = stubs(on)
  await start($)
  const r = await $.tool.call({ tool: TOOL, resume_path: RESUME, reason: 'no digest' })
  expect(String(r.result)).toContain('refused')
  expect(calls.runs.length).toBe(0)
})

test("the checker runs as an argument list, by default python3 on the plugin's own checker, with the session id", async ($, on) => {
  const calls = stubs(on)
  await start($)
  await ask($)
  expect(calls.runs[0]).toEqual(['python3', '-I', '-B', CHECKER, 'check', '--resume', RESUME, '--sha256', DIGEST,
    '--session', SID])
})

test('a session that changes during the turn-end check is not compacted', async ($, on) => {
  // The 1st $.session.id call is the tool call's and the 2nd the turn end's before its check; from the 3rd on, another
  // session answers.
  const calls = stubs(on, { sessionAt: (n) => (n < 3 ? SID : 'session-b') })
  await start($)
  await ask($)
  await turnEnd($)
  expect(calls.runs.length).toBe(2)
  expect(calls.compacts.length).toBe(0)
  expect(calls.submits.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('not compacted')
})

test('a session that changes after the compaction gets no prompt', async ($, on) => {
  const calls = stubs(on, { sessionAt: (n) => (n < 4 ? SID : 'session-b') })
  await start($)
  await ask($)
  await turnEnd($)
  expect(calls.compacts.length).toBe(1)
  expect(calls.submits.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('compacted at')
  expect(calls.logs.join(' ')).toContain('not sent')
})

test("the session's end during the turn-end check stops the compaction", async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const calls = stubs(on, { slowCheck: { clock, at: 2, reached: slow.reached } })
  await start($)
  await ask($)
  const turn = turnEnd($)
  await slow.started
  const ended = $.session.end({ reason: 'clear', sessionId: SID })
  await clock.advance(1000)
  await Promise.all([turn, ended])
  expect(calls.runs.length).toBe(2)
  expect(calls.compacts.length).toBe(0)
  expect(calls.submits.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('not compacted')
})

test('a new request during the turn-end check replaces the one being checked', async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const calls = stubs(on, { slowCheck: { clock, at: 2, reached: slow.reached } })
  await start($)
  await ask($)
  const turn = turnEnd($)
  await slow.started
  const asked = await ask($, { resume_path: OTHER })
  expect(String(asked.result)).toContain('accepted')
  await clock.advance(1000)
  await turn
  expect(calls.compacts.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('not compacted')
  await turnEnd($)
  expect(calls.compacts.length).toBe(1)
  expect(String(calls.compacts[0])).toContain(OTHER)
})

test("the session's end during the tool call's check queues nothing", async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const calls = stubs(on, { slowCheck: { clock, at: 1, reached: slow.reached } })
  await start($)
  const asked = ask($)
  await slow.started
  await $.session.end({ reason: 'clear', sessionId: SID })
  await clock.advance(1000)
  const r = await asked
  expect(String(r.result)).toContain('refused')
  await turnEnd($)
  expect(calls.runs.length).toBe(1)
  expect(calls.compacts.length).toBe(0)
})

test('a turn end whose session id cannot be read leaves a transcript line and compacts nothing', async ($, on) => {
  const calls = stubs(on, { sessionAt: (n) => (n === 1 ? SID : null) })
  await start($)
  await ask($)
  try {
    await turnEnd($)
  } catch (_error) {
    // Where a failed hook surfaces is the kit's business; what matters is the transcript line below.
  }
  expect(calls.runs.length).toBe(1)
  expect(calls.compacts.length).toBe(0)
  expect(calls.submits.length).toBe(0)
  expect(calls.logs.join(' ')).toContain('not compacted')
})

// A fake $ for calling compactAt directly: the compaction answers what result() returns or throws, the prompt and the
// transcript line behave as given, and the checker accepts every checkpoint.
type Fake = {
  result?: () => any
  submit?: (text: string) => any
  log?: (text: string) => any
  onRun?: () => void
}

function fakeHost(opts: Fake = {}) {
  const logs: string[] = []
  const submits: string[] = []
  const compacts: string[] = []
  const fake: any = {
    session: {
      id: async () => SID,
      compact: async (p: any) => {
        compacts.push(p.instructions)
        return opts.result ? opts.result() : { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
      },
    },
    plugin: { name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' },
    env: { get: async (_name: string) => undefined },
    process: {
      run: async () => {
        if (opts.onRun) opts.onRun()
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' }
      },
    },
    prompt: {
      submit: (p: any) => {
        submits.push(p.text)
        return opts.submit ? opts.submit(p.text) : Promise.resolve({ text: p.text })
      },
    },
    ui: {
      log: (text: string) => {
        logs.push(text)
        return opts.log ? opts.log(text) : Promise.resolve(undefined)
      },
    },
  }
  return { fake, logs, submits, compacts }
}

const REQUEST = { session: SID, resume: RESUME, sha256: DIGEST, keep: '' }

async function settle() {
  for (let i = 0; i < 10; i++) await Promise.resolve()
}

test('a compaction call that rejects submits nothing and leaves a transcript line', async () => {
  const host = fakeHost({ result: () => { throw new Error('compaction is switched off in this session (DISABLE_COMPACT)') } })
  const said = await compactAt(host.fake, REQUEST)
  expect(said).toContain('not compacted')
  expect(said).toContain('DISABLE_COMPACT')
  expect(host.submits.length).toBe(0)
  expect(host.logs.join(' ')).toContain('not compacted')
})

test('only a finished compaction counts: any other answer submits nothing and leaves a transcript line', async () => {
  const answers: any[] = [undefined, null, false, 0, '', 'done', [], {}, { kind: 'error' }, { ok: false }, { ok: true },
    { messages: 'summary' }, { messages: null }, { tokensBefore: 10, tokensAfter: 2 }, { skip: 'vetoed', messages: [] }]
  for (const answer of answers) {
    const host = fakeHost({ result: () => answer })
    const said = await compactAt(host.fake, REQUEST)
    expect(said).toContain('not compacted')
    expect(host.compacts.length).toBe(1)
    expect(host.submits.length).toBe(0)
    expect(host.logs.join(' ')).toContain('not compacted')
    expect(host.logs.join(' ')).not.toContain('compacted at')
  }
  for (const answer of [{ messages: [] }, { messages: [{ role: 'user', text: 'summary', toolUses: [] }], tokensBefore: 10, tokensAfter: 2 }]) {
    const host = fakeHost({ result: () => answer })
    const said = await compactAt(host.fake, REQUEST)
    expect(said).toContain('compacted at')
    expect(host.submits.length).toBe(1)
  }
})

test('a request that goes out of date during the turn-end check is not compacted', async () => {
  let live = true
  const host = fakeHost({ onRun: () => { live = false } })
  const said = await compactAt(host.fake, REQUEST, () => live)
  expect(said).toContain('not compacted')
  expect(host.compacts.length).toBe(0)
  expect(host.submits.length).toBe(0)
  expect(host.logs.join(' ')).toContain('not compacted')
})

test('a request that goes out of date during the compaction gets no prompt', async () => {
  let live = true
  const host = fakeHost({ result: () => { live = false; return { messages: [] } } })
  const said = await compactAt(host.fake, REQUEST, () => live)
  expect(said).toContain('compacted at')
  expect(said).toContain('not sent')
  expect(host.submits.length).toBe(0)
})

// Runs fn depth microtasks from now, as an event handler already waiting in the queue would.
function later(depth: number, fn: () => void) {
  if (depth <= 0) {
    fn()
    return
  }
  Promise.resolve().then(() => later(depth - 1, fn))
}

// The n-th $.session.id call puts the request out of date `depth` microtasks later; the compaction and the prompt record
// whether the request was still current when they were called. The 2nd call is the one just before the compaction and
// the 3rd the one just before the prompt.
async function outOfDateLater(at: number, depth: number) {
  let live = true
  let ids = 0
  const calledWhile: { compact: boolean[]; prompt: boolean[] } = { compact: [], prompt: [] }
  const host = fakeHost()
  host.fake.session.id = async () => {
    ids += 1
    if (ids === at) later(depth, () => { live = false })
    return SID
  }
  host.fake.session.compact = async () => {
    calledWhile.compact.push(live)
    return { messages: [] }
  }
  host.fake.prompt.submit = () => {
    calledWhile.prompt.push(live)
    return Promise.resolve()
  }
  const said = await compactAt(host.fake, REQUEST, () => live)
  return { said, calledWhile, logs: host.logs }
}

test('a request that goes out of date just after its last check is never compacted on', async () => {
  for (const depth of [1, 2, 3]) {
    const { calledWhile } = await outOfDateLater(2, depth)
    expect(calledWhile.compact.every((current) => current)).toBe(true)
    // Out of date before the check: refused. Out of date only after the call: compacted, as it was current then.
    expect(calledWhile.compact.length).toBe(depth === 1 ? 0 : 1)
  }
})

test('a request that goes out of date just after its last check gets no prompt', async () => {
  for (const depth of [1, 2, 3]) {
    const { calledWhile } = await outOfDateLater(3, depth)
    expect(calledWhile.compact.length).toBe(1)
    expect(calledWhile.prompt.every((current) => current)).toBe(true)
  }
})

test('a prompt that throws as it is sent leaves a transcript line and does not escape', async () => {
  const host = fakeHost({ submit: () => { throw new Error('the prompt box is closed') } })
  const said = await compactAt(host.fake, REQUEST)
  expect(said).toContain('compacted at')
  expect(said).toContain('could not be sent')
  expect(host.logs.join(' ')).toContain('could not be sent')
})

test('a prompt that is refused after it is sent leaves a transcript line', async () => {
  const host = fakeHost({ submit: () => Promise.reject(new Error('refused by another mod')) })
  const said = await compactAt(host.fake, REQUEST)
  expect(said).toContain('compacted at')
  await settle()
  expect(host.logs.join(' ')).toContain('could not be sent')
})

test('a transcript line that cannot be written changes nothing', async () => {
  const failures = [() => { throw new Error('no transcript here') }, () => Promise.reject(new Error('no transcript here'))]
  for (const log of failures) {
    const host = fakeHost({ log })
    const said = await compactAt(host.fake, REQUEST)
    await settle()
    expect(said).toContain('compacted at')
    expect(host.compacts.length).toBe(1)
    expect(host.submits.length).toBe(1)
  }
})

test('an error that cannot be turned into text still ends in a transcript line', async () => {
  // An error whose conversion to text throws the error itself, however often it is tried.
  const unshowable: any = { toString() { throw unshowable } }
  const host = fakeHost({ result: () => { throw unshowable } })
  const said = await compactAt(host.fake, REQUEST)
  expect(said).toContain('not compacted')
  expect(said).toContain('cannot be shown')
  expect(host.submits.length).toBe(0)
  expect(host.logs.join(' ')).toContain('not compacted')
})
