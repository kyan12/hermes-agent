"""Resume authority is issued under the dashboard's own authentication boundary.

There is no new credential and no new gate: ``web_server._require_token`` already
distinguishes a verified session (gated mode) from the dashboard token
(loopback). The granting identity comes from that boundary; a body that names its
own ``issued_by`` is not authority, so the field does not exist.

The operator confirms an exact snapshot they were shown. Anything that moved
between the preview and the confirmation refuses.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PR = "https://github.com/NousResearch/hermes-agent/pull/4242"


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_kanban_resume_surface_test",
        repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def authorized(monkeypatch):
    """The real gate, answering yes. Its own coverage lives in web_server tests."""
    calls: list[str] = []

    def _require_token(request):
        calls.append(request.url.path)

    import hermes_cli
    import types

    stub = types.SimpleNamespace(_require_token=_require_token)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)
    return calls


@pytest.fixture
def client(tmp_path, monkeypatch, authorized):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_module().router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def parked():
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="mid-PR crash", assignee="a")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
            "VALUES (?, 'crashed', ?, ?, 'crashed')", (task_id, now - 600, now - 300))
        kb.add_comment(conn, task_id, "worker", f"opened {PR}")
        conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
        assert kbd.check_respawn_guard(conn, task_id) == "active_pr"
        return task_id
    finally:
        conn.close()


def _preview(client, task_id):
    response = client.get(f"/api/plugins/kanban/tasks/{task_id}/resume-preview")
    assert response.status_code == 200, response.text
    return response.json()


def _authorize(client, task_id, expected, attest=True):
    return client.post(f"/api/plugins/kanban/tasks/{task_id}/resume-authorize",
                       json={**expected, "attest_lineage": attest})


def test_the_preview_shows_every_comment_and_the_tuple_to_confirm(client, parked):
    preview = _preview(client, parked)
    assert preview["resumable"] is True and preview["blocked_reason"] is None
    assert preview["pr_url"] == PR.lower()
    assert [c["body"] for c in preview["comments"]] == [f"opened {PR}"]
    assert set(preview["expected"]) == {
        "board_identity", "task_id", "run_id", "occurrence_event_id", "pr_url",
        "comment_digest", "lifecycle", "assignee", "workspace_kind", "workspace_path"}


def test_authorizing_the_confirmed_tuple_lifts_only_the_active_pr_guard(client, parked):
    response = _authorize(client, parked, _preview(client, parked)["expected"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pr_url"] == PR.lower()
    assert body["issued_by"].startswith("dashboard:")

    conn = kbc.connect()
    try:
        assert kbd.evaluate_respawn_guard(conn, parked).reason is None
        assert kbd.check_respawn_guard(conn, parked) == "active_pr"
        stored = conn.execute(
            "SELECT issued_by FROM task_resume_receipts WHERE task_id = ?", (parked,)).fetchone()
        assert stored["issued_by"] == body["issued_by"]
    finally:
        conn.close()


def test_the_body_cannot_name_its_own_granting_identity(client, parked):
    expected = _preview(client, parked)["expected"]
    response = _authorize(client, parked, {**expected, "issued_by": "operator:kevin"})
    assert response.status_code == 200
    assert response.json()["issued_by"] != "operator:kevin"


def test_without_the_attestation_nothing_is_issued(client, parked):
    response = _authorize(client, parked, _preview(client, parked)["expected"], attest=False)
    assert response.status_code == 409
    conn = kbc.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_resume_receipts").fetchone()[0] == 0
        assert kbd.evaluate_respawn_guard(conn, parked).reason == "active_pr"
    finally:
        conn.close()


def test_a_comment_arriving_after_the_preview_refuses_the_confirmation(client, parked):
    expected = _preview(client, parked)["expected"]
    conn = kbc.connect()
    try:
        kb.add_comment(conn, parked, "human", "actually, hold off")
    finally:
        conn.close()
    response = _authorize(client, parked, expected)
    assert response.status_code == 409
    assert "changed while it was being reviewed" in response.text


def test_the_endpoints_require_the_dashboards_own_gate(client, parked, authorized):
    _preview(client, parked)
    _authorize(client, parked, _preview(client, parked)["expected"])
    assert any(path.endswith("resume-preview") for path in authorized)
    assert any(path.endswith("resume-authorize") for path in authorized)


def test_an_unauthenticated_caller_gets_nothing(tmp_path, monkeypatch, parked):
    """When the gate raises, the route does not run."""
    import types

    import hermes_cli
    from fastapi import HTTPException

    def _deny(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    stub = types.SimpleNamespace(_require_token=_deny)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", stub)
    monkeypatch.setattr(hermes_cli, "web_server", stub, raising=False)
    app = FastAPI()
    app.include_router(_load_plugin_module().router, prefix="/api/plugins/kanban")
    denied = TestClient(app)

    assert denied.get(f"/api/plugins/kanban/tasks/{parked}/resume-preview").status_code == 401
    assert denied.post(f"/api/plugins/kanban/tasks/{parked}/resume-authorize",
                       json={"task_id": parked, "board_identity": "x", "run_id": 1,
                             "occurrence_event_id": 1, "pr_url": PR, "comment_digest": "x",
                             "lifecycle": "ready", "attest_lineage": True}).status_code == 401
    conn = kbc.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM task_resume_receipts").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_confirmation_for_another_task_is_refused(client, parked):
    expected = _preview(client, parked)["expected"]
    conn = kbc.connect()
    try:
        other = kb.create_task(conn, title="unrelated", assignee="a")
    finally:
        conn.close()
    response = client.post(
        f"/api/plugins/kanban/tasks/{other}/resume-authorize",
        json={**expected, "attest_lineage": True})
    assert response.status_code == 400
