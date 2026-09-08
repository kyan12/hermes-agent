"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(worker_board):
    task_id = worker_board.task_id
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
    assert task_id in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
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






# ---------------------------------------------------------------------------
# The board is the authority, not the inherited environment
# ---------------------------------------------------------------------------
#
# ``HERMES_KANBAN_*`` is a routing hint the dispatcher exports into the process
# it spawned. A desktop/TUI follow-up, a cron tick, a delegated child or a
# replayed transcript can carry the very same strings long after that run
# closed — the literal t_74e92ef2 / run 2499 regression, where a completed
# card's binding made an ordinary follow-up answer get thrown away and replaced
# by "task is still running, call kanban_complete".
#
# So the guard fires only when the board itself still shows this exact run
# holding a live claim on that exact task. Everything else preserves the real
# answer. True worker completion enforcement is untouched: a genuinely live
# worker that narrates its exit is still nudged.

import time
from pathlib import Path
from types import SimpleNamespace


@pytest.fixture
def worker_board(tmp_path, monkeypatch):
    """A real dispatcher-spawned worker: live board plus the exported claim."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="worker task", assignee="code-crab")
        claimed = kb.claim_task(conn, task_id, claimer="code-crab")
        assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)
        yield SimpleNamespace(
            kb=kb, conn=conn, task_id=task_id,
            run_id=int(claimed.current_run_id), claim_lock=claimed.claim_lock,
            monkeypatch=monkeypatch,
        )
    finally:
        conn.close()


_NARRATED_EXIT = [
    {"role": "user", "content": "work the task"},
    {"role": "assistant", "content": "Let me write the report now."},
]


def test_a_genuinely_live_worker_run_is_still_nudged(worker_board):
    """Real completion enforcement is untouched by the authority check."""
    nudge = build_kanban_stop_nudge(messages=list(_NARRATED_EXIT))
    assert nudge is not None
    assert worker_board.task_id in nudge
    assert "kanban_complete" in nudge


def test_a_completed_task_binding_never_replaces_the_final_answer(worker_board):
    """The t_74e92ef2 / run 2499 shape: the card is done, the env lingers."""
    kb = worker_board.kb
    assert kb.complete_task(
        worker_board.conn, worker_board.task_id, summary="done",
        expected_run_id=worker_board.run_id,
    )
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_replayed_completed_binding_stays_silent_every_time(worker_board):
    """Replaying the same stale transcript must not eventually fire."""
    kb = worker_board.kb
    assert kb.complete_task(
        worker_board.conn, worker_board.task_id, summary="done",
        expected_run_id=worker_board.run_id,
    )
    messages = list(_NARRATED_EXIT)
    for _ in range(4):
        assert build_kanban_stop_nudge(messages=messages) is None
        messages = messages + [
            {"role": "assistant", "content": "Here is the answer you asked for."},
        ]


def test_a_task_run_mismatch_is_never_nudged(worker_board):
    """A newer run of the same card is not this process's authority."""
    worker_board.monkeypatch.setenv(
        "HERMES_KANBAN_RUN_ID", str(worker_board.run_id + 99)
    )
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_foreign_claim_lock_is_never_nudged(worker_board):
    worker_board.monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "someone-elses")
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_missing_claim_lock_is_never_nudged(worker_board):
    worker_board.monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_reaped_run_row_is_never_nudged(worker_board):
    """``tasks`` lags: only the run row settles when a reaper takes the claim."""
    worker_board.conn.execute(
        "UPDATE task_runs SET status='reclaimed', ended_at=?, claim_lock=NULL, "
        "claim_expires=NULL WHERE id=?",
        (int(time.time()), worker_board.run_id),
    )
    worker_board.conn.commit()
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_an_expired_claim_is_never_nudged(worker_board):
    past = int(time.time()) - 60
    worker_board.conn.execute(
        "UPDATE tasks SET claim_expires=? WHERE id=?",
        (past, worker_board.task_id),
    )
    worker_board.conn.execute(
        "UPDATE task_runs SET claim_expires=? WHERE id=?",
        (past, worker_board.run_id),
    )
    worker_board.conn.commit()
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_task_id_this_board_does_not_know_is_never_nudged(worker_board):
    """A stale binding from another board must not fabricate a live worker."""
    worker_board.monkeypatch.setenv("HERMES_KANBAN_TASK", "t_74e92ef2")
    worker_board.monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "2499")
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_delegated_child_is_never_nudged(worker_board):
    """The variables still belong to the parent worker, not this sub-agent."""
    from agent.delegation_context import delegated_child_context

    with delegated_child_context():
        assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_an_in_process_cron_tick_is_never_nudged(worker_board):
    """A cron/scheduled session inside a worker process is not the worker."""
    from agent.delegation_context import non_dispatcher_owned_context

    with non_dispatcher_owned_context():
        assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None


def test_a_prior_synthetic_nudge_in_the_transcript_counts_as_an_attempt(
    worker_board
):
    """A resumed transcript must not restart the nudge budget from zero.

    ``attempts`` lives on the agent object, so a follow-up session that
    replays a transcript already carrying two nudges would otherwise inject
    two more on top of them.
    """
    nudge = build_kanban_stop_nudge(messages=list(_NARRATED_EXIT))
    assert nudge is not None
    replayed = list(_NARRATED_EXIT) + [
        {"role": "user", "content": nudge, "_kanban_stop_synthetic": True},
        {"role": "assistant", "content": "Let me write the report now."},
        {"role": "user", "content": nudge, "_kanban_stop_synthetic": True},
        {"role": "assistant", "content": "Let me write the report now."},
    ]
    assert build_kanban_stop_nudge(messages=replayed, attempts=0) is None


def test_a_board_lookup_failure_preserves_the_final_answer(worker_board):
    """Unprovable authority is not authority: fail closed, keep the answer."""
    from hermes_cli import kanban_db as kb

    def _boom(*_a, **_kw):
        raise RuntimeError("board unavailable")

    worker_board.monkeypatch.setattr(kb, "connect_closing", _boom)
    assert build_kanban_stop_nudge(messages=list(_NARRATED_EXIT)) is None
