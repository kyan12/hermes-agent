"""Bind recovery-owned source comments and links to native task/run/occurrence identity."""
from __future__ import annotations

import os


def source_write_evidence(conn, source_id):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_blocker_reconcile import _reconciliation_source_from_key
    from hermes_cli.kanban_blocker_outcomes import _latest_reconciliation_source_event_id
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if not task_id or not run_id.isdecimal():
        return None
    recovery = kb.get_task(conn, task_id)
    if recovery is None or recovery.status != "running" or recovery.current_run_id != int(run_id):
        return None
    assigned_source, original = _reconciliation_source_from_key(recovery)
    if assigned_source != source_id or original is None:
        return None
    latest, status = _latest_reconciliation_source_event_id(conn, source_id, task_id, original)
    if status is None:
        return None
    return {"task_id": task_id, "run_id": int(run_id), "profile": recovery.assignee,
            "source_task_id": source_id, "source_event_id": latest}


def matches(conn, payload, recovery_id, source_id, source_event_id, evidence_event_id):
    evidence = payload.get("reconciliation_evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "task_id", "run_id", "profile", "source_task_id", "source_event_id",
    }:
        return False
    if (evidence["task_id"] != recovery_id or evidence["source_task_id"] != source_id
            or type(evidence["source_event_id"]) is not int or evidence["source_event_id"] != source_event_id
            or type(evidence["run_id"]) is not int):
        return False
    run = conn.execute(
        "SELECT r.ended_at, r.outcome, t.status, t.current_run_id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.id = ? AND r.task_id = ? AND t.assignee = ?",
        (evidence["run_id"], recovery_id, evidence["profile"]),
    ).fetchone()
    if run is None:
        return False
    claimed = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'claimed'",
        (recovery_id, evidence["run_id"]),
    ).fetchone()[0]
    if claimed is None or evidence_event_id <= claimed:
        return False
    if run["ended_at"] is None:
        # Completion validates again after its task CAS, before closing the run.
        return run["current_run_id"] == evidence["run_id"]
    # Event ordering survives frozen clocks and proves the write preceded the
    # native run closure; a copied stamp after that closure has no authority.
    terminal = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ?",
        (recovery_id, evidence["run_id"], run["outcome"]),
    ).fetchone()[0]
    return terminal is not None and evidence_event_id < terminal
