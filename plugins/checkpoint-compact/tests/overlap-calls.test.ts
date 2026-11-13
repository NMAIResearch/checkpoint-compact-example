// Two tool calls checked at the same time, run with `claude plugin test`: the first call the checker accepts stands and
// the other is refused, whichever was issued first, and the session's end while a call is checked leaves nothing to
// compact. The test body is the host: it calls the hooks module's own handlers, holds each resume file's first check
// until it releases it, and answers every other call at once. No model, process, file or network runs.
import { expect, test } from 'claude-code/testing'
import { register } from '../hooks/register.js'

const SID = 'session-under-test'
const FIRST = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const SECOND = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1130.md'
const DIGEST = 'a'.repeat(64)
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'
const ACCEPTS = { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' }

function host() {
  const hooks = new Map<string, any>()
  const held: { resume: string; release: () => void }[] = []
  const checked = new Set<string>()
  const compactions: string[] = []
  const $: any = {
    plugin: { name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' },
    session: {
      id: async () => SID,
      compact: async (e: any) => { compactions.push(e.instructions); return { messages: [] } },
      append: async (e: any) => ({ message: e.message, uuid: 'note' }),
    },
    tool: { register: async () => undefined },
    env: { get: async () => undefined },
    process: {
      run: (argv: string[]) => {
        const resume = argv[argv.indexOf('--resume') + 1]
        if (checked.has(resume)) return Promise.resolve(ACCEPTS)
        checked.add(resume)
        return new Promise((settle) => held.push({ resume, release: () => settle(ACCEPTS) }))
      },
    },
    prompt: { submit: async (e: any) => ({ text: e.text }) },
    ui: { log: () => undefined },
  }
  register((event: string, filter: any, handler?: any) => {
    hooks.set(event, handler ?? filter)
    return { catch: () => undefined }
  })
  const call = (resume: string) =>
    hooks.get('tool.call')($, { tool: TOOL, resume_path: resume, resume_sha256: DIGEST, reason: 'task finished' })
  const turnEnd = () =>
    hooks.get('turn.complete')($, { turnId: 't1', answer: '', durationMs: 1, isAborted: false }, async () => ({ text: '' }))
  const end = () => hooks.get('session.end')($, { reason: 'clear', sessionId: SID }, async (e: any) => e)
  // Lets the calls run until `count` checks are held; each await is one step of the module's own awaits.
  const until = async (count: number) => {
    for (let step = 0; step < 1000 && held.length < count; step += 1) await Promise.resolve()
    expect(held.length).toBe(count)
  }
  const release = (resume: string) => held.find((item) => item.resume === resume)!.release()
  return { compactions, call, turnEnd, end, until, release }
}

test('of two calls checked at the same time, the first accepted stands: issued first, answered first', async () => {
  const h = host()
  await h.end()
  const first = h.call(FIRST)
  const second = h.call(SECOND)
  await h.until(2)
  h.release(FIRST)
  expect(String((await first).result)).toContain('accepted')
  h.release(SECOND)
  const refused = String((await second).result)
  expect(refused).toContain('refused')
  expect(refused).toContain('another request was accepted while this one was being checked')
  await h.turnEnd()
  expect(h.compactions.length).toBe(1)
  expect(h.compactions[0]).toContain(FIRST)
})

test('of two calls checked at the same time, the first accepted stands: issued second, answered first', async () => {
  const h = host()
  await h.end()
  const first = h.call(FIRST)
  const second = h.call(SECOND)
  await h.until(2)
  h.release(SECOND)
  expect(String((await second).result)).toContain('accepted')
  h.release(FIRST)
  expect(String((await first).result)).toContain('refused')
  await h.turnEnd()
  expect(h.compactions.length).toBe(1)
  expect(h.compactions[0]).toContain(SECOND)
})

test("the session's end while a call is checked refuses that call and leaves nothing to compact", async () => {
  const h = host()
  await h.end()
  const first = h.call(FIRST)
  await h.until(1)
  await h.end()
  h.release(FIRST)
  const refused = String((await first).result)
  expect(refused).toContain('refused')
  expect(refused).toContain('the session ended')
  await h.turnEnd()
  expect(h.compactions.length).toBe(0)
})

test("the session's end while the second of two calls is checked drops the first and refuses the second", async () => {
  const h = host()
  await h.end()
  const first = h.call(FIRST)
  const second = h.call(SECOND)
  await h.until(2)
  h.release(FIRST)
  expect(String((await first).result)).toContain('accepted')
  await h.end()
  h.release(SECOND)
  expect(String((await second).result)).toContain('refused')
  await h.turnEnd()
  expect(h.compactions.length).toBe(0)
})
