"""The no-agent sentinel: a deterministic verifier of last resort.

It is NOT a second controller. It inspects every board, refuses to act when
it cannot prove it is talking to the controller version it was written
against, stands down while the native reconciler is demonstrably alive, and
performs only the one narrowly-proven reversible repair that board state
authorizes. When the controller genuinely cannot recover it emits ONE atomic
human action for the whole sweep — not one per card.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
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
    conn = kb.connect(board=board_slug)
    try:
        tid = kb.create_task(conn, title="due wake", assignee="alice")
        kh.set_hold(conn, tid, kind="wake", wake_at=now - 60, apply=True)
    finally:
        conn.close()
    return tid


def _stale_controller(board_slug, *, now):
    conn = kb.connect(board=board_slug)
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
    conn = kb.connect(board=board_slug)
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
    conn = kb.connect()
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
    conn = kb.connect()
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
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_sentinel_never_resumes_a_legacy_untyped_hold(home):
    now = int(time.time())
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="legacy", assignee="alice")
        kb.schedule_task(conn, tid, reason="waiting on something")
    finally:
        conn.close()
    _stale_controller(kb.DEFAULT_BOARD, now=now)

    report = ks.run_sentinel(now=now, apply=True)
    assert tid not in {r["task_id"] for r in report.repairs}
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()
    assert report.kevin_action is not None


def test_sentinel_emits_exactly_one_atomic_action_for_many_bad_cards(home):
    now = int(time.time())
    conn = kb.connect()
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
    conn = kb.connect()
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


def test_healthy_board_produces_no_action_and_no_repair(home):
    now = int(time.time())
    conn = kb.connect()
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
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "scheduled"
    finally:
        conn.close()
