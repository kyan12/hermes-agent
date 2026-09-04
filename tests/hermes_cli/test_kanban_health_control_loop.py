"""Acceptance tests for the native Kanban health control loop.

These pin the invariants that make board health self-maintaining:

* Visible ``blocked`` means ONE current atomic human-only action backed by
  typed evidence. Untyped/machine failures route to automation recovery
  before the dashboard ever projects them as a human gate.
* Every nonterminal card has a machine-verifiable forward path.
* Typed scheduled holds resume exactly once, park when their dependency is
  unfinished, and stay parked when the hold is an intentional
  external/physical/roadmap wait.
* A legacy prose-only scheduled card is visibly unhealthy and is never
  silently healthy nor bulk-resumed.

Everything runs against a real kanban DB in a temp ``HERMES_HOME``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _mk(conn, title="card", **kw):
    return kb.create_task(conn, title=title, assignee="alice", **kw)


def _affirmed_evidence(action="Sign the vendor contract", now=None):
    return {
        "type": "human_decision",
        "action": action,
        "affirmed_by": "kevin",
        "affirmed_at": int(now or time.time()),
    }


# ---------------------------------------------------------------------------
# Invariant A — visible blocked is an affirmed, typed, atomic human gate
# ---------------------------------------------------------------------------


def test_untyped_machine_block_is_not_projected_as_a_human_gate(board):
    """An un-typed block (the machine-failure shape) must never render as a
    human gate; it routes to automation recovery instead."""
    tid = _mk(board, "worker crashed with a stack trace")
    assert kb.block_task(board, tid, reason="Traceback: KeyError", kind=None)

    health = kh.board_health(board)
    kevin_ids = {row["task_id"] for row in health["kevin_blocked"]}
    recovery = {row["task_id"]: row["reason_code"] for row in health["automation_recovery"]}

    assert tid not in kevin_ids
    assert recovery.get(tid) == kh.REASON_UNTYPED_BLOCK


def test_typed_block_without_affirmed_evidence_is_not_a_human_gate(board):
    """``needs_input`` alone is a claim, not evidence. Without an affirmed,
    typed evidence record it stays out of the human column."""
    tid = _mk(board, "worker says it needs input")
    assert kb.block_task(board, tid, reason="need a decision", kind="needs_input")

    health = kh.board_health(board)
    assert tid not in {row["task_id"] for row in health["kevin_blocked"]}
    recovery = {row["task_id"]: row["reason_code"] for row in health["automation_recovery"]}
    assert recovery.get(tid) == kh.REASON_UNAFFIRMED_GATE


def test_affirmed_human_gate_stays_visible_with_one_atomic_action(board):
    tid = _mk(board, "vendor contract needs a signature")
    assert kh.affirm_human_gate(
        board,
        tid,
        evidence=_affirmed_evidence("Sign the vendor contract"),
        kind="capability",
        reason="needs signature",
    )

    health = kh.board_health(board)
    gates = {row["task_id"]: row for row in health["kevin_blocked"]}
    assert tid in gates
    assert gates[tid]["action"] == "Sign the vendor contract"
    assert gates[tid]["evidence_type"] == "human_decision"
    # A human gate is a legitimate terminal-for-automation state, not a
    # forward-path violation.
    assert tid not in {row["task_id"] for row in health["no_forward_path"]}


def test_gate_evidence_must_be_typed_and_atomic(board):
    tid = _mk(board, "needs something")
    kb.block_task(board, tid, reason="x", kind="needs_input")
    # Untyped evidence type
    assert not kh.set_gate_evidence(
        board, tid, evidence={"type": "vibes", "action": "do it", "affirmed_by": "kevin"}
    )
    # No atomic action
    assert not kh.set_gate_evidence(
        board, tid, evidence={"type": "human_decision", "affirmed_by": "kevin"}
    )
    # No affirming principal
    assert not kh.set_gate_evidence(
        board, tid, evidence={"type": "human_decision", "action": "do it"}
    )


# ---------------------------------------------------------------------------
# Invariant B / D — typed scheduled holds
# ---------------------------------------------------------------------------


def test_parent_complete_scheduled_dependency_resumes_exactly_once(board):
    parent = _mk(board, "parent work")
    child = kb.create_task(
        board, title="child work", assignee="alice", parents=(parent,)
    )
    assert kh.set_hold(
        board, child, kind="dependency", reason="waiting on parent", apply=True
    )
    assert kb.get_task(board, child).status == "scheduled"

    # Parent unfinished → parked, never resumed.
    first = kh.reconcile_board(board)
    assert child in {r["task_id"] for r in first.parked}
    assert child not in {r["task_id"] for r in first.resumed}
    assert kb.get_task(board, child).status == "scheduled"

    kb.complete_task(board, parent, result="done")

    second = kh.reconcile_board(board)
    assert child in {r["task_id"] for r in second.resumed}
    assert kb.get_task(board, child).status in ("ready", "todo")

    # Idempotent: a second pass must not resume it again.
    third = kh.reconcile_board(board)
    assert child not in {r["task_id"] for r in third.resumed}


def test_unfinished_dependency_hold_stays_parked_across_many_passes(board):
    parent = _mk(board, "long parent")
    child = kb.create_task(board, title="child", assignee="alice", parents=(parent,))
    kh.set_hold(board, child, kind="dependency", reason="waiting", apply=True)

    for _ in range(3):
        report = kh.reconcile_board(board)
        assert child not in {r["task_id"] for r in report.resumed}
    parked = {r["task_id"]: r["reason_code"] for r in report.parked}
    assert parked[child] == kh.REASON_DEPENDENCY_UNFINISHED
    assert kb.get_task(board, child).status == "scheduled"


def test_due_wake_hold_resumes_once_and_records_a_checkpoint(board):
    tid = _mk(board, "retry after the maintenance window")
    now = int(time.time())
    kh.set_hold(
        board, tid, kind="wake", wake_at=now - 5, reason="window closes", apply=True
    )

    report = kh.reconcile_board(board, now=now)
    assert tid in {r["task_id"] for r in report.resumed}
    assert kb.get_task(board, tid).status == "ready"

    again = kh.reconcile_board(board, now=now)
    assert tid not in {r["task_id"] for r in again.resumed}

    checkpoint = kh.read_checkpoint(board, kh.CHECKPOINT_RECONCILE)
    assert checkpoint is not None
    assert checkpoint["status"] == "ok"


def test_future_wake_hold_is_healthy_and_parked(board):
    tid = _mk(board, "scheduled for later")
    now = int(time.time())
    kh.set_hold(board, tid, kind="wake", wake_at=now + 3600, apply=True)

    report = kh.reconcile_board(board, now=now)
    assert tid not in {r["task_id"] for r in report.resumed}

    health = kh.board_health(board, now=now)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is True
    assert holds[tid]["reason_code"] == kh.REASON_WAKE_ARMED


# ---------------------------------------------------------------------------
# Invariant D — wake health diagnoses missing / disabled / failed
# ---------------------------------------------------------------------------


def test_wake_hold_without_a_wake_time_diagnoses_missing(board):
    tid = _mk(board, "wake hold with no armed time")
    kh.set_hold(board, tid, kind="wake", wake_at=None, apply=True)

    health = kh.board_health(board)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is False
    assert holds[tid]["reason_code"] == kh.REASON_WAKE_MISSING
    assert tid in {row["task_id"] for row in health["no_forward_path"]}
    assert health["healthy"] is False


def test_disabled_reconciler_diagnoses_wake_disabled(board, monkeypatch):
    tid = _mk(board, "scheduled with the controller switched off")
    now = int(time.time())
    kh.set_hold(board, tid, kind="wake", wake_at=now + 60, apply=True)
    monkeypatch.setattr(kh, "reconcile_enabled", lambda: False)

    health = kh.board_health(board, now=now)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is False
    assert holds[tid]["reason_code"] == kh.REASON_WAKE_DISABLED
    assert health["wake"]["enabled"] is False


def test_stale_or_failed_checkpoint_diagnoses_wake_failed(board):
    tid = _mk(board, "scheduled while the controller is down")
    now = int(time.time())
    kh.set_hold(board, tid, kind="wake", wake_at=now + 60, apply=True)
    kh.record_checkpoint(
        board, kh.CHECKPOINT_RECONCILE, status="failed", now=now - 10, detail="boom"
    )

    health = kh.board_health(board, now=now)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is False
    assert holds[tid]["reason_code"] == kh.REASON_WAKE_FAILED

    # Stale-but-ok checkpoints are equally unhealthy: nothing is waking.
    kh.record_checkpoint(
        board,
        kh.CHECKPOINT_RECONCILE,
        status="ok",
        now=now - (kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS + 60),
    )
    health = kh.board_health(board, now=now)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["reason_code"] == kh.REASON_WAKE_FAILED


# ---------------------------------------------------------------------------
# Invariant D — intentional holds stay parked; legacy holds are loud
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(kh.PARKED_HOLD_KINDS))
def test_intentional_holds_stay_parked_and_healthy(board, kind):
    tid = _mk(board, f"{kind} hold")
    kh.set_hold(
        board,
        tid,
        kind=kind,
        evidence={
            "type": "external_party",
            "action": "Await the counterparty",
            "affirmed_by": "kevin",
            "affirmed_at": int(time.time()),
        },
        apply=True,
    )

    report = kh.reconcile_board(board)
    assert tid not in {r["task_id"] for r in report.resumed}
    assert kb.get_task(board, tid).status == "scheduled"

    health = kh.board_health(board)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is True
    assert holds[tid]["reason_code"] == kh.REASON_INTENTIONAL_HOLD
    assert tid not in {row["task_id"] for row in health["no_forward_path"]}


def test_legacy_prose_only_scheduled_hold_is_unhealthy_and_never_bulk_resumed(board):
    tid = _mk(board, "legacy scheduled card")
    # The pre-control-loop shape: prose reason, no typed hold at all.
    assert kb.schedule_task(board, tid, reason="waiting until the thing happens")

    health = kh.board_health(board)
    holds = {row["task_id"]: row for row in health["holds"]}
    assert holds[tid]["healthy"] is False
    assert holds[tid]["reason_code"] == kh.REASON_LEGACY_UNTYPED
    assert holds[tid]["needs_classification"] is True
    assert health["healthy"] is False
    assert tid in {row["task_id"] for row in health["no_forward_path"]}

    # Reconciliation diagnoses it but never resumes it, no matter how often
    # it runs — a legacy card is not evidence that the work may proceed.
    for _ in range(3):
        report = kh.reconcile_board(board)
        assert tid not in {r["task_id"] for r in report.resumed}
        assert tid in {r["task_id"] for r in report.diagnosed}
    assert kb.get_task(board, tid).status == "scheduled"


# ---------------------------------------------------------------------------
# Invariant B — forward paths for the non-hold statuses
# ---------------------------------------------------------------------------


def test_every_nonterminal_card_reports_a_forward_path(board, all_assignees_spawnable):
    ready = _mk(board, "eligible ready card")
    parent = _mk(board, "parent")
    child = kb.create_task(board, title="gated child", assignee="alice", parents=(parent,))
    gate = _mk(board, "human gate")
    assert kh.affirm_human_gate(
        board,
        gate,
        evidence=_affirmed_evidence(),
        kind="capability",
        reason="needs a signature",
    )

    paths = {p.task_id: p for p in kh.forward_paths(board)}
    assert paths[ready].kind == kh.PATH_ELIGIBLE_READY and paths[ready].ok
    assert paths[child].kind == kh.PATH_DEPENDENCY_CHAIN and paths[child].ok
    assert paths[gate].kind == kh.PATH_HUMAN_GATE and paths[gate].ok


def test_unassigned_ready_card_has_no_forward_path(board):
    tid = kb.create_task(board, title="nobody owns this", assignee=None)
    paths = {p.task_id: p for p in kh.forward_paths(board)}
    assert paths[tid].ok is False
    assert paths[tid].reason_code == kh.REASON_UNASSIGNED

    health = kh.board_health(board)
    assert tid in {row["task_id"] for row in health["no_forward_path"]}
