"""The dashboard board endpoint renders the operator projection, not raw status.

Column totals, filters and the detail view have to agree, and none of them may
show a Triage or Review lane. This drives the real HTTP surface so a projection
that only exists in ``kanban_health`` cannot be mistaken for a shipped fix.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_kanban_plugin_operator_test", plugin_file
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def plugin(kanban_home):
    return _load_plugin_module()


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def cards(kanban_home):
    """One card in every internal stage that used to leak into the board."""
    conn = kb.connect()
    try:
        review = kb.create_task(conn, title="under review", assignee="alice")
        claimed = kb.claim_task(conn, review, claimer="alice")
        kb.request_review(
            conn, review, summary="ready", expected_run_id=claimed.current_run_id,
        )
        stalled = kb.create_task(conn, title="stalled source", assignee="alice")
        assert kb.claim_task(conn, stalled, claimer="alice") is not None
        assert kb.block_task(conn, stalled, reason="provider timed out",
                             kind="transient")
        waiting = kb.create_task(conn, title="waiting", assignee="alice")
        return {"review": review, "stalled": stalled, "waiting": waiting}
    finally:
        conn.close()


def _columns(client):
    body = client.get("/api/plugins/kanban/board").json()
    return {col["name"]: col["tasks"] for col in body["columns"]}


def test_the_board_has_no_triage_or_review_lane(client, cards):
    columns = _columns(client)
    assert "triage" not in columns
    assert "review" not in columns
    assert [name for name in columns if name != "archived"] == list(
        kh.OPERATOR_COLUMNS
    )


def test_every_live_card_lands_in_exactly_one_operator_lane(client, cards):
    columns = _columns(client)
    placed = [task["id"] for tasks in columns.values() for task in tasks]
    conn = kb.connect()
    try:
        live = [t.id for t in kb.list_tasks(conn, include_archived=False)]
    finally:
        conn.close()
    assert sorted(placed) == sorted(live)
    assert len(placed) == len(set(placed))


def test_review_reads_as_in_progress(client, cards):
    """Ordinary review is a stage of work in flight, not a lane for Kevin."""
    columns = _columns(client)
    assert cards["review"] in {task["id"] for task in columns["running"]}


def test_a_stalled_source_is_machine_owned_wherever_it_shows(client, cards):
    """With no reconciler lane configured, it is Blocked — owned by automation.

    The point is not which lane it lands in but that it never becomes a
    request for a person to classify it, and never disappears into Triage.
    """
    columns = _columns(client)
    card = next(
        task for tasks in columns.values() for task in tasks
        if task["id"] == cards["stalled"]
    )
    assert card["operator"]["lifecycle_status"] == "triage"
    assert card["operator"]["column"] in {"running", "blocked"}
    assert card["operator"]["owner"] == "machine"
    assert card["operator"]["next_owner"]
    if card["operator"]["column"] == "blocked":
        assert card["operator"]["action"]


def test_a_card_carries_its_durable_status_for_inspection(client, cards):
    columns = _columns(client)
    card = next(
        task for task in columns["running"] if task["id"] == cards["review"]
    )
    assert card["operator"]["lifecycle_status"] == "review"
    assert card["operator"]["stage"] == "review"
    assert card["operator"]["column"] == "running"


def test_the_detail_view_agrees_with_the_column_it_came_from(client, cards):
    columns = _columns(client)
    for name, tasks in columns.items():
        for task in tasks:
            detail = client.get(
                f"/api/plugins/kanban/tasks/{task['id']}"
            ).json()
            assert detail["operator"]["column"] == name, (
                f"{task['id']} sits in {name} but its detail view says "
                f"{detail['operator']['column']}"
            )


def test_the_board_counts_agree_with_the_columns(client, cards):
    columns = _columns(client)
    boards = client.get("/api/plugins/kanban/boards").json()
    entry = next(b for b in boards["boards"] if b.get("counts"))
    for name, tasks in columns.items():
        assert entry["counts"].get(name, 0) == len(tasks), name
    assert "triage" not in entry["counts"]
    assert "review" not in entry["counts"]


def test_the_detail_view_names_dependencies_by_title(client, kanban_home):
    conn = kb.connect()
    try:
        parent = kb.create_task(
            conn, title="Restore the provider credential", assignee="alice",
        )
        child = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent],
        )
        assert kb.claim_task(conn, parent, claimer="alice") is not None
    finally:
        conn.close()

    detail = client.get(f"/api/plugins/kanban/tasks/{child}").json()
    entry = detail["dependencies"]["parents"][0]
    assert entry["title"] == "Restore the provider credential"
    assert entry["label"] == "In progress"
    assert detail["dependencies"]["label"] == "Waiting on dependency"
