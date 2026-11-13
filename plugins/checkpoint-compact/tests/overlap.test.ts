// Tests for turns of the main loop that end while the plugin holds a request: while a turn-end attempt on it is under way,
// or while the note that it waits is being stored (findings F01 and F02 of the independent review of 0.1.1), run with
// `claude plugin test`. The stubs stand for the engine as Claude Code 2.1.306's declarations describe it, as in
// queued-prompt.test.ts: a plugin's $.prompt.submit waits in a queue until the session is idle; $.session.compact is
// refused while a turn runs (the compaction hook throws, which reaches the plugin as a rejected call). The test body plays
// the engine's scheduler, and the mocked clock holds back one answer (a compaction's, a session id's or a note's storage)
// while a turn starts and ends; nothing depends on timing.
//
// What matters: a turn of the main loop that the engine runs when the compaction is called, and that ends while the
// refusal, or the session id read after it, is still on its way, does not leave the checkpoint waiting for a turn end that
// has already gone: it is compacted once, on its own resume file, with no further turn end, and the session is not told
// that it waits; such a refusal is not the one unexplained refusal a request may wait out, even after that one, and it
// still counts towards the four waits; a turn that ended before the compaction call does not explain its refusal; a
// refusal that comes while another turn is under way waits for that turn, with no try at once. The note that a
// checkpoint waits is stored before anything else can compact it, so it is never stored after that compaction, and a
// turn of the main loop that ends while it is being stored is not lost: the checkpoint is then tried again at once, or
// waits for the turn that is under way by then; a subagent's turn end does not count; a session that ends meanwhile keeps
// nothing. The note says when it is out of date, so one stored after a newer checkpoint's compaction says so. This file
// imports nothing from the plugin, so it runs unchanged against 0.1.1, where it reproduces F01 and F02.
import { expect, mock, test } from 'claude-code/testing'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const OTHER = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1130.md'
const DIGEST = 'a'.repeat(64)
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'
const PEER = 'Another Claude session sent a message: the audit figures are in.'
const LATE = 'Compaction done at your checkpoint (a prompt queued after an earlier compaction).'
const WAITS = 'not compacted yet'

type Engine = {
  busy: boolean // a turn of the main loop runs, as the engine sees it
  queue: string[] // plugin prompts waiting for the session to be idle, oldest first
  calls: number // compaction calls
  compactions: string[] // the instructions of each compaction that ran
  compactedAt: number[] // for each compaction that ran, how many rows the conversation had stored when it ran
  refused: number // compaction calls refused
  logs: string[]
  runs: number // checker runs
  session: any // mock.session: the rows stored, oldest first
}

type Options = {
  // The at-th checker run answers only once the test has moved the clock on by 1000 ms; reached() is called as it starts.
  slowCheck?: { clock: any; at: number; reached: () => void }
  // Each compaction call (or only the at-th) decides at once whether it is refused, as the engine does, and answers only
  // once the test has moved the clock on by 1000 ms; reached(n) is called as the n-th call starts.
  slowCompact?: { clock: any; at?: number; reached: (n: number) => void }
  // The first session id read after a refused compaction answers only once the clock has moved on by 1000 ms.
  slowIdAfterRefusal?: { clock: any; reached: () => void }
  // The first note of the plugin whose text holds `match` is stored only once the clock has moved on by 1000 ms.
  slowNote?: { clock: any; match: string; reached: () => void }
  // Every compaction call is refused, as when compaction is switched off.
  alwaysRefuse?: boolean
}

const textOf = (row: any) => (row.message.content as any[]).map((block) => String(block.text ?? '')).join('')

function engine(on: any, opts: Options = {}): Engine {
  const session = mock.session(on)
  const eng: Engine = { busy: false, queue: [], calls: 0, compactions: [], compactedAt: [], refused: 0, logs: [], runs: 0, session }
  let slowIdDone = false
  let slowNoteDone = false
  on('session.append', { door: 'note' }, async ($: any, e: any, next: any) => {
    if (opts.slowNote && !slowNoteDone && textOf(e).includes(opts.slowNote.match)) {
      slowNoteDone = true
      opts.slowNote.reached()
      await opts.slowNote.clock.sleep(1000)
    }
    return next(e)
  })
  on('tool.register', () => ({ value: undefined }))
  on('session.id', async () => {
    if (opts.slowIdAfterRefusal && !slowIdDone && eng.refused > 0) {
      slowIdDone = true
      opts.slowIdAfterRefusal.reached()
      await opts.slowIdAfterRefusal.clock.sleep(1000)
    }
    return { value: SID }
  })
  on('env.get', () => ({ value: undefined }))
  on('process.run', async () => {
    eng.runs += 1
    if (opts.slowCheck && eng.runs === opts.slowCheck.at) {
      opts.slowCheck.reached()
      await opts.slowCheck.clock.sleep(1000)
    }
    return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' } }
  })
  on('session.compact', async ($: any, e: any) => {
    eng.calls += 1
    const n = eng.calls
    const refusing = eng.busy || opts.alwaysRefuse === true
    if (opts.slowCompact && (opts.slowCompact.at === undefined || opts.slowCompact.at === n)) {
      opts.slowCompact.reached(n)
      await opts.slowCompact.clock.sleep(1000)
    }
    if (refusing) {
      eng.refused += 1
      throw new Error('synthetic: the session is not between turns')
    }
    eng.compactions.push(e.instructions)
    eng.compactedAt.push(session.appended().length)
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
function endTurn($: any, eng: Engine, turnId: string, agentId?: string) {
  if (agentId === undefined) eng.busy = false
  const e = { turnId, answer: '', durationMs: 1, isAborted: false, usage: null }
  return $.turn.complete(agentId === undefined ? e : { ...e, agentId })
}

const rows = (eng: Engine): string[] => eng.session.appended().map(textOf)

// What a test saw, on one line of the test's output.
function observe(name: string, eng: Engine) {
  console.log('OBSERVED ' + name + ': ' + JSON.stringify({
    calls: eng.calls, refused: eng.refused, compactions: eng.compactions.map((text) => (text.includes(OTHER) ? 'OTHER' : 'RESUME')),
    compactedAt: eng.compactedAt, rows: rows(eng).map((text) => text.slice(0, 90)), logs: eng.logs, queued: eng.queue.length,
  }))
}

// F01's sequence: an accepted checkpoint's turn t1 ends; the engine starts the late prompt's turn t2 at once, before the
// plugin has heard of it, so the compaction call is refused; t2 then starts and ends, as the plugin hears it, while the
// refusal (or the session id read after it) is still on its way back to the plugin. Nothing ends after that.
async function refusedWhileATurnStartsAndEnds($: any, on: any, where: 'refusal' | 'session id', opts: Options = {}) {
  const clock = mock.clock(on)
  const held = signal()
  const eng = engine(on, where === 'refusal'
    ? { ...opts, slowCompact: { clock, at: 1, reached: () => held.reached() } }
    : { ...opts, slowIdAfterRefusal: { clock, reached: held.reached } })
  await start($)
  await ask($)
  const ending = endTurn($, eng, 't1')
  eng.busy = true // the engine has started t2; the plugin has not heard of it yet
  await held.started
  await beginTurn($, eng, 't2', LATE)
  await endTurn($, eng, 't2')
  await clock.advance(1000)
  await ending
  await clock.settle()
  return { clock, eng }
}

for (const where of ['refusal', 'session id'] as const) {
  test(`F01: a turn of the main loop that ends while the ${where === 'refusal' ? 'refused compaction' : 'session id read after a refusal'} is still on its way does not strand the checkpoint`, async ($, on) => {
    const { eng } = await refusedWhileATurnStartsAndEnds($, on, where)
    observe('F01 ' + where, eng)
    expect(eng.refused).toBe(1)
    expect(eng.compactions.length).toBe(1)
    expect(eng.compactions[0]).toContain(RESUME)
    expect(eng.queue.length).toBe(1)
    expect(eng.queue[0]).toContain(RESUME)
  })
}

test('a checkpoint tried again at once is not announced as waiting', async ($, on) => {
  const { eng } = await refusedWhileATurnStartsAndEnds($, on, 'refusal')
  expect(eng.compactions.length).toBe(1)
  expect(rows(eng).filter((text) => text.includes(WAITS))).toEqual([])
  expect(eng.logs.join(' ')).toContain('tried again now')
})

test('a refusal explained by a turn that ended during the call is not the one unexplained refusal a request may wait out', async ($, on) => {
  const { eng } = await refusedWhileATurnStartsAndEnds($, on, 'refusal', { alwaysRefuse: true })
  expect(eng.refused).toBe(2) // the explained one, then the one at once after it, which waits
  expect(eng.logs.filter((line) => line.includes('not compacted: ')).length).toBe(0) // nothing dropped yet
  await endTurn($, eng, 't3')
  expect(eng.refused).toBe(3)
  expect(eng.compactions.length).toBe(0)
  const dropped = eng.logs.filter((line) => line.includes('not compacted: '))
  expect(dropped.length).toBe(1)
  expect(dropped[0]).toMatch(/not compacted: the compaction was refused \(.*\) again$/)
})

test('a refusal explained by a turn that ended during the call, after an unexplained one, is tried again at once', async ($, on) => {
  const clock = mock.clock(on)
  const second = signal()
  const eng = engine(on, { slowCompact: { clock, at: 2, reached: () => second.reached() } })
  await start($)
  await ask($)
  const first = endTurn($, eng, 't1')
  eng.busy = true // a turn the plugin has not heard of: the first call is refused with no turn seen, and the request waits
  await first
  expect(eng.refused).toBe(1)
  const ending = endTurn($, eng, 't2') // that turn's end; the engine starts the late prompt's turn t3 at once
  eng.busy = true
  await second.started
  await beginTurn($, eng, 't3', LATE)
  await endTurn($, eng, 't3') // t3 starts and ends while the second refusal is on its way
  await clock.advance(1000)
  await ending
  await clock.settle()
  expect(eng.refused).toBe(2)
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
})

test('a refusal that comes while another turn of the main loop is under way waits for that turn, with no try at once', async ($, on) => {
  const clock = mock.clock(on)
  const held = signal()
  const eng = engine(on, { slowCompact: { clock, at: 1, reached: () => held.reached() } })
  await start($)
  await ask($)
  const ending = endTurn($, eng, 't1')
  eng.busy = true
  await held.started
  await beginTurn($, eng, 't2', LATE)
  await endTurn($, eng, 't2')
  await beginTurn($, eng, 't3', PEER) // under way when the refusal reaches the plugin
  await clock.advance(1000)
  await ending
  expect(eng.calls).toBe(1)
  expect(eng.logs.filter((line) => line.includes(WAITS)).length).toBe(1)
  await endTurn($, eng, 't3')
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
})

test('refusals each explained by a turn that ended during the call stop after four waits', async ($, on) => {
  const clock = mock.clock(on)
  const calls = [signal(), signal(), signal(), signal(), signal(), signal()]
  const eng = engine(on, { alwaysRefuse: true, slowCompact: { clock, reached: (n) => calls[n - 1].reached() } })
  await start($)
  await ask($)
  const ending = endTurn($, eng, 't0')
  for (let n = 1; n <= 5; n++) {
    await calls[n - 1].started
    await beginTurn($, eng, 'x' + n, LATE)
    await endTurn($, eng, 'x' + n)
    await clock.advance(1000)
  }
  await ending
  await clock.settle()
  expect(eng.calls).toBe(5)
  expect(eng.refused).toBe(5)
  expect(eng.logs.filter((line) => line.includes(WAITS)).length).toBe(4)
  expect(eng.logs.join(' ')).toContain('after waiting through 4 turn ends')
  await endTurn($, eng, 't9')
  expect(eng.calls).toBe(5)
})

test('a turn of the main loop that ended before the compaction call does not explain its refusal', async ($, on) => {
  const clock = mock.clock(on)
  const slow = signal()
  const eng = engine(on, { slowCheck: { clock, at: 2, reached: slow.reached } })
  await start($)
  await ask($)
  const ending = endTurn($, eng, 't1')
  await slow.started // the turn-end check of the checkpoint runs
  await beginTurn($, eng, 't2', PEER)
  await endTurn($, eng, 't2')
  eng.busy = true // then a turn the plugin has not heard of holds the session
  await clock.advance(1000)
  await ending
  expect(eng.refused).toBe(1) // not tried again at once: that refusal is the one a request may wait out
  expect(eng.compactions.length).toBe(0)
  await endTurn($, eng, 't3') // the end of the turn the plugin had not heard of
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
})

// F02's sequence: an accepted checkpoint's turn t1 ends while a peer's turn t2 runs, so the checkpoint waits and the
// plugin adds the note that it waits; that note is held back from storage while t2 ends.
async function noteHeldWhileTheTurnEnds($: any, on: any) {
  const clock = mock.clock(on)
  const note = signal()
  const eng = engine(on, { slowNote: { clock, match: WAITS, reached: note.reached } })
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  const first = endTurn($, eng, 't1')
  await note.started
  const second = endTurn($, eng, 't2')
  await clock.settle()
  await clock.advance(1000)
  await Promise.all([first, second])
  await clock.settle()
  return eng
}

test('F02: a note that the checkpoint waits, stored late, says nothing out of date after the compaction', async ($, on) => {
  const eng = await noteHeldWhileTheTurnEnds($, on)
  observe('F02', eng)
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
  const after = rows(eng).slice(eng.compactedAt[0])
  expect(after.some((text) => text.includes('compaction done') && text.includes(RESUME))).toBe(true)
  // A note after the compaction that says the checkpoint still waits is allowed only when it says when it is out of date.
  expect(after.filter((text) => text.includes(WAITS) && !text.includes('out of date'))).toEqual([])
})

test('the note that a checkpoint waits is in the conversation before that checkpoint is compacted', async ($, on) => {
  const eng = await noteHeldWhileTheTurnEnds($, on)
  expect(eng.compactions.length).toBe(1)
  const all = rows(eng)
  expect(all.slice(0, eng.compactedAt[0]).filter((text) => text.includes(WAITS)).length).toBe(1)
  expect(all.slice(eng.compactedAt[0]).filter((text) => text.includes(WAITS))).toEqual([])
})

test('a turn of the main loop that ends while the note is stored, another then under way, leaves the checkpoint waiting for that turn', async ($, on) => {
  const clock = mock.clock(on)
  const note = signal()
  const eng = engine(on, { slowNote: { clock, match: WAITS, reached: note.reached } })
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  const first = endTurn($, eng, 't1')
  await note.started
  await endTurn($, eng, 't2')
  await beginTurn($, eng, 't3', PEER)
  await clock.advance(1000)
  await first
  expect(eng.compactions.length).toBe(0)
  await endTurn($, eng, 't3')
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
  expect(rows(eng).filter((text) => text.includes(WAITS)).length).toBe(1)
})

test("a subagent's turn end while the note is stored does not make the checkpoint be tried again", async ($, on) => {
  const clock = mock.clock(on)
  const note = signal()
  const eng = engine(on, { slowNote: { clock, match: WAITS, reached: note.reached } })
  await start($)
  await ask($)
  const first = endTurn($, eng, 't1')
  eng.busy = true // a turn the plugin has not heard of holds the session: the compaction is refused and the request waits
  await note.started
  await beginTurn($, eng, 'a1', 'subagent task', 'agent-1')
  await endTurn($, eng, 'a1', 'agent-1')
  await clock.advance(1000)
  await first
  expect(eng.refused).toBe(1)
  await endTurn($, eng, 't2') // the end of the turn the plugin had not heard of
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(RESUME)
})

test('a waiting checkpoint whose session ends while its note is stored is not kept', async ($, on) => {
  const clock = mock.clock(on)
  const note = signal()
  const eng = engine(on, { slowNote: { clock, match: WAITS, reached: note.reached } })
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  const first = endTurn($, eng, 't1')
  await note.started
  await $.session.end({ reason: 'clear', sessionId: SID })
  await clock.advance(1000)
  await first
  await beginTurn($, eng, 't3', PEER)
  await endTurn($, eng, 't3')
  expect(eng.calls).toBe(0)
  expect(eng.logs.join(' ')).not.toContain('the session ended or a newer request replaced this one')
})

test('the note that a checkpoint waits says when it is out of date', async ($, on) => {
  const eng = engine(on)
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  await endTurn($, eng, 't1')
  const notes = rows(eng).filter((text) => text.includes(WAITS))
  expect(notes.length).toBe(1)
  expect(notes[0]).toContain(RESUME)
  expect(notes[0]).toContain('out of date')
})

test("a note that a checkpoint waits, stored after a newer checkpoint's compaction, says when it is out of date", async ($, on) => {
  const clock = mock.clock(on)
  const note = signal()
  const eng = engine(on, { slowNote: { clock, match: WAITS, reached: note.reached } })
  await start($)
  await ask($)
  await beginTurn($, eng, 't2', PEER)
  const first = endTurn($, eng, 't1')
  await note.started
  await ask($, { resume_path: OTHER }) // a newer checkpoint, accepted in t2 while the note is held back
  const second = endTurn($, eng, 't2')
  await clock.settle()
  await clock.advance(1000)
  await Promise.all([first, second])
  await clock.settle()
  observe('F02 newer checkpoint', eng)
  expect(eng.compactions.length).toBe(1)
  expect(eng.compactions[0]).toContain(OTHER)
  const late = rows(eng).slice(eng.compactedAt[0]).filter((text) => text.includes(WAITS))
  for (const text of late) expect(text).toContain('out of date')
  await endTurn($, eng, 't3')
  expect(eng.compactions.length).toBe(1)
})
