"""A continuation card must inherit its parent's scope, not lose it.

Invariant C: child/continuation routing preserves every scope dimension the
current schema can express —

==================  ================================================
dimension           schema carrier
==================  ================================================
Project             ``tasks.project_id``
workspace/repo      ``tasks.workspace_kind`` + ``workspace_path``
                    (a *fresh* worktree under the SAME repo)
principal           ``tasks.session_id`` (originating chat/agent session)
executor            ``tasks.assignee``
legal/entity scope  ``tasks.tenant``
authority           the pinned board boundary + ``tasks.created_by``
==================  ================================================

The parent task row — not the ambient process environment — is the source
of truth. A worker whose env lost ``HERMES_TENANT`` (a restart, a re-exec,
a nested spawn) must still produce in-scope children.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def scoped_worker(monkeypatch, tmp_path):
    """A dispatcher-spawned worker on a tenant- and session-scoped card."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    # The env lost the tenant — the parent row must still carry the scope.
    monkeypatch.delenv("HERMES_TENANT", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="parent work",
            assignee="test-worker",
            tenant="acme-legal-entity",
            session_id="sess-principal-1",
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
    return tid


def _create(**args):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_create(args))


def _child_of(parent_id, **overrides):
    args = {"title": "continuation", "assignee": "test-worker", "parents": [parent_id]}
    args.update(overrides)
    out = _create(**args)
    assert "error" not in out, out
    return out["task_id"]


def _task(task_id, board=None):
    conn = kb.connect(board=board)
    try:
        return kb.get_task(conn, task_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# legal/entity scope
# ---------------------------------------------------------------------------


def test_child_inherits_tenant_from_the_parent_row_not_the_environment(scoped_worker):
    child = _child_of(scoped_worker)
    assert _task(scoped_worker).tenant == "acme-legal-entity"
    assert _task(child).tenant == "acme-legal-entity"


def test_worker_cannot_override_parent_tenant(scoped_worker):
    out = _create(
        title="escape", assignee="test-worker", parents=[scoped_worker],
        tenant="other-entity",
    )
    assert "error" in out
    assert "tenant" in out["error"].lower()


# ---------------------------------------------------------------------------
# principal
# ---------------------------------------------------------------------------


def test_child_inherits_originating_session_principal(scoped_worker):
    child = _child_of(scoped_worker)
    assert _task(child).session_id == "sess-principal-1"


def test_stale_ambient_scope_cannot_override_authoritative_parent(
    scoped_worker, monkeypatch
):
    monkeypatch.setenv("HERMES_TENANT", "stale-ambient-tenant")
    monkeypatch.setenv("HERMES_SESSION_ID", "stale-ambient-session")

    child = _child_of(scoped_worker)

    assert _task(child).tenant == "acme-legal-entity"
    assert _task(child).session_id == "sess-principal-1"


def test_worker_cannot_override_parent_principal(scoped_worker):
    out = _create(
        title="escape", assignee="test-worker", parents=[scoped_worker],
        session_id="sess-other",
    )
    assert "error" in out
    assert "session" in out["error"].lower()


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------


def test_child_carries_an_explicit_executor(scoped_worker):
    child = _child_of(scoped_worker, assignee="other-profile")
    assert _task(child).assignee == "other-profile"


def test_child_without_an_executor_is_refused(scoped_worker):
    out = _create(title="orphan", parents=[scoped_worker])
    assert "error" in out
    assert "assignee" in out["error"]


# ---------------------------------------------------------------------------
# authority
# ---------------------------------------------------------------------------


def test_child_records_the_acting_profile_as_creator(scoped_worker):
    child = _child_of(scoped_worker)
    assert _task(child).created_by == "test-worker"


def test_worker_cannot_route_a_continuation_off_its_pinned_board(scoped_worker):
    """The board is the hard isolation boundary. A dispatcher-spawned worker
    carries ``HERMES_KANBAN_BOARD``; letting it name another board in
    ``kanban_create`` would let a card escape the scope it was authorized
    for and land where its principal has no standing."""
    kb.create_board("other", name="Other")
    out = _create(
        title="escape attempt",
        assignee="test-worker",
        parents=[scoped_worker],
        board="other",
    )
    assert "error" in out, out
    assert "board" in out["error"].lower()

    conn = kb.connect(board="other")
    try:
        assert kb.list_tasks(conn) == []
    finally:
        conn.close()


def test_worker_may_name_its_own_pinned_board_explicitly(scoped_worker):
    """Naming the board you are already pinned to is a no-op, not an escape."""
    child = _child_of(scoped_worker, board=kb.DEFAULT_BOARD)
    assert _task(child).tenant == "acme-legal-entity"


def test_orchestrator_without_a_pinned_task_may_still_route_across_boards(
    monkeypatch, tmp_path
):
    """The guard is scoped to dispatcher-spawned workers. An orchestrator
    (no ``HERMES_KANBAN_TASK``) keeps the documented cross-board routing
    that the multi-board Telegram/agent surfaces depend on."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board("other", name="Other")

    out = _create(title="routed", assignee="orchestrator", board="other")
    assert "error" not in out, out
    assert _task(out["task_id"], board="other") is not None


# ---------------------------------------------------------------------------
# Project + workspace/repo
# ---------------------------------------------------------------------------


@pytest.fixture
def project_worker(monkeypatch, tmp_path):
    """A worker on a Project-linked worktree card.

    The parent sits at ``<repo>/.worktrees/<parent-id>`` on branch
    ``alpha/<parent-id>`` — the canonical project-linked shape. The child
    must recover the same repo + branch convention from the parent row even
    though the worker profile has no ``projects.db`` entry of its own.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_TENANT", raising=False)
    from pathlib import Path as _Path

    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    repo = tmp_path / "repo"
    (repo / ".worktrees").mkdir(parents=True)

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="project parent", assignee="test-worker", tenant="acme"
        )
        parent_wt = repo / ".worktrees" / tid
        parent_wt.mkdir()
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET project_id = ?, workspace_kind = 'worktree', "
                "workspace_path = ?, branch_name = ? WHERE id = ?",
                ("alpha", str(parent_wt), f"alpha/{tid}", tid),
            )
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", kb.DEFAULT_BOARD)
    return tid, repo


def test_child_inherits_the_project_link(project_worker):
    parent, _repo = project_worker
    child = _child_of(parent)
    assert _task(child).project_id == "alpha"


def test_child_inherits_the_repo_but_never_the_parents_checkout(project_worker):
    from pathlib import Path

    parent, repo = project_worker
    child = _child_of(parent)
    kid = _task(child)
    parent_task = _task(parent)

    assert kid.workspace_kind == "worktree"
    child_path = Path(kid.workspace_path)
    # Same repo …
    assert child_path.parent.parent == repo
    assert child_path.parent.name == ".worktrees"
    # … its own checkout, keyed on its own task id.
    assert child_path.name == child
    assert str(child_path) != str(parent_task.workspace_path)


def test_child_inherits_project_alongside_tenant_and_principal(project_worker):
    parent, _repo = project_worker
    child = _child_of(parent)
    kid = _task(child)
    assert (kid.project_id, kid.tenant, kid.assignee) == (
        "alpha",
        "acme",
        "test-worker",
    )


def test_worker_cannot_override_parent_project_or_workspace(project_worker):
    parent, repo = project_worker
    for override in (
        {"project": "other-project"},
        {"workspace_kind": "dir", "workspace_path": str(repo)},
        {"workspace_kind": "scratch"},
    ):
        out = _create(
            title="escape", assignee="test-worker", parents=[parent], **override
        )
        assert "error" in out, (override, out)
        assert any(
            word in out["error"].lower() for word in ("project", "workspace")
        )
