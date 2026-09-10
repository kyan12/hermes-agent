"""The dashboard's board response and its event cursor must describe one moment.

``GET /board`` returns the whole board *and* a ``latest_event_id`` the client
uses as its ``since=`` when it opens the ``/events`` socket. That pair is a
promise: "here is the board, and here is where the stream continues from". The
two halves were read on an autocommit connection, one statement at a time, so a
write that landed between them was absent from the board **and** already behind
the cursor. Nobody would ever mention it again: the socket only sends ids
greater than the cursor, and the board it was reconciling against never had it.

That is not a slow update, it is a lost one — and it is silent, which is why it
reads as "the dashboard is stale until I hit reload".

These are behaviour contracts on the REAL router and REAL SQLite: what the
response promises, not which statements produce it.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_freshness_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
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
    kb.init_db()
    return home


@pytest.fixture
def ws_auth(monkeypatch):
    """Accept the socket upgrade.

    The endpoint delegates to the dashboard's canonical gate; these tests are
    about the stream, not the gate, which has its own coverage in
    ``test_kanban_dashboard_plugin.py``.
    """
    import types

    import hermes_cli

    stub = types.SimpleNamespace(_SESSION_TOKEN="", _ws_auth_ok=lambda ws: True)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", stub)
    monkeypatch.setattr(hermes_cli, "web_server_chat", stub, raising=False)


@pytest.fixture
def plugin(kanban_home):
    return _load_plugin_module()


@pytest.fixture
def client(plugin, ws_auth):
    app = FastAPI()
    app.include_router(plugin.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _create(title: str) -> str:
    """Create a task on a connection of its own, before the request starts."""
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=title, assignee="a")
    finally:
        conn.close()


def _task_ids(payload) -> set[str]:
    return {
        task["id"]
        for column in payload["columns"]
        for task in column["tasks"]
    }


def _first_event_id(task_id: str) -> int:
    conn = kbc.connect()
    try:
        row = conn.execute(
            "SELECT MIN(id) AS m FROM task_events WHERE task_id = ?", (task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["m"] is not None, f"{task_id} left no event"
    return int(row["m"])


def test_the_board_cursor_never_covers_a_task_the_board_omits(client, plugin, monkeypatch):
    """A write landing mid-response is either IN the board or AFTER the cursor.

    The intruder is committed from a separate connection, part-way through
    building the response — after the tasks have been read and before the cursor
    is. Under one read snapshot the response simply predates it and the cursor
    says so; without one, the board is missing a card that the client will never
    be told about, because its own cursor already claims to have seen it.
    """
    _create("already there")

    intruders: list[str] = []
    real_diagnostics = plugin._compute_task_diagnostics

    def write_between_the_halves(conn, task_ids=None):
        # Once, and on a connection of its own: this is another writer, not a
        # re-entrant call on the request's connection.
        if not intruders:
            other = kbc.connect()
            try:
                intruders.append(kb.create_task(other, title="landed mid-response", assignee="a"))
            finally:
                other.close()
        return real_diagnostics(conn, task_ids=task_ids)

    monkeypatch.setattr(plugin, "_compute_task_diagnostics", write_between_the_halves)

    response = client.get("/api/plugins/kanban/board")
    assert response.status_code == 200
    payload = response.json()
    assert intruders, "the intruding write never ran; the test proved nothing"

    cursor = payload["latest_event_id"]
    present = _task_ids(payload)
    for task_id in intruders:
        if task_id in present:
            continue
        assert _first_event_id(task_id) > cursor, (
            f"task {task_id} is absent from the board and its creation event is "
            f"already behind the response's cursor ({cursor}): a client resuming "
            "the stream there is never told the card exists"
        )


def test_the_board_cursor_never_covers_a_comment_the_rollups_omit(client, plugin, monkeypatch):
    """Same contract for the aggregate rollups, which are separate statements.

    The comment counts are their own query. A comment committed after them and
    before the cursor is invisible in the response and behind the cursor.
    """
    task_id = _create("has comments")

    added: list[int] = []
    real_diagnostics = plugin._compute_task_diagnostics

    def comment_between_the_halves(conn, task_ids=None):
        if not added:
            other = kbc.connect()
            try:
                kb.add_comment(other, task_id, "human", "landed mid-response")
                row = other.execute(
                    "SELECT MAX(id) AS m FROM task_events WHERE task_id = ?", (task_id,),
                ).fetchone()
                added.append(int(row["m"]))
            finally:
                other.close()
        return real_diagnostics(conn, task_ids=task_ids)

    monkeypatch.setattr(plugin, "_compute_task_diagnostics", comment_between_the_halves)

    payload = client.get("/api/plugins/kanban/board").json()
    assert added, "the intruding comment never ran; the test proved nothing"

    counts = {
        task["id"]: task["comment_count"]
        for column in payload["columns"] for task in column["tasks"]
    }
    if counts.get(task_id, 0) == 0:
        assert added[0] > payload["latest_event_id"], (
            "the board shows no comment on the task and the comment's event is "
            "already behind the response's cursor: the count never updates"
        )


def test_the_board_still_reflects_writes_that_completed_before_the_request(client):
    """The snapshot must be a snapshot of *now*, not a cached earlier one."""
    first = client.get("/api/plugins/kanban/board").json()
    task_id = _create("created between requests")
    second = client.get("/api/plugins/kanban/board").json()

    assert task_id not in _task_ids(first)
    assert task_id in _task_ids(second)
    assert second["latest_event_id"] >= _first_event_id(task_id)


def test_the_board_read_does_not_block_a_concurrent_writer(client):
    """A read snapshot must not become a write lock.

    ``BEGIN DEFERRED`` on a WAL database takes a read snapshot and no more; if
    the board response ever escalated to a write transaction it would stall
    every dispatcher tick behind a dashboard poll.
    """
    committed: list[str] = []
    errors: list[BaseException] = []
    started = threading.Event()

    def write_during_the_read(conn, task_ids=None):
        started.set()
        thread = threading.Thread(target=_write_from_another_connection, args=(committed, errors))
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "a concurrent write blocked on the board read"
        return {}

    module = _load_plugin_module()
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/kanban")
    module._compute_task_diagnostics = write_during_the_read

    payload = TestClient(app).get("/api/plugins/kanban/board").json()
    assert started.is_set()
    assert not errors, f"the concurrent write failed: {errors[:2]}"
    assert committed
    assert payload["latest_event_id"] >= 0


def _write_from_another_connection(committed, errors):
    try:
        other = kbc.connect()
        try:
            committed.append(kb.create_task(other, title="written during the read", assignee="a"))
        finally:
            other.close()
    except BaseException as exc:  # noqa: BLE001 - surfaced by the caller
        errors.append(exc)


# --- the event socket -------------------------------------------------------
#
# The client's ``since=`` is where the stream continues from, and it used to be
# obtainable in exactly one way: from a board response that rendered. A board
# that 500s therefore left the socket with nowhere to start — the UI would not
# even open one — and a board *switch* carried the previous board's cursor onto
# the new board's socket, which is a socket that connects, upgrades and then
# says nothing at all until the new board's ids happen to pass the old board's
# maximum. Both look identical from the outside: a live connection, no frames.

def _drain_bootstrap(socket) -> dict:
    """The first frame on every socket says where the stream begins."""
    frame = socket.receive_json()
    assert frame.get("type") == "bootstrap", f"first frame was not a bootstrap: {frame}"
    return frame


def test_a_socket_announces_where_the_stream_begins(client):
    """The cursor is available from the socket itself, not only from /board.

    Without this the only source of a board-scoped cursor is a board render, so
    a board that cannot render cannot recover: the client has no position to
    resume from and no way to learn one.
    """
    _create("before the socket")
    with client.websocket_connect("/api/plugins/kanban/events") as socket:
        frame = _drain_bootstrap(socket)
    conn = kbc.connect()
    try:
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
    finally:
        conn.close()
    assert frame["cursor"] == int(latest)


def test_a_socket_with_no_since_starts_from_now_and_not_from_history(client):
    """An unanchored client is caught up, not replayed at.

    ``since`` absent used to parse as 0, so a client that had lost its position
    was sent the whole history of the board — on a long-lived board that is a
    flood, which is why the UI gated the socket on a rendered board in the first
    place. Announcing the current position removes the reason for the gate.
    """
    _create("history")
    with client.websocket_connect("/api/plugins/kanban/events") as socket:
        bootstrap = _drain_bootstrap(socket)
        after = _create("after the socket opened")
        frame = socket.receive_json()

    assert frame["cursor"] > bootstrap["cursor"]
    delivered = {event["task_id"] for event in frame["events"]}
    assert delivered == {after}, (
        f"an unanchored socket replayed history instead of tailing: {delivered}")


def test_an_explicit_since_still_replays_from_there(client):
    """``since=0`` keeps meaning "everything": the parameter is unchanged."""
    task_id = _create("historic")
    with client.websocket_connect("/api/plugins/kanban/events?since=0") as socket:
        _drain_bootstrap(socket)
        frame = socket.receive_json()
    assert task_id in {event["task_id"] for event in frame["events"]}


def test_every_frame_names_the_board_it_came_from(client):
    """A client cannot tell a stale socket's frames from its own without this.

    Board switches close one socket and open another; the old one's in-flight
    frames and its pending reconnect both survive the switch, and a frame from
    the wrong board applied to the current board is a phantom update.
    """
    with client.websocket_connect("/api/plugins/kanban/events?board=default") as socket:
        bootstrap = _drain_bootstrap(socket)
        _create("named")
        frame = socket.receive_json()
    assert bootstrap["board"] == "default"
    assert frame["board"] == "default"


def test_the_bootstrap_arrives_on_an_empty_board(client):
    """Zero events is a position too — the client must not wait forever for one."""
    conn = kbc.connect()
    try:
        conn.execute("DELETE FROM task_events")
    finally:
        conn.close()
    with client.websocket_connect("/api/plugins/kanban/events") as socket:
        assert _drain_bootstrap(socket)["cursor"] == 0
