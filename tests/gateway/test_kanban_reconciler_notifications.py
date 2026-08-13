from __future__ import annotations

from gateway.kanban_watchers import should_notify_kanban_event


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
