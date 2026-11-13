// Tests for the queued-prompt defect, run with
// `claude plugin test`. The stubs stand for the engine as Claude Code 2.1.306's declarations describe it: a plugin's
// $.prompt.submit waits in a queue until the session is idle and is never folded into a running turn; a peer's message
// can take the turn first; $.session.compact is refused while a turn runs (here the compaction hook throws, which reaches
// the plugin as a rejected call). The test body plays the engine's scheduler: it starts and ends the main loop's turns.
//
// What matters: right after a compaction a note naming the resume file is in the conversation, before any turn starts,
// whoever starts it; the queued prompt names the file and says when it is out of date; a checkpoint accepted in a turn
// that a peer's message started, whose turn end meets the late prompt's turn, waits for that turn and is then compacted,
// on its own resume file, and the session is told once that it waits; a compaction refused while no other turn has been
// seen waits once and is then tried again, and one refused twice is dropped with a transcript line; a request waits
// through at most four turn ends; a waiting request gives way to a newer one and to the session's end; turns seen before
// the session's end do not hold a later request; a subagent's turn does not hold the session; the note and the prompt are
// each sent only while the request is current; a note that cannot be added, or is refused, leaves the prompt to go. This file imports only what the plugin exported before this change,
// so it also runs against the earlier register.js.
import { expect, mock, test } from 'claude-code/testing'
import { compactAt } from '../hooks/register.js'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const OTHER = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1130.md'
const DIGEST = 'a'.repeat(64)
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'
const PEER = 'Another Claude session sent a message: the audit figures are in.'

type Engine = {
  busy: boolean // a turn of the main loop runs, as the engine sees it
  queue: string[] // plugin prompts waiting for the session to be idle, oldest first
  compactions: string[] // the instructions of each compaction that ran
  refused: number // compaction calls refused because a turn ran
  logs: string[]
  runs: number // checker runs
}

type Options = {
  // The at-th checker run answers only once the test has moved the clock on by 1000 ms; reached() is called as it starts.
  slow?: { clock: any; at: number; reached: () => void }
  // Every compaction call is refused, as when compaction is switched off.
  alwaysRefuse?: boolean
}

function engine(on: any, opts: Options = {}): Engine {
  const eng: Engine = { busy: false, queue: [], compactions: [], refused: 0, logs: [], runs: 0 }
  on('tool.register', () => ({ value: undefined }))
  on('session.id', () => ({ value: SID }))
  on('env.get', () => ({ value: undefined }))
  on('process.run', async () => {
    eng.runs += 1
    if (opts.slow && eng.runs === opts.slow.at) {
      opts.slow.reached()
      await opts.slow.clock.sleep(1000)
    }
    return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' } }
  })
  on('session.compact', ($: any, e: any) => {
    if (eng.busy || opts.alwaysRefuse) {
      eng.refused += 1
      throw new Error('synthetic: the session is not between turns')
    }
    eng.compactions.push(e.instructions)
    return { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
  })
  on('prompt.submit', ($: any, e: any) => {
    eng.queue.push(e.text)
    return { text: e.text }
  })
  on('ui.log', ($: any, e: any) => { eng.logs.push(e.text); return { value: undefined } })
  on('session.start', () => ({ cwd: '/work' }))
  on('session.end', ($: any, e: any) => ({ sessionId: e.sessionId ?? SID }))
  on('turn.start', ($: any, e: any) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  return eng
}

function signal(): { reached: () => void; started: Promise<void> } {
  let reached = () => {}
  const started = new Promise<void>((resolve) => { reached = resolve })
  return { reached: () => reached(), started }
}

const start = ($: any) => $.session.start({ cwd: '/work' })

const ask = ($: any, fields: object = {}) =>
  $.tool.call({ tool: TOOL, resume_path: RESUME, resume_sha256: DIGEST, reason: 'task finished', ...fields })

// The engine starts a turn of the main loop (or of a subagent, with agentId).
async function beginTurn($: any, eng: Engine, turnId: string, text: string, agentId?: string) {
  if (agentId === undefined) eng.busy = true
  await $.turn.start(agentId === undefined ? { text, turnId } : { text, turnId, agentId })
}

// The turn has ended: the session is between turns as its turn.complete hooks run.
function endTurn($: any, eng: Engine, turnId: string) {
  eng.busy = false
  return $.turn.complete({ turnId, answer: '', durationMs: 1, isAborted: false, usage: null })
}

// The idle session starts the oldest queued plugin prompt as a turn of its own; returns its text.
async function idle($: any, eng: Engine, turnId: string): Promise<string> {
  const text = eng.queue.shift()
  expect(text).toBeDefined()
  await beginTurn($, eng, turnId, String(text))
  return String(text)
}

const textOf = (row: any) => (row.message.content as any[]).map((block) => String(block.text ?? '')).join('')

test('the note naming the resume file is in the conversation before the next turn starts, whoever starts it', async ($, on) => {
  const eng = engine(on)
  const session = mock.session(on)
  await start($)
  await ask($)
  await endTurn($, eng, 't1')
  expect(eng.compactions.length).toBe(1)
  const notes = session.appended().filter((row: any) => row.door === 'note').map(textOf)
  expect(notes.length).toBe(1)
  expect(notes[0]).toContain(RESUME)
  expect(notes[0]).toContain(DIGEST)
  expect(notes[0]).toContain('then continue with the next task it names')
  expect(eng.queue.length).toBe(1) // the prompt waits for the session to be idle
  await beginTurn($, eng, 't2', PEER) // a peer's message takes the turn; the note is already there for it
  expect(session.appended().filter((row: any) => row.door === 'note').length).toBe(1)
})

test('with nothing else waiting, the queued prompt takes the next turn and names the right file', async ($, on) => {
  const eng = engine(on)
  await start($)
  await ask($)
  await endTurn($, eng, 't1')
  const prompt = await idle($, eng, 't2')
  expect(prompt).toContain(RESUME)
  expect(prompt).toContain(DIGEST)
  expect(prompt).toContain('then continue with the next task it names')
  expect(eng.logs.join(' ')).not.toContain('not compacted')
})

test('a checkpoint whose turn end meets a late prompt waits for that turn, then compacts on its own file', async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const eng = engine(on, { slow: { clock, at: 4, reached: slow.reached } })
  await start($)
  await ask($) // checker run 1
  await endTurn($, eng, 't1') // checker run 2; compaction on RESUME; its prompt is queued
  expect(eng.compactions.length).toBe(1)
  await beginTurn($, eng, 't2', PEER) // the peer's message, delivered during the compaction, takes the turn
  await ask($, { resume_path: OTHER }) // checker run 3: a new checkpoint, accepted in that turn
  const ending = endTurn($, eng, 't2') // checker run 4 waits on the clock
  await slow.started
  const late = await idle($, eng, 't3') // the idle session starts the prompt queued after the first compaction
  expect(late).toContain(RESUME)
  await clock.advance(1000)
  await ending
  expect(eng.compactions.length).toBe(1) // not compacted while the late prompt's turn runs
  expect(eng.refused).toBe(0) // nor asked for, since that turn was seen to start
  await endTurn($, eng, 't3')
  expect(eng.compactions.length).toBe(2)
  expect(eng.compactions[1]).toContain(OTHER)
  expect(eng.queue[eng.queue.length - 1]).toContain(OTHER)
})

test('the late prompt says it is out of date, and the session is told once that the checkpoint waits', async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const eng = engine(on, { slow: { clock, at: 4, reached: slow.reached } })
  const session = mock.session(on)
  await start($)
  await ask($)
  await endTurn($, eng, 't1')
  await beginTurn($, eng, 't2', PEER)
  await ask($, { resume_path: OTHER })
  const ending = endTurn($, eng, 't2')
  await slow.started
  const late = await idle($, eng, 't3')
  await clock.advance(1000)
  await ending
  expect(late).toContain('out of date')
  const waits = session.appended().map(textOf).filter((text: string) => text.includes('not compacted yet'))
  expect(waits.length).toBe(1)
  expect(waits[0]).toContain(OTHER)
  expect(eng.logs.join(' ')).toContain('not compacted yet')
})

test('a compaction refused while no other turn was seen waits once and is then tried again', async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const eng = engine(on, { slow: { clock, at: 2, reached: slow.reached } })
  await start($)
  await ask($)
  const ending = endTurn($, eng, 't1')
  await slow.started
  eng.busy = true // a turn the plugin has not been told of yet holds the session
  await clock.advance(1000)
  await ending
  expect(eng.refused).toBe(1)
  expect(eng.compactions.length).toBe(0)
  await endTurn($, eng, 't2') // that turn's end
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
})

test('a compaction refused twice is dropped with a transcript line', async ($, on) => {
  const eng = engine(on, { alwaysRefuse: true })
  await start($)
  await ask($)
  await endTurn($, eng, 't1')
  await endTurn($, eng, 't2')
  await endTurn($, eng, 't3')
  expect(eng.refused).toBe(2)
  expect(eng.compactions.length).toBe(0)
  expect(eng.queue.length).toBe(0)
  expect(eng.logs.join(' ')).toContain('not compacted: the compaction was refused')
  expect(eng.logs.join(' ')).toContain('again')
})

test('a request waits through at most four turn ends, and the session is told once', async ($, on) => {
  const eng = engine(on)
  const session = mock.session(on)
  await start($)
  await ask($)
  await beginTurn($, eng, 'held', PEER) // a turn the plugin sees start and never sees end
  for (const id of ['t1', 't2', 't3', 't4', 't5', 't6']) await endTurn($, eng, id)
  expect(eng.compactions.length).toBe(0)
  expect(eng.runs).toBe(1) // only the tool call's check: a wait does not run the checker
  expect(eng.logs.filter((line) => line.includes('not compacted yet')).length).toBe(4)
  expect(eng.logs.join(' ')).toContain('after waiting through 4 turn ends')
  expect(session.appended().map(textOf).filter((text: string) => text.includes('not compacted yet')).length).toBe(1)
})

test("turns seen before the session's end do not hold a request made after it", async ($, on) => {
  const eng = engine(on)
  await start($)
  await beginTurn($, eng, 'before', PEER) // its end is never seen
  await $.session.end({ reason: 'clear', sessionId: SID })
  await ask($)
  await endTurn($, eng, 't1')
  expect(eng.compactions.length).toBe(1)
})

test('a waiting request gives way to a newer one', async ($, on) => {
  const eng = engine(on)
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  await endTurn($, eng, 't1') // t2 holds the session: the request on RESUME waits
  expect(eng.compactions.length).toBe(0)
  await ask($, { resume_path: OTHER })
  await endTurn($, eng, 't2')
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(OTHER)
  expect(eng.compactions[0]).not.toContain(RESUME)
})

test("a waiting request is dropped at the session's end", async ($, on) => {
  const eng = engine(on)
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  await endTurn($, eng, 't1')
  await $.session.end({ reason: 'clear', sessionId: SID })
  await endTurn($, eng, 't2')
  expect(eng.compactions.length).toBe(0)
})

test("a subagent's turn does not hold the session", async ($, on) => {
  const eng = engine(on)
  await start($)
  await ask($)
  await beginTurn($, eng, 'a1', 'subagent task', 'agent-1')
  await endTurn($, eng, 't1')
  expect(eng.compactions.length).toBe(1)
})

// A fake $ for calling attempt and compactAt directly: the checker accepts, the compaction answers as given, and the
// note, the prompt and the transcript line are recorded.
function fakeHost(opts: { compact?: () => any; append?: (p: any) => any; onId?: (n: number) => void } = {}) {
  const logs: string[] = []
  const submits: string[] = []
  const notes: string[] = []
  let ids = 0
  const fake: any = {
    session: {
      id: async () => {
        ids += 1
        if (opts.onId) opts.onId(ids)
        return SID
      },
      compact: async () => (opts.compact ? opts.compact() : { messages: [] }),
      append: async (p: any) => {
        notes.push(textOf(p))
        return opts.append ? opts.append(p) : { message: p.message, uuid: 'u1' }
      },
    },
    plugin: { name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' },
    env: { get: async (_name: string) => undefined },
    process: { run: async () => ({ exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' }) },
    prompt: { submit: (p: any) => { submits.push(p.text); return Promise.resolve({ text: p.text }) } },
    ui: { log: (text: string) => { logs.push(text); return Promise.resolve(undefined) } },
  }
  return { fake, logs, submits, notes }
}

const request = () => ({ session: SID, resume: RESUME, sha256: DIGEST, keep: '', generation: 1 })

function later(depth: number, fn: () => void) {
  if (depth <= 0) {
    fn()
    return
  }
  Promise.resolve().then(() => later(depth - 1, fn))
}

// The n-th $.session.id call puts the request out of date `depth` microtasks later. The 3rd call is the one just before
// the note and the 4th the one just before the prompt; each records whether the request was current when it was called.
async function outOfDateAt(at: number, depth: number) {
  let live = true
  const calledWhile: { note: boolean[]; prompt: boolean[] } = { note: [], prompt: [] }
  const host = fakeHost({ onId: (n) => { if (n === at) later(depth, () => { live = false }) } })
  host.fake.session.append = async (p: any) => { calledWhile.note.push(live); return { message: p.message, uuid: 'u1' } }
  host.fake.prompt.submit = () => { calledWhile.prompt.push(live); return Promise.resolve() }
  await compactAt(host.fake, request(), () => live)
  return calledWhile
}

test('the note and the prompt are each sent only while the request is current', async () => {
  for (const depth of [1, 2, 3]) {
    const atNote = await outOfDateAt(3, depth)
    expect(atNote.note.every((current) => current)).toBe(true)
    expect(atNote.prompt.every((current) => current)).toBe(true)
    expect(atNote.note.length).toBe(depth === 1 ? 0 : 1)
    const atPrompt = await outOfDateAt(4, depth)
    expect(atPrompt.note.length).toBe(1)
    expect(atPrompt.prompt.every((current) => current)).toBe(true)
  }
})

test('a note that cannot be added, or is refused, leaves the prompt to go', async () => {
  for (const append of [() => { throw new Error('no conversation here') }, () => ({ deny: 'refused by another mod' })]) {
    const host = fakeHost({ append })
    const said = await compactAt(host.fake, request())
    expect(said).toContain('compacted at')
    expect(host.submits.length).toBe(1)
    expect(host.logs.join(' ')).toMatch(/the note naming the resume file (could not be added|was refused)/)
  }
})
