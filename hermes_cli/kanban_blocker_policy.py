"""The native dispatcher publishes board policy for workers on other profiles."""
import json

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS blocker_reconciler_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config TEXT NOT NULL
);
"""


def board_config(conn):
    from hermes_cli.kanban_blocker_reconcile import _blocker_reconciler_config
    row = conn.execute("SELECT config FROM blocker_reconciler_policy WHERE id = 1").fetchone()
    return json.loads(row[0]) if row else _blocker_reconciler_config()


def sync_dispatcher_policy(conn):
    from hermes_cli.kanban_blocker_reconcile import _blocker_reconciler_config
    from hermes_cli.kanban_db_connect import write_txn
    config = _blocker_reconciler_config()
    encoded = json.dumps(config, sort_keys=True)
    with write_txn(conn):
        conn.execute(
            "INSERT INTO blocker_reconciler_policy (id, config) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET config = excluded.config "
            "WHERE config != excluded.config", (encoded,),
        )
        if not config['enabled']:
            conn.execute("DELETE FROM blocker_reconciler_pending")
