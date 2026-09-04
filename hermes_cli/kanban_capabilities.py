"""Kanban lifecycle capability manifest and pre-activation canary.

The failure this exists to stop
------------------------------
``hermes update`` resolves its branch to a hardcoded ``main`` unless
``--branch`` is passed. An install whose lifecycle capability lives on a
maintained branch therefore gets fast-forwarded onto an upstream tree that
never carried it — and the update reports success, because "the tree
imports" was the only post-swap check. The board then looks fine while its
control loop is simply gone: scheduled cards stop waking, machine failures
start rendering as human gates, and nothing anywhere says so.

Two guards, both required
-------------------------
1. **A maintained-branch strategy.** ``update.branch`` in ``config.yaml``
   (read by ``_resolve_update_branch``) so the updater pulls the lineage
   the install actually runs.
2. **A pre-activation canary.** Before a staged tree is activated, probe it
   in a subprocess for every capability the *running* install requires. A
   capability that disappeared fails the update loudly rather than silently.

Who owns the probe, and why it matters
--------------------------------------
Both the requirement list AND the probe *logic* belong to the running
install. Two distinct self-certification holes are closed by that:

* Reading the requirement list out of the staged tree would make the check
  vacuous — a tree that dropped a capability would also have dropped it from
  its own manifest and cheerfully certify itself.
* Executing the staged tree's *own* ``probe_capabilities()`` is the same
  hole one level down. A candidate shipping no-op probes (or probes that
  only check ``callable(...)``) passes while the behaviour is gone.

So :func:`preactivation_canary` loads THIS module by absolute path inside a
subprocess whose ``hermes_cli`` / ``tools`` resolve to the candidate tree,
and runs the probes defined here against the candidate's implementations.
The candidate supplies the code under test; it never supplies the test.

Probes are behavioural: each one drives a real transition against a private
temporary board and asserts the real outcome, including the fail-closed
outcomes. None of them read source text and none assert that a symbol is
merely callable, so a rename or a refactor that preserves behaviour keeps
passing while a stub that preserves the name fails.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 2

# How long the staged-tree probe may take before we treat it as failed. The
# probes drive real SQLite transitions now, so this is wall-clock work, not a
# handful of attribute lookups.
CANARY_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class Capability:
    """One lifecycle capability the board's health depends on.

    ``probe`` receives an open connection to a throwaway board (see
    :func:`_isolated_board`) and returns True only when the real behaviour is
    observed end to end.
    """

    name: str
    description: str
    probe: Callable[..., bool]


# ---------------------------------------------------------------------------
# Isolated probe state
# ---------------------------------------------------------------------------

# Every ambient binding that could make a probe read or write the operator's
# real board instead of the throwaway one.
_ISOLATED_ENV_KEYS = (
    "HERMES_HOME",
    "HOME",
    "USERPROFILE",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_OPERATOR",
    "HERMES_PROFILE",
    "HERMES_TENANT",
    "HERMES_SESSION_ID",
    "HERMES_DELEGATED_CHILD_CONTEXT",
)


@contextlib.contextmanager
def _isolated_board():
    """Yield a connection to a private, temporary board.

    The probes exercise real writes — blocks, holds, reconciliation, child
    creation. They must never be able to touch the operator's board, and they
    must not inherit a worker's ambient scope, so the whole Hermes home is
    redirected into a temp directory for the duration and restored after.
    """
    saved = {key: os.environ.get(key) for key in _ISOLATED_ENV_KEYS}
    tmp = Path(tempfile.mkdtemp(prefix="hermes-capability-probe-"))
    conn = None
    kb = None
    try:
        home = tmp / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        for key in _ISOLATED_ENV_KEYS:
            os.environ.pop(key, None)
        # ``Path.home()`` honours HOME on POSIX and USERPROFILE on Windows;
        # kanban_db falls back to it whenever HERMES_HOME is not decisive.
        os.environ["HOME"] = str(tmp)
        os.environ["USERPROFILE"] = str(tmp)
        os.environ["HERMES_HOME"] = str(home)

        from hermes_cli import kanban_db as kb  # noqa: F811
        from hermes_cli import kanban_health as kh

        try:
            kb._INITIALIZED_PATHS.clear()
        except Exception:
            pass
        # The principal allowlist is memoised per process and keyed on
        # nothing; the temp home has a different config, so drop it.
        for cached in (getattr(kh, "human_gate_principals", None),):
            with contextlib.suppress(Exception):
                cached.cache_clear()

        kb.init_db()
        conn = kb.connect()
        yield conn
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        if kb is not None:
            with contextlib.suppress(Exception):
                kb._INITIALIZED_PATHS.clear()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        with contextlib.suppress(Exception):
            from hermes_cli import kanban_health as kh  # noqa: F811

            kh.human_gate_principals.cache_clear()
        shutil.rmtree(tmp, ignore_errors=True)


def _evidence(action="Sign the named contract", **overrides):
    payload = {
        "type": "human_decision",
        "action": action,
        "affirmed_by": "kevin",
        "affirmed_at": int(time.time()),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Probes — each drives the real code path against the temporary board
# ---------------------------------------------------------------------------


def _probe_typed_block_projection(conn) -> bool:
    """CLI, tool and dashboard serializers must all project block authority."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh
    from hermes_cli import kanban as kcli
    from plugins.kanban.dashboard import plugin_api as dashboard
    from tools import kanban_tools as kt

    def _surfaces(task):
        return (
            kcli._task_to_dict(task, conn),
            kt._task_summary_dict(kb, conn, task),
            dashboard._task_dict(conn, task),
        )

    legacy = kb.create_task(conn, title="legacy block", assignee="default")
    unaffirmed = kb.create_task(conn, title="unaffirmed block", assignee="default")
    stale_id = kb.create_task(conn, title="stale gate", assignee="default")
    gate = kb.create_task(conn, title="current gate", assignee="default")

    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (legacy,))
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (unaffirmed,))
        kb._append_event(conn, unaffirmed, "blocked", {"reason": "untyped"})

    # Fail-closed: a transport identity is not a human principal.
    if kh.affirm_human_gate(
        conn, gate, evidence=_evidence(affirmed_by="operator:dashboard")
    ):
        return False
    # Fail-closed: one record may carry only one atomic ask.
    if kh.affirm_human_gate(
        conn, gate, evidence=_evidence(action="Sign the contract and wire the deposit")
    ):
        return False
    if not kh.affirm_human_gate(conn, gate, evidence=_evidence()):
        return False
    if not kh.affirm_human_gate(conn, stale_id, evidence=_evidence(action="Old ask")):
        return False
    with kb.write_txn(conn):
        kb._append_event(conn, stale_id, "unblocked", {"by": "probe"})
        kb._append_event(conn, stale_id, "blocked", {"reason": "re-block"})

    for task_id in (legacy, unaffirmed, stale_id):
        task = kb.get_task(conn, task_id)
        if task is None or task.status != "blocked":
            return False
        for payload in _surfaces(task):
            projection = payload.get("block_projection") or {}
            if payload.get("status") != "triage" or projection.get("visible") is not False:
                return False

    current = kb.get_task(conn, gate)
    if current is None or current.status != "blocked":
        return False
    for payload in _surfaces(current):
        projection = payload.get("block_projection") or {}
        if not (
            payload.get("status") == "blocked"
            and projection.get("visible") is True
            and projection.get("action") == "Sign the named contract"
        ):
            return False
    return True


def _probe_typed_scheduled_hold(conn) -> bool:
    """Typing a hold must actually park the card and record the type."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    if not {"dependency", "wake"} <= set(kh.VALID_HOLD_KINDS):
        return False
    if not kh.PARKED_HOLD_KINDS:
        return False

    tid = kb.create_task(conn, title="wake hold", assignee="default")
    wake_at = int(time.time()) + 3600
    if not kh.set_hold(conn, tid, kind="wake", wake_at=wake_at, apply=True):
        return False
    task = kb.get_task(conn, tid)
    if task.status != "scheduled" or task.hold_kind != "wake":
        return False
    if int(task.hold_wake_at or 0) != wake_at:
        return False

    # Fail-closed: an unknown kind is refused outright.
    try:
        kh.set_hold(conn, tid, kind="whenever", apply=False)
    except ValueError:
        pass
    else:
        return False

    # Fail-closed: an intentional park without affirmed evidence is not
    # healthy just because it was typed.
    parked = kb.create_task(conn, title="external wait", assignee="default")
    if not kh.set_hold(conn, parked, kind="external", apply=True):
        return False
    state = kh.classify_hold(
        kb.get_task(conn, parked),
        now=int(time.time()),
        wake_health={"enabled": True, "healthy": True, "reason_code": None},
        parents_done=True,
    )
    return state.healthy is False


def _probe_durable_wake_reconciler(conn) -> bool:
    """A dispatcher tick must resume a due wake once and stamp its checkpoint."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    if kh.read_checkpoint(conn, kh.CHECKPOINT_RECONCILE) is not None:
        return False
    now = int(time.time())
    tid = kb.create_task(conn, title="due wake", assignee="default")
    if not kh.set_hold(conn, tid, kind="wake", wake_at=now - 60, apply=True):
        return False
    # max_spawn=0 keeps the resumed card in ready, making the transition easy
    # to observe without starting any worker process.
    first = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None, max_spawn=0)
    if first.health_resumed.count(tid) != 1:
        return False
    resumed_task = kb.get_task(conn, tid)
    if resumed_task is None or resumed_task.status == "scheduled":
        return False
    checkpoint = kh.read_checkpoint(conn, kh.CHECKPOINT_RECONCILE)
    if not checkpoint or checkpoint["status"] != "ok":
        return False
    second = kb.dispatch_once(conn, spawn_fn=lambda *_a, **_k: None, max_spawn=0)
    return tid not in second.health_resumed


def _probe_legacy_hold_is_loud(conn) -> bool:
    """A prose-only scheduled card must classify as unhealthy, not healthy."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    tid = kb.create_task(conn, title="legacy hold", assignee="default")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='scheduled', hold_kind=NULL, "
            "hold_wake_at=NULL WHERE id=?",
            (tid,),
        )
    state = kh.classify_hold(
        kb.get_task(conn, tid),
        now=int(time.time()),
        wake_health={"enabled": True, "healthy": True, "reason_code": None},
        parents_done=True,
    )
    if not (
        state.healthy is False
        and state.reason_code == kh.REASON_LEGACY_UNTYPED
        and state.needs_classification is True
    ):
        return False
    health = kh.board_health(conn)
    row = next((h for h in health["holds"] if h["task_id"] == tid), None)
    if row is None or row["healthy"] is not False:
        return False
    return tid in {r["task_id"] for r in health["no_forward_path"]}


def _probe_forward_path_audit(conn) -> bool:
    """Cards nothing can move must be reported, not assumed fine."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    unassigned = kb.create_task(conn, title="unrouted", assignee=None)
    broken = kb.create_task(
        conn,
        title="unresolvable workspace",
        assignee="default",
        workspace_kind="dir",
        workspace_path="relative/not/absolute",
    )
    live = kb.create_task(conn, title="genuinely ready", assignee="default")

    paths = {p.task_id: p for p in kh.forward_paths(conn)}
    if unassigned not in paths or paths[unassigned].ok:
        return False
    if broken not in paths or paths[broken].ok:
        return False
    if paths[broken].reason_code != kh.READY_INVALID_WORKSPACE:
        return False
    return bool(paths.get(live) and paths[live].ok)


def _probe_board_health_report(conn) -> bool:
    """The payload must be machine-readable AND reflect real board state."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    if int(getattr(kh, "HEALTH_SCHEMA_VERSION", 0)) < 1:
        return False
    payload = kh.board_health(conn)
    required = {
        "schema_version", "control_loop_version", "counts", "kevin_blocked",
        "automation_recovery", "holds", "no_forward_path", "scope", "wake",
        "healthy",
    }
    if not required <= set(payload):
        return False

    stuck = kb.create_task(conn, title="nobody owns this", assignee=None)
    unhealthy = kh.board_health(conn)
    if unhealthy["healthy"] is not False:
        return False
    return stuck in {row["task_id"] for row in unhealthy["no_forward_path"]}


def _probe_ready_queue_reason_codes(conn) -> bool:
    """Dispatcher dry-run and telemetry must agree across production gates."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh
    from hermes_cli import profiles

    codes = {
        kh.READY_SPAWNABLE,
        kh.READY_CAPACITY_GLOBAL,
        kh.READY_CAPACITY_MAX_SPAWN,
        kh.READY_CAPACITY_PER_PROFILE,
        kh.READY_INVALID_WORKSPACE,
        kh.READY_GUARD_ACTIVE_PR,
        kh.READY_CONTROL_PLANE_LANE,
        kh.READY_UNASSIGNED,
        kh.READY_MEMORY_PRESSURE_CRITICAL,
        kh.READY_MEMORY_PRESSURE_ELEVATED,
        kh.READY_REVIEW_DISABLED,
    }
    if len(codes) != 11:
        return False

    originals = {
        "profile_exists": profiles.profile_exists,
        "review_dispatch_enabled": kb.review_dispatch_enabled,
        "memory": kb._memory_pressure_level,
        "other_boards": kb.count_running_tasks_other_boards,
        "guard": kb.check_respawn_guard,
        "release": kb.release_stale_claims,
        "stale": kb.detect_stale_running,
        "crashed": kb.detect_crashed_workers,
    }

    def _retire(*task_ids):
        with kb.write_txn(conn):
            for task_id in task_ids:
                conn.execute(
                    "UPDATE tasks SET status='done', claim_lock=NULL, "
                    "claim_expires=NULL, worker_pid=NULL WHERE id=?",
                    (task_id,),
                )

    def _review(task_id):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))

    def _parity(*, expected, report_kw=None, **dispatch_kw):
        report_kw = dict(report_kw or dispatch_kw)
        report = kh.ready_queue_report(conn, **report_kw)
        result = kb.dispatch_once(
            conn, dry_run=True, spawn_fn=lambda *_a, **_k: None,
            reconcile_orphans=False, **dispatch_kw,
        )
        dispatched = {task_id for task_id, _assignee, _workspace in result.spawned}
        return dispatched == set(report.spawnable_ids) == set(expected), report

    try:
        # Make executor and host-state decisions deterministic while retaining
        # the candidate's real dispatch/report implementations.
        profiles.profile_exists = lambda name: name in {"default", "other"}
        kb.review_dispatch_enabled = lambda: True
        kb._memory_pressure_level = lambda: "ok"
        kb.count_running_tasks_other_boards = lambda _board: 0
        kb.check_respawn_guard = lambda _conn, _tid, **_kw: None
        kb.release_stale_claims = lambda _conn: []
        kb.detect_stale_running = lambda _conn, **_kw: []
        kb.detect_crashed_workers = lambda _conn: []

        # default_assignee applies to ready only; review remains unassigned.
        ready_default = kb.create_task(conn, title="ready default", assignee=None)
        review_unassigned = kb.create_task(conn, title="review unassigned", assignee=None)
        _review(review_unassigned)
        ok, report = _parity(
            expected={ready_default}, max_spawn=4,
            default_assignee="default",
        )
        if not ok or report.reason_for(review_unassigned) != kh.READY_UNASSIGNED:
            return False
        _retire(ready_default, review_unassigned)

        # Separate lane ordering reserves one bounded slot for review.
        ready_high = kb.create_task(conn, title="ready high", assignee="default", priority=20)
        ready_low = kb.create_task(conn, title="ready low", assignee="default", priority=10)
        review = kb.create_task(conn, title="review", assignee="default", priority=-10)
        _review(review)
        ok, _ = _parity(
            expected={ready_high, review}, max_spawn=2
        )
        if not ok:
            return False
        _retire(ready_high, ready_low, review)

        # Card gates: workspace precondition and lane-specific respawn guard.
        broken = kb.create_task(
            conn, title="bad workspace", assignee="default", priority=30,
            workspace_kind="dir", workspace_path="relative/not/absolute",
        )
        guarded = kb.create_task(conn, title="active PR", assignee="default", priority=20)
        eligible = kb.create_task(conn, title="eligible", assignee="default", priority=10)
        kb.check_respawn_guard = (
            lambda _conn, tid, **_kw: "active_pr" if tid == guarded else None
        )
        ok, report = _parity(expected={eligible}, max_spawn=8)
        if not (
            ok
            and report.reason_for(broken) == kh.READY_INVALID_WORKSPACE
            and report.reason_for(guarded) == kh.READY_GUARD_ACTIVE_PR
        ):
            return False
        _retire(broken, guarded, eligible)
        kb.check_respawn_guard = lambda _conn, _tid, **_kw: None

        # max_spawn=0 is an explicit dispatch-off cap, not "unlimited".
        zero_wait = kb.create_task(conn, title="dispatch disabled", assignee="default")
        ok, report = _parity(expected=set(), max_spawn=0)
        if not ok or report.reason_for(zero_wait) != kh.READY_CAPACITY_MAX_SPAWN:
            return False
        _retire(zero_wait)

        # max_spawn, host-global and per-profile budgets are distinct reasons.
        running = kb.create_task(conn, title="running", assignee="default")
        kb.claim_task(conn, running)
        waiting = kb.create_task(conn, title="max wait", assignee="other")
        ok, report = _parity(expected=set(), max_spawn=1)
        if not ok or report.reason_for(waiting) != kh.READY_CAPACITY_MAX_SPAWN:
            return False
        _retire(running, waiting)

        kb.count_running_tasks_other_boards = lambda _board: 1
        global_wait = kb.create_task(conn, title="global wait", assignee="default")
        ok, report = _parity(
            expected=set(), max_in_progress=1,
            report_kw={
                "max_in_progress": 1, "memory_pressure": "ok",
                "include_other_boards": True,
            },
        )
        if not ok or report.reason_for(global_wait) != kh.READY_CAPACITY_GLOBAL:
            return False
        _retire(global_wait)
        kb.count_running_tasks_other_boards = lambda _board: 0

        running = kb.create_task(conn, title="profile running", assignee="default")
        kb.claim_task(conn, running)
        profile_wait = kb.create_task(conn, title="profile wait", assignee="default")
        ok, report = _parity(
            expected=set(), max_in_progress_per_profile=1
        )
        if not ok or report.reason_for(profile_wait) != kh.READY_CAPACITY_PER_PROFILE:
            return False
        _retire(running, profile_wait)

        # Dynamic memory pressure is part of the real dispatch decision too.
        first = kb.create_task(conn, title="pressure first", assignee="default", priority=2)
        second = kb.create_task(conn, title="pressure second", assignee="other", priority=1)
        kb._memory_pressure_level = lambda: "critical"
        ok, report = _parity(expected=set())
        if not ok or report.reason_for(first) != kh.READY_MEMORY_PRESSURE_CRITICAL:
            return False
        kb._memory_pressure_level = lambda: "elevated"
        ok, report = _parity(expected={first})
        return bool(
            ok and report.reason_for(second) == kh.READY_MEMORY_PRESSURE_ELEVATED
        )
    finally:
        profiles.profile_exists = originals["profile_exists"]
        kb.review_dispatch_enabled = originals["review_dispatch_enabled"]
        kb._memory_pressure_level = originals["memory"]
        kb.count_running_tasks_other_boards = originals["other_boards"]
        kb.check_respawn_guard = originals["guard"]
        kb.release_stale_claims = originals["release"]
        kb.detect_stale_running = originals["stale"]
        kb.detect_crashed_workers = originals["crashed"]


def _probe_dispatcher_stuck_is_gated(conn) -> bool:
    """A queue with no spawnable card must never raise dispatcher-stuck."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh

    running = kb.create_task(conn, title="in flight", assignee="default")
    kb.claim_task(conn, running)
    waiting = kb.create_task(conn, title="capacity wait", assignee="default")
    capped = kh.ready_queue_report(conn, max_spawn=1, memory_pressure="ok")
    if capped.reason_for(waiting) == kh.READY_SPAWNABLE:
        return False
    if kh.dispatcher_stuck_alert(
        [capped], consecutive_idle_ticks=99, grace_ticks=3
    ) is not None:
        return False

    live = kh.ready_queue_report(conn, max_spawn=8, memory_pressure="ok")
    if waiting not in live.spawnable_ids:
        return False
    # Inside the grace window it stays silent; past it, it must fire.
    if kh.dispatcher_stuck_alert(
        [live], consecutive_idle_ticks=1, grace_ticks=3
    ) is not None:
        return False
    alert = kh.dispatcher_stuck_alert(
        [live], consecutive_idle_ticks=3, grace_ticks=3
    )
    return bool(alert and waiting in alert.task_ids)


def _probe_continuation_scope_inheritance(conn) -> bool:
    """A child must carry the full parent-row authority, not ambient scope."""
    import json as _json

    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    repo = Path(os.environ["HOME"]) / "probe-repo"
    (repo / ".worktrees").mkdir(parents=True)
    parent = kb.create_task(
        conn,
        title="scoped parent",
        assignee="default",
        created_by="probe-originator",
        tenant="probe-legal-entity",
        session_id="probe-session",
    )
    parent_workspace = repo / ".worktrees" / parent
    parent_workspace.mkdir()
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET project_id=?, workspace_kind='worktree', "
            "workspace_path=?, branch_name=? WHERE id=?",
            ("probe-project", str(parent_workspace), f"probe-project/{parent}", parent),
        )
    parent_task = kb.claim_task(conn, parent, ttl_seconds=3600)
    if (
        parent_task is None
        or parent_task.current_run_id is None
        or not parent_task.claim_lock
    ):
        return False
    run = conn.execute(
        "SELECT status, ended_at, claim_lock, claim_expires FROM task_runs WHERE id=?",
        (parent_task.current_run_id,),
    ).fetchone()
    if (
        run is None
        or run["status"] != "running"
        or run["ended_at"] is not None
        or run["claim_lock"] != parent_task.claim_lock
        or not run["claim_expires"]
    ):
        return False

    previous = {
        key: os.environ.get(key)
        for key in (
            "HERMES_KANBAN_TASK",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_KANBAN_CLAIM_LOCK",
            "HERMES_KANBAN_BOARD",
            "HERMES_PROFILE",
            "HERMES_TENANT",
            "HERMES_SESSION_ID",
        )
    }
    try:
        os.environ["HERMES_KANBAN_TASK"] = parent
        os.environ["HERMES_KANBAN_RUN_ID"] = str(parent_task.current_run_id)
        os.environ["HERMES_KANBAN_CLAIM_LOCK"] = parent_task.claim_lock
        os.environ["HERMES_KANBAN_BOARD"] = kb.DEFAULT_BOARD
        os.environ["HERMES_PROFILE"] = "default"
        # A stale ambient scope (a restart, a re-exec) must lose to the row.
        os.environ["HERMES_TENANT"] = "stale-ambient-tenant"
        os.environ["HERMES_SESSION_ID"] = "stale-ambient-session"
        create_args = {
            "title": "continuation",
            "assignee": "default",
            "parents": [parent],
        }

        # Capability certification includes fail-closed run and claim authority,
        # not merely inheritance on one happy-path continuation.
        os.environ["HERMES_KANBAN_RUN_ID"] = "999999"
        if "error" not in _json.loads(kt._handle_create(create_args)):
            return False
        os.environ["HERMES_KANBAN_RUN_ID"] = str(parent_task.current_run_id)

        os.environ["HERMES_KANBAN_CLAIM_LOCK"] = "foreign-claim-authority"
        if "error" not in _json.loads(kt._handle_create(create_args)):
            return False
        os.environ["HERMES_KANBAN_CLAIM_LOCK"] = parent_task.claim_lock

        result = _json.loads(kt._handle_create(create_args))

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires=0 WHERE id=?", (parent,))
            conn.execute(
                "UPDATE task_runs SET claim_expires=0 WHERE id=?",
                (parent_task.current_run_id,),
            )
        if "error" not in _json.loads(kt._handle_create(create_args)):
            return False
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET claim_expires=? WHERE id=?",
                (parent_task.claim_expires, parent),
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires=? WHERE id=?",
                (parent_task.claim_expires, parent_task.current_run_id),
            )

        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status='done', ended_at=strftime('%s','now') "
                "WHERE id=?",
                (parent_task.current_run_id,),
            )
        if "error" not in _json.loads(kt._handle_create(create_args)):
            return False
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if "error" in result:
        return False
    child = kb.get_task(conn, result["task_id"])
    if child is None:
        return False
    child_workspace = Path(child.workspace_path or "")
    return (
        child.tenant,
        child.session_id,
        child.project_id,
        child.assignee,
        child.created_by,
        child.workspace_kind,
        child_workspace.parent,
        child_workspace.name,
    ) == (
        "probe-legal-entity",
        "probe-session",
        "probe-project",
        "default",
        "probe-originator",
        "worktree",
        repo / ".worktrees",
        child.id,
    )


def _probe_health_cli(conn) -> bool:
    """The CLI must expose the board-health / scheduled-wake diagnostics."""
    import argparse

    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kcli.build_parser(sub)
    for argv in (
        ["kanban", "board-health", "--json"],
        ["kanban", "scheduled-wake", "--json"],
        ["kanban", "sentinel", "--json"],
    ):
        try:
            parser.parse_args(argv)
        except SystemExit:
            return False
    return True


def _probe_sentinel(conn) -> bool:
    """The sentinel must sweep, agree with the controller, and stay read-only."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_health as kh
    from hermes_cli import kanban_sentinel as ks

    if ks.detect_drift() is not None:
        return False

    now = int(time.time())
    kh.record_checkpoint(conn, kh.CHECKPOINT_RECONCILE, status="ok", now=now)
    stuck = kb.create_task(conn, title="nobody owns this", assignee=None)

    before = conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
    report = ks.run_sentinel(now=now, apply=False)
    after = conn.execute("SELECT COUNT(*) AS n FROM task_events").fetchone()["n"]
    if after != before:
        # A dry run that writes is not a dry run.
        return False
    if report.ok is not False:
        return False
    if stuck not in {row.get("task_id") for row in report.unresolved}:
        return False
    return bool(report.kevin_action)


REQUIRED_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "kanban.typed_block_projection",
        "Visible blocked is an affirmed, typed, atomic human gate bound to the "
        "current occurrence; machine holds route to automation recovery.",
        _probe_typed_block_projection,
    ),
    Capability(
        "kanban.typed_scheduled_hold",
        "Scheduled cards carry a typed hold classification.",
        _probe_typed_scheduled_hold,
    ),
    Capability(
        "kanban.durable_wake_reconciler",
        "Idempotent reconciliation with a durable wake checkpoint.",
        _probe_durable_wake_reconciler,
    ),
    Capability(
        "kanban.legacy_hold_is_loud",
        "Legacy prose-only scheduled holds are visibly unhealthy and never "
        "silently healthy.",
        _probe_legacy_hold_is_loud,
    ),
    Capability(
        "kanban.forward_path_audit",
        "Every nonterminal card is audited for a machine-verifiable forward path.",
        _probe_forward_path_audit,
    ),
    Capability(
        "kanban.board_health_report",
        "First-class machine-readable board-health payload.",
        _probe_board_health_report,
    ),
    Capability(
        "kanban.ready_queue_reason_codes",
        "Nonspawnable ready states expose distinct reason codes under every "
        "production spawn constraint.",
        _probe_ready_queue_reason_codes,
    ),
    Capability(
        "kanban.dispatcher_stuck_is_gated",
        "Dispatcher-stuck alerts only on eligible, below-cap work.",
        _probe_dispatcher_stuck_is_gated,
    ),
    Capability(
        "kanban.continuation_scope_inheritance",
        "Continuations inherit Project/principal/executor/tenant scope from "
        "the parent row.",
        _probe_continuation_scope_inheritance,
    ),
    Capability(
        "kanban.health_cli",
        "board-health / scheduled-wake / sentinel CLI diagnostics.",
        _probe_health_cli,
    ),
    Capability(
        "kanban.sentinel",
        "Deterministic no-agent sentinel available as a local CLI surface.",
        _probe_sentinel,
    ),
)

REQUIRED_CAPABILITY_NAMES: tuple[str, ...] = tuple(
    c.name for c in REQUIRED_CAPABILITIES
)


def capability_manifest() -> dict:
    """The serializable contract the canary checks a staged tree against."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "required": list(REQUIRED_CAPABILITY_NAMES),
        "descriptions": {c.name: c.description for c in REQUIRED_CAPABILITIES},
    }


def probe_capabilities() -> dict:
    """Run every probe against a private temporary board. ``{name: bool}``.

    A probe that raises counts as absent — a capability that cannot be
    exercised without blowing up is not a capability. If the isolated board
    cannot be built at all, every capability is reported absent, because "we
    could not tell" must never read as "fine".
    """
    out: dict[str, bool] = {}
    for cap in REQUIRED_CAPABILITIES:
        try:
            # A probe owns its entire board fixture. Besides preventing one
            # probe's writes from changing a later probe's census, this makes
            # each result reproducible in both the live process and the
            # running-install-owned subprocess used for mirrored candidates.
            with _isolated_board() as conn:
                out[cap.name] = bool(cap.probe(conn))
        except Exception as exc:
            logger.debug("capability probe %s failed: %s", cap.name, exc)
            out[cap.name] = False
    return out


@dataclass
class CapabilityReport:
    ok: bool
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: str = ""
    probed: bool = True
    """False when the probe could not be executed at all (tree unimportable,
    subprocess blocked, timeout) — as opposed to executing and reporting a
    capability as absent.

    The report still fails closed either way (``ok`` is False, everything is
    listed missing), because "we could not tell" must never read as "fine".
    The two cases warrant different diagnostics, but both fail activation: a
    probe that ran and found a capability gone proves a dropped invariant; an
    unprobeable candidate has not supplied the evidence required to activate
    safely."""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "present": self.present,
            "missing": self.missing,
            "detail": self.detail,
            "probed": self.probed,
        }


def verify_capabilities(probe: Optional[dict] = None) -> CapabilityReport:
    """Check a probe result against the required manifest.

    Absent AND present-but-false both count as missing: a capability wired in
    but broken is the more dangerous of the two, because its name still
    appears everywhere that looks for it.
    """
    results = probe_capabilities() if probe is None else probe
    present, missing = [], []
    for name in REQUIRED_CAPABILITY_NAMES:
        if bool(results.get(name)):
            present.append(name)
        else:
            missing.append(name)
    return CapabilityReport(
        ok=not missing,
        present=present,
        missing=missing,
        detail=(
            "all required Kanban lifecycle capabilities present"
            if not missing
            else "missing required Kanban lifecycle capability: "
            + ", ".join(missing)
        ),
    )


# The subprocess loads THIS module by absolute path (argv[1]) and runs its
# probes, while ``hermes_cli`` / ``tools`` resolve to the candidate tree on
# PYTHONPATH. The candidate therefore supplies the implementations under test
# and never the test itself: a candidate shipping no-op probes, or a manifest
# that quietly forgot a capability, cannot certify itself.
#
# It reports WHERE both halves came from, so a staged root missing
# ``hermes_cli`` (which would silently resolve the *installed* package and
# certify the wrong tree) is detected rather than believed.
_PROBE_SCRIPT = (
    "import importlib.util,json,sys\n"
    "try:\n"
    "    spec = importlib.util.spec_from_file_location(\n"
    "        '_hermes_running_capabilities', sys.argv[1])\n"
    "    running = importlib.util.module_from_spec(spec)\n"
    "    sys.modules[spec.name] = running\n"
    "    spec.loader.exec_module(running)\n"
    "    import hermes_cli.kanban_health as candidate\n"
    "    print(json.dumps({\n"
    "        'probe_module_file': getattr(running, '__file__', None),\n"
    "        'candidate_module_file': getattr(candidate, '__file__', None),\n"
    "        'capabilities': running.probe_capabilities(),\n"
    "    }))\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'__error__': repr(exc)}))\n"
    "    sys.exit(3)\n"
)


def _path_variants(value) -> set:
    """``{abspath, realpath}`` of *value*.

    Both are needed. Resolving alone would follow a symlinked staging tree
    back out to its origin and reject a candidate that IS the tree about to be
    activated; not resolving alone breaks wherever a platform hands back a
    resolved path for an unresolved input (macOS ``/var`` → ``/private/var``,
    a temp dir behind a symlinked mount).
    """
    text = str(value)
    out = {os.path.abspath(text)}
    try:
        out.add(os.path.realpath(text))
    except OSError:
        pass
    return out


def _under(path: Optional[str], root: Path) -> bool:
    """Whether *path* lies inside *root* under any consistent naming."""
    if not path:
        return False
    roots = _path_variants(root)
    for candidate in _path_variants(path):
        for base in roots:
            try:
                if os.path.commonpath([candidate, base]) == base:
                    return True
            except (OSError, ValueError):
                continue
    return False


def preactivation_canary(
    root: Path, *, timeout: int = CANARY_TIMEOUT_SECONDS
) -> CapabilityReport:
    """Probe a STAGED tree for every capability this install requires.

    Runs out-of-process against *root* so the answer describes the tree about
    to be activated, not the modules already imported here. The probe *logic*
    is this module's, loaded by absolute path inside that subprocess, so the
    candidate cannot judge itself. Fails closed: an unimportable or crashing
    tree reports every capability missing, because "we could not tell" must
    never read as "fine".
    """
    root = Path(root)
    running_module = os.path.abspath(__file__)
    try:
        if not (root / "hermes_cli" / "kanban_capabilities.py").is_file():
            raise RuntimeError(
                f"{root} does not contain hermes_cli/kanban_capabilities.py"
            )
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT, running_module],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_probe_env(root),
        )
        payload = json.loads((proc.stdout or "").strip() or "{}")
        if not isinstance(payload, dict) or "__error__" in payload:
            raise RuntimeError(
                payload.get("__error__", "probe produced no result")
                if isinstance(payload, dict)
                else "probe produced a non-object result"
            )
        # Provenance, both directions. The behaviour tested must come from the
        # STAGED tree, and the test itself must come from the RUNNING install.
        candidate_file = payload.get("candidate_module_file")
        if not _under(candidate_file, root):
            raise RuntimeError(
                f"probe resolved hermes_cli from {candidate_file!r}, which is "
                f"not under the staged tree {root} — the result would describe "
                f"the wrong tree"
            )
        probe_file = payload.get("probe_module_file")
        if not probe_file or not (
            _path_variants(probe_file) & _path_variants(running_module)
        ):
            raise RuntimeError(
                f"probe logic came from {probe_file!r}, not the running "
                f"install at {running_module} — a candidate cannot certify "
                f"itself"
            )
        payload = payload.get("capabilities")
        if not isinstance(payload, dict):
            raise RuntimeError("probe returned no capability map")
    except Exception as exc:
        return CapabilityReport(
            ok=False,
            present=[],
            missing=list(REQUIRED_CAPABILITY_NAMES),
            probed=False,
            detail=(
                f"pre-activation capability canary could not probe {root}: {exc}. "
                "Missing (fail-closed): " + ", ".join(REQUIRED_CAPABILITY_NAMES)
            ),
        )
    return verify_capabilities(payload)


def _probe_env(root: Path) -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{root}{os.pathsep}{existing}" if existing else str(root)
    )
    # Never let the probe touch the operator's real board. ``_isolated_board``
    # redirects the home again inside the subprocess; clearing here means even
    # a candidate that never reaches that code cannot inherit a live DB path.
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        env.pop(key, None)
    return env
