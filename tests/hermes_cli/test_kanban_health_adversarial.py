"""Adversarial regressions for health-loop authority and races."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh
from hermes_cli import kanban_sentinel as ks


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


def _mk(conn, title="card"):
    return kb.create_task(conn, title=title, assignee="alice")


def _evidence(**overrides):
    value = {
        "type": "human_decision",
        "action": "Sign the named contract",
        "affirmed_by": "Kevin Yan",
        "affirmed_at": int(time.time()),
    }
    value.update(overrides)
    return value


def test_evidence_rejects_untrusted_affirmer_and_missing_timestamp():
    assert kh.parse_evidence(_evidence(affirmed_by="mallory")) is None
    evidence = _evidence()
    evidence.pop("affirmed_at")
    assert kh.parse_evidence(evidence) is None


def test_gate_affirmation_binds_current_block_occurrence_and_reblock_clears_it(board):
    tid = _mk(board)
    # An untrusted/machine block request is recovery work, never a visible
    # Kevin gate.  Only the atomic trusted operator transition below may put
    # the card in ``blocked``.
    assert kb.block_task(board, tid, reason="claim", kind="needs_input")
    assert kb.get_task(board, tid).status == "triage"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    bound = json.loads(kb.get_task(board, tid).gate_evidence)
    event = board.execute(
        "SELECT id FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert bound["task_id"] == tid
    assert bound["occurrence_event_id"] == event["id"]

    assert kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).gate_evidence is None
    assert kb.block_task(board, tid, reason="new claim", kind="needs_input")
    task = kb.get_task(board, tid)
    assert task.status == "triage"
    assert task.gate_evidence is None
    assert kh.classify_block(task).visible is False


def test_gate_and_hold_fields_clear_on_complete_and_archive(board):
    tid = _mk(board, "complete")
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    assert kb.complete_task(board, tid, result="done")
    done = kb.get_task(board, tid)
    assert (done.gate_evidence, done.hold_kind, done.hold_wake_at) == (None, None, None)

    other = _mk(board, "archive")
    assert kh.set_hold(board, other, kind="wake", wake_at=int(time.time()) + 60)
    assert kb.archive_task(board, other)
    archived = kb.get_task(board, other)
    assert (archived.gate_evidence, archived.hold_kind, archived.hold_wake_at) == (None, None, None)


def test_intentional_park_requires_current_trusted_evidence(board):
    tid = _mk(board)
    assert kh.set_hold(board, tid, kind="external", apply=True)
    health = kh.board_health(board)
    hold = next(row for row in health["holds"] if row["task_id"] == tid)
    assert hold["healthy"] is False
    assert tid in {row["task_id"] for row in health["no_forward_path"]}


def test_dead_running_row_has_no_forward_path(board, monkeypatch):
    tid = _mk(board)
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET status='running', worker_pid=999999, claim_lock='x', claim_expires=? WHERE id=?",
            (int(time.time()) + 60, tid),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    path = next(p for p in kh.forward_paths(board) if p.task_id == tid)
    assert path.ok is False


def test_dispatch_result_carries_complete_reconciliation_report(board, monkeypatch, all_assignees_spawnable):
    expected = kh.ReconcileReport(errors=[{"task_id": None, "error": "boom"}])
    monkeypatch.setattr(kh, "reconcile_board", lambda conn: expected)
    result = kb.dispatch_once(board, spawn_fn=lambda *a, **k: 1)
    assert result.health_reconciliation == expected.to_dict()
    assert result.health_reconciliation["errors"] == [{"task_id": None, "error": "boom"}]


def test_board_enumeration_failure_is_not_silently_defaulted(monkeypatch):
    monkeypatch.setattr(kb, "list_boards", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("catalog down")))
    with pytest.raises(RuntimeError, match="catalog down"):
        kh.all_board_slugs()


def test_sentinel_alert_dedupe_is_transactional(board, monkeypatch, tmp_path):
    monkeypatch.setattr(ks, "_state_path", lambda: tmp_path / "sentinel-state.json")
    action = {"title": "one", "action": "do one thing", "evidence": {"task_ids": ["t_x"]}}
    reports = [ks.SentinelReport(generated_at=100, kevin_action=action) for _ in range(8)]
    barrier = threading.Barrier(len(reports))
    emitted = []

    def invoke(report):
        barrier.wait()
        emitted.append(ks._maybe_emit(report, 100, apply=True))

    threads = [threading.Thread(target=invoke, args=(report,)) for report in reports]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert emitted.count(True) == 1


def test_set_hold_rolls_back_status_when_typing_event_fails(board, monkeypatch):
    tid = _mk(board)
    original = kb._append_event

    def fail_typed(conn, task_id, kind, payload=None, **kwargs):
        if kind == "hold_typed":
            raise RuntimeError("event write failed")
        return original(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_typed)
    with pytest.raises(RuntimeError, match="event write failed"):
        kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) + 60)
    task = kb.get_task(board, tid)
    assert task.status == "ready"
    assert task.hold_kind is None


def test_reconcile_rolls_back_resume_when_audit_event_fails(board):
    tid = _mk(board)
    assert kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 1)
    board.execute(
        "CREATE TRIGGER reject_hold_resume BEFORE INSERT ON task_events "
        "WHEN NEW.kind='hold_resumed' BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
    )
    report = kh.reconcile_board(board)
    assert report.errors
    task = kb.get_task(board, tid)
    assert task.status == "scheduled"
    assert task.hold_kind == "wake"


def test_reconcile_classification_failure_replaces_fresh_ok_checkpoint(
    board, monkeypatch
):
    now = int(time.time())
    tid = _mk(board)
    assert kh.set_hold(board, tid, kind="dependency")
    kh.record_checkpoint(
        board, kh.CHECKPOINT_RECONCILE, status="ok", now=now
    )
    monkeypatch.setattr(
        kh,
        "_parents_done",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dependency read failed")),
    )

    report = kh.reconcile_board(board, now=now + 1)
    assert report.errors == [{"task_id": tid, "error": "dependency read failed"}]
    checkpoint = kh.read_checkpoint(board, kh.CHECKPOINT_RECONCILE)
    assert checkpoint["status"] == "failed"
    assert checkpoint["updated_at"] == now + 1


def test_dashboard_health_get_is_read_only_and_post_owns_reconcile():
    from plugins.kanban.dashboard import plugin_api

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in plugin_api.router.routes
    }
    assert ("/board-health", ("GET",)) in routes
    assert ("/board-health/reconcile", ("POST",)) in routes
    assert "reconcile" not in plugin_api.get_board_health.__annotations__


def test_update_canary_fails_closed_when_probe_crashes(tmp_path, monkeypatch):
    from hermes_cli import update_cmd
    from hermes_cli import kanban_capabilities as kc

    monkeypatch.setattr(kc, "preactivation_canary", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
    assert update_cmd._run_capability_canary(tmp_path, label="candidate") is False


def test_affirm_human_gate_is_atomic_when_audit_event_fails(board, monkeypatch):
    tid = _mk(board)
    original = kb._append_event

    def fail_gate(conn, task_id, kind, payload=None, **kwargs):
        if kind == "blocked":
            raise RuntimeError("gate audit failed")
        return original(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_gate)
    with pytest.raises(RuntimeError, match="gate audit failed"):
        kh.affirm_human_gate(board, tid, evidence=_evidence())
    task = kb.get_task(board, tid)
    assert task.status == "ready"
    assert task.gate_evidence is None


def test_dashboard_exposes_typed_gate_and_hold_mutation_routes():
    from plugins.kanban.dashboard import plugin_api

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in plugin_api.router.routes
    }
    assert ("/tasks/{task_id}/affirm-gate", ("POST",)) in routes
    assert ("/tasks/{task_id}/hold", ("POST",)) in routes
