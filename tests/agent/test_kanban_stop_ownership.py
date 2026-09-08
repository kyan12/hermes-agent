"""Stop nudges require live run ownership, not an inherited task label."""
import asyncio
import os
import sqlite3
import time

import pytest

from agent.kanban_stop import build_kanban_stop_nudge
from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context


# Column subset of the real board schema (hermes_cli/kanban_db.py) that the
# stop guard reads.  Written by hand rather than through ``kanban_db.connect``
# so the fixture cannot accidentally exercise the migrate-on-connect path the
# guard is forbidden to trigger.
BOARD_SCHEMA = """
CREATE TABLE tasks (
    id             TEXT PRIMARY KEY,
    status         TEXT,
    current_run_id INTEGER,
    claim_lock     TEXT,
    claim_expires  INTEGER
);
CREATE TABLE task_runs (
    id            INTEGER PRIMARY KEY,
    task_id       TEXT,
    status        TEXT,
    claim_lock    TEXT,
    claim_expires INTEGER,
    ended_at      INTEGER
);
"""

LOCK = "claim-lock-a"


def write_board(db, *, task="t_test", run_id=42, claim_lock=LOCK):
    """Create a board whose task ``task`` is live under run ``run_id``."""
    db.parent.mkdir(parents=True, exist_ok=True)
    expires = int(time.time()) + 3600
    with sqlite3.connect(db) as conn:
        conn.executescript(BOARD_SCHEMA)
        conn.execute(
            "INSERT INTO tasks VALUES (?, 'running', ?, ?, ?)",
            (task, run_id, claim_lock, expires),
        )
        conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, 'running', ?, ?, NULL)",
            (run_id, task, claim_lock, expires),
        )
    return db


@pytest.fixture
def board_root(tmp_path, monkeypatch):
    """Isolate kanban path resolution under a throwaway Hermes root."""
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    return root


@pytest.fixture
def owned_run(board_root, monkeypatch):
    """A genuine dispatcher worker: live task, live run, matching claim."""
    db = write_board(board_root / "kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", LOCK)
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    return db


# ── inherited / released identity ───────────────────────────────────────────

@pytest.mark.parametrize("status", ["done", "blocked", "review", "todo", "ready", "scheduled", "archived"])
def test_finished_or_released_run_cannot_override_reply(owned_run, status):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE tasks SET status=?, current_run_id=NULL", (status,))
    assert build_kanban_stop_nudge(messages=[]) is None


def test_reclaimed_run_cannot_nudge_new_owner(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE tasks SET current_run_id=43")
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("context", [delegated_child_context, non_dispatcher_owned_context])
def test_child_or_cron_cannot_inherit_parent_nudge(owned_run, context):
    with context():
        assert build_kanban_stop_nudge(messages=[]) is None
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_missing_database_does_not_create_one_or_assert_running(owned_run):
    owned_run.unlink()
    assert build_kanban_stop_nudge(messages=[]) is None
    assert not owned_run.exists()


def test_missing_task_does_not_assert_running(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("DELETE FROM tasks")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_live_worker_still_gets_bounded_nudge(owned_run):
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None and "t_test" in nudge
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None


@pytest.mark.parametrize("run_id", ["", "garbage", "0", "-1", "43"])
def test_no_nudge_without_exact_run_identity(owned_run, monkeypatch, run_id):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert build_kanban_stop_nudge(messages=[]) is None


def test_cannot_name_a_foreign_task_in_nudge(owned_run):
    assert build_kanban_stop_nudge(messages=[], task_id="t_other") is None


def test_explicit_task_id_matches_padded_env_identity(owned_run, monkeypatch):
    """A worker whose env carries stray whitespace keeps its own authority."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", " t_test\n")
    assert build_kanban_stop_nudge(messages=[], task_id="t_test") is not None


# ── session surface ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "source",
    ["desktop", "tui", "web", "telegram", "acp", "gateway", "cli", "tool", "api_server"],
)
def test_only_a_kanban_session_surface_may_nudge(owned_run, source):
    """Allowlist, not denylist: an unknown surface is never a worker turn."""
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(source=source)
    try:
        assert build_kanban_stop_nudge(messages=[]) is None
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("source", ["kanban", ""])
def test_worker_session_surface_keeps_nudge(owned_run, source):
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(source=source)
    try:
        assert build_kanban_stop_nudge(messages=[]) is not None
    finally:
        clear_session_vars(tokens)


# ── board identity ──────────────────────────────────────────────────────────

def test_board_pin_mismatch_denies_nudge(owned_run, board_root, monkeypatch):
    """A DB path that is not the pinned board's DB is not proof of ownership."""
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_board_pinned_worker_without_db_env_keeps_nudge(board_root, monkeypatch):
    """Board slug alone resolves the worker's board — authority is preserved."""
    db = write_board(board_root / "kanban" / "boards" / "other" / "kanban.db")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", LOCK)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    assert db.exists()
    assert build_kanban_stop_nudge(messages=[]) is not None


def test_unpinned_board_never_falls_back_to_operator_selection(board_root, monkeypatch):
    """Without a pinned board or DB there is no worker identity to verify."""
    write_board(board_root / "kanban.db")
    current = board_root / "kanban" / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text("default\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    assert build_kanban_stop_nudge(messages=[]) is None


def test_malformed_board_slug_denies_nudge(owned_run, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "../escape")
    assert build_kanban_stop_nudge(messages=[]) is None


# ── run row identity ────────────────────────────────────────────────────────

def test_run_owned_by_another_task_denies_nudge(owned_run):
    """The denormalised tasks pointer alone is not ownership evidence."""
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE task_runs SET task_id='t_other' WHERE id=42")
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("run_status", ["done", "blocked", "crashed", "timed_out", "failed", "released"])
def test_terminal_run_row_denies_nudge(owned_run, run_status):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE task_runs SET status=? WHERE id=42", (run_status,))
    assert build_kanban_stop_nudge(messages=[]) is None


def test_ended_run_row_denies_nudge(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE task_runs SET ended_at=? WHERE id=42", (int(time.time()),))
    assert build_kanban_stop_nudge(messages=[]) is None


def test_missing_run_row_denies_nudge(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("DELETE FROM task_runs")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_claim_authority_mismatch_denies_nudge(owned_run, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "foreign-claim-authority")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_reclaimed_board_claim_denies_nudge(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("UPDATE tasks SET claim_lock='claim-lock-b'")
        conn.execute("UPDATE task_runs SET claim_lock='claim-lock-b'")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_worker_without_claim_lock_env_cannot_assert_current_ownership(board_root, monkeypatch):
    """Exact current ownership requires the dispatcher's unforgeable claim."""
    db = write_board(board_root / "kanban.db", claim_lock=None)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("table", ["tasks", "task_runs"])
def test_expired_claim_denies_nudge(owned_run, table):
    with sqlite3.connect(owned_run) as conn:
        conn.execute(
            f"UPDATE {table} SET claim_expires=?", (int(time.time()) - 1,)
        )
    assert build_kanban_stop_nudge(messages=[]) is None


@pytest.mark.parametrize("table", ["tasks", "task_runs"])
def test_missing_claim_expiry_denies_nudge(owned_run, table):
    with sqlite3.connect(owned_run) as conn:
        conn.execute(f"UPDATE {table} SET claim_expires=NULL")
    assert build_kanban_stop_nudge(messages=[]) is None


# ── unreadable / hostile boards ─────────────────────────────────────────────

def test_unreadable_database_denies_nudge(owned_run):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    owned_run.chmod(0o000)
    try:
        assert build_kanban_stop_nudge(messages=[]) is None
    finally:
        owned_run.chmod(0o600)


def test_corrupt_database_denies_nudge(owned_run):
    owned_run.write_bytes(b"this is not a sqlite database" * 64)
    assert build_kanban_stop_nudge(messages=[]) is None


def test_schemaless_database_denies_nudge(owned_run):
    with sqlite3.connect(owned_run) as conn:
        conn.execute("DROP TABLE task_runs")
    assert build_kanban_stop_nudge(messages=[]) is None


def test_directory_in_place_of_database_denies_nudge(owned_run):
    owned_run.unlink()
    owned_run.mkdir()
    assert build_kanban_stop_nudge(messages=[]) is None


def test_exclusively_locked_board_denies_nudge_without_hanging(owned_run):
    """A writer's exclusive lock must fail the guard fast, not stall the turn."""
    blocker = sqlite3.connect(owned_run, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        assert build_kanban_stop_nudge(messages=[]) is None
        assert time.monotonic() - started < 5.0
    finally:
        blocker.rollback()
        blocker.close()


def test_cancellation_is_not_swallowed_into_a_verdict(owned_run, monkeypatch):
    """Turn cancellation must propagate, never be read as 'not a worker'."""
    import agent.kanban_stop as ks

    def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ks.sqlite3, "connect", cancel)
    with pytest.raises(asyncio.CancelledError):
        build_kanban_stop_nudge(messages=[])


def test_explicit_disable_still_wins_before_any_board_read(owned_run, monkeypatch):
    import agent.kanban_stop as ks

    def explode(*args, **kwargs):
        raise AssertionError("board must not be read when the guard is disabled")

    monkeypatch.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    monkeypatch.setattr(ks.sqlite3, "connect", explode)
    assert build_kanban_stop_nudge(messages=[]) is None
