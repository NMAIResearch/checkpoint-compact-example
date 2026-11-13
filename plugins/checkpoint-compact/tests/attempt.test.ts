// Tests for attempt, the one try at compacting that a main-loop turn end makes, called directly with a fake $ (run with
// `claude plugin test`). What matters: with mayWait, a compaction call the engine refuses is held once (wait: true) and
// refused the second time; a request held while another turn of the main loop is under way does not run the checker;
// compactAt, which never waits, refuses at once as before.
import { expect, test } from 'claude-code/testing'
import { attempt, compactAt } from '../hooks/register.js'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const DIGEST = 'a'.repeat(64)

function fakeHost(compact: () => any) {
  const logs: string[] = []
  let runs = 0
  const fake: any = {
    session: {
      id: async () => SID,
      compact: async () => compact(),
      append: async (p: any) => ({ message: p.message, uuid: 'u1' }),
    },
    plugin: { name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' },
    env: { get: async (_name: string) => undefined },
    process: {
      run: async () => {
        runs += 1
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' }
      },
    },
    prompt: { submit: (p: any) => Promise.resolve({ text: p.text }) },
    ui: { log: (text: string) => { logs.push(text); return Promise.resolve(undefined) } },
  }
  return { fake, logs, runs: () => runs }
}

const request = () => ({ session: SID, resume: RESUME, sha256: DIGEST, keep: '', generation: 1 })
const refusing = () => { throw new Error('synthetic: the session is not between turns') }

test('with mayWait, a refused compaction is held once and refused the second time', async () => {
  const host = fakeHost(refusing)
  const r = request()
  const first = await attempt(host.fake, r, () => true, () => false, true)
  expect(first.wait).toBe(true)
  expect(first.said).toContain('not compacted yet: the compaction was refused')
  const second = await attempt(host.fake, r, () => true, () => false, true)
  expect(second.wait).toBe(false)
  expect(second.said).toContain('not compacted: the compaction was refused')
  expect(second.said).toContain('again')
})

test('a request held while another turn of the main loop is under way does not run the checker', async () => {
  const host = fakeHost(() => ({ messages: [] }))
  const held = await attempt(host.fake, request(), () => true, () => true, true)
  expect(held.wait).toBe(true)
  expect(held.said).toContain('another turn of the main loop is under way')
  expect(host.runs()).toBe(0)
})

test('compactAt never waits: a refused compaction is refused at once', async () => {
  const host = fakeHost(refusing)
  const said = await compactAt(host.fake, request())
  expect(said).toBe('not compacted: synthetic: the session is not between turns')
})
