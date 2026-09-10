/**
 * Drive the REAL Kanban dashboard bundle against a synthetic host.
 *
 * The bundle is a plain IIFE that registers `KanbanPage` on
 * `window.__HERMES_PLUGINS__`; everything it can observe -- React hooks, the
 * plugin SDK's transport, the WebSocket, the clock -- arrives through globals.
 * So the honest way to test *when it issues requests* is to supply those
 * globals and run the actual file, rather than assert on its source text.
 *
 * Two things make the result trustworthy:
 *
 *   - Hooks honour their dependency arrays. `useCallback`/`useMemo` return the
 *     cached value when deps are unchanged and `useEffect` re-runs (after its
 *     cleanup) only when they change. A harness that ignored deps would make
 *     every render look like a change and could not tell request churn from
 *     correct scheduling -- which is the entire subject here.
 *   - The clock is virtual. Timers, HTTP latency and the event stream are all
 *     scheduled on it, so "a response takes longer than the refresh debounce"
 *     is exact and the test takes milliseconds instead of a minute.
 *
 * usage: node driver.js <bundle.js> <scenario.json>   -> report JSON on stdout
 */
"use strict";

const fs = require("fs");

const bundlePath = process.argv[2];
const scenario = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

const cfg = Object.assign({
  board: "default",                 // slug the server reports as current
  boards: ["default", "beta"],
  boardLatencyMs: 450,              // a real board response on a real-size board
  boardsLatencyMs: 10,
  configLatencyMs: 10,
  eventPeriodMs: 0,                 // 0 = quiet board
  eventStartMs: 0,
  runMs: 12000,
  actions: [],                      // [{atMs, kind, ...}]
}, scenario);

// --- virtual clock ----------------------------------------------------------
let now = 0;
let timerSeq = 0;
const timers = new Map();

function vSetTimeout(fn, delay) {
  const id = ++timerSeq;
  timers.set(id, { at: now + Math.max(0, delay || 0), fn });
  return id;
}
function vClearTimeout(id) { timers.delete(id); }

const drainMicrotasks = () => new Promise((resolve) => setImmediate(resolve));

async function advanceTo(target) {
  for (;;) {
    await drainMicrotasks();
    flushRender();
    await drainMicrotasks();
    let next = null;
    for (const [id, t] of timers) {
      if (next === null || t.at < timers.get(next).at) next = id;
    }
    if (next === null || timers.get(next).at > target) break;
    const t = timers.get(next);
    timers.delete(next);
    now = t.at;
    try { t.fn(); } catch (err) { report.errors.push("timer: " + String(err && err.message || err)); }
  }
  now = Math.max(now, target);
  await drainMicrotasks();
  flushRender();
  await drainMicrotasks();
}

// --- report -----------------------------------------------------------------
const report = {
  boardRequests: [],      // every GET /board the bundle issued
  boardListRequests: [],  // every GET /boards
  applied: [],            // payloads the bundle actually put on screen
  sockets: [],
  renderStates: [],
  errors: [],
  maxInFlightBoardRequests: 0,
};

// --- the synthetic React ----------------------------------------------------
let cells = [];
let cellIndex = 0;
let pendingEffects = [];
let renderScheduled = false;
let rendering = false;
let lastTree = null;
let renderCount = 0;

function depsEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return false;
  return true;
}

function scheduleRender() { renderScheduled = true; }

const hooks = {
  useState(init) {
    const cell = cells[cellIndex] || (cells[cellIndex] = {
      value: typeof init === "function" ? init() : init,
    });
    const slot = cell;
    cellIndex++;
    return [slot.value, function (next) {
      const value = typeof next === "function" ? next(slot.value) : next;
      if (Object.is(value, slot.value)) return;   // React bails out on same value
      slot.value = value;
      scheduleRender();
    }];
  },
  useRef(init) {
    const cell = cells[cellIndex] || (cells[cellIndex] = { current: init });
    cellIndex++;
    return cell;
  },
  useCallback(fn, deps) {
    const cell = cells[cellIndex] || (cells[cellIndex] = {});
    cellIndex++;
    if (!("fn" in cell) || !depsEqual(cell.deps, deps)) { cell.fn = fn; cell.deps = deps; }
    return cell.fn;
  },
  useMemo(fn, deps) {
    const cell = cells[cellIndex] || (cells[cellIndex] = {});
    cellIndex++;
    if (!("value" in cell) || !depsEqual(cell.deps, deps)) { cell.value = fn(); cell.deps = deps; }
    return cell.value;
  },
  useEffect(fn, deps) {
    const cell = cells[cellIndex] || (cells[cellIndex] = { mounted: false });
    cellIndex++;
    if (!cell.mounted || deps === undefined || !depsEqual(cell.deps, deps)) {
      cell.deps = deps;
      cell.mounted = true;
      pendingEffects.push(cell);
    }
  },
  useContext() { return null; },
  createContext() { return {}; },
  useToast() { return { showToast() {}, toast: null }; },
  useConfirmDelete() { return { requestDelete() {}, confirm() {}, cancel() {}, isOpen: false }; },
};

const captured = {};

function element(type, props, ...children) {
  if (props && typeof props.onNudgeDispatch === "function") {
    captured.onNudgeDispatch = props.onNudgeDispatch;
  }
  if (props && typeof props.onSwitch === "function") captured.onSwitchBoard = props.onSwitch;
  if (props && typeof props.setIncludeArchived === "function") {
    captured.setIncludeArchived = props.setIncludeArchived;
  }
  if (props && typeof props.setTenantFilter === "function") {
    captured.setTenantFilter = props.setTenantFilter;
  }
  return { type, props: props || {}, children };
}

/** Which of the page's three top-level returns this render produced. */
function classify(tree) {
  if (tree === null || tree === undefined) return "empty";
  const first = tree.children && tree.children[0];
  if (tree.type === "div" && typeof first === "string" && first.indexOf("Loading") === 0) {
    return "loading";
  }
  if (typeof tree.type === "function" && /Card/.test(tree.type.name || "")) return "error";
  return "board";
}

function treeText(tree) {
  const seen = new Set();
  return JSON.stringify(tree, function (key, value) {
    if (typeof value === "function") return "fn:" + (value.name || "");
    if (typeof value === "object" && value !== null) {
      if (seen.has(value)) return "[circular]";
      seen.add(value);
    }
    return value;
  });
}

let unmounted = false;

function flushRender() {
  let guard = 0;
  if (unmounted) { renderScheduled = false; return; }
  while (renderScheduled && guard++ < 50) {
    renderScheduled = false;
    rendering = true;
    cellIndex = 0;
    pendingEffects = [];
    try {
      lastTree = captured.Page({});
    } catch (err) {
      report.errors.push("render: " + String((err && err.stack) || err));
      lastTree = null;
    }
    rendering = false;
    renderCount++;
    const state = classify(lastTree);
    const last = report.renderStates[report.renderStates.length - 1];
    if (!last || last.state !== state) report.renderStates.push({ t: now, state });
    for (const cell of pendingEffects) {
      if (typeof cell.cleanup === "function") {
        try { cell.cleanup(); } catch (err) { report.errors.push("cleanup: " + String(err)); }
        cell.cleanup = null;
      }
    }
    for (const cell of pendingEffects) {
      try {
        const ret = cell.run ? cell.run() : null;
        cell.cleanup = typeof ret === "function" ? ret : null;
      } catch (err) { report.errors.push("effect: " + String((err && err.stack) || err)); }
    }
  }
}

// `useEffect` records the function on the cell; flushRender runs it after commit.
const rawUseEffect = hooks.useEffect;
hooks.useEffect = function (fn, deps) {
  const before = cellIndex;
  rawUseEffect(fn, deps);
  const cell = cells[before];
  cell.run = fn;
};

// --- the synthetic host -----------------------------------------------------
let selectedBoardStorage = null;
let inFlightBoard = 0;
const sockets = [];
let boardVersion = 0;      // bumped by every synthetic event; the marker on /board
const responseHolds = [];  // [{board, latencyMs}] consumed in request order

let boardRequestSerial = 0;

function boardPayload(boardSlug, serial) {
  // The marker carries BOTH the board's version at answer time and the serial
  // of the request that asked. An older response overwriting a newer one is
  // then visible on screen as a serial going backwards, which is the only way
  // to tell "stale data was applied" from "the same data was applied twice".
  const marker = "MARKER-" + boardSlug + "-v" + boardVersion + "-r" + serial;
  return {
    columns: [{ name: "ready", tasks: [{ id: "t_marker", title: marker, status: "ready" }] }],
    tenants: [], assignees: [], latest_event_id: 1000 + boardVersion, now: 0,
    __marker: marker,
  };
}

function respond(value, latency) {
  return new Promise(function (resolve) { vSetTimeout(function () { resolve(value); }, latency); });
}

function fetchJSON(url, opts) {
  const path = String(url).split("?")[0];
  const qs = String(url).indexOf("?") >= 0 ? String(url).split("?")[1] : "";
  const params = new URLSearchParams(qs);
  if (/\/board$/.test(path)) {
    const asked = params.get("board");
    const serial = ++boardRequestSerial;
    const req = {
      t: now, serial: serial, board: asked, tenant: params.get("tenant"),
      includeArchived: params.get("include_archived") === "true",
    };
    report.boardRequests.push(req);
    inFlightBoard++;
    report.maxInFlightBoardRequests = Math.max(report.maxInFlightBoardRequests, inFlightBoard);
    let latency = cfg.boardLatencyMs;
    for (let i = 0; i < responseHolds.length; i++) {
      if (responseHolds[i].board === asked) {
        latency = responseHolds[i].latencyMs;
        responseHolds.splice(i, 1);
        req.held = true;
        break;
      }
    }
    const payload = boardPayload(asked || "(none)", serial);
    req.marker = payload.__marker;
    return respond(payload, latency).then(function (data) {
      inFlightBoard--;
      req.respondedAt = now;
      // Recorded from inside `then` so it reflects what the bundle received;
      // whether it APPLIED it is recorded by setBoardData below.
      return data;
    });
  }
  if (/\/boards$/.test(path)) {
    report.boardListRequests.push({ t: now });
    return respond({
      boards: cfg.boards.map(function (s) { return { slug: s }; }),
      current: cfg.board,
    }, cfg.boardsLatencyMs);
  }
  if (/\/config$/.test(path)) return respond({ render_markdown: true }, cfg.configLatencyMs);
  return respond({ ok: true }, 5);
}

global.window = {
  location: { origin: "http://127.0.0.1", pathname: "/kanban" },
  localStorage: {
    getItem: function () { return selectedBoardStorage; },
    setItem: function (_k, v) { selectedBoardStorage = v; },
    removeItem: function () { selectedBoardStorage = null; },
  },
  addEventListener() {}, removeEventListener() {},
  matchMedia: function () { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
  setTimeout: vSetTimeout, clearTimeout: vClearTimeout,
  setInterval: function () { return 0; }, clearInterval() {},
};
global.document = {
  addEventListener() {}, removeEventListener() {},
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
  body: { classList: { add() {}, remove() {} } },
};
global.setTimeout = vSetTimeout;
global.clearTimeout = vClearTimeout;

global.WebSocket = function (url) {
  const ws = this;
  ws.url = url;
  ws.readyState = 1;
  ws.onopen = null; ws.onmessage = null; ws.onclose = null;
  ws.closedByClient = false;
  ws.close = function () { ws.closedByClient = true; ws.readyState = 3; };
  const entry = { t: now, url: String(url), frames: 0 };
  report.sockets.push(entry);
  sockets.push(ws);
  vSetTimeout(function () {
    if (ws.closedByClient) return;
    if (ws.onopen) ws.onopen();
    const boardParam = new URLSearchParams(String(url).split("?")[1] || "").get("board");
    if (ws.onmessage) {
      entry.frames++;
      ws.onmessage({ data: JSON.stringify({
        type: "bootstrap", cursor: 1000 + boardVersion, board: boardParam || null }) });
    }
  }, 5);
};

const passthrough = function (tag) {
  const fn = function (p) { return element(tag, p); };
  Object.defineProperty(fn, "name", { value: String(tag) });
  return fn;
};

const SDK = {
  React: Object.assign({ createElement: element, Component: class {}, Fragment: "fragment" }, hooks),
  hooks: null,
  components: new Proxy({}, { get: function (_t, name) { return passthrough(String(name)); } }),
  utils: { cn: function () { return Array.prototype.filter.call(arguments, Boolean).join(" "); },
           timeAgo: function (t) { return String(t); },
           isoTimeAgo: function (t) { return String(t); } },
  fetchJSON: fetchJSON,
  authedFetch: function () { return Promise.resolve({ ok: true, json: function () { return Promise.resolve({}); } }); },
  buildWsUrl: function (base, params) {
    const qs = new URLSearchParams(params || {}).toString();
    return Promise.resolve("ws://synthetic" + base + (qs ? "?" + qs : ""));
  },
  buildWsAuthParam: function () { return Promise.resolve(["token", "x"]); },
  useI18n: function () { return { t: {} }; },
};
SDK.hooks = SDK.React;
global.window.__HERMES_PLUGIN_SDK__ = SDK;
global.window.__HERMES_PLUGINS__ = {
  register: function (_name, component) { captured.Page = component; },
  registerSlot() {},
};

// --- run the actual bundle --------------------------------------------------
new Function("window", "document", "WebSocket", "setTimeout", "clearTimeout",
             fs.readFileSync(bundlePath, "utf8"))(
  global.window, global.document, global.WebSocket, vSetTimeout, vClearTimeout);

if (!captured.Page) {
  console.log(JSON.stringify({ error: "bundle did not register KanbanPage" }));
  process.exit(2);
}

// Record what actually reaches the screen: the marker in the committed tree.
let lastAppliedMarker = null;

function observeApplied() {
  if (classify(lastTree) !== "board") return;
  const text = treeText(lastTree);
  const m = text.match(/MARKER-[a-z0-9()-]+-v\d+-r\d+/g);
  if (!m) return;
  const marker = m[m.length - 1];
  if (marker !== lastAppliedMarker) {
    lastAppliedMarker = marker;
    const parts = /MARKER-(.+)-v(\d+)-r(\d+)/.exec(marker);
    report.applied.push({ t: now, marker: marker, board: parts[1],
                          version: Number(parts[2]), serial: Number(parts[3]) });
  }
}

function emitEvent() {
  boardVersion++;
  for (const ws of sockets) {
    if (ws.closedByClient || ws.readyState !== 1 || !ws.onmessage) continue;
    const boardParam = new URLSearchParams(String(ws.url).split("?")[1] || "").get("board");
    ws.onmessage({ data: JSON.stringify({
      events: [{ id: 1000 + boardVersion, task_id: "t_marker", kind: "progress" }],
      cursor: 1000 + boardVersion, board: boardParam || null }) });
  }
}

async function main() {
  selectedBoardStorage = cfg.initialStoredBoard || null;
  scheduleRender();

  const actions = (cfg.actions || []).slice().sort(function (a, b) { return a.atMs - b.atMs; });
  let nextEventAt = cfg.eventPeriodMs ? Math.max(cfg.eventStartMs, cfg.eventPeriodMs) : Infinity;
  let step = 10;

  for (let target = 0; target <= cfg.runMs; target += step) {
    await advanceTo(target);
    observeApplied();
    while (actions.length && actions[0].atMs <= now) {
      const action = actions.shift();
      applyAction(action);
      scheduleRender();
    }
    while (nextEventAt <= now) {
      emitEvent();
      nextEventAt += cfg.eventPeriodMs;
    }
  }
  await advanceTo(cfg.runMs + 2000);
  observeApplied();

  report.finalState = classify(lastTree);
  report.finalMarker = lastAppliedMarker;
  report.renderCount = renderCount;
  report.renderedAtMs = (report.renderStates.find(function (s) { return s.state === "board"; }) || {}).t;
  report.boardVersion = boardVersion;
  console.log(JSON.stringify(report, null, 2));
}

function applyAction(action) {
  if (action.kind === "switchBoard") {
    // Exactly what the switcher does: the stored pin moves and the page is told.
    selectedBoardStorage = action.board;
    if (captured.onSwitchBoard) captured.onSwitchBoard(action.board);
    else report.errors.push("no switcher captured");
    return;
  }
  if (action.kind === "closeSocket") {
    for (const ws of sockets) {
      if (!ws.closedByClient && ws.readyState === 1 && ws.onclose) {
        ws.readyState = 3;
        ws.onclose({ code: 1006 });
      }
    }
    return;
  }
  if (action.kind === "holdNextBoardResponse") {
    responseHolds.push({ board: action.board === undefined ? null : action.board,
                         latencyMs: action.latencyMs });
    return;
  }
  if (action.kind === "emitEvent") { emitEvent(); return; }
  if (action.kind === "unmount") {
    // What React does when the tab is left: every effect cleanup runs, and the
    // component never renders again. Anything the page starts after this point
    // is work nobody asked for.
    unmounted = true;
    report.unmountedAtMs = now;
    for (const cell of cells) {
      if (cell && typeof cell.cleanup === "function") {
        try { cell.cleanup(); } catch (err) { report.errors.push("cleanup: " + String(err)); }
        cell.cleanup = null;
      }
    }
    return;
  }
  if (action.kind === "includeArchived") {
    if (captured.setIncludeArchived) captured.setIncludeArchived(!!action.value);
    else report.errors.push("no includeArchived setter captured");
    return;
  }
  if (action.kind === "tenantFilter") {
    if (captured.setTenantFilter) captured.setTenantFilter(action.value || "");
    else report.errors.push("no tenant setter captured");
    return;
  }
  if (action.kind === "staleCallback") {
    if (captured.onNudgeDispatch) captured.onNudgeDispatch();
    else report.errors.push("no stale callback captured");
    return;
  }
  report.errors.push("unknown action " + action.kind);
}

main().catch(function (err) {
  console.log(JSON.stringify({ error: String((err && err.stack) || err) }));
  process.exit(2);
});
