"""Interactive card creation is unbound; worker card creation stays bound.

The desktop backend that inherited a finished worker's identity was creating
cards as that worker — and being denied, because the run it named was over.
After the startup scrub the human surface is simply not task-bound (an
orchestrator, free to open roots), while a genuine live worker keeps every
cross-task and stale-run denial it had.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def live_worker(monkeypatch, tmp_path):
    """A real board with one claimed, running task owned by this process."""
    from pathlib import Path as _Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    from hermes_cli import kanban_db_connect

    kanban_db_connect._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        other = kb.create_task(conn, title="unrelated", assignee="test-worker")
        task = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", task.claim_lock)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    return {"task": tid, "other": other}


def scope_for(parents):
    from hermes_cli import kanban_db as kb
    from tools.kanban_tools import _inherited_parent_scope

    with kb.connect() as conn:
        return _inherited_parent_scope(kb, conn, parents)


def test_interactive_startup_makes_creation_unbound(live_worker):
    """After the scrub the process has no parent to inherit scope from."""
    from tui_gateway.interactive_env import clear_inherited_worker_identity

    assert scope_for([])[1] is not None  # bound worker: parents are required

    clear_inherited_worker_identity()

    assert scope_for([]) == ({}, None)
    created = json.loads(
        __import__("tools.kanban_tools", fromlist=["kt"])._handle_create(
            {"title": "human ask", "assignee": "test-worker"}
        )
    )
    assert "error" not in created, created


def test_interactive_creation_is_independent_of_the_finished_task(live_worker):
    """A card opened from the human surface is a root, not the worker's child."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    from tui_gateway.interactive_env import clear_inherited_worker_identity

    clear_inherited_worker_identity()
    created = json.loads(
        kt._handle_create({"title": "human ask", "assignee": "test-worker"})
    )
    with kb.connect() as conn:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id=?", (created["task_id"],)
        ).fetchall()
    assert [row[0] for row in parents] == []


def test_live_worker_still_denied_a_foreign_parent(live_worker):
    """Genuine worker authority is unchanged: no cross-task card creation."""
    from tools import kanban_tools as kt

    out = json.loads(
        kt._handle_create({
            "title": "cross-task",
            "assignee": "test-worker",
            "parents": [live_worker["other"]],
        })
    )
    assert "error" in out, out


def test_live_worker_still_denied_on_stale_run_authority(live_worker, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
    out = json.loads(
        kt._handle_create({
            "title": "continuation",
            "assignee": "test-worker",
            "parents": [live_worker["task"]],
        })
    )
    assert "error" in out, out


def test_live_worker_still_denied_on_foreign_claim_authority(live_worker, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "foreign-claim-authority")
    out = json.loads(
        kt._handle_create({
            "title": "continuation",
            "assignee": "test-worker",
            "parents": [live_worker["task"]],
        })
    )
    assert "error" in out, out


def test_scrub_does_not_relax_a_live_worker_in_another_context(live_worker):
    """The scrub is a startup act; it must not be reachable as a turn escape.

    Running it does not hand the *dispatcher's* worker unbound authority in
    some other process — the board rows are untouched, so a worker process that
    still has its identity keeps being denied.
    """
    from hermes_cli import kanban_db as kb

    from tui_gateway.interactive_env import clear_inherited_worker_identity

    clear_inherited_worker_identity()
    with kb.connect() as conn:
        task = kb.get_task(conn, live_worker["task"])
    assert task.status == "running"
    assert task.current_run_id is not None
    assert task.claim_lock
