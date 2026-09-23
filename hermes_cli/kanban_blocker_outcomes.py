"""Event-driven blocker recovery on the native Kanban dispatcher.

Forward-ported from 3da22247; source failures and their recovery are one transaction.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli.kanban_blocker_reconcile import (
    RECONCILIATION_EVENT_KINDS, RECONCILIATION_IDEMPOTENCY_PREFIX,
    _reconciliation_source_from_key, _redact_reconciliation_text,
)

RECONCILIATION_OUTCOMES = frozenset({
    "cleared/resumed", "continuation_created", "dependency_wait", "backoff_scheduled",
    "genuine_human_gate", "reconciliation_failed",
})

def _latest_reconciliation_source_event_id(
    conn: sqlite3.Connection,
    source_task_id: str,
    recovery_task_id: str,
    original_event_id: int,
) -> tuple[int, Optional[str]]:
    """Return the newest coalesced occurrence and its assigned source state."""
    from hermes_cli.kanban_db import list_events
    latest = original_event_id
    source_status: Optional[str] = None
    for event in list_events(conn, source_task_id):
        if event.kind not in {"reconciliation_enqueued", "reconciliation_coalesced"}:
            continue
        payload = event.payload or {}
        if payload.get("reconciliation_task_id") != recovery_task_id:
            continue
        raw_event_id = payload.get("source_event_id")
        if raw_event_id is None:
            continue
        try:
            latest = int(raw_event_id)
        except (TypeError, ValueError):
            continue
        raw_status = payload.get("source_status")
        source_status = raw_status if isinstance(raw_status, str) else None
    return latest, source_status


def _newer_reconciliation_source_event(
    conn: sqlite3.Connection, source_task_id: str, source_event_id: int, *,
    recovery_task_id: Optional[str] = None, ignored_event_ids: frozenset[int] = frozenset(),
) -> Optional[sqlite3.Row]:
    """Only exactly attributed recovery evidence may cross the source fence."""
    from hermes_cli.kanban_blocker_evidence import matches
    rows = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind NOT IN ('reconciliation_enqueued', 'reconciliation_coalesced', "
        "'heartbeat', 'claim_extended') ORDER BY id ASC",
        (source_task_id, source_event_id),
    ).fetchall()
    for row in rows:
        if row["id"] in ignored_event_ids:
            continue
        payload = json.loads(row["payload"] or '{}')
        if (recovery_task_id and row["kind"] in {"commented", "linked"}
                and matches(conn, payload, recovery_task_id, source_task_id, source_event_id, row["id"])):
            continue
        return row
    return None


def _reconcile_park_artifacts(
    conn: sqlite3.Connection, source_task_id: str, source_event_id: int,
    recovery_task_id: Optional[str] = None,
) -> tuple[frozenset[int], bool]:
    """Recognize one benign park-artifact chain after the pinned occurrence.

    A misfiring park (``kanban_block`` citing an unrelated reason, contradicting
    the controlling operator directive) leaves exactly two non-exempt source
    events: an evidence-free ``commented`` park note followed by the
    ``scheduled`` park itself. That pair is bookkeeping, not source progress, so
    it is exempt from the advance guard; when it is the only non-exempt flagged
    history it also drifted the source out of its expected status, which
    ``apply_completion`` may then settle from. Recovery-owned evidence comments
    and links are exempt through ``matches`` exactly as in
    ``_newer_reconciliation_source_event``. Any additional non-exempt flagged
    event (a genuine later blocker occurrence, a foreign comment or link)
    breaks the chain and nothing is exempted.
    """
    from hermes_cli.kanban_blocker_evidence import matches
    rows = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind NOT IN ('reconciliation_enqueued', 'reconciliation_coalesced', "
        "'heartbeat', 'claim_extended') ORDER BY id ASC",
        (source_task_id, source_event_id),
    ).fetchall()
    comment_id: Optional[int] = None
    park_id: Optional[int] = None
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        if (recovery_task_id and row["kind"] in {"commented", "linked"}
                and matches(conn, payload, recovery_task_id, source_task_id,
                            source_event_id, row["id"])):
            continue
        if row["kind"] == "commented" and comment_id is None and park_id is None:
            if not isinstance(payload.get("reconciliation_evidence"), dict):
                comment_id = row["id"]
                continue
            # Evidence-shaped but not provably recovery-owned: fail closed.
            return frozenset(), False
        if row["kind"] == "scheduled" and comment_id is not None and park_id is None:
            park_id = row["id"]
            continue
        return frozenset(), False
    if comment_id is None or park_id is None:
        return frozenset(), False
    return frozenset({comment_id, park_id}), True


def _required_reconciliation_text(
    reconciliation: Mapping[str, Any],
    field: str,
) -> str:
    value = reconciliation.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"reconciliation.{field} is required")
    return value.strip()


def validate_completion(
    conn: sqlite3.Connection,
    recovery_task_id: str,
    metadata: Optional[Mapping[str, Any]],
) -> Optional[dict[str, Any]]:
    """Validate and normalize a recovery worker's machine-readable verdict.

    Non-reconciliation tasks return ``None``. Recovery tasks fail closed: they
    cannot transition to done without a complete verdict for the newest source
    occurrence assigned to them.
    """
    from hermes_cli.kanban_db import get_task
    recovery = get_task(conn, recovery_task_id)
    if recovery is None or not (recovery.idempotency_key or "").startswith(
        RECONCILIATION_IDEMPOTENCY_PREFIX
    ):
        return None

    reconciliation = metadata.get("reconciliation") if isinstance(metadata, Mapping) else None
    if not isinstance(reconciliation, Mapping):
        raise ValueError("reconciliation metadata is required")

    outcome = _required_reconciliation_text(reconciliation, "outcome")
    if outcome not in RECONCILIATION_OUTCOMES:
        raise ValueError(
            "reconciliation.outcome must be one of " + ", ".join(sorted(RECONCILIATION_OUTCOMES))
        )
    outcome_field = {
        "cleared/resumed": None,
        "continuation_created": "continuation_task_id",
        "dependency_wait": "dependency_task_id",
        "backoff_scheduled": "resume_at",
        "genuine_human_gate": "human_action",
        "reconciliation_failed": "error",
    }[outcome]
    allowed_fields = {"outcome", "source_task_id", "source_event_id"}
    if outcome_field:
        allowed_fields.add(outcome_field)
    unexpected_fields = sorted(
        str(field) for field in reconciliation.keys() if field not in allowed_fields
    )
    if unexpected_fields:
        raise ValueError(
            "reconciliation contains unexpected fields for "
            f"{outcome}: {', '.join(unexpected_fields)}"
        )
    source_id = _required_reconciliation_text(reconciliation, "source_task_id")
    source_from_key, original_event_id = _reconciliation_source_from_key(recovery)
    if source_from_key != source_id or original_event_id is None:
        raise ValueError("reconciliation.source_task_id does not match recovery lineage")

    if not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'reconciliation_enqueued' "
        "AND json_extract(payload, '$.reconciliation_task_id') = ? "
        "AND json_extract(payload, '$.source_event_id') = ?",
        (source_id, recovery_task_id, original_event_id),
    ).fetchone():
        raise ValueError("reconciliation requires native enqueue provenance")
    raw_source_event_id = reconciliation.get("source_event_id")
    if type(raw_source_event_id) is not int:
        raise ValueError("reconciliation.source_event_id must be an integer")
    source_event_id = raw_source_event_id
    latest_event_id, expected_source_status = _latest_reconciliation_source_event_id(
        conn, source_id, recovery_task_id, original_event_id,
    )
    if source_event_id != latest_event_id:
        raise ValueError(
            "reconciliation.source_event_id is stale; "
            f"expected newest coalesced event {latest_event_id}"
        )
    source_event = conn.execute(
        "SELECT kind FROM task_events WHERE id = ? AND task_id = ?",
        (source_event_id, source_id),
    ).fetchone()
    if source_event is None or source_event["kind"] not in RECONCILIATION_EVENT_KINDS:
        raise ValueError("reconciliation.source_event_id is not a reconciliation occurrence")

    verdict: dict[str, Any] = {
        "outcome": outcome,
        "source_task_id": source_id,
        "source_event_id": source_event_id,
        "expected_source_status": expected_source_status,
    }
    parent_field = {"continuation_created": "continuation_task_id", "dependency_wait": "dependency_task_id"}.get(outcome)
    if parent_field:
        parent_id = _required_reconciliation_text(reconciliation, parent_field)
        if get_task(conn, parent_id) is None:
            raise ValueError(f"reconciliation.{parent_field} does not exist")
        if not conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, source_id),
        ).fetchone():
            raise ValueError(f"reconciliation.{parent_field} must be a linked parent of the source task")
        verdict[parent_field] = parent_id
    elif outcome == "backoff_scheduled":
        resume_at = reconciliation.get("resume_at")
        if type(resume_at) is not int or not int(time.time()) < resume_at <= int(time.time()) + 86400:
            raise ValueError("reconciliation.resume_at must be a future unix timestamp within 24 hours")
        verdict["resume_at"] = resume_at
    elif outcome in {"genuine_human_gate", "reconciliation_failed"}:
        field = {"genuine_human_gate": "human_action", "reconciliation_failed": "error"}[outcome]
        verdict[field] = _redact_reconciliation_text(_required_reconciliation_text(reconciliation, field), limit=1000)

    park_artifacts, drift_settlement = _reconcile_park_artifacts(
        conn, source_id, source_event_id, recovery_task_id=recovery_task_id,
    )
    newer_source_event = _newer_reconciliation_source_event(
        conn,
        source_id,
        source_event_id,
        recovery_task_id=recovery_task_id,
        ignored_event_ids=park_artifacts,
    )
    if newer_source_event is not None:
        raise ValueError(
            "reconciliation source advanced after source event "
            f"{source_event_id} via {newer_source_event['kind']}:{newer_source_event['id']}"
        )
    verdict["source_drifted_by_recorded_park_chain"] = drift_settlement
    return verdict


def apply_completion(conn: sqlite3.Connection, recovery_id: str, metadata: Any) -> None:
    """Revalidate inside the native completion transaction; never mint PR authority."""
    from hermes_cli import kanban_db as kb
    verdict = validate_completion(conn, recovery_id, metadata)
    if verdict is None:
        return
    source_id = verdict.pop("source_task_id")
    expected_status = verdict.pop("expected_source_status")
    drifted = bool(verdict.pop("source_drifted_by_recorded_park_chain", False))
    source = kb.get_task(conn, source_id)
    if drifted:
        # The only non-exempt post-occurrence history is the recorded benign
        # park-artifact chain, so the misfiring park itself moved the source
        # out of its expected status. The parked state is never legitimate
        # while an active run holds the source: settle nothing.
        if source is None or source.current_run_id:
            raise ValueError("reconciliation source advanced or still has an active run")
        if source.status != "scheduled":
            raise ValueError("reconciliation source drifted outside the recorded park chain")
    elif (source is None or source.status != expected_status
          or source.status != "blocked" or source.current_run_id):
        raise ValueError("reconciliation source advanced or still has an active run")
    outcome = verdict["outcome"]
    payload = {**verdict, "reconciliation_task_id": recovery_id}
    generations = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'reconciliation_outcome' "
        "AND json_extract(payload, '$.outcome') IN "
        "('cleared/resumed', 'continuation_created', 'dependency_wait', 'backoff_scheduled')",
        (source_id,),
    ).fetchone()[0]
    if outcome in {"cleared/resumed", "continuation_created", "dependency_wait", "backoff_scheduled"}:
        if generations >= 2:
            payload.update(outcome="reconciliation_failed", error="recovery generation limit reached", fallback="automation_exhausted")
        else:
            next_status = {
                "dependency_wait": "todo", "backoff_scheduled": "scheduled",
            }.get(outcome, resume_status(conn, source_id))
            conn.execute(
                "UPDATE tasks SET status = ?, block_kind = NULL, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?", (next_status, source_id),
            )
    elif outcome == "genuine_human_gate":
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (source_id,))
    kb._append_event(conn, source_id, "reconciliation_outcome", payload)


def release_backoffs(conn: sqlite3.Connection) -> int:
    """Use the existing dispatcher's recomputation; no additional timer or worker."""
    from hermes_cli import kanban_db as kb
    count = 0
    for row in conn.execute("SELECT id FROM tasks WHERE status = 'scheduled'").fetchall():
        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? "
            "AND kind NOT IN ('heartbeat', 'claim_extended', 'commented') ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if not event or event["kind"] != "reconciliation_outcome":
            continue
        payload = json.loads(event["payload"] or '{}')
        deadline = payload.get("resume_at")
        if payload.get("outcome") != "backoff_scheduled" or type(deadline) is not int or deadline > int(time.time()):
            continue
        status = resume_status(conn, row["id"])
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, row["id"]))
        kb._append_event(conn, row["id"], "backoff_elapsed", {"resume_at": deadline, "status": status})
        count += 1
    return count


def resume_status(conn, source_id):
    from hermes_cli import kanban_db as kb
    return kb._resume_status_from_events(conn, source_id) if kb._parents_satisfied(conn, source_id) else "todo"


def completion_replayed(conn, task_id, metadata, expected_run_id):
    """An exact settled native verdict is an idempotent no-op, even after archive."""
    if type(expected_run_id) is not int or not isinstance(metadata, Mapping):
        return False
    row = conn.execute(
        "SELECT r.metadata FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.id = ? AND r.task_id = ? AND r.outcome = 'completed' "
        "AND t.status IN ('done', 'archived') AND t.idempotency_key LIKE ?",
        (expected_run_id, task_id, RECONCILIATION_IDEMPOTENCY_PREFIX + '%'),
    ).fetchone()
    if row is None:
        return False
    saved = json.loads(row["metadata"] or '{}').get("reconciliation")
    incoming = metadata.get("reconciliation")
    if not isinstance(saved, dict) or json.dumps(saved, sort_keys=True) != json.dumps(incoming, sort_keys=True):
        return False
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'reconciliation_outcome' "
        "AND json_extract(payload, '$.reconciliation_task_id') = ? "
        "AND json_extract(payload, '$.source_event_id') = ?",
        (saved.get("source_task_id"), task_id, saved.get("source_event_id")),
    ).fetchone() is not None


def validate_run(conn, task_id, expected_run_id):
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if (type(expected_run_id) is not int or row is None or row['status'] != 'running'
            or row['current_run_id'] != expected_run_id):
        raise ValueError('reconciliation requires its current claimed run')
