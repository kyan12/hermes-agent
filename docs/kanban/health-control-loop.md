# Kanban health control loop

Operations guide for the native controller that keeps a board's lifecycle
self-maintaining, the deterministic sentinel that verifies it, and the update
guard that stops a Hermes upgrade from silently removing either.

Implementation: `hermes_cli/kanban_health.py`,
`hermes_cli/kanban_sentinel.py`, `hermes_cli/kanban_capabilities.py`.

---

## Why this exists

A board can look perfectly healthy while being completely stuck. The four
failure shapes this loop is built around, all observed in practice:

1. **A crashed worker's block renders as a human gate.** `blocked` was one
   undifferentiated bucket, so a stack trace and a contract awaiting signature
   were displayed identically. The human column filled with machine failures
   until nobody read it.
2. **A scheduled card waits forever.** `scheduled` had no wake mechanism at
   all — nothing in the codebase ever moved a card out of it. A card parked
   "until Tuesday" was parked permanently, and nothing reported that.
3. **Dispatcher-stuck warnings blame credentials for capacity.** The health
   probe asked one question — "is the ready queue non-empty?" — so a capacity
   wait, an active-PR guard and a control-plane lane all produced the same
   "check venv, PATH, credentials" warning, which then never cleared.
4. **An update removes the fix.** `hermes update` resolved its branch to a
   hardcoded `main`. An install tracking a maintained lineage was
   fast-forwarded onto a tree without its lifecycle capability — and the
   update reported success, because "the tree imports" was the only check.

---

## Ownership

Three layers, deliberately different in kind. Each must be able to fail
without the layer above it failing the same way.

| Layer | Owns | Runs |
| --- | --- | --- |
| **Native controller** (`kanban_health`) | Routine recovery: resuming due typed holds, classifying blocks, auditing forward paths | Every dispatcher tick, in-process |
| **Sentinel** (`kanban_sentinel`) | Verification and paging when the controller is down. One narrowly-proven reversible repair class | Operator-scheduled, on demand |
| **Unblocker bot** | Optional operator/debug UI over the same payloads | Optional, never authoritative |

The controller is the only routine actor. The sentinel is a *verifier*: it is
model-free and shares no code path with the provider stack, so it fails
differently than what it watches. It stands down entirely while the
controller's checkpoint is fresh — two writers racing the same holds is a
defect, not redundancy. The Unblocker bot is a view; nothing depends on it.

---

## Invariants

**A card in `blocked` is visible to a human only when it is an affirmed,
typed, atomic gate.** That means a `block_kind` of `needs_input` or
`capability`, *plus* a `gate_evidence` record that parses, carries a
recognised evidence type, names one atomic action, and names the principal who
affirmed it. Everything else — untyped blocks, `transient`, and typed-but-
unaffirmed claims — routes to automation recovery and never reaches the human
column. An affirmed gate stays visible; suppressing it is the opposite failure
and is equally a bug.

**Every nonterminal card has exactly one machine-verifiable forward path:**
a live worker, an eligible ready slot, an accepted dependency chain, a typed
scheduled hold with a healthy durable wake, or an affirmed human gate. Cards
with none are listed under `no_forward_path` and make the board unhealthy.

**A typed hold resumes exactly once.** The resume clears `hold_kind` and moves
the card out of `scheduled`, so re-running reconciliation is a no-op.

**A legacy prose-only hold is never silently healthy and never bulk-resumed.**
It reports `legacy_untyped` with `needs_classification`, forever, until a human
classifies it. Guessing a classification is how a card silently never runs.

---

## Hold kinds

| Kind | Meaning | Controller behaviour |
| --- | --- | --- |
| `dependency` | Waiting on parent cards | Resumes when all parents are done/archived |
| `wake` | Waiting on the clock | Resumes when `hold_wake_at` is due |
| `external` | Waiting on a third party | **Parked.** Healthy, never auto-resumed |
| `physical` | Waiting on the physical world | **Parked.** Healthy, never auto-resumed |
| `roadmap` | Deliberately deferred | **Parked.** Healthy, never auto-resumed |
| *(none)* | Legacy prose-only | **Diagnosed** `legacy_untyped`, never resumed |

Classify a hold when parking it:

```bash
hermes kanban schedule t_abc123 "vendor replies by the 14th" \
    --kind external
hermes kanban schedule t_def456 "retry after the maintenance window" \
    --kind wake --wake-at +7200
```

`--kind wake` requires `--wake-at` (an epoch second, or `+N` seconds from
now). An unarmed wake hold has no forward path, so the CLI refuses it up front
rather than reporting `wake_missing` about it forever afterwards.

---

## Wake health

A `wake` hold is only credible while something is demonstrably running the
reconciler. The controller stamps a durable checkpoint into
`board_health_checkpoints` on every pass; observers read it.

| Reason code | Meaning | Fix |
| --- | --- | --- |
| `wake_armed` | Future wake, controller healthy | — |
| `wake_due` | Due now; resumes this pass | — |
| `wake_missing` | `hold_kind=wake` with no `hold_wake_at` | Re-park with `--wake-at` |
| `wake_disabled` | `kanban.health_reconcile: false` | Re-enable, or re-classify the hold |
| `wake_failed` | Checkpoint absent, stale (>15 min) or failed | Check the dispatcher is running |

The controller exempts *itself* from the staleness check — inside a
reconciliation pass the answer to "is anything running the reconciler?" is
trivially yes. Without that exemption the first-ever pass would find no
checkpoint, declare every wake hold unwakeable, and never stamp the checkpoint
that would have fixed it.

---

## Ready-queue reason codes

`dispatcher stuck` now fires only for cards the dispatcher itself would have
spawned this tick: assigned to a real profile, workspace resolvable, below
both the global and per-profile caps, and past every respawn guard. Everything
else gets its own code and is reported as correctly waiting.

`spawnable` · `unassigned` · `control_plane_lane` · `capacity_global` ·
`capacity_per_profile` · `invalid_workspace` · `guard_active_pr` ·
`guard_recent_success` · `guard_blocker_auth` · `guard_rate_limit_cooldown`

```bash
hermes kanban board-health --ready-queue
```

---

## Commands

```bash
# Lifecycle health for the current board (exit 1 when unhealthy)
hermes kanban board-health
hermes kanban board-health --all-boards --json
hermes kanban board-health --reconcile        # reconcile, then report
hermes kanban board-health --ready-queue      # + per-card spawn reason codes

# Scheduled holds and the durable wake subsystem
hermes kanban scheduled-wake
hermes kanban scheduled-wake --reconcile --all-boards

# One deterministic sentinel sweep over every board
hermes kanban sentinel --dry-run
hermes kanban sentinel --json
```

HTTP (same payloads, same functions — the CLI, dashboard and sentinel cannot
drift into disagreeing):

```
GET /api/plugins/kanban/board-health
      ?board=<slug>&all_boards=<bool>&ready_queue=<bool>
POST /api/plugins/kanban/board-health/reconcile
      {"board": "<slug>", "all_boards": false, "ready_queue": false}
```

`GET /board` is deliberately unchanged: it is on the hot path (refetched on
every task WebSocket event) and cached against a cheap version probe. Health
is comparatively expensive and requested on demand, so folding it in would
either poison that cache or slow the board render.

---

## Running the sentinel

The sentinel ships **inactive**. Scheduling it is an operator decision.

It is safe to run by hand at any time — `--dry-run` writes nothing. Before
scheduling it, confirm the behaviour you expect:

```bash
hermes kanban sentinel --dry-run --json | jq '.ok, .boards, .would_repair'
```

To activate, schedule `hermes kanban sentinel` on the cadence you want alerts
at (hourly is a reasonable default; it is cheap and does nothing on a healthy
board). Route its stdout wherever you read pages. It exits non-zero when a
sweep needs attention.

What it will and will not do:

- **Stands down completely on drift.** If `kanban_health`'s control-loop
  version, board-health schema version, or reason-code vocabulary is not the
  one the sentinel was written against, it makes no repairs and pages instead.
  A verifier that keeps approving a subsystem it no longer understands is
  worse than no verifier. After an intentional controller change, re-verify
  the sentinel and bump `EXPECTED_CONTROL_LOOP_VERSION`.
- **Never races the controller.** Read-only while the controller checkpoint is
  fresh.
- **One repair class only.** Resuming a typed hold the controller would itself
  have resumed. Reversible (the card returns to its normal queue), idempotent
  (same guard), and it never touches a legacy untyped hold or a human gate.
- **Does not stamp the controller's checkpoint** when it steps in — that would
  erase the evidence that the controller is down. It stamps its own.
- **One atomic action per sweep.** Not one per card. Repeated identical alerts
  are deduped for 6 hours via `<kanban home>/sentinel-state.json`; the sweep
  still reports true board state every time, suppression only governs whether
  it is a *new* page.

---

## Update capability guard

Two guards, both required.

**1. Maintained-branch strategy.** `update.branch` in `config.yaml` (a
behavioural setting, so config and not an env var):

```yaml
update:
  branch: main        # or your maintained lineage
```

Resolution order: explicit `--branch` → `update.branch` → `main`.

**2. Pre-activation capability canary.** After the code swap and dependency
install, and before the fleet restart, `hermes update` probes the updated tree
**out-of-process** for every lifecycle capability declared in
`hermes_cli/kanban_capabilities.py`. Missing or broken capabilities fail the
update with exit 1 and a `capability_canary` step in the update receipt.

The probes are behavioural — each calls the real code path, none reads source
text — so a rename or refactor that preserves behaviour keeps passing, while a
stub that preserves the name but not the behaviour fails. A capability that is
present but returns the wrong answer counts as missing: wired-in-but-broken is
the more dangerous case, because its name still appears everywhere that looks
for it.

The required list is read from the **running** install, never from the staged
tree. Reading it from the staged tree would make the check vacuous — a tree
that dropped a capability would also have dropped it from its own manifest and
would cheerfully certify itself.

Desktop, CLI and gateway share one release lineage: the updater's existing
fleet version matrix already fails an update that leaves a provably-stale
gateway, and the canary now applies the same rule to capability.

Inspect the manifest:

```bash
python -c "import json,hermes_cli.kanban_capabilities as k; print(json.dumps(k.capability_manifest(), indent=2))"
```

---

## Configuration

```yaml
kanban:
  health_reconcile: true    # native control loop on the dispatcher tick
update:
  branch: main              # maintained lineage the updater pulls
```

Turning `health_reconcile` off does not make parked cards healthy. It makes
every `wake` hold report `wake_disabled`, because with the controller off
nothing will ever wake them.
