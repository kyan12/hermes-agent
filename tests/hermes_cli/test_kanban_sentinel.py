"""The no-agent sentinel: a deterministic verifier of last resort.

It is NOT a second controller. It inspects every board, refuses to act when
it cannot prove it is talking to the controller version it was written
against, stands down while the native reconciler is demonstrably alive, and
performs only the one narrowly-proven reversible repair that board state
authorizes. When the controller genuinely cannot recover it emits ONE atomic
human action for the whole sweep — not one per card.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_health as kh
from hermes_cli import kanban_sentinel as ks


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return h


def _due_wake(board_slug, *, now):
    conn = kbc.connect(board=board_slug)
    try:
        tid = kb.create_task(conn, title="due wake", assignee="alice")
        kh.set_hold(conn, tid, kind="wake", wake_at=now - 60, apply=True)
    finally:
        conn.close()
    return tid


def _stale_controller(board_slug, *, now):
    conn = kbc.connect(board=board_slug)
    try:
        kh.record_checkpoint(
            conn,
            kh.CHECKPOINT_RECONCILE,
            status="ok",
            now=now - (kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS * 10),
        )
    finally:
        conn.close()


def _fresh_controller(board_slug, *, now):
    conn = kbc.connect(board=board_slug)
    try:
        kh.record_checkpoint(conn, kh.CHECKPOINT_RECONCILE, status="ok", now=now)
    finally:
        conn.close()


def test_sentinel_inspects_every_board(home):
    kb.create_board("second", name="Second")
    now = int(time.time())
    report = ks.run_sentinel(now=now, apply=False)
    assert {b["board"] for b in report.boards} >= {kb.DEFAULT_BOARD, "second"}


def test_sentinel_stands_down_on_controller_version_drift(home, monkeypatch):
    now = int(time.time())
    _due_wake(kb.DEFAULT_BOARD, now=now)
    _stale_controller(kb.DEFAULT_BOARD, now=now)
    monkeypatch.setattr(kh, "CONTROL_LOOP_VERSION", kh.CONTROL_LOOP_VERSION + 1)

    report = ks.run_sentinel(now=now, apply=True)
    assert report.ok is False
    assert report.drift is not None
    assert report.repairs == []
    # Loud, not silent: drift is itself an escalation.
    assert report.kevin_action is not None
    assert "drift" in report.kevin_action["title"].lower()


def test_sentinel_does_not_race_a_live_controller(home):
    now = int(time.time())
    tid = _due_wake(kb.DEFAULT_BOARD, now=now)
    _fresh_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert report.repairs == []
    assert ks.REASON_CONTROLLER_ACTIVE in {b["reason_code"] for b in report.boards}
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()


def test_controller_start_between_read_and_lock_makes_sentinel_stand_down(
    home, monkeypatch
):
    now = int(time.time())
    tid = _due_wake(kb.DEFAULT_BOARD, now=now)
    stale = {
        "status": "ok",
        "updated_at": now - kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS - 1,
    }
    fresh = {"status": "ok", "updated_at": now}
    reads = iter((stale, fresh))
    real_read = kh.read_checkpoint

    def checkpoint(conn, name):
        if name == kh.CHECKPOINT_RECONCILE:
            try:
                return next(reads)
            except StopIteration:
                return fresh
        return real_read(conn, name)

    monkeypatch.setattr(kh, "read_checkpoint", checkpoint)
    monkeypatch.setattr(
        kh,
        "reconcile_board",
        lambda *_a, **_k: pytest.fail("sentinel raced the newly-live controller"),
    )

    report = ks.run_sentinel(now=now, apply=True)
    assert report.repairs == []
    assert report.boards[0]["reason_code"] == ks.REASON_CONTROLLER_ACTIVE
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()


def test_sentinel_repairs_a_due_typed_wake_when_the_controller_is_down(home):
    now = int(time.time())
    tid = _due_wake(kb.DEFAULT_BOARD, now=now)
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert tid in {r["task_id"] for r in report.repairs}
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_sentinel_never_resumes_a_legacy_untyped_hold(home):
    now = int(time.time())
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="legacy", assignee="alice")
        kb.schedule_task(conn, tid, reason="waiting on something")
    finally:
        conn.close()
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert tid not in {r["task_id"] for r in report.repairs}
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()
    assert report.kevin_action is not None


def test_sentinel_emits_exactly_one_atomic_action_for_many_bad_cards(home):
    now = int(time.time())
    conn = kbc.connect()
    try:
        for i in range(4):
            tid = kb.create_task(conn, title=f"legacy {i}", assignee="alice")
            kb.schedule_task(conn, tid, reason="prose only")
    finally:
        conn.close()
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert report.kevin_action is not None
    assert isinstance(report.kevin_action, dict)
    assert report.kevin_action["action"]
    assert len(report.kevin_action["evidence"]["task_ids"]) == 4


def test_sentinel_dedupes_repeated_identical_alerts(home):
    now = int(time.time())
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="legacy", assignee="alice")
        kb.schedule_task(conn, tid, reason="prose only")
    finally:
        conn.close()
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    first = ks.run_sentinel(now=now, apply=True)
    assert first.alert_emitted is True

    second = ks.run_sentinel(now=now + 30, apply=True)
    assert second.alert_emitted is False
    assert second.kevin_action is not None  # still reported, just not re-alerted

    later = ks.run_sentinel(now=now + ks.ALERT_DEDUPE_SECONDS + 60, apply=True)
    assert later.alert_emitted is True


def test_healthy_board_produces_no_action_and_no_repair(home, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(
        kh, "control_plane_assignees", lambda: frozenset({"alice"})
    )
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="ordinary ready card", assignee="alice")
    finally:
        conn.close()
    _fresh_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert report.ok is True
    assert report.repairs == []
    assert report.kevin_action is None


def test_dry_run_makes_no_state_change(home):
    now = int(time.time())
    tid = _due_wake(kb.DEFAULT_BOARD, now=now)
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=False)
    assert tid in {r["task_id"] for r in report.would_repair}
    assert report.repairs == []
    conn = kbc.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()


def test_dry_run_alert_dedupe_never_opens_or_initializes_a_board(home, monkeypatch):
    report = ks.SentinelReport(
        generated_at=123,
        kevin_action={
            "title": "Needs one action",
            "action": "Classify it",
            "evidence": {"task_ids": ["t_bad"]},
        },
    )
    monkeypatch.setattr(
        kbc,
        "connect",
        lambda *_a, **_k: pytest.fail("dry-run opened a mutable board connection"),
    )

    assert ks._maybe_emit(report, 123, apply=False) is True


def test_scope_diagnostics_reach_the_overall_verdict_and_evidence(home, monkeypatch):
    """A board reported unhealthy for ``unscoped_active_work`` used to leave
    the sweep ``ok=True`` with no action at all: the sentinel copied only
    ``no_forward_path`` into ``unresolved``. A verifier that exits 0 on a
    failure class it can see is worse than no verifier."""
    now = int(time.time())
    _fresh_controller(kb.DEFAULT_BOARD, now=now)

    scope_row = {
        "task_id": "t_unscoped",
        "title": "no project",
        "reason_code": kh.REASON_UNSCOPED_ACTIVE_WORK,
        "canonical_project_id": "alpha",
        "status": "ready",
    }
    real_board_health = kh.board_health

    def _health_with_scope(conn, **kwargs):
        payload = real_board_health(conn, **kwargs)
        payload["scope"] = [scope_row]
        payload["healthy"] = False
        return payload

    monkeypatch.setattr(kh, "board_health", _health_with_scope)

    report = ks.run_sentinel(now=now, apply=False)

    assert report.ok is False
    codes = {row["reason_code"] for row in report.unresolved}
    assert kh.REASON_UNSCOPED_ACTIVE_WORK in codes
    assert report.kevin_action is not None
    evidence = report.kevin_action["evidence"]
    assert "t_unscoped" in evidence["task_ids"]
    assert kh.REASON_UNSCOPED_ACTIVE_WORK in evidence["reason_codes"]


def test_unresolved_evidence_is_deduplicated_across_failure_classes(home, monkeypatch):
    """One card failing two classes is one page, not two."""
    now = int(time.time())
    _fresh_controller(kb.DEFAULT_BOARD, now=now)

    row = {
        "task_id": "t_dup",
        "title": "both",
        "reason_code": kh.REASON_UNSCOPED_ACTIVE_WORK,
        "status": "ready",
    }
    real_board_health = kh.board_health

    def _health(conn, **kwargs):
        payload = real_board_health(conn, **kwargs)
        payload["scope"] = [row, dict(row)]
        payload["no_forward_path"] = [dict(row, reason_code=kh.READY_UNASSIGNED)]
        payload["healthy"] = False
        return payload

    monkeypatch.setattr(kh, "board_health", _health)

    report = ks.run_sentinel(now=now, apply=False)
    assert report.ok is False
    assert report.kevin_action["evidence"]["task_ids"] == ["t_dup"]
    assert sorted(report.kevin_action["evidence"]["reason_codes"]) == sorted(
        [kh.READY_UNASSIGNED, kh.REASON_UNSCOPED_ACTIVE_WORK]
    )


def test_dry_run_read_only_does_not_create_or_modify_board_files(tmp_path, monkeypatch):
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    missing = ks.run_sentinel(now=123, apply=False)
    assert missing.ok is False
    assert not home.exists()
    home.mkdir()
    kb.init_db()
    db = kb.kanban_db_path()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    report = ks.run_sentinel(now=124, apply=False)
    after = hashlib.sha256(db.read_bytes()).hexdigest()
    assert report.ok is False
    assert before == after


@pytest.mark.parametrize("checkpoint", [None, "stale", "failed"])
def test_controller_failure_pages_once_even_on_empty_healthy_board(home, checkpoint):
    now = int(time.time())
    if checkpoint is not None:
        with kbc.connect() as conn:
            kh.record_checkpoint(
                conn, kh.CHECKPOINT_RECONCILE,
                status="failed" if checkpoint == "failed" else "ok",
                now=now if checkpoint == "failed" else now - kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS - 1,
            )
    report = ks.run_sentinel(now=now, apply=False)
    assert report.ok is False
    assert report.kevin_action is not None
    assert report.kevin_action["evidence"]["reason_codes"] == [ks.REASON_CONTROLLER_DOWN]
    assert report.repairs == []
