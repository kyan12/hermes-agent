"""Blocker regressions from the independent exact-head review.

Each section pins one concrete way the restored exact-occurrence recovery lane
could still lose authority, lose the forward path, or let an unrelated actor
mutate a source:

1. the auto-decomposer must never see a recovery source;
2. the reserved occurrence key is unique at the database boundary, in-transaction;
3. a dead owner never absorbs a new occurrence;
4. only provenance-bound owner writes are exempt from the stale-source guard;
5. an outcome requires the owner's own live claim and reconciler provenance;
6. the kill switch is rechecked inside the outcome transaction;
7. an unspawnable configured profile never mints a dead-end owner;
8. a stale or exhausted owner still leaves the source a forward path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


ENABLED = {"enabled": True, "profile": "code-crab", "max_active": 2}


@pytest.fixture
def set_reconciler(monkeypatch):
    import hermes_cli.config as config_module

    state = {"cfg": {"kanban": {"blocker_reconciler": dict(ENABLED)}}}
    monkeypatch.setattr(
        config_module, "load_config",
        lambda *a, **k: json.loads(json.dumps(state["cfg"])),
    )

    def _set(**overrides):
        merged = dict(ENABLED)
        merged.update(overrides)
        state["cfg"] = {"kanban": {"blocker_reconciler": merged}}

    return _set


@pytest.fixture
def spawnable(monkeypatch):
    """The configured recovery profile exists on this host."""
    from hermes_cli import profiles as profiles_module

    monkeypatch.setattr(
        profiles_module, "profile_exists",
        lambda name: str(name).strip().lower() in {"code-crab", "alice", "default"},
    )


@pytest.fixture
def board(tmp_path, monkeypatch, set_reconciler, spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _stall(conn, *, title="stalled", kind="transient", **kw):
    tid = kb.create_task(conn, title=title, assignee="alice", body="src body", **kw)
    assert kb.claim_task(conn, tid, claimer="alice") is not None
    assert kb.block_task(conn, tid, reason="provider timed out", kind=kind)
    return tid


def _owners(conn, source_id):
    return conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key LIKE ? "
        "ORDER BY created_at, id",
        (f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}%:{source_id}:%",),
    ).fetchall()


def _occurrence(conn, task_id):
    row = conn.execute(
        "SELECT MAX(id) AS id FROM task_events WHERE task_id=? AND kind IN "
        "('automation_recovery_requested','gave_up','block_loop_detected')",
        (task_id,),
    ).fetchone()
    return int(row["id"])


def _later_occurrence(conn, task_id, kind="gave_up"):
    with kb.write_txn(conn):
        return kb._append_event(conn, task_id, kind, {"reason": "still failing"})


def _outcomes(conn, source_id):
    return [
        e.payload or {} for e in kb.list_events(conn, source_id)
        if e.kind == "reconciliation_outcome"
    ]


def _claim_owner(conn, owner_id):
    owner = kb.claim_task(conn, owner_id, claimer="code-crab")
    assert owner is not None
    return owner


def _verdict(source_id, event_id, **extra):
    out = {
        "outcome": "cleared/resumed",
        "source_task_id": source_id,
        "source_event_id": event_id,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 1 — the auto-decomposer must never see a recovery source
# ---------------------------------------------------------------------------


def test_a_recovery_source_is_excluded_from_the_auto_decomposer(board, monkeypatch):
    """Otherwise the LLM specifier rewrites the source's approval envelope."""
    from hermes_cli import kanban_decompose as kd

    source_id = _stall(board, title="AV Chemist canary")
    intake_id = kb.create_task(board, title="fresh intake", assignee="alice", triage=True)
    assert kb.get_task(board, source_id).status == "triage"

    monkeypatch.setattr(kb, "connect_closing", lambda *a, **k: _NoCloseConn(board))
    ids = kd.list_triage_ids()
    assert intake_id in ids, "fresh intake must still be decomposable"
    assert source_id not in ids, "a machine-recovery source was fed to the decomposer"


def test_the_kernel_names_a_recovery_source_explicitly(board):
    source_id = _stall(board)
    intake_id = kb.create_task(board, title="intake", assignee="alice", triage=True)
    assert kb.is_automation_recovery_source(board, source_id) is True
    assert kb.is_automation_recovery_source(board, intake_id) is False


class _NoCloseConn:
    """Context manager yielding an existing connection without closing it."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# 2 — the reserved occurrence key is unique at the database boundary
# ---------------------------------------------------------------------------


def test_the_occurrence_key_is_unique_at_the_database_boundary(board):
    source_id = _stall(board)
    owner = _owners(board, source_id)[0]
    key = board.execute(
        "SELECT idempotency_key FROM tasks WHERE id=?", (owner["id"],)
    ).fetchone()["idempotency_key"]

    with pytest.raises(sqlite3.IntegrityError):
        board.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
            "idempotency_key) VALUES ('t_dupe', 'dupe', 'ready', ?, 'scratch', ?)",
            (int(time.time()), key),
        )
    board.rollback()


def test_two_connections_racing_the_same_occurrence_yield_one_owner(board, tmp_path):
    source_id = _stall(board)
    for row in _owners(board, source_id):
        board.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    board.commit()
    event_id = _occurrence(board, source_id)

    second = kb.connect()
    try:
        a = kb.enqueue_blocker_reconciliation(board, event_id)
        b = kb.enqueue_blocker_reconciliation(second, event_id)
    finally:
        second.close()
    assert a is not None and a == b
    assert len(_owners(board, source_id)) == 1


def test_migration_retires_preexisting_duplicate_owners(tmp_path, monkeypatch, set_reconciler):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = tmp_path / "legacy.db"

    conn = kb.connect(db)
    key = f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}default:t_source:42"
    now = int(time.time())
    # A database migrated from a version without the unique constraint.
    conn.execute("DROP INDEX IF EXISTS idx_tasks_recovery_owner_key")
    for tid in ("t_dup1", "t_dup2"):
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind, "
            "idempotency_key) VALUES (?, ?, 'ready', ?, 'scratch', ?)",
            (tid, tid, now, key),
        )
    conn.commit()
    conn.close()
    # A real deployment reopens the migrated DB in a fresh process; drop the
    # per-process "already initialized" cache so the migration actually runs.
    kb._INITIALIZED_PATHS.clear()

    conn = kb.connect(db)
    try:
        dispatchable = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key=? "
            "AND status NOT IN ('archived','done')",
            (key,),
        ).fetchall()
        assert len(dispatchable) == 1, "duplicate owners stayed dispatchable"
        indexes = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_tasks_recovery_owner_key" in indexes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3 — a dead owner never absorbs a new occurrence
# ---------------------------------------------------------------------------


def test_a_dead_owner_does_not_absorb_a_new_occurrence(board):
    source_id = _stall(board)
    dead_id = _owners(board, source_id)[0]["id"]
    _claim_owner(board, dead_id)
    board.execute(
        "UPDATE tasks SET worker_pid=?, claim_expires=? WHERE id=?",
        (2 ** 22 - 1, int(time.time()) + 3600, dead_id),
    )
    board.commit()
    assert kb.active_recovery_owner(board, source_id) is None

    _later_occurrence(board, source_id)
    live = kb.active_recovery_owner(board, source_id)
    assert live is not None and live != dead_id, (
        "the new occurrence was coalesced onto a dead owner"
    )


def test_an_expired_claim_owner_does_not_absorb_a_new_occurrence(board):
    source_id = _stall(board)
    stale_id = _owners(board, source_id)[0]["id"]
    _claim_owner(board, stale_id)
    board.execute(
        "UPDATE tasks SET claim_expires=? WHERE id=?",
        (int(time.time()) - 5, stale_id),
    )
    board.commit()

    _later_occurrence(board, source_id)
    live = kb.active_recovery_owner(board, source_id)
    assert live is not None and live != stale_id


def test_a_genuinely_live_owner_still_coalesces(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    _claim_owner(board, owner_id)

    _later_occurrence(board, source_id)
    assert [r["id"] for r in _owners(board, source_id)] == [owner_id]


# ---------------------------------------------------------------------------
# 4 — only provenance-bound owner writes are exempt from the stale guard
# ---------------------------------------------------------------------------


def test_an_operator_comment_on_the_source_is_material_advancement(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    kb.add_comment(board, source_id, "kevin", "hold on, do not resume this")

    with pytest.raises(ValueError, match="advanced"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )
    assert kb.get_task(board, source_id).status == "triage"


def test_an_unrelated_link_on_the_source_is_material_advancement(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)
    unrelated = kb.create_task(board, title="unrelated parent", assignee="alice")
    kb.link_tasks(board, unrelated, source_id)

    with pytest.raises(ValueError, match="advanced"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )


def test_the_owners_own_provenance_bound_evidence_is_not_advancement(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    assert kb.add_recovery_evidence_comment(
        board, source_id,
        recovery_task_id=owner_id,
        run_id=owner.current_run_id,
        claim_lock=owner.claim_lock,
        source_event_id=event_id,
        body="Re-read the source: the provider quota window has reset.",
    )
    assert kb.complete_task(
        board, owner_id, result="x", summary="done",
        metadata={"reconciliation": _verdict(source_id, event_id)},
        expected_run_id=owner.current_run_id,
    )
    assert kb.get_task(board, source_id).status == "ready"


def test_evidence_writes_require_the_owners_live_claim(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    assert kb.add_recovery_evidence_comment(
        board, source_id, recovery_task_id=owner_id,
        run_id=owner.current_run_id, claim_lock="forged-lock",
        source_event_id=event_id, body="not mine",
    ) is False
    assert kb.add_recovery_evidence_comment(
        board, source_id, recovery_task_id=owner_id,
        run_id=int(owner.current_run_id) + 99, claim_lock=owner.claim_lock,
        source_event_id=event_id, body="not mine",
    ) is False


def test_a_provenance_bound_continuation_link_is_accepted(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)
    cont = kb.create_task(board, title="continuation", assignee="alice")

    assert kb.link_recovery_parent(
        board, parent_id=cont, child_id=source_id,
        recovery_task_id=owner_id, run_id=owner.current_run_id,
        claim_lock=owner.claim_lock, source_event_id=event_id,
    )
    assert kb.complete_task(
        board, owner_id, result="x", summary="done",
        metadata={"reconciliation": {
            "outcome": "continuation_created",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "continuation_task_id": cont,
        }},
        expected_run_id=owner.current_run_id,
    )
    assert kb.get_task(board, source_id).status == "todo"


# ---------------------------------------------------------------------------
# 5 — outcome authority is the owner's own live claim
# ---------------------------------------------------------------------------


def test_an_unclaimed_ready_owner_cannot_report_an_outcome(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)

    with pytest.raises(ValueError, match="live claim|running"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
        )
    assert kb.get_task(board, source_id).status == "triage"


def test_an_outcome_without_the_callers_run_id_is_refused(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    _claim_owner(board, owner_id)

    with pytest.raises(ValueError, match="live claim|run"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
        )


def test_an_outcome_with_a_foreign_run_id_is_refused(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    with pytest.raises(ValueError, match="live claim|run"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=int(owner.current_run_id) + 99,
        )


def test_an_owner_with_an_expired_claim_cannot_report_an_outcome(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)
    board.execute(
        "UPDATE tasks SET claim_expires=? WHERE id=?",
        (int(time.time()) - 5, owner_id),
    )
    board.commit()

    with pytest.raises(ValueError, match="live claim"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )


def test_a_forged_owner_provenance_cannot_report_an_outcome(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)
    board.execute(
        "UPDATE tasks SET created_by='attacker', assignee='attacker' WHERE id=?",
        (owner_id,),
    )
    board.commit()

    with pytest.raises(ValueError, match="authorized"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )


# ---------------------------------------------------------------------------
# 6 — the kill switch is rechecked inside the outcome transaction
# ---------------------------------------------------------------------------


def test_the_kill_switch_is_rechecked_under_the_outcome_transaction(
    board, monkeypatch
):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    calls = {"n": 0}
    real = kb.blocker_reconciler_enabled

    def _flip():
        calls["n"] += 1
        # Enabled through validation, disabled by the time the write lock is
        # taken — the classic TOCTOU on an operator kill switch.
        return calls["n"] <= 1 and real()

    monkeypatch.setattr(kb, "blocker_reconciler_enabled", _flip)
    with pytest.raises(ValueError, match="disabled"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )
    monkeypatch.undo()
    assert kb.get_task(board, source_id).status == "triage"
    assert kb.get_task(board, owner_id).status != "done"


# ---------------------------------------------------------------------------
# 7 — an unspawnable configured profile never mints a dead-end owner
# ---------------------------------------------------------------------------


def test_an_unspawnable_recovery_profile_never_mints_a_dead_end_owner(
    board, set_reconciler
):
    set_reconciler(profile="ghost-profile")
    source_id = _stall(board, title="ghost lane")

    assert not _owners(board, source_id), "an unspawnable owner would never run"
    deferred = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "reconciliation_enqueue_deferred"
    ]
    assert deferred and deferred[-1]["reason"] == "unspawnable_recovery_profile"
    # The source stays armed so a later tick retries once the lane is fixed.
    assert board.execute(
        "SELECT recovery_backfill_pending FROM tasks WHERE id=?", (source_id,)
    ).fetchone()["recovery_backfill_pending"] == 1


def test_the_lane_recovers_once_the_profile_becomes_spawnable(board, set_reconciler):
    set_reconciler(profile="ghost-profile")
    source_id = _stall(board, title="ghost lane")
    assert not _owners(board, source_id)

    set_reconciler(profile="code-crab")
    assert kb.reconcile_orphaned_automation_recovery(board)
    assert len(_owners(board, source_id)) == 1


# ---------------------------------------------------------------------------
# 8 — a stale or exhausted owner still leaves the source a forward path
# ---------------------------------------------------------------------------


def test_a_stale_outcome_rearms_the_source_for_a_new_owner(board):
    source_id = _stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id = _occurrence(board, source_id)
    owner = _claim_owner(board, owner_id)

    # The verdict validates, then the source materially advances before the
    # write lock is taken. The owner settles, but the source must not stop.
    real_apply = kb._apply_reconciliation_completion

    def _advance_then_apply(conn, recovery_task_id, verdict):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'edited', '{}', ?)",
            (source_id, int(time.time())),
        )
        return real_apply(conn, recovery_task_id, verdict)

    kb._apply_reconciliation_completion = _advance_then_apply
    try:
        kb.complete_task(
            board, owner_id, result="x", summary="done",
            metadata={"reconciliation": _verdict(source_id, event_id)},
            expected_run_id=owner.current_run_id,
        )
    finally:
        kb._apply_reconciliation_completion = real_apply

    payload = _outcomes(board, source_id)[-1]
    assert payload.get("stale") is True
    assert board.execute(
        "SELECT recovery_backfill_pending FROM tasks WHERE id=?", (source_id,)
    ).fetchone()["recovery_backfill_pending"] == 1, (
        "a discarded verdict left the source with no owner and no re-arm"
    )

    _later_occurrence(board, source_id)
    assert kb.reconcile_orphaned_automation_recovery(board)
    assert kb.active_recovery_owner(board, source_id) is not None


def test_bounded_exhaustion_surfaces_one_precise_action(board):
    source_id = _stall(board)
    for _ in range(kb.RECONCILIATION_SOURCE_FAILURE_LIMIT + 1):
        owner_id = kb.active_recovery_owner(board, source_id)
        if owner_id is None:
            break
        owner = _claim_owner(board, owner_id)
        assert kb.complete_task(
            board, owner_id, result="x", summary="done",
            metadata={"reconciliation": _verdict(
                source_id, _occurrence(board, source_id)
            )},
            expected_run_id=owner.current_run_id,
        )
        if kb.get_task(board, source_id).status not in ("ready", "todo"):
            break
        assert kb.claim_task(board, source_id, claimer="alice") is not None
        assert kb.block_task(board, source_id, reason="again", kind="transient")

    exhausted = [
        p for p in _outcomes(board, source_id)
        if p.get("fallback") == "automation_exhausted"
    ]
    assert exhausted
    surfaced = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "recovery_human_action"
    ]
    assert surfaced, "bounded exhaustion left the source silently stopped"
    assert surfaced[-1]["affirmed"] is False
    assert surfaced[-1]["human_action"]
    # Still not an affirmed gate: only the operator boundary may create one.
    assert kb.get_task(board, source_id).status == "triage"
    assert kb.get_task(board, source_id).gate_evidence is None


def test_continuation_outcome_links_owner_created_child_without_private_kernel_call(board):
    """A real recovery worker only has create + complete, not private DB helpers."""
    source_id = _stall(board)
    event_id = _occurrence(board, source_id)
    owner_id = kb.active_recovery_owner(board, source_id)
    owner = _claim_owner(board, owner_id)
    continuation_id = kb.create_task(
        board,
        title="bounded continuation",
        assignee="code-crab",
        parents=[owner_id],
        created_by="code-crab",
    )

    assert kb.complete_task(
        board,
        owner_id,
        result="continuation prepared",
        metadata={"reconciliation": {
            "outcome": "continuation_created",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "continuation_task_id": continuation_id,
        }},
        expected_run_id=owner.current_run_id,
    )
    assert board.execute(
        "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
        (continuation_id, source_id),
    ).fetchone() is not None
    assert kb.get_task(board, source_id).status == "todo"


def test_completed_desktop_session_binding_is_scrubbed_at_context_boundary(board, monkeypatch):
    """A follow-up session must not inherit dead worker authority or warnings."""
    task_id = kb.create_task(board, title="finished desktop worker", assignee="code-crab")
    claimed = kb.claim_task(board, task_id, claimer="desktop-worker")
    assert claimed is not None
    run_id = claimed.current_run_id
    claim_lock = claimed.claim_lock
    assert kb.complete_task(board, task_id, summary="done", expected_run_id=run_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(claim_lock))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))

    from agent.delegation_context import scrub_stale_dispatcher_worker_env

    assert scrub_stale_dispatcher_worker_env() is True
    assert "HERMES_KANBAN_TASK" not in __import__("os").environ
    from agent.kanban_stop import build_kanban_stop_nudge
    assert build_kanban_stop_nudge(messages=[]) is None
