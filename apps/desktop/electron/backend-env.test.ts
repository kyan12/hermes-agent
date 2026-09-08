import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  buildDesktopBackendPath,
  hermesManagedNodePathEntries,
  normalizeHermesHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES,
  withoutInheritedWorkerIdentity
} from './backend-env'

test('desktop backend PATH adds Hermes-managed bins and missing POSIX sane entries', () => {
  const result = buildDesktopBackendPath({
    hermesHome: '/Users/test/.hermes',
    venvRoot: '/Users/test/.hermes/hermes-agent/venv',
    currentPath: '/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  const entries = result.split(':')
  // Both managed-Node layouts lead, POSIX-native shape first, then the venv.
  assert.deepEqual(entries.slice(0, 3), [
    '/Users/test/.hermes/node/bin',
    '/Users/test/.hermes/node',
    '/Users/test/.hermes/hermes-agent/venv/bin'
  ])
  assert.ok(entries.includes('/opt/homebrew/bin'), 'Apple Silicon Homebrew bin is added')
  assert.ok(entries.includes('/opt/homebrew/sbin'), 'Apple Silicon Homebrew sbin is added')
  assert.ok(entries.includes('/usr/local/sbin'), 'missing standard sbin is added')

  for (const expected of POSIX_SANE_PATH_ENTRIES) {
    assert.ok(entries.includes(expected), `${expected} should be present`)
  }
})

test('managed Node dirs lead with the platform-native layout but always offer both', () => {
  const posix = hermesManagedNodePathEntries('/Users/test/.hermes', {
    platform: 'darwin',
    pathModule: path.posix
  })

  const windows = hermesManagedNodePathEntries('C:\\Users\\test\\AppData\\Local\\hermes', {
    platform: 'win32',
    pathModule: path.win32
  })

  // install.sh uses node/bin; install.ps1 unpacks node.exe into node\ itself.
  // Both shapes are always emitted so migrated installs keep resolving.
  assert.deepEqual(posix, ['/Users/test/.hermes/node/bin', '/Users/test/.hermes/node'])
  assert.deepEqual(windows, [
    'C:\\Users\\test\\AppData\\Local\\hermes\\node',
    'C:\\Users\\test\\AppData\\Local\\hermes\\node\\bin'
  ])
})

test('managed Node dirs are empty without a Hermes home', () => {
  assert.deepEqual(hermesManagedNodePathEntries(undefined, { platform: 'darwin', pathModule: path.posix }), [])
  assert.deepEqual(hermesManagedNodePathEntries('', { platform: 'win32', pathModule: path.win32 }), [])
})

test('every managed Node dir outranks the inherited PATH on both platforms', () => {
  for (const [platform, pathModule, home, inherited, delimiter] of [
    ['darwin', path.posix, '/Users/test/.hermes', '/usr/local/bin:/usr/bin', ':'],
    ['win32', path.win32, 'C:\\hermes', 'C:\\Program Files\\nodejs;C:\\Windows\\System32', ';']
  ] as const) {
    const entries = buildDesktopBackendPath({
      hermesHome: home,
      venvRoot: null,
      currentPath: inherited,
      platform,
      pathModule
    }).split(delimiter)

    const managed = hermesManagedNodePathEntries(home, { platform, pathModule })
    const firstInherited = Math.min(...inherited.split(delimiter).map(entry => entries.indexOf(entry)))

    for (const dir of managed) {
      assert.ok(
        entries.indexOf(dir) >= 0 && entries.indexOf(dir) < firstInherited,
        `${dir} must precede the inherited PATH on ${platform}`
      )
    }
  }
})

test('desktop backend PATH preserves first occurrence and avoids duplicates', () => {
  const result = buildDesktopBackendPath({
    hermesHome: '/Users/test/.hermes',
    venvRoot: '/Users/test/.hermes/hermes-agent/venv',
    currentPath: '/opt/homebrew/bin:/usr/bin:/opt/homebrew/bin:/bin',
    platform: 'darwin',
    pathModule: path.posix
  })

  const entries = result.split(':')
  assert.equal(entries.filter(entry => entry === '/opt/homebrew/bin').length, 1)
  assert.ok(
    entries.indexOf('/opt/homebrew/bin') < entries.indexOf('/opt/homebrew/sbin'),
    'existing Homebrew bin keeps its precedence over appended missing sane entries'
  )
})

test('buildDesktopBackendEnv extends PYTHONPATH and backend PATH together', () => {
  const env = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    pythonPathEntries: ['/repo/hermes-agent'],
    venvRoot: '/Users/test/.hermes/hermes-agent/venv',
    currentEnv: {
      PATH: '/usr/bin:/bin',
      PYTHONPATH: '/existing/pythonpath'
    },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(env.PYTHONPATH, '/repo/hermes-agent:/existing/pythonpath')
  assert.ok(
    env.PATH.startsWith(
      '/Users/test/.hermes/node/bin:/Users/test/.hermes/node:/Users/test/.hermes/hermes-agent/venv/bin:'
    )
  )
  assert.ok(env.PATH.includes('/opt/homebrew/bin'))
})

test('buildDesktopBackendEnv forces PYTHONUTF8 unless the user set it explicitly', () => {
  const defaulted = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    currentEnv: { PATH: '/usr/bin' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(defaulted.PYTHONUTF8, '1')

  const optedOut = buildDesktopBackendEnv({
    hermesHome: '/Users/test/.hermes',
    currentEnv: { PATH: '/usr/bin', PYTHONUTF8: '0' },
    platform: 'darwin',
    pathModule: path.posix
  })

  assert.equal(optedOut.PYTHONUTF8, '0')
})

test('normalizeHermesHomeRoot maps profile homes back to the global Hermes root', () => {
  assert.equal(
    normalizeHermesHomeRoot('/Users/test/.hermes/profiles/oracle', { pathModule: path.posix }),
    '/Users/test/.hermes'
  )
  assert.equal(
    normalizeHermesHomeRoot('C:\\Users\\test\\AppData\\Local\\hermes\\profiles\\oracle', { pathModule: path.win32 }),
    'C:\\Users\\test\\AppData\\Local\\hermes'
  )
  assert.equal(normalizeHermesHomeRoot('/Users/test/.hermes', { pathModule: path.posix }), '/Users/test/.hermes')
})

test('Windows PATH casing and delimiter are preserved without POSIX sane entries', () => {
  const env = buildDesktopBackendEnv({
    hermesHome: 'C:\\Users\\test\\AppData\\Local\\hermes',
    pythonPathEntries: ['C:\\repo\\hermes-agent'],
    venvRoot: 'C:\\Users\\test\\AppData\\Local\\hermes\\hermes-agent\\venv',
    currentEnv: {
      Path: 'C:\\Windows\\System32;C:\\Windows',
      PYTHONPATH: 'C:\\existing\\pythonpath'
    },
    platform: 'win32',
    pathModule: path.win32
  })

  assert.equal(pathEnvKey({ Path: 'x' }, 'win32'), 'Path')
  assert.equal(env.PATH, undefined)
  // Windows leads with the portable layout (install.ps1 unpacks node.exe
  // straight into node\, no bin\), then the POSIX shape for migrated installs.
  assert.ok(
    env.Path.startsWith(
      'C:\\Users\\test\\AppData\\Local\\hermes\\node;C:\\Users\\test\\AppData\\Local\\hermes\\node\\bin;'
    )
  )
  assert.ok(env.Path.includes('\\venv\\Scripts;'))
  assert.ok(env.Path.includes(';C:\\Windows\\System32;C:\\Windows'))
  assert.equal(env.Path.includes('/opt/homebrew/bin'), false)
})

test('appendUniquePathEntries drops empty entries and keeps first occurrence', () => {
  assert.equal(appendUniquePathEntries([':/a::/b', ['/a', '/c']], { delimiter: ':' }), '/a:/b:/c')
})


// ── Inherited Kanban worker identity ────────────────────────────────────────
// The desktop spawns its backend with `...process.env`. When the app itself
// was launched from a Kanban worker's shell, that spread hands a finished
// worker's task + run to a human-facing backend, which then answers ordinary
// chat with the worker stop protocol. Scrub the CHILD env only — the parent
// Electron process is left alone.

const WORKER_ENV = Object.freeze({
  HERMES_KANBAN_TASK: 't_finished',
  HERMES_KANBAN_RUN_ID: '2495',
  HERMES_KANBAN_DB: '/home/u/.hermes/kanban.db',
  HERMES_KANBAN_BOARD: 'worker-board',
  HERMES_KANBAN_WORKSPACE: '/home/u/work/.worktrees/t_finished',
  HERMES_KANBAN_WORKSPACES_ROOT: '/home/u/work/.worktrees',
  HERMES_KANBAN_BRANCH: 'worker-branch',
  HERMES_KANBAN_CLAIM_LOCK: 'worker-lock',
  HERMES_KANBAN_GOAL_MODE: '1',
  HERMES_KANBAN_GOAL_MAX_TURNS: '20',
  HERMES_SESSION_SOURCE: 'kanban',
  HERMES_DELEGATED_CHILD_CONTEXT: '1'
})

test('backend child env drops every inherited Kanban worker variable', () => {
  const env = withoutInheritedWorkerIdentity({
    ...WORKER_ENV,
    TERMINAL_CWD: '/home/u/work/.worktrees/t_finished',
    HERMES_HOME: '/home/u/.hermes',
    PATH: '/usr/bin'
  })

  assert.deepEqual(Object.keys(env).filter(k => k.startsWith('HERMES_KANBAN_')), [])
  assert.equal(env.HERMES_SESSION_SOURCE, undefined)
  assert.equal(env.HERMES_DELEGATED_CHILD_CONTEXT, undefined)
  assert.equal(env.TERMINAL_CWD, undefined)
  // Profile / install intent is the user's, not the worker's.
  assert.equal(env.HERMES_HOME, '/home/u/.hermes')
  assert.equal(env.PATH, '/usr/bin')
})

test('backend child env leaves the parent process env untouched', () => {
  const source = { ...WORKER_ENV }
  withoutInheritedWorkerIdentity(source)
  assert.equal(source.HERMES_KANBAN_TASK, 't_finished')
})

test('backend child env keeps a standalone board selection', () => {
  const env = withoutInheritedWorkerIdentity({
    HERMES_KANBAN_DB: '/home/u/.hermes/kanban.db',
    HERMES_KANBAN_BOARD: 'ops',
    TERMINAL_CWD: '/home/u/projects/site',
    HERMES_SESSION_SOURCE: 'desktop'
  })

  assert.equal(env.HERMES_KANBAN_DB, '/home/u/.hermes/kanban.db')
  assert.equal(env.HERMES_KANBAN_BOARD, 'ops')
  assert.equal(env.TERMINAL_CWD, '/home/u/projects/site')
  assert.equal(env.HERMES_SESSION_SOURCE, 'desktop')
})

test('backend child env keeps a cwd the user chose, not the worker workspace', () => {
  const env = withoutInheritedWorkerIdentity({
    ...WORKER_ENV,
    TERMINAL_CWD: '/home/u/projects/site'
  })

  assert.equal(env.TERMINAL_CWD, '/home/u/projects/site')
  assert.equal(env.HERMES_KANBAN_WORKSPACE, undefined)
})

test('backend child env drops a delegate_task lineage marker on its own', () => {
  // scrub_kanban_env() strips HERMES_KANBAN_* but stamps the marker, so it
  // arrives with no task var beside it.
  const env = withoutInheritedWorkerIdentity({
    HERMES_DELEGATED_CHILD_CONTEXT: '1',
    HERMES_HOME: '/home/u/.hermes'
  })

  assert.equal(env.HERMES_DELEGATED_CHILD_CONTEXT, undefined)
  assert.equal(env.HERMES_HOME, '/home/u/.hermes')
})

test('backend child env scrub is idempotent across desktop restarts', () => {
  const once = withoutInheritedWorkerIdentity({ ...WORKER_ENV, HERMES_HOME: '/home/u/.hermes' })
  const twice = withoutInheritedWorkerIdentity(once)

  assert.deepEqual(twice, once)
})
