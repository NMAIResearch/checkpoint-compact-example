// Tests for the plugin's settings, run with `claude plugin test`. Each setting is an environment variable the plugin
// reads whenever it runs the checker; every call the plugin makes is answered by a stub. What matters: with none set,
// the checker runs as python3 from the PATH on checker/checkpoint.py in the plugin's own folder, with no --root;
// CHECKPOINT_COMPACT_PYTHON, CHECKPOINT_COMPACT_CHECKER and CHECKPOINT_COMPACT_ROOT each set their own part, alone or
// together, and the turn-end check uses them as the call's check did; an empty value counts as unset; a checker path that
// is not absolute, or a plugin folder that is not known, refuses the call before anything runs.
import { expect, test } from 'claude-code/testing'
import { compactAt } from '../hooks/register.js'

const SID = 'session-under-test'
const RESUME = '/home/user/.local/state/checkpoint-compact/lead/RESUME_2000-01-01_1100.md'
const DIGEST = 'a'.repeat(64)
const TOOL = 'mcp__checkpoint-compact__checkpoint_compact'
// This file is in the plugin's tests/ folder, so the plugin's own folder is one level up.
const PLUGIN_DIR = decodeURIComponent(new URL('..', import.meta.url).pathname).replace(/\/+$/, '')
const TAIL = ['check', '--resume', RESUME, '--sha256', DIGEST, '--session', SID]

function stubs(on: any, env: Record<string, string> = {}): string[][] {
  const runs: string[][] = []
  on('tool.register', () => ({ value: undefined }))
  on('session.id', () => ({ value: SID }))
  on('env.get', ($: any, e: any) => ({ value: env[e.name] }))
  on('process.run', ($: any, e: any) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [], resume: RESUME, sha256: DIGEST }), stderr: '' } }
  })
  on('session.compact', () => ({ messages: [{ role: 'user', text: 'summary', toolUses: [] }] }))
  on('prompt.submit', ($: any, e: any) => ({ text: e.text }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: '/work' }))
  on('turn.complete', () => ({ text: '' }))
  return runs
}

const ask = async ($: any) => {
  await $.session.start({ cwd: '/work' })
  return $.tool.call({ tool: TOOL, resume_path: RESUME, resume_sha256: DIGEST, reason: 'task finished' })
}

const turnEnd = ($: any) => $.turn.complete({ turnId: 't1', answer: '', durationMs: 1, isAborted: false, usage: null })

test("with no setting, python3 runs checker/checkpoint.py from the plugin's own folder, with no --root", async ($, on) => {
  const runs = stubs(on)
  const r = await ask($)
  expect(String(r.result)).toContain('accepted')
  expect(runs).toEqual([['python3', '-I', '-B', PLUGIN_DIR + '/checker/checkpoint.py', ...TAIL]])
})

test('CHECKPOINT_COMPACT_PYTHON alone names the Python', async ($, on) => {
  const runs = stubs(on, { CHECKPOINT_COMPACT_PYTHON: '/opt/python/bin/python3' })
  await ask($)
  expect(runs).toEqual([['/opt/python/bin/python3', '-I', '-B', PLUGIN_DIR + '/checker/checkpoint.py', ...TAIL]])
})

test('CHECKPOINT_COMPACT_CHECKER alone names the checker', async ($, on) => {
  const runs = stubs(on, { CHECKPOINT_COMPACT_CHECKER: '/opt/checkpoint/checkpoint.py' })
  await ask($)
  expect(runs).toEqual([['python3', '-I', '-B', '/opt/checkpoint/checkpoint.py', ...TAIL]])
})

test('CHECKPOINT_COMPACT_ROOT alone is passed to the checker as --root', async ($, on) => {
  const runs = stubs(on, { CHECKPOINT_COMPACT_ROOT: '/srv/records' })
  await ask($)
  expect(runs).toEqual([['python3', '-I', '-B', PLUGIN_DIR + '/checker/checkpoint.py', ...TAIL, '--root', '/srv/records']])
})

test('all three settings together, and the turn-end check uses them too', async ($, on) => {
  const runs = stubs(on, {
    CHECKPOINT_COMPACT_PYTHON: '/opt/python/bin/python3',
    CHECKPOINT_COMPACT_CHECKER: '/opt/checkpoint/checkpoint.py',
    CHECKPOINT_COMPACT_ROOT: '/srv/records',
  })
  const r = await ask($)
  expect(String(r.result)).toContain('accepted')
  await turnEnd($)
  const argv = ['/opt/python/bin/python3', '-I', '-B', '/opt/checkpoint/checkpoint.py', ...TAIL, '--root', '/srv/records']
  expect(runs).toEqual([argv, argv])
})

test('an empty setting counts as unset', async ($, on) => {
  const runs = stubs(on, { CHECKPOINT_COMPACT_PYTHON: '', CHECKPOINT_COMPACT_CHECKER: '', CHECKPOINT_COMPACT_ROOT: '' })
  await ask($)
  expect(runs).toEqual([['python3', '-I', '-B', PLUGIN_DIR + '/checker/checkpoint.py', ...TAIL]])
})

test('a checker setting that is not an absolute path refuses the call, and nothing runs', async ($, on) => {
  const runs = stubs(on, { CHECKPOINT_COMPACT_CHECKER: 'checker/checkpoint.py' })
  const r = await ask($)
  expect(String(r.result)).toContain('refused')
  expect(String(r.result)).toContain('CHECKPOINT_COMPACT_CHECKER is not an absolute path')
  expect(runs.length).toBe(0)
})

// A fake $ for compactAt, whose plugin folder is what the test gives; the checker accepts every checkpoint.
function fakeHost(plugin: any) {
  let runs = 0
  const fake: any = {
    plugin,
    session: {
      id: async () => SID,
      compact: async () => ({ messages: [{ role: 'user', text: 'summary', toolUses: [] }] }),
      append: async (p: any) => ({ message: p.message, uuid: 'u1' }),
    },
    env: { get: async (_name: string) => undefined },
    process: {
      run: async () => {
        runs += 1
        return { exitCode: 0, stdout: JSON.stringify({ ok: true, reasons: [] }), stderr: '' }
      },
    },
    prompt: { submit: (p: any) => Promise.resolve({ text: p.text }) },
    ui: { log: (_text: string) => Promise.resolve(undefined) },
  }
  return { fake, runs: () => runs }
}

const request = () => ({ session: SID, resume: RESUME, sha256: DIGEST, keep: '', generation: 1 })

test("a plugin folder that is not known refuses the compaction before the checker runs", async () => {
  for (const plugin of [{ name: 'checkpoint-compact' }, { name: 'checkpoint-compact', root: 'plugins/checkpoint-compact' }]) {
    const host = fakeHost(plugin)
    const said = await compactAt(host.fake, request())
    expect(said).toContain("not compacted: the checker could not be run: this plugin's folder is not known")
    expect(host.runs()).toBe(0)
  }
})

test("a known plugin folder lets the compaction go ahead", async () => {
  const host = fakeHost({ name: 'checkpoint-compact', root: '/home/user/plugins/checkpoint-compact' })
  const said = await compactAt(host.fake, request())
  expect(said).toContain('compacted at ' + RESUME)
  expect(host.runs()).toBe(1)
})
