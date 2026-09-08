"""Unscoped active work is only a finding when the Project mapping is
unambiguous.

Flagging every card without a ``project_id`` on a board that legitimately
serves several projects produces noise that operators learn to ignore. The
diagnostic fires only when the board has exactly one canonical Project, so
"this card should have been scoped to X" is a fact, not a guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_health as kh


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    try:
        yield conn
    finally:
        conn.close()


def _set_project(conn, task_id, project_id):
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET project_id = ? WHERE id = ?", (project_id, task_id)
        )


def test_unscoped_active_card_is_flagged_when_one_canonical_project_exists(board):
    scoped = kb.create_task(board, title="scoped", assignee="alice")
    _set_project(board, scoped, "proj-alpha")
    unscoped = kb.create_task(board, title="unscoped", assignee="alice")

    findings = kh.scope_diagnostics(board)
    flagged = {f["task_id"]: f for f in findings}
    assert unscoped in flagged
    assert flagged[unscoped]["reason_code"] == kh.REASON_UNSCOPED_ACTIVE_WORK
    assert flagged[unscoped]["canonical_project_id"] == "proj-alpha"
    assert scoped not in flagged


def test_ambiguous_project_mapping_produces_no_finding(board):
    a = kb.create_task(board, title="a", assignee="alice")
    _set_project(board, a, "proj-alpha")
    b = kb.create_task(board, title="b", assignee="alice")
    _set_project(board, b, "proj-beta")
    kb.create_task(board, title="unscoped", assignee="alice")

    assert kh.scope_diagnostics(board) == []


def test_board_with_no_project_mapping_produces_no_finding(board):
    kb.create_task(board, title="unscoped", assignee="alice")
    assert kh.scope_diagnostics(board) == []


def test_terminal_cards_are_not_flagged(board):
    scoped = kb.create_task(board, title="scoped", assignee="alice")
    _set_project(board, scoped, "proj-alpha")
    finished = kb.create_task(board, title="finished unscoped", assignee="alice")
    kb.complete_task(board, finished, result="ok")

    assert finished not in {f["task_id"] for f in kh.scope_diagnostics(board)}
