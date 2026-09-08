"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import sqlite3

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


# The guard reads live board rows, so these tests need a board rather than a
# bare env var: two tasks, each claimed under its own run, exactly as the
# dispatcher would leave them mid-flight.
RUNS = {"t_abc": 41, "t_46be8aa5": 42}


@pytest.fixture
def clear_kanban_env(monkeypatch, tmp_path):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE", "HERMES_SESSION_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    db = tmp_path / "board.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE tasks (id TEXT, status TEXT, current_run_id INTEGER, "
            "claim_lock TEXT, claim_expires INTEGER)"
        )
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER, task_id TEXT, status TEXT, "
            "claim_lock TEXT, claim_expires INTEGER, ended_at INTEGER)"
        )
        for task, run in RUNS.items():
            conn.execute(
                "INSERT INTO tasks VALUES (?, 'running', ?, 'test-claim', 4102444800)",
                (task, run),
            )
            conn.execute(
                "INSERT INTO task_runs VALUES (?, ?, 'running', 'test-claim', 4102444800, NULL)",
                (run, task),
            )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "test-claim")
    return monkeypatch


def become_worker(monkeypatch, task):
    """Take on the live board identity of ``task``, as the dispatcher would."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(RUNS[task]))






def test_env_can_disable(clear_kanban_env):
    become_worker(clear_kanban_env, "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    become_worker(clear_kanban_env, "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    become_worker(clear_kanban_env, "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




