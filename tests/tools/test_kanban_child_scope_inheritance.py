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
        run_id = kb.get_task(conn, tid).current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
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


def test_child_rejects_cross_executor_delegation_without_a_grant(scoped_worker):
    out = _create(title="delegate", assignee="other-profile", parents=[scoped_worker])
    assert "error" in out
    assert "assignee" in out["error"].lower() or "executor" in out["error"].lower()


def test_child_without_an_executor_is_refused(scoped_worker):
    out = _create(title="orphan", parents=[scoped_worker])
    assert "error" in out
    assert "assignee" in out["error"]


def test_task_bound_create_fails_closed_when_self_row_is_missing(
    scoped_worker, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing_authority")
    out = _create(
        title="escape", assignee="test-worker", parents=["t_missing_authority"]
    )
    assert "error" in out
    assert "authoritative" in out["error"].lower() or "not found" in out["error"].lower()


def test_task_bound_create_requires_self_as_parent(scoped_worker):
    out = _create(title="orphan continuation", assignee="test-worker", parents=[])
    assert "error" in out
    assert "parent" in out["error"].lower()


def test_task_bound_create_rejects_stale_run_and_executor(
    scoped_worker, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
    stale = _create(
        title="stale run", assignee="test-worker", parents=[scoped_worker]
    )
    assert "error" in stale and "run" in stale["error"].lower()

    with kb.connect() as conn:
        current_run = kb.get_task(conn, scoped_worker).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(current_run))
    monkeypatch.setenv("HERMES_PROFILE", "other-worker")
    wrong_executor = _create(
        title="wrong executor", assignee="test-worker", parents=[scoped_worker]
    )
    assert "error" in wrong_executor
    assert "executor" in wrong_executor["error"].lower()


@pytest.mark.parametrize(
    "column,value",
    [
        ("tenant", "other-tenant"),
        ("session_id", "other-session"),
        ("project_id", "other-project"),
        ("assignee", "other-worker"),
        ("workspace_path", "/other/repository/worktree"),
    ],
)
def test_task_bound_create_rejects_cross_scope_additional_parent(
    scoped_worker, column, value
):
    with kb.connect() as conn:
        other = kb.create_task(
            conn, title="foreign authority", assignee="test-worker",
            tenant="acme-legal-entity", session_id="sess-principal-1",
        )
        authoritative = kb.get_task(conn, scoped_worker)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET project_id=?, workspace_kind=?, workspace_path=? WHERE id=?",
                (authoritative.project_id, authoritative.workspace_kind,
                 authoritative.workspace_path, other),
            )
            conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, other))

    out = _create(
        title="mixed graph", assignee="test-worker",
        parents=[scoped_worker, other],
    )
    assert "error" in out, (column, out)
    assert "parent" in out["error"].lower() or column.split("_")[0] in out["error"].lower()


# ---------------------------------------------------------------------------
# authority
# ---------------------------------------------------------------------------


def test_child_records_the_acting_profile_as_creator(scoped_worker):
    child = _child_of(scoped_worker)
    assert _task(child).created_by == "test-worker"


def test_child_creator_authority_rejects_ambient_executor_impostor(scoped_worker, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "ambient-impostor")
    out = _create(
        title="continuation", assignee="test-worker", parents=[scoped_worker]
    )
    assert "error" in out
    assert "executor" in out["error"].lower()


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
        run_id = kb.get_task(conn, tid).current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
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


def test_non_project_parent_workspace_is_inherited_and_conflicts_rejected(scoped_worker, tmp_path):
    parent_path = tmp_path / "authoritative-workspace"
    parent_path.mkdir()
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET workspace_kind='dir', workspace_path=? WHERE id=?",
                         (str(parent_path), scoped_worker))
    child = _child_of(scoped_worker)
    assert (_task(child).workspace_kind, _task(child).workspace_path) == ("dir", str(parent_path))
    out = _create(title="escape", assignee="test-worker", parents=[scoped_worker],
                  workspace_kind="scratch")
    assert "error" in out and "workspace" in out["error"].lower()
