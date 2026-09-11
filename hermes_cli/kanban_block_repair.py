"""Operator repair for legacy unblock-loop escalations parked in intake."""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_db as kb


def repair_block_loop_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: str,
) -> bool:
    """Atomically restore a legacy triage escalation to sticky blocked.

    Return False only for an already repaired, still-blocked task. Refuse
    stale evidence or any execution ownership. The ordinary blocked audit
    event also keeps pre-escalation sticky readers from auto-promoting it.
    No run, claim, recurrence, blocker, or PR authority field is changed.
    """
    if not actor.strip() or not reason.strip():
        raise ValueError("repair requires a non-empty actor and reason")
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status not in {"triage", "blocked"}:
            raise ValueError("repair requires a legacy block-loop triage task")
        if any(value is not None for value in (
            task.claim_lock, task.claim_expires, task.worker_pid, task.current_run_id,
        )) or conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1", (task_id,),
        ).fetchone():
            raise ValueError("cannot repair a task with execution ownership or an open run")
        event = conn.execute(
            "SELECT id, kind, payload FROM task_events WHERE task_id=? AND kind IN ("
            "'block_loop_detected', 'blocked', 'unblocked', 'status', 'completed', "
            "'archived', 'claimed', 'dependency_wait', 'gave_up', 'review_requested', "
            "'changes_requested', 'review_reopened', 'specified', 'decomposed', 'imported'"
            ") ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        payload = kb._json_dict(event["payload"]) if event else {}
        if task.status == "blocked":
            if event and event["kind"] == "blocked" and payload.get("repair") == "block_loop_triage":
                return False
            raise ValueError("task is not a previously repaired legacy escalation")
        count = payload.get("recurrences")
        limit = payload.get("limit")
        if (
            not event or event["kind"] != "block_loop_detected"
            or type(count) is not int or type(limit) is not int
            or limit < 1 or count < limit or count != task.block_recurrences
            or payload.get("kind") != task.block_kind
            or task.block_kind == "dependency"
            or task.block_kind not in kb.VALID_BLOCK_KINDS | {None}
            or "reason" not in payload
        ):
            raise ValueError("missing, stale, or inconsistent block-loop evidence")
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "blocked", {
            **payload, "repair": "block_loop_triage", "actor": actor.strip(),
            "repair_reason": reason.strip(), "escalation_event_id": event["id"],
            "from_status": "triage", "status": "blocked",
        })
    kb.notify_task_updated(conn, task_id, ("status",))
    return True


def cmd_repair_block_loop(args) -> int:
    """Operator CLI adapter; normal board routing is owned by kanban_command."""
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        changed = repair_block_loop_task(
            conn, args.task_id, actor=args.actor, reason=args.reason,
        )
    print(f"{args.task_id} → blocked" if changed else f"{args.task_id} already repaired; remains blocked")
    return 0
