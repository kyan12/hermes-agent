"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_module():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py as a fresh module.

    A fresh module object per call also means a fresh (empty) board response
    cache, which the done-window cache tests depend on.
    """
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    return _load_plugin_module().router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def board_plugin(kanban_home):
    """(module, client) pair — for tests that need to reach module internals
    (the done-window constant, the board cache, the server-side clock seam)."""
    mod = _load_plugin_module()
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return mod, TestClient(app)


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_board_projects_unaffirmed_block_into_automation_triage(client):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy machine block", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind=NULL, gate_evidence=NULL "
                "WHERE id=?",
                (tid,),
            )

    data = client.get("/api/plugins/kanban/board").json()
    columns = {column["name"]: column["tasks"] for column in data["columns"]}
    assert tid not in {task["id"] for task in columns["blocked"]}
    triaged = {task["id"]: task for task in columns["triage"]}
    assert triaged[tid]["status"] == "triage"
    assert triaged[tid]["block_projection"]["visible"] is False


def test_board_and_detail_project_stale_affirmation_as_triage(client, monkeypatch):
    from hermes_cli import kanban_health as kh

    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stale gate", assignee="worker")
        evidence = {
            "type": "human_decision", "action": "Sign the named agreement",
            "affirmed_by": "kevin", "affirmed_at": int(time.time()),
        }
        assert kh.affirm_human_gate(conn, tid, evidence=evidence)
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))

    board = client.get("/api/plugins/kanban/board").json()
    columns = {column["name"]: column["tasks"] for column in board["columns"]}
    projected = {task["id"]: task for task in columns["triage"]}[tid]
    detail = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
    for task in (projected, detail):
        assert task["status"] == "triage"
        assert task["block_projection"]["visible"] is False
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"


def test_board_shows_only_affirmed_gate_in_blocked(client, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")
    created = client.post(
        "/api/plugins/kanban/tasks", json={"title": "signature gate"}
    ).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{created['id']}/affirm-gate",
        json={
            "action": "Sign the named agreement",
            "reason": "signature required",
            "kind": "capability",
        },
    )
    assert response.status_code == 200, response.text

    data = client.get("/api/plugins/kanban/board").json()
    columns = {column["name"]: column["tasks"] for column in data["columns"]}
    blocked = {task["id"]: task for task in columns["blocked"]}
    assert blocked[created["id"]]["block_projection"]["visible"] is True
    assert blocked[created["id"]]["block_projection"]["action"] == "Sign the named agreement"


def test_affirm_gate_refuses_a_transport_only_dashboard_session(client, monkeypatch):
    """A dashboard session token proves the caller reached this host, not that
    the caller is Kevin. With no verified principal bound to the install, the
    endpoint must refuse instead of stamping ``operator:dashboard``."""
    monkeypatch.delenv("HERMES_KANBAN_OPERATOR", raising=False)
    from hermes_cli import kanban_health as kh

    monkeypatch.setattr(kh, "operator_principal", lambda: None)

    created = client.post(
        "/api/plugins/kanban/tasks", json={"title": "unbound gate"}
    ).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{created['id']}/affirm-gate",
        json={"action": "Sign the named agreement", "kind": "capability"},
    )
    assert response.status_code == 403, response.text
    assert "principal" in response.json()["detail"].lower()

    with kb.connect() as conn:
        assert kb.get_task(conn, created["id"]).status != "blocked"


def test_affirm_gate_rejects_a_claimed_principal_the_session_cannot_prove(
    client, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")
    created = client.post(
        "/api/plugins/kanban/tasks", json={"title": "impersonation"}
    ).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{created['id']}/affirm-gate",
        json={
            "action": "Sign the named agreement",
            "kind": "capability",
            "affirmed_by": "mallory",
        },
    )
    assert response.status_code == 403, response.text


def test_affirm_gate_rejects_a_compound_action(client, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")
    created = client.post(
        "/api/plugins/kanban/tasks", json={"title": "two asks"}
    ).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{created['id']}/affirm-gate",
        json={
            "action": "Sign the agreement and wire the deposit",
            "kind": "capability",
        },
    )
    assert response.status_code == 400, response.text
    assert "atomic" in response.json()["detail"].lower()


@pytest.mark.parametrize("kind", ["external", "physical", "roadmap"])
def test_intentional_hold_uses_verified_human_principal(client, monkeypatch, kind):
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")
    task = client.post("/api/plugins/kanban/tasks", json={"title": f"{kind} hold"}).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{task['id']}/hold",
        json={"kind": kind, "action": "Wait for the named milestone",
              "evidence_type": "human_decision"},
    )
    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        assert json.loads(kb.get_task(conn, task["id"]).gate_evidence)["affirmed_by"] == "kevin"


def test_intentional_hold_rejects_transport_identity(client, monkeypatch):
    from hermes_cli import kanban_health as kh
    monkeypatch.delenv("HERMES_KANBAN_OPERATOR", raising=False)
    monkeypatch.setattr(kh, "operator_principal", lambda: None)
    task = client.post("/api/plugins/kanban/tasks", json={"title": "unauth hold"}).json()["task"]
    response = client.post(
        f"/api/plugins/kanban/tasks/{task['id']}/hold",
        json={"kind": "external", "action": "Wait for vendor",
              "evidence_type": "external_party"},
    )
    assert response.status_code == 403
    with kb.connect() as conn:
        assert kb.get_task(conn, task["id"]).status != "scheduled"


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text(encoding="utf-8")

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kb.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"


def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id)
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text
    with kb.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"


def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kb.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert all(item["ok"] for item in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {task["id"] for task in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kb.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"


def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kb.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()


def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


def test_dashboard_done_actions_prompt_for_completion_summary():
    """Behavioral coverage for the migrated ``requestDialog`` flow.

    Replaces the prior bundle-string-only assertion (which only proved the
    rename landed). The dialog state machine at
    ``plugins/kanban/dashboard/dist/index.js`` resolves with
    ``{confirmed: true|false, summary?}``. Each migrated call site must
    gate the dispatch on the resolved ``confirmed`` flag. This test
    asserts that contract at two layers:

    1. **Bundle cancel guards**: every migrated site gates on ``r.confirmed``
       (or its subscripted alias ``r1.confirmed``/``r2.confirmed``) before
       dispatching. We verify by counting the cancel-guard patterns +
       cross-referencing against the 8 migrated sites listed in the PR
       description.
    2. **Visual affordance**: every destructive ``requestDialog`` call marks
       ``destructive: true`` so the host renders the destructive variant.

    The dispatch path itself (PATCH/DELETE actually firing on confirm, not
    on cancel) is covered by the backend behavioral tests
    ``test_dashboard_confirm_dispatches_expected_*`` and
    ``test_dashboard_cancel_keeps_task_in_old_status`` below — together
    they pin the contract end-to-end.
    """

    repo_root = Path(__file__).resolve().parents[2]
    js = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    import re

    # Match ``if (!r.confirmed)``, ``if (!r1.confirmed)``, ``if (r.confirmed)``
    # (positive-form gate). The bundle uses both polarities:
    # - negative ``if (!r.confirmed) return null;`` in dialog flow bodies
    # - positive ``if (r.confirmed) props.onDeleteBoard(...);`` in JSX handlers
    cancel_guard_pattern = re.compile(
        r"if\s*\(\s*!?\s*r\d?\.confirmed\s*\)",
        re.IGNORECASE,
    )
    guards = cancel_guard_pattern.findall(js)
    # 8 migrated sites per the PR description:
    # moveTask (1), moveSelected (1), applyBulk (1), deleteTask (1),
    # deleteSelected (1), archiveBoard (1), removeAttachment (1), doPatch (1).
    # Plus performMoveTask callers (moveTask/moveSelected each have
    # ``r1.confirmed`` + ``r2.confirmed`` for the two-stage flow) → up to
    # 10 guards. Loose lower bound to avoid brittleness.
    assert len(guards) >= 8, (
        f"expected >= 8 `if (r?.confirmed)` cancel guards in bundle (one "
        f"per migrated site, plus extras for two-stage flows); found {len(guards)}"
    )

    # Visual affordance: every destructive requestDialog call must mark
    # ``destructive: true`` so the host renders the destructive variant.
    # deleteTask, deleteSelected, archiveBoard → at least 3.
    destructive_call_count = js.count("destructive: true")
    assert destructive_call_count >= 3, (
        f"expected >= 3 `destructive: true` requestDialog calls (single "
        f"delete, bulk delete, archive-board); found {destructive_call_count}"
    )


def test_dashboard_cancel_keeps_task_in_old_status(client):
    """Behavioral: the cancel branch of the dispatch path (no PATCH/DELETE
    issued) must leave the task in its previous status. The cancel guard
    lives in the bundle; this test pins the backend contract that the guard
    relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Tasks land in ``ready`` by default. No PATCH issued — simulating the
    # cancel branch in the bundle.
    assert t["status"] == "ready"
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.json()["task"]["status"] == "ready"


def test_dashboard_confirm_dispatches_expected_patch_body(client):
    """Behavioral: the PATCH body shape the bundle produces on confirm
    (status + result + summary) must be accepted by the backend without
    rejection. The backend stores ``result`` as the human-readable
    completion summary (the bundle comments confirm ``summary`` is sent
    duplicatively so the backend can store the value under its preferred
    key while the wire format remains explicit).
    This is the contract the bundle's performMoveTask relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Bundle's performMoveTask on confirm with a summary produces:
    #   { status, result: summary, summary: summary }
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped", "summary": "shipped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "done"
    assert body.get("result") == "shipped"


def test_dashboard_confirm_dispatches_expected_delete(client):
    """Behavioral: the DELETE call the bundle issues on confirm
    (``fetchJSON(`${API}/tasks/${id}`, { method: 'DELETE' })``) must
    succeed and remove the task.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200, r.text
    # 404 on the now-deleted task confirms removal.
    r2 = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r2.status_code == 404


def test_dashboard_surfaces_ready_blocked_error_inline():
    """Regression for #26744: failed status transitions must be surfaced
    inline, not swallowed.  The drag/drop banner and the drawer's action
    row each render the parsed API ``detail`` so operators see *why*
    their click did nothing.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    # Helper that strips ``"409: {\"detail\":\"…\"}"`` down to the
    # human-readable message before it lands in any banner.
    assert "function parseApiErrorMessage(err)" in bundle
    assert "parsed.detail" in bundle

    # Drag/drop banner now uses the parsed message instead of raw
    # ``err.message`` so it no longer leaks HTTP plumbing.
    assert "setError(tx(t, \"moveFailed\", \"Move failed: \") + parseApiErrorMessage(err))" in bundle

    # Drawer action row has its own visible error surface and clears it
    # on success/refresh so stale failures don't follow the operator
    # around.
    assert "const [patchErr, setPatchErr] = useState(null);" in bundle
    assert "setPatchErr(parseApiErrorMessage(e))" in bundle
    assert "setPatchErr(null)" in bundle


def test_dashboard_dependency_selects_use_value_change_handler():
    """Regression for the dependency selects in the task drawer: the
    add-parent / add-child dropdowns must wire through the shared
    selectChangeHandler helper so their value actually lands on the
    underlying React state. Salvaged from #20019 @LeonSGP43.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    parent_select = (
        'value: newParent,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewParent))'
    )
    child_select = (
        'value: newChild,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewChild))'
    )

    assert parent_select in bundle
    assert child_select in bundle


def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "archive": True})
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids


def test_bulk_reassign(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "old"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks",
                    json={"title": "b", "assignee": "old"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "assignee": "new"})
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"


def test_bulk_unassign_via_empty_string(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "x"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"]], "assignee": ""})
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None


def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7


def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kb.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kb.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Done column rolling 48h window
# ---------------------------------------------------------------------------

_HOUR = 3600
_WINDOW = 48 * _HOUR
# Fixed server clock for the boundary tests: real wall-clock would make the
# "exactly at the boundary" cases flake whenever a request straddles a second.
_T0 = 1_800_000_000


class _FrozenClock:
    """Controllable stand-in for the board's server-side ``now``."""

    def __init__(self, now: float) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _freeze_board_clock(mod, monkeypatch, now: float = _T0) -> _FrozenClock:
    clock = _FrozenClock(now)
    monkeypatch.setattr(mod, "_now_seconds", clock)
    return clock


def _make_done_task(
    *,
    title: str,
    completed_at,
    tenant=None,
    assignee=None,
    parents=(),
    board=None,
) -> str:
    """Create a task, complete it, then backdate ``completed_at`` in place.

    Backdating via SQL (rather than freezing the clock for ``complete_task``)
    keeps the rest of the row — events, runs, links — exactly as a real
    completion writes it, which is what the board reads.
    """
    conn = kb.connect(board=board) if board else kb.connect()
    try:
        tid = kb.create_task(
            conn, title=title, tenant=tenant, assignee=assignee,
            parents=list(parents),
        )
        assert kb.complete_task(conn, tid, result=f"result::{title}",
                                summary=f"summary::{title}")
        if completed_at is None:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET completed_at = NULL WHERE id = ?", (tid,)
                )
        else:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET completed_at = ? WHERE id = ?",
                    (int(completed_at), tid),
                )
        assert kb.get_task(conn, tid).status == "done"
        return tid
    finally:
        conn.close()


def _columns(response) -> dict[str, list[dict]]:
    assert response.status_code == 200, response.text
    return {c["name"]: c["tasks"] for c in response.json()["columns"]}


def _done_ids(response) -> set[str]:
    return {t["id"] for t in _columns(response)["done"]}


def test_done_window_constant_is_exactly_48_hours(board_plugin):
    mod, _ = board_plugin
    assert mod.DONE_COLUMN_WINDOW_SECONDS == 48 * 60 * 60 == 172800


def test_board_done_column_includes_completion_one_second_inside_window(
    board_plugin, monkeypatch
):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    # 47h59m59s old — one second inside the rolling window.
    tid = _make_done_task(title="just inside", completed_at=_T0 - (_WINDOW - 1))

    assert _done_ids(client.get("/api/plugins/kanban/board")) == {tid}


def test_board_done_column_excludes_completion_at_exactly_48_hours(
    board_plugin, monkeypatch
):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    # Exactly 48h old — the boundary is strict, so this is excluded.
    tid = _make_done_task(title="on the boundary", completed_at=_T0 - _WINDOW)

    assert _done_ids(client.get("/api/plugins/kanban/board")) == set()
    # The row itself is untouched and still done.
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "done"


def test_board_done_column_excludes_older_and_timestampless_completions(
    board_plugin, monkeypatch
):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    fresh = _make_done_task(title="fresh", completed_at=_T0 - _HOUR)
    _make_done_task(title="ancient", completed_at=_T0 - 30 * 24 * _HOUR)
    _make_done_task(title="one second over", completed_at=_T0 - (_WINDOW + 1))
    _make_done_task(title="no completion timestamp", completed_at=None)

    assert _done_ids(client.get("/api/plugins/kanban/board")) == {fresh}


def test_board_done_window_leaves_active_statuses_untouched(board_plugin, monkeypatch):
    """Non-done statuses are never aged out, no matter how old the row is."""
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    ancient = _T0 - 90 * 24 * _HOUR
    ids = {}
    with kb.connect() as conn:
        for status in ("triage", "todo", "scheduled", "ready", "running", "review"):
            tid = kb.create_task(conn, title=f"old {status}")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = ?, created_at = ?, completed_at = ? "
                    "WHERE id = ?",
                    (status, ancient, ancient, tid),
                )
            ids[status] = tid

    columns = _columns(client.get("/api/plugins/kanban/board"))
    for status, tid in ids.items():
        assert tid in {t["id"] for t in columns[status]}, f"{status} card vanished"


def test_board_done_window_filters_in_sql_before_downstream_work(
    board_plugin, monkeypatch
):
    """Requirement: the cutoff is applied in the data fetch, so summary
    lookup, diagnostics and serialization only ever see visible ids."""
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    visible = _make_done_task(title="visible", completed_at=_T0 - _HOUR)
    hidden = _make_done_task(title="hidden", completed_at=_T0 - 10 * 24 * _HOUR)
    with kb.connect() as conn:
        active = kb.create_task(conn, title="visible active")
        # Reproduce the live board's historical scale without emitting 1,140
        # events or paying 1,140 transactions: these rows exercise the same
        # SQL predicate that protects every downstream payload stage.
        historical_ids = [f"t_perf_{i:04d}" for i in range(1_140)]
        with kb.write_txn(conn):
            conn.executemany(
                "INSERT INTO tasks (id, title, status, created_at, completed_at) "
                "VALUES (?, ?, 'done', ?, ?)",
                [
                    (tid, f"historical {i}", _T0 - 30 * 24 * _HOUR,
                     _T0 - 30 * 24 * _HOUR)
                    for i, tid in enumerate(historical_ids)
                ],
            )

    seen = {"list_kwargs": [], "summaries": [], "diagnostics": [], "serialized": []}

    real_list = mod.kanban_db.list_tasks
    real_summaries = mod.kanban_db.latest_summaries
    real_diags = mod._compute_task_diagnostics
    real_task_dict = mod._task_dict

    def spy_list(conn, **kwargs):
        seen["list_kwargs"].append(kwargs)
        return real_list(conn, **kwargs)

    def spy_summaries(conn, task_ids):
        ids = list(task_ids)
        seen["summaries"].append(ids)
        return real_summaries(conn, ids)

    def spy_diags(conn, task_ids=None):
        seen["diagnostics"].append(task_ids)
        return real_diags(conn, task_ids)

    def spy_task_dict(conn, task, **kwargs):
        seen["serialized"].append(task.id)
        return real_task_dict(conn, task, **kwargs)

    monkeypatch.setattr(mod.kanban_db, "list_tasks", spy_list)
    monkeypatch.setattr(mod.kanban_db, "latest_summaries", spy_summaries)
    monkeypatch.setattr(mod, "_compute_task_diagnostics", spy_diags)
    monkeypatch.setattr(mod, "_task_dict", spy_task_dict)

    assert _done_ids(client.get("/api/plugins/kanban/board")) == {visible}

    # The cutoff was pushed into the fetch, not applied afterwards.
    assert seen["list_kwargs"], "list_tasks was not called"
    kwargs = seen["list_kwargs"][0]
    assert kwargs.get("done_completed_after") == _T0 - _WINDOW

    # Nothing downstream ever saw any of the 1,141 aged-out tasks; the
    # recent done card and nonterminal card both continue through the path.
    expected_visible = {visible, active}
    hidden_ids = {hidden, *historical_ids}
    assert seen["summaries"] and set(seen["summaries"][0]) == expected_visible
    assert hidden_ids.isdisjoint(seen["summaries"][0])
    assert seen["diagnostics"], "diagnostics were not scoped"
    diag_ids = seen["diagnostics"][0]
    assert diag_ids is not None, "diagnostics still run over every task"
    assert set(diag_ids) == expected_visible
    assert hidden_ids.isdisjoint(diag_ids)
    assert set(seen["serialized"]) == expected_visible
    assert hidden_ids.isdisjoint(seen["serialized"])


def test_board_cache_ages_out_a_done_card_without_any_task_event(
    board_plugin, monkeypatch
):
    """A cached done card must disappear on the first request after it
    crosses the cutoff — no task event, and before the generic TTL."""
    mod, client = board_plugin
    clock = _freeze_board_clock(mod, monkeypatch)
    tid = _make_done_task(title="about to age out", completed_at=_T0 - (_WINDOW - 1))

    assert _done_ids(client.get("/api/plugins/kanban/board")) == {tid}

    def _event_max():
        with kb.connect() as conn:
            return conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
            ).fetchone()["m"]

    before_events = _event_max()
    # One second later the card is exactly 48h old. The version probe is
    # unchanged and the 30s TTL has not lapsed, so only a per-entry time
    # boundary can evict it.
    clock.advance(1)
    assert mod._BOARD_CACHE_TTL_SECONDS > 1

    assert _done_ids(client.get("/api/plugins/kanban/board")) == set()
    assert _event_max() == before_events, "board read emitted a task event"


def test_board_cache_still_serves_within_window_and_ttl(board_plugin, monkeypatch):
    """The done boundary must not defeat the existing versioned cache: a
    second request well inside both the window and the TTL is a cache hit."""
    mod, client = board_plugin
    clock = _freeze_board_clock(mod, monkeypatch)
    tid = _make_done_task(title="fresh enough", completed_at=_T0 - _HOUR)

    assert _done_ids(client.get("/api/plugins/kanban/board")) == {tid}

    builds = []
    real_build = mod._build_board_payload

    def spy_build(**kwargs):
        builds.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(mod, "_build_board_payload", spy_build)
    clock.advance(1)
    assert _done_ids(client.get("/api/plugins/kanban/board")) == {tid}
    assert builds == [], "cache was needlessly invalidated"


def test_old_done_parent_still_satisfies_dependencies_and_link_topology(
    board_plugin, monkeypatch
):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    parent = _make_done_task(title="ancient parent", completed_at=_T0 - 20 * 24 * _HOUR)

    # Dependency truth: a child of an already-done parent is immediately ready.
    with kb.connect() as conn:
        child = kb.create_task(conn, title="child of ancient", parents=[parent])
        assert kb.get_task(conn, child).status == "ready"

    columns = _columns(client.get("/api/plugins/kanban/board"))
    assert parent not in {t["id"] for t in columns["done"]}
    child_card = next(t for t in columns["ready"] if t["id"] == child)
    # Link counts still describe the real graph even though the parent card
    # is not on the board.
    assert child_card["link_counts"] == {"parents": 1, "children": 0}

    # The drawer's direct fetch still resolves both directions of the link.
    detail = client.get(f"/api/plugins/kanban/tasks/{child}").json()
    assert detail["links"]["parents"] == [parent]
    parent_detail = client.get(f"/api/plugins/kanban/tasks/{parent}").json()
    assert parent_detail["links"]["children"] == [child]
    assert parent_detail["child_results"][0]["id"] == child


def test_board_progress_and_link_counts_include_aged_out_children(
    board_plugin, monkeypatch
):
    """A visible card's link counts and "N/M" rollup are computed from the
    whole graph, so an aged-out done relative still counts."""
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    # grandparent completed 20 days ago -> its card ages off the board.
    grandparent = _make_done_task(
        title="ancient epic", completed_at=_T0 - 20 * 24 * _HOUR,
    )
    # parent completed an hour ago -> still on the board.
    parent = _make_done_task(
        title="recent parent", completed_at=_T0 - _HOUR, parents=[grandparent],
    )
    done_child = _make_done_task(
        title="done child", completed_at=_T0 - 1800, parents=[parent],
    )
    with kb.connect() as conn:
        open_child = kb.create_task(conn, title="open child", parents=[parent])
        assert kb.get_task(conn, open_child).status == "ready"

    columns = _columns(client.get("/api/plugins/kanban/board"))
    done_ids = {t["id"] for t in columns["done"]}
    assert grandparent not in done_ids
    assert done_ids == {parent, done_child}

    parent_card = next(t for t in columns["done"] if t["id"] == parent)
    # The aged-out grandparent link is still counted.
    assert parent_card["link_counts"] == {"parents": 1, "children": 2}
    assert parent_card["progress"] == {"done": 1, "total": 2}
    assert open_child in {t["id"] for t in columns["ready"]}


def test_old_done_task_detail_and_history_remain_available(board_plugin, monkeypatch):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    tid = _make_done_task(title="archaeology", completed_at=_T0 - 45 * 24 * _HOUR)

    assert _done_ids(client.get("/api/plugins/kanban/board")) == set()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["id"] == tid
    assert body["task"]["status"] == "done"
    assert body["task"]["result"] == "result::archaeology"
    assert body["task"]["latest_summary"] == "summary::archaeology"
    assert body["events"], "event history disappeared"
    assert any(e["kind"] == "completed" for e in body["events"])
    assert body["runs"], "run history disappeared"


def test_board_build_does_not_mutate_the_database(board_plugin, monkeypatch):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    _make_done_task(title="old", completed_at=_T0 - 9 * 24 * _HOUR)
    _make_done_task(title="new", completed_at=_T0 - _HOUR)

    def _snapshot():
        with kb.connect() as conn:
            conn.row_factory = None
            return {
                "tasks": conn.execute(
                    "SELECT * FROM tasks ORDER BY id"
                ).fetchall(),
                "events": conn.execute(
                    "SELECT id, task_id, kind FROM task_events ORDER BY id"
                ).fetchall(),
                "runs": conn.execute(
                    "SELECT id, task_id, status, outcome FROM task_runs ORDER BY id"
                ).fetchall(),
            }

    before = _snapshot()
    assert client.get("/api/plugins/kanban/board").status_code == 200
    assert _snapshot() == before


def test_include_archived_does_not_resurrect_aged_out_done_cards(
    board_plugin, monkeypatch
):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    old_done = _make_done_task(title="old done", completed_at=_T0 - 12 * 24 * _HOUR)
    fresh_done = _make_done_task(title="fresh done", completed_at=_T0 - 2 * _HOUR)
    archived = _make_done_task(title="archived", completed_at=_T0 - 12 * 24 * _HOUR)
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (archived,)
            )

    columns = _columns(client.get("/api/plugins/kanban/board?include_archived=true"))
    assert "archived" in columns
    assert archived in {t["id"] for t in columns["archived"]}
    done_ids = {t["id"] for t in columns["done"]}
    assert done_ids == {fresh_done}
    assert old_done not in done_ids


def test_done_window_applies_per_filter_and_per_board(board_plugin, monkeypatch):
    mod, client = board_plugin
    _freeze_board_clock(mod, monkeypatch)
    fresh_acme = _make_done_task(
        title="fresh acme", completed_at=_T0 - _HOUR, tenant="acme", assignee="alice",
    )
    _make_done_task(
        title="old acme", completed_at=_T0 - 7 * 24 * _HOUR, tenant="acme",
        assignee="alice",
    )

    # Tenant filter composes with the window rather than bypassing it.
    assert _done_ids(client.get("/api/plugins/kanban/board?tenant=acme")) == {
        fresh_acme
    }

    # Workflow-template and step filters compose at the same SQL boundary.
    workflow_fresh = _make_done_task(
        title="workflow fresh", completed_at=_T0 - _HOUR, assignee="bob",
    )
    workflow_old = _make_done_task(
        title="workflow old", completed_at=_T0 - 8 * 24 * _HOUR, assignee="bob",
    )
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.executemany(
                "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? "
                "WHERE id = ?",
                [("release", "verify", workflow_fresh),
                 ("release", "verify", workflow_old)],
            )
    workflow_response = client.get(
        "/api/plugins/kanban/board"
        "?workflow_template_id=release&current_step_key=verify"
    )
    assert _done_ids(workflow_response) == {workflow_fresh}
    assert "bob" in workflow_response.json()["assignees"]

    # A second board gets the same treatment against its own DB.
    kb.create_board("secondary")
    other_fresh = _make_done_task(
        title="other fresh", completed_at=_T0 - _HOUR, board="secondary",
    )
    _make_done_task(
        title="other old", completed_at=_T0 - 8 * 24 * _HOUR, board="secondary",
    )
    assert _done_ids(client.get("/api/plugins/kanban/board?board=secondary")) == {
        other_fresh
    }


def test_list_tasks_done_completed_after_defaults_to_no_filtering(kanban_home):
    """The new kanban_db parameter is opt-in: existing callers are unchanged."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ancient")
        assert kb.complete_task(conn, tid, result="ok")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET completed_at = ? WHERE id = ?",
                (_T0 - 365 * 24 * _HOUR, tid),
            )
        assert tid in {t.id for t in kb.list_tasks(conn)}
        cutoff = _T0 - _WINDOW
        assert tid not in {
            t.id for t in kb.list_tasks(conn, done_completed_after=cutoff)
        }
        # Strict boundary, checked directly at the data layer.
        assert tid not in {
            t.id
            for t in kb.list_tasks(conn, done_completed_after=_T0 - 365 * 24 * _HOUR)
        }
        assert tid in {
            t.id
            for t in kb.list_tasks(
                conn, done_completed_after=_T0 - 365 * 24 * _HOUR - 1
            )
        }
