"""Machine-stalled Kanban work must obtain a real recovery owner.

Regression lineage: the exact-occurrence blocker reconciler that shipped in
40b307357d (``enqueue_blocker_reconciliation`` /
``reconcile_orphaned_automation_recovery``) is absent from the current tree.
``block_task`` still parks a machine/worker block in ``triage`` and emits an
``automation_recovery_requested`` occurrence, but nothing consumes it: the
maintained reconcile/dispatch path produces detection only and no forward
owner.

Everything here runs against a throwaway ``HERMES_HOME``/board. No probe ever
touches a live board.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


RECONCILER_CONFIG = {
    "kanban": {
        "blocker_reconciler": {
            "enabled": True,
            "profile": "code-crab",
            "max_active": 2,
        }
    }
}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME + board with the reconciler lane enabled."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
    ):
        monkeypatch.delenv(var, raising=False)

    import hermes_cli.config as config_module

    monkeypatch.setattr(
        config_module, "load_config", lambda *a, **k: json.loads(
            json.dumps(RECONCILER_CONFIG)
        )
    )
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _machine_stall(conn, *, title="stalled source", kind="transient"):
    """A worker/machine block: the exact shape ``block_task`` parks in triage."""
    tid = kb.create_task(
        conn, title=title, assignee="alice", initial_status="running",
        body="relationship_mode: internal\nprincipal: Kevin Yan\n",
    )
    assert kb.block_task(conn, tid, reason="provider timed out", kind=kind) is True
    return tid


def _occurrence(conn, task_id):
    rows = conn.execute(
        "SELECT id, run_id FROM task_events WHERE task_id=? "
        "AND kind='automation_recovery_requested' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchall()
    assert rows, "block_task did not emit an automation_recovery_requested occurrence"
    return int(rows[0]["id"]), rows[0]["run_id"]


def _owners(conn, source_id):
    """Every recovery owner bound to ``source_id`` by its reserved key."""
    return conn.execute(
        "SELECT id, idempotency_key, status, assignee, max_runtime_seconds "
        "FROM tasks WHERE idempotency_key LIKE ? ORDER BY created_at, id",
        (f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}%:{source_id}:%",),
    ).fetchall()


# ---------------------------------------------------------------------------
# The regression: the occurrence is emitted, and nothing owns it.
# ---------------------------------------------------------------------------


def test_a_machine_stall_emits_an_occurrence_with_no_consumer(board):
    """RED: block_task parks the card in triage and files the occurrence, but
    the maintained reconcile/dispatch path leaves it with no forward owner."""
    source_id = _machine_stall(board)

    task = kb.get_task(board, source_id)
    assert task.status == "triage"
    event_id, run_id = _occurrence(board, source_id)
    assert run_id is not None, "occurrence must carry the exact source run"

    # The consumer that has to exist for the occurrence to mean anything.
    owner_id = kb.enqueue_blocker_reconciliation(board, event_id)
    assert owner_id is not None, "the committed occurrence obtained no recovery owner"

    owners = _owners(board, source_id)
    assert len(owners) == 1
    assert owners[0]["idempotency_key"].endswith(f":{source_id}:{event_id}")
    assert owners[0]["status"] in ("ready", "todo")
    assert owners[0]["max_runtime_seconds"], "the owner must be bounded"


def test_the_occurrence_owner_is_created_inside_the_blocking_transaction(board):
    """The owner and the occurrence commit together — no tick required."""
    source_id = _machine_stall(board, title="atomic stall")
    assert _owners(board, source_id), (
        "block_task committed an occurrence with no owner in the same txn"
    )


def test_the_maintained_dispatch_tick_backfills_a_stranded_occurrence(board):
    """A pre-hook dispatcher can commit the occurrence without an owner; the
    bounded backfill pass must adopt it on an ordinary tick."""
    source_id = _machine_stall(board, title="stranded stall")
    for row in _owners(board, source_id):
        board.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    board.commit()
    board.execute(
        "UPDATE tasks SET recovery_backfill_pending=1 WHERE id=?", (source_id,)
    )
    board.commit()

    recovered = kb.reconcile_orphaned_automation_recovery(board)
    assert recovered, "the stranded occurrence was never backfilled"
    assert len(_owners(board, source_id)) == 1
