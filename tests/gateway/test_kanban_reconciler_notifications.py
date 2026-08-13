from __future__ import annotations

from gateway.kanban_watchers import (
    current_human_gate_event,
    should_notify_kanban_event,
)
from hermes_cli import kanban_db as kb


def test_raw_recovery_events_never_notify_even_when_reconciler_disabled() -> None:
    for kind in ("blocked", "gave_up", "crashed", "timed_out", "block_loop_detected"):
        assert not should_notify_kanban_event(kind, {}, reconciler_enabled=False)
    assert not should_notify_kanban_event(
        "reconciliation_outcome",
        {"outcome": "genuine_human_gate"},
        reconciler_enabled=False,
    )
    assert should_notify_kanban_event(
        "human_gate_affirmed",
        {"attention_owner": "Kevin Yan", "human_action": "Approve once.",
         "why_automation_cannot_perform": "Personal authorization.",
         "current_evidence": "Current policy requires approval."},
        reconciler_enabled=False,
    )


def test_reconciler_suppresses_raw_recovery_events() -> None:
    for kind in (
        "blocked",
        "block_loop_detected",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
        "protocol_violation",
        "rate_limited",
    ):
        assert not should_notify_kanban_event(kind, {}, reconciler_enabled=True)


def test_only_affirmed_human_gate_outcome_notifies() -> None:
    for outcome in (
        "cleared/resumed",
        "continuation_created",
        "dependency_wait",
        "backoff_scheduled",
        "reconciliation_failed",
    ):
        assert not should_notify_kanban_event(
            "reconciliation_outcome",
            {"outcome": outcome},
            reconciler_enabled=True,
        )
    assert not should_notify_kanban_event(
        "reconciliation_outcome",
        {"outcome": "genuine_human_gate"},
        reconciler_enabled=True,
    )
    assert should_notify_kanban_event(
        "human_gate_affirmed",
        {"attention_owner": "Kevin Yan", "human_action": "Approve once.",
         "why_automation_cannot_perform": "Personal authorization.",
         "current_evidence": "Current policy requires approval."},
        reconciler_enabled=True,
    )
    assert should_notify_kanban_event("completed", {}, reconciler_enabled=True)


def test_human_gate_notification_is_suppressed_after_source_advances(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="current gate")
        assert kb.block_task(conn, task_id, reason="preflight", kind="needs_input")
        assert kb.affirm_human_gate(
            conn,
            task_id,
            attention_owner="Kevin Yan",
            human_action="Approve once",
            why_automation_cannot_perform="Personal authorization",
            current_evidence="Current policy requires approval",
            affirmed_by="operator:test",
        )
        gate = [event for event in kb.list_events(conn, task_id) if event.kind == "human_gate_affirmed"][-1]

    assert current_human_gate_event(task_id, gate.id)
    with kb.connect_closing() as conn:
        assert kb.unblock_task(conn, task_id)
    assert not current_human_gate_event(task_id, gate.id)
