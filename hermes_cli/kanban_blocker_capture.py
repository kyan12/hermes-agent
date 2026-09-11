"""Durable capture for pre-hook writers, drained by the sole native dispatcher."""
from __future__ import annotations

import json
import re

from hermes_cli.kanban_blocker_reconcile import RECONCILIATION_EVENT_KINDS

BATCH_SIZE = 16
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blocker_reconciler_pending (
    event_id INTEGER PRIMARY KEY REFERENCES task_events(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blocker_source_events ON task_events(task_id, kind, id);
"""
_TRIGGER_NAME = "blocker_reconcile_capture_v2"
_KINDS_SQL = ",".join("'" + kind + "'" for kind in sorted(RECONCILIATION_EVENT_KINDS))
_TRIGGER_SQL = f"""CREATE TRIGGER {_TRIGGER_NAME} AFTER INSERT ON task_events
WHEN NEW.kind IN ({_KINDS_SQL})
 AND COALESCE((SELECT json_extract(config, '$.enabled') FROM blocker_reconciler_policy WHERE id = 1), 0) = 1
 AND EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id
             AND COALESCE(idempotency_key, '') NOT LIKE 'kanban-reconcile:%')
BEGIN
 INSERT OR IGNORE INTO blocker_reconciler_pending(event_id) VALUES (NEW.id);
END"""


def install_capture(conn):
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_blocker_policy import board_config
    with write_txn(conn):
        # Install only AFTER legacy task_events PK rebuilding, so SQLite does
        # not retarget this FK to the temporary *_legacy table.
        for statement in SCHEMA_SQL.split(';'):
            if statement.strip():
                conn.execute(statement)
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (_TRIGGER_NAME,)).fetchone()
        normalize = lambda sql: re.sub(r'\s+', ' ', sql).strip().rstrip(';').lower()
        if row and normalize(row[0]) == normalize(_TRIGGER_SQL):
            return
        conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}")
        conn.execute(_TRIGGER_SQL)
        config = board_config(conn)
        if not config['enabled']:
            return
        # Initial opt-in can establish policy; never overwrite a durable kill switch.
        conn.execute("INSERT OR IGNORE INTO blocker_reconciler_policy(id, config) VALUES (1, ?)",
                     (json.dumps(config, sort_keys=True),))
        conn.execute(f"""
            INSERT OR IGNORE INTO blocker_reconciler_pending(event_id)
            SELECT MAX(e.id) FROM tasks t JOIN task_events e ON e.task_id = t.id
            WHERE t.status = 'blocked' AND t.current_run_id IS NULL
              AND COALESCE(t.idempotency_key, '') NOT LIKE 'kanban-reconcile:%'
              AND e.kind IN ({_KINDS_SQL})
              AND NOT EXISTS (SELECT 1 FROM task_events settled WHERE settled.task_id = t.id
                AND settled.kind IN ('reconciliation_enqueued', 'reconciliation_coalesced', 'reconciliation_outcome')
                AND json_extract(settled.payload, '$.source_event_id') = e.id)
            GROUP BY t.id
        """)


def drain_pending(conn):
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_blocker_policy import board_config
    from hermes_cli.kanban_blocker_reconcile import enqueue_blocker_reconciliation
    with write_txn(conn):
        if not board_config(conn)['enabled']:
            return []
        rows = conn.execute(
            "SELECT p.event_id, e.kind FROM blocker_reconciler_pending p "
            "JOIN task_events e ON e.id = p.event_id ORDER BY p.event_id LIMIT ?", (BATCH_SIZE,),
        ).fetchall()
        # These rows may come from DIFFERENT old transactions. Unlike the
        # native transaction hook, replay cannot assert a shared generation.
        # A later material occurrence supersedes an older pending row.
        assigned = []
        for row in rows:
            task_id = enqueue_blocker_reconciliation(conn, row['event_id'])
            if task_id and task_id not in assigned:
                assigned.append(task_id)
        return assigned
