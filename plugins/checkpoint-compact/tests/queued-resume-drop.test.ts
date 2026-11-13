// The test body plays the engine's idle queue. A plugin's submit queues raw text first;
// prompt.submit hooks run when that text is delivered, before its own turn starts.
import { expect, test } from 'claude-code/testing'
import { register } from '../hooks/register.js'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const DIGEST = 'a'.repeat(64)
const PLUGIN = 'checkpoint-compact'
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'

function scheduler(options: any = {}) {
  const hooks = new Map<string, any>()
  const queue: any[] = []
  const notes: string[] = []
  const model: string[] = []
  const turns: string[] = []
  const compactions: string[] = []
  const host: any = {
    session: {
      id: async () => {
        if (options.onId) await options.onId()
        return SID
      },
      compact: async (e: any) => { compactions.push(e.instructions); return { messages: [] } },
      append: async (e: any) => {
        if (options.beforeNote) await options.beforeNote()
        if (options.refuseNote) return { deny: 'synthetic refusal' }
        if (options.throwNote) throw new Error('synthetic append failure')
        notes.push(e.message.content[0].text)
        return { message: e.message, uuid: 'note-' + notes.length }
      },
    },
    tool: { register: async () => undefined },
    plugin: { name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' },
    env: { get: async () => undefined },
    process: { run: async () => ({ exitCode: 0, stdout: '{"ok":true,"reasons":[]}', stderr: '' }) },
    prompt: { submit: (e: any) => new Promise((settle) => queue.push({ text: e.text, settle })) },
    ui: { log: () => undefined },
  }
  register((event: string, filter: any, handler?: any) => {
    hooks.set(event, handler ?? filter)
    return { catch: () => undefined }
  })
  const dispatch = async (event: string, e: any) => {
    const bottom = async (arg: any) => event === 'prompt.submit' ? { text: arg.text, origin: arg.origin } : { turnId: arg.turnId }
    return hooks.has(event) ? hooks.get(event)(host, e, bottom) : bottom(e)
  }
  const begin = async (id: string, text = 'Another message', agentId?: string) => {
    turns.push(id)
    await dispatch('turn.start', { text, turnId: id, ...(agentId === undefined ? {} : { agentId }) })
  }
  const end = (id: string, agentId?: string) =>
    dispatch('turn.complete', { turnId: id, answer: '', durationMs: 1, isAborted: false, ...(agentId === undefined ? {} : { agentId }) })
  const deliver = async (id: string) => {
    const queued = queue.shift()
    expect(queued).toBeDefined()
    const result = await dispatch('prompt.submit', { text: queued.text, wait: false, origin: { kind: 'plugin', name: PLUGIN } })
    queued.settle(result)
    if (result.drop === undefined) {
      model.push(result.text)
      await begin(id, result.text)
    }
    return result
  }
  const reset = () => dispatch('session.end', { reason: 'clear', sessionId: SID })
  const checkpoint = async (resume = RESUME) => {
    await dispatch('tool.call', { tool: TOOL, resume_path: resume, resume_sha256: DIGEST, reason: 'task finished' })
    await end('checkpoint')
  }
  return { queue, notes, model, turns, compactions, dispatch, begin, end, deliver, reset, checkpoint }
}

test('a queued resume is dropped once another main-loop turn has started after the note was added', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  expect(eng.notes.length).toBe(1)
  expect(eng.queue.length).toBe(1)
  await eng.begin('person')
  await eng.end('person')
  const result = await eng.deliver('stale-resume')
  expect(result.drop).toBeDefined()
  expect(eng.model.length).toBe(0)
  expect(eng.turns).not.toContain('stale-resume')
})

test('the queued resume still starts its own turn when no other main-loop turn starts', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  const result = await eng.deliver('resume')
  expect(result.drop).toBeUndefined()
  expect(eng.model.length).toBe(1)
  expect(eng.model[0]).toContain(RESUME)
  expect(eng.model[0]).toContain(DIGEST)
  expect(eng.model[0]).toContain('then continue with the next task it names')
  expect(eng.turns).toContain('resume')
})

test('a subagent turn does not consume the main conversation note', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  await eng.begin('subagent', 'subagent task', 'agent-1')
  await eng.end('subagent', 'agent-1')
  expect((await eng.deliver('resume')).drop).toBeUndefined()
  expect(eng.model.length).toBe(1)
})

test('a refused or failed note leaves the resume prompt available after another turn', async () => {
  for (const options of [{ refuseNote: true }, { throwNote: true }]) {
    const eng = scheduler(options)
    await eng.reset()
    await eng.checkpoint()
    expect(eng.notes.length).toBe(0)
    await eng.begin('person')
    await eng.end('person')
    expect((await eng.deliver('resume')).drop).toBeUndefined()
    expect(eng.model.length).toBe(1)
  }
})

test('a turn before the note is stored does not consume it', async () => {
  let enter = () => {}
  let release = () => {}
  const entered = new Promise<void>((resolve) => { enter = resolve })
  const held = new Promise<void>((resolve) => { release = resolve })
  const eng = scheduler({ beforeNote: async () => { enter(); await held } })
  await eng.reset()
  const checkpoint = eng.checkpoint()
  await entered
  await eng.begin('before-note')
  await eng.end('before-note')
  release()
  await checkpoint
  expect((await eng.deliver('resume')).drop).toBeUndefined()
  expect(eng.model.length).toBe(1)
})

test('a turn after the note but before prompt submission also consumes the note', async () => {
  let ids = 0
  let eng: any
  eng = scheduler({ onId: async () => {
    ids += 1
    if (ids === 5) {
      expect(eng.notes.length).toBe(1)
      await eng.begin('between-note-and-submit')
      await eng.end('between-note-and-submit')
    }
  } })
  await eng.reset()
  await eng.checkpoint()
  expect((await eng.deliver('stale-resume')).drop).toBeDefined()
  expect(eng.model.length).toBe(0)
})

test('the guard leaves person and other-plugin prompts with matching text alone', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  await eng.begin('person')
  await eng.end('person')
  const text = eng.queue[0].text
  for (const origin of [{ kind: 'composer' }, { kind: 'plugin', name: 'another-plugin' }]) {
    const result = await eng.dispatch('prompt.submit', { text, wait: false, origin })
    expect(result.text).toBe(text)
    expect(result.drop).toBeUndefined()
  }
  expect((await eng.deliver('stale-resume')).drop).toBeDefined()
})

test('an older consumed prompt is dropped while the newer checkpoint prompt still runs', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  await eng.begin('person')
  await eng.end('person')
  await eng.checkpoint(RESUME.replace('1100', '1130'))
  expect(eng.queue.length).toBe(2)
  expect((await eng.deliver('old')).drop).toBeDefined()
  expect((await eng.deliver('new')).drop).toBeUndefined()
  expect(eng.model.length).toBe(1)
  expect(eng.model[0]).toContain('1130')
})

test('reusing the same checkpoint text keeps the queued submissions distinct', async () => {
  const eng = scheduler()
  await eng.reset()
  await eng.checkpoint()
  await eng.begin('person')
  await eng.end('person')
  await eng.checkpoint()
  expect(eng.queue.length).toBe(2)
  expect((await eng.deliver('old')).drop).toBeDefined()
  expect((await eng.deliver('new')).drop).toBeUndefined()
  expect(eng.model.length).toBe(1)
})
