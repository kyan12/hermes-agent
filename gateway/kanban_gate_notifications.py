"""Resolve claimed notices against a live source generation immediately before delivery."""
from __future__ import annotations


def revalidate(board, sub, events):
    from gateway.kanban_watchers_notifier import should_notify_kanban_event
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli import kanban_blocker_reconcile as recovery

    with connect_closing(board=board) as conn:
        task = kb.get_task(conn, sub["task_id"])
        gate = recovery.current_human_gate(conn, sub["task_id"])
        from hermes_cli.kanban_blocker_policy import board_config
        enabled = board_config(conn)["enabled"]
        managed = recovery.managed_source(conn, sub["task_id"])
        is_recovery = task and (task.idempotency_key or '').startswith(recovery.RECONCILIATION_IDEMPOTENCY_PREFIX)
        row = conn.execute(
            "SELECT last_ping_event_id FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or ""),
        ).fetchone()
        ping = row[0] if row else 0
        result, seen = [], set()
        for event in events:
            canonical = None
            if event.kind == "reconciliation_outcome":
                if gate and (event.payload or {}).get("source_event_id") == gate.id:
                    canonical = gate
            elif event.kind in recovery.RECONCILIATION_EVENT_KINDS:
                if gate and event.id == gate.id:
                    canonical = gate
                elif should_notify_kanban_event(
                    event.kind, event.payload, reconciler_enabled=bool(enabled or managed or is_recovery),
                ):
                    canonical = event
            else:
                canonical = event
            if canonical is not None and canonical.id not in seen:
                seen.add(canonical.id)
                result.append(canonical)
        return task, result, ping
