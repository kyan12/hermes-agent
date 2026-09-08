"""Stop nudges require live run ownership, not an inherited task label."""
import sqlite3

import pytest

from agent.kanban_stop import build_kanban_stop_nudge
from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context


@pytest.fixture
def owned_run(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER)")
        conn.execute("INSERT INTO tasks VALUES ('t_test', 'running', 42)")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_STOP_NUDGE", raising=False)
    return db


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


@pytest.mark.parametrize("source", ["desktop", "tui", "web"])
def test_interactive_session_context_beats_inherited_worker(owned_run, source):
    from gateway.session_context import set_session_vars, clear_session_vars

    tokens = set_session_vars(source=source)
    try:
        assert build_kanban_stop_nudge(messages=[]) is None
    finally:
        clear_session_vars(tokens)


@pytest.mark.parametrize("run_id", ["", "garbage", "0", "-1", "43"])
def test_no_nudge_without_exact_run_identity(owned_run, monkeypatch, run_id):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    assert build_kanban_stop_nudge(messages=[]) is None


def test_cannot_name_a_foreign_task_in_nudge(owned_run):
    assert build_kanban_stop_nudge(messages=[], task_id="t_other") is None
