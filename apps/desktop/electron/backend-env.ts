import path from 'node:path'

// Match the POSIX fallback surface used by the Python terminal environment.
// macOS apps launched from Finder/Dock often inherit only /usr/bin:/bin:/usr/sbin:/sbin,
// which misses Apple Silicon Homebrew and user-installed CLI tools such as codex.
const POSIX_SANE_PATH_ENTRIES = Object.freeze([
  '/opt/homebrew/bin',
  '/opt/homebrew/sbin',
  '/usr/local/sbin',
  '/usr/local/bin',
  '/usr/sbin',
  '/usr/bin',
  '/sbin',
  '/bin'
])

function delimiterForPlatform(platform = process.platform) {
  return platform === 'win32' ? ';' : ':'
}

function pathModuleForPlatform(platform = process.platform) {
  return platform === 'win32' ? path.win32 : path.posix
}

function pathEnvKey(env = process.env, platform = process.platform) {
  if (platform !== 'win32') {
    return 'PATH'
  }

  return Object.keys(env || {}).find(key => key.toUpperCase() === 'PATH') || 'PATH'
}

function currentPathValue(env = process.env, platform = process.platform) {
  const key = pathEnvKey(env, platform)

  return env?.[key] || ''
}

function appendUniquePathEntries(entries, { delimiter = path.delimiter } = {}) {
  const seen = new Set()
  const ordered = []

  for (const entry of entries) {
    if (!entry) {
      continue
    }

    const parts = Array.isArray(entry) ? entry : String(entry).split(delimiter)

    for (const part of parts) {
      if (!part || seen.has(part)) {
        continue
      }

      seen.add(part)
      ordered.push(part)
    }
  }

  return ordered.join(delimiter)
}

/**
 * Hermes-managed Node.js directories, in preferred lookup order.
 *
 * There are two on-disk layouts. `scripts/install.ps1` unpacks portable Node
 * straight into `%LOCALAPPDATA%\hermes\node` (node.exe at the root, no `bin\`);
 * `scripts/install.sh` and the node-bootstrap helper use the POSIX
 * `$HERMES_HOME/node/bin`. Emit BOTH on every platform so mixed and migrated
 * installs resolve, leading with the layout native to the current platform.
 *
 * This is the single source of truth for the ordering rule on the Node side —
 * `main.ts` imports it rather than keeping its own copy. Mirrors
 * `iter_hermes_node_dirs()` in hermes_constants.py, which the Electron main
 * process cannot import.
 */
function hermesManagedNodePathEntries(
  hermesHome,
  { platform = process.platform, pathModule = pathModuleForPlatform(platform) }: any = {}
) {
  if (!hermesHome) {
    return []
  }

  const root = pathModule.join(hermesHome, 'node')
  const bin = pathModule.join(root, 'bin')

  return platform === 'win32' ? [root, bin] : [bin, root]
}

function buildDesktopBackendPath({
  hermesHome,
  venvRoot,
  currentPath = '',
  platform = process.platform,
  pathModule = pathModuleForPlatform(platform)
}: any = {}) {
  const delimiter = delimiterForPlatform(platform)
  const hermesNodeDirs = hermesManagedNodePathEntries(hermesHome, { platform, pathModule })
  const venvBin = venvRoot ? pathModule.join(venvRoot, platform === 'win32' ? 'Scripts' : 'bin') : null
  const saneEntries = platform === 'win32' ? [] : POSIX_SANE_PATH_ENTRIES

  return appendUniquePathEntries([hermesNodeDirs, venvBin, currentPath, saneEntries], { delimiter })
}

function normalizeHermesHomeRoot(hermesHome, { pathModule = pathModuleForPlatform(process.platform) }: any = {}) {
  if (!hermesHome) {
    return hermesHome
  }

  const resolved = pathModule.resolve(String(hermesHome))
  const parent = pathModule.dirname(resolved)

  if (pathModule.basename(parent).toLowerCase() === 'profiles') {
    return pathModule.dirname(parent)
  }

  return resolved
}

function buildDesktopBackendEnv({
  hermesHome,
  pythonPathEntries = [],
  venvRoot,
  currentEnv = process.env,
  platform = process.platform,
  pathModule = pathModuleForPlatform(platform)
}: any = {}) {
  const delimiter = delimiterForPlatform(platform)
  const currentPythonPath = currentEnv?.PYTHONPATH || ''
  const key = pathEnvKey(currentEnv, platform)

  return {
    PYTHONPATH: appendUniquePathEntries([...pythonPathEntries, currentPythonPath], { delimiter }),
    // Force PEP 540 UTF-8 mode in the spawned Python backend so its stdio and
    // subprocess defaults are UTF-8 even on non-UTF-8 Windows locales (GBK,
    // cp1252, ...). hermes_bootstrap sets this inside the child too, but only
    // after import — anything emitted earlier (interpreter startup errors,
    // pre-bootstrap tracebacks) still decodes with the locale default without
    // this. User's explicit setting wins. Re-port of PR #56499 (echoriver89).
    PYTHONUTF8: currentEnv?.PYTHONUTF8 ?? '1',
    [key]: buildDesktopBackendPath({
      hermesHome,
      venvRoot,
      currentPath: currentPathValue(currentEnv, platform),
      platform,
      pathModule
    })
  }
}

/**
 * Kanban worker identity the dispatcher injects into a worker's environment.
 * Mirrors KANBAN_ENV_KEYS in agent/delegation_context.py plus the
 * ``HERMES_KANBAN_`` prefix rule in tui_gateway/interactive_env.py; the
 * Electron main process cannot import either.
 */
const DELEGATED_CHILD_ENV_MARKER = 'HERMES_DELEGATED_CHILD_CONTEXT'
const KANBAN_ENV_PREFIX = 'HERMES_KANBAN_'

/**
 * Return a copy of `env` with any inherited Kanban worker identity removed.
 *
 * The desktop spawns its backend with `...process.env`. When the app itself was
 * launched from a worker's shell — a worker that ran `hermes desktop`, or a
 * user who opened the app from that terminal — the spread hands a finished
 * worker's task and run to a human-facing backend, which then answers ordinary
 * chat with the worker stop protocol and creates cards as that dead worker.
 *
 * Only the CHILD env is scrubbed: `process.env` in the Electron parent is left
 * exactly as it was, so nothing about the launching worker's own lifecycle
 * changes. The Python entrypoints scrub again on their side
 * (tui_gateway/interactive_env.py) — this is the same rule applied one process
 * earlier, so stdio MCP servers and terminal sessions started by the backend
 * never see the stale identity either.
 *
 * Deliberately preserved:
 *   - HERMES_HOME / profile pins — the desktop's own explicit choice.
 *   - TERMINAL_CWD, unless it is precisely the worker's workspace.
 *   - A standalone board selection (HERMES_KANBAN_DB / _BOARD with no task):
 *     an operator pinning a board is not an inherited worker.
 */
function withoutInheritedWorkerIdentity(env: any = process.env) {
  const next = { ...(env || {}) }

  // The delegate_task lineage marker travels WITHOUT the task vars —
  // scrub_kanban_env() strips those and stamps this instead — so it has to be
  // dropped on its own, or the backend spends its life failing board writes
  // closed as somebody else's child.
  delete next[DELEGATED_CHILD_ENV_MARKER]

  if (!next[`${KANBAN_ENV_PREFIX}TASK`]) {
    return next
  }

  const workspace = next[`${KANBAN_ENV_PREFIX}WORKSPACE`]

  for (const key of Object.keys(next)) {
    if (key.startsWith(KANBAN_ENV_PREFIX)) {
      delete next[key]
    }
  }

  if (next.HERMES_SESSION_SOURCE === 'kanban') {
    delete next.HERMES_SESSION_SOURCE
  }

  if (workspace && next.TERMINAL_CWD === workspace) {
    delete next.TERMINAL_CWD
  }

  return next
}

export {
  appendUniquePathEntries,
  buildDesktopBackendEnv,
  buildDesktopBackendPath,
  delimiterForPlatform,
  hermesManagedNodePathEntries,
  normalizeHermesHomeRoot,
  pathEnvKey,
  POSIX_SANE_PATH_ENTRIES,
  withoutInheritedWorkerIdentity
}
