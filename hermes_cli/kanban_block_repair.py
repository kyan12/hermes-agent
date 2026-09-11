"""Operator repair for legacy unblock-loop escalations parked in intake."""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_db as kb


def repair_block_loop_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: str,
) -> bool:
    """Atomically restore a legacy triage escalation to sticky blocked.

    Return False only for an exact retry (trimmed actor/reason) whose original
    escalation, audit facts, and still-blocked task remain consistent. Refuse
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
        event = _latest_lifecycle_event(conn, task_id)
        audit = None
        if task.status == "blocked":
            if event is None or event["kind"] != "blocked":
                raise ValueError("task is not a previously repaired legacy escalation")
            audit = kb._json_dict(event["payload"])
            reference = audit.get("escalation_event_id")
            # The referenced escalation must be the preceding lifecycle occurrence,
            # not a marker copied across an unblock, new run, or second repair.
            event = _latest_lifecycle_event(conn, task_id, before_id=event["id"])
            if type(reference) is not int or event is None or event["id"] != reference:
                raise ValueError("repair audit does not reference the preceding escalation")
        if event is None or event["kind"] != "block_loop_detected":
            raise ValueError("missing or stale block-loop escalation")
        payload = kb._json_dict(event["payload"])
        _validate_escalation_payload(payload, task)
        expected_audit = {
            **payload, "repair": "block_loop_triage", "actor": actor.strip(),
            "repair_reason": reason.strip(), "escalation_event_id": event["id"],
            "from_status": "triage", "status": "blocked",
        }
        if audit is not None:
            # JSON comparison preserves numeric types (Python equates 2.0 and 2).
            if json.dumps(audit, sort_keys=True) != json.dumps(expected_audit, sort_keys=True):
                raise ValueError("repair audit or retry identity differs from original evidence")
            return False
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "blocked", expected_audit)
    kb.notify_task_updated(conn, task_id, ("status",))
    return True


def _latest_lifecycle_event(conn, task_id, *, before_id=None):
    """Ignore commentary, but never bridge an intervening lifecycle occurrence."""
    return conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id=? "
        "AND (? IS NULL OR id < ?) AND kind IN ("
        "'created', 'block_loop_detected', 'blocked', 'unblocked', 'status', 'completed', "
        "'archived', 'claimed', 'dependency_wait', 'gave_up', 'review_requested', "
        "'changes_requested', 'review_reopened', 'specified', 'decomposed', 'imported', "
        "'promoted', 'promoted_manual', 'scheduled', 'reclaimed', 'reconciled', "
        "'spawned', 'spawn_failed', 'stale', 'timed_out', 'crashed', 'rate_limited', "
        "'descendant_invalidated', 'resumed_on_authority'"
        ") ORDER BY id DESC LIMIT 1", (task_id, before_id, before_id),
    ).fetchone()


def _validate_escalation_payload(payload: dict, task: kb.Task) -> None:
    """Validate the original typed blocker and counters before either success path."""
    count, limit = payload.get("recurrences"), payload.get("limit")
    kind = payload.get("kind")
    if (
        type(count) is not int or type(limit) is not int
        or limit < 1 or count < limit or count != task.block_recurrences
        or "kind" not in payload
        or (kind is not None and type(kind) is not str)
        or kind not in kb.VALID_BLOCK_KINDS | {None}
        or kind == "dependency" or kind != task.block_kind
        or "reason" not in payload
        or (payload["reason"] is not None and type(payload["reason"]) is not str)
    ):
        raise ValueError("missing, malformed, or inconsistent block-loop evidence")


def cmd_repair_block_loop(args) -> int:
    """Operator CLI adapter; normal board routing is owned by kanban_command."""
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        changed = repair_block_loop_task(
            conn, args.task_id, actor=args.actor, reason=args.reason,
        )
    print(f"{args.task_id} → blocked" if changed else f"{args.task_id} already repaired; remains blocked")
    return 0
