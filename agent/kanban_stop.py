"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from agent.delegation_context import is_dispatcher_owned_worker_context

logger = logging.getLogger(__name__)

_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2

# Session surfaces a dispatcher worker can legitimately run on. The dispatcher
# stamps ``kanban``; workers spawned by older builds carry nothing at all.
# Allowlisted rather than denylisted: every other surface — desktop, tui, web,
# acp, a chat platform, a surface added next month — is someone else's turn,
# and a turn we cannot classify must not be answered with the worker protocol.
_WORKER_SESSION_SOURCES = frozenset({"kanban", ""})

# Bounded so a board locked by a writer costs a turn a few hundred ms, never a
# stall. Losing the race denies the nudge (see the module docstring's
# fail-closed rule) instead of blocking the reply.
_BOARD_READ_TIMEOUT_SECONDS = 0.5

# Task + run + claim must ALL still name this worker on the board it was
# pinned to. ``tasks.current_run_id`` alone is a denormalised pointer; joining
# ``task_runs`` proves the run row itself belongs to this task and is still
# open, which is what a completed worker's inherited env can never satisfy.
_OWNERSHIP_SQL = """
SELECT 1
  FROM tasks t
  JOIN task_runs r ON r.id = t.current_run_id
 WHERE t.id = :task
   AND t.status = 'running'
   AND t.current_run_id = :run_id
   AND r.id = :run_id
   AND r.task_id = t.id
   AND r.status = 'running'
   AND r.ended_at IS NULL
   AND t.claim_lock = :lock
   AND r.claim_lock = :lock
   AND t.claim_expires IS NOT NULL
   AND r.claim_expires IS NOT NULL
   AND t.claim_expires > :now
   AND r.claim_expires > :now
"""


def _pinned_board_db() -> Optional[Path]:
    """Return the board DB this process was pinned to, or ``None``.

    The dispatcher injects both ``HERMES_KANBAN_DB`` and ``HERMES_KANBAN_BOARD``
    so a worker is immune to path-resolution disagreement. Here they are two
    independent identity claims: when both are present they must agree, or the
    "board" being read is not demonstrably the board this identity came from.

    Never falls back to the operator's on-disk board selection — an inherited
    task id read against whatever board the human happens to have switched to
    is exactly the confusion this guard exists to prevent.
    """
    from hermes_cli.kanban_db import board_db_path

    pinned = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if not pinned and not board:
        return None
    if not board:
        return Path(pinned).expanduser()
    # Raises ValueError on a malformed slug -> caller fails closed.
    resolved = board_db_path(board)
    if pinned and Path(pinned).expanduser().resolve() != resolved.resolve():
        logger.debug("Kanban stop guard: pinned DB does not match pinned board")
        return None
    return resolved


def _owns_live_run() -> bool:
    """Read-only proof that this process is the board's current worker."""
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task or not is_dispatcher_owned_worker_context():
        return False
    from gateway.session_context import get_session_env

    if get_session_env("HERMES_SESSION_SOURCE") not in _WORKER_SESSION_SOURCES:
        return False
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
        if run_id <= 0:
            return False
        claim_lock = (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
        if not claim_lock:
            return False
        db_path = _pinned_board_db()
        if db_path is None:
            return False
        # ``mode=ro`` opens an existing board and never runs the create /
        # migrate path: a board that is absent, unreadable, or not a database
        # raises here rather than being brought into existence.
        with closing(sqlite3.connect(
            db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=_BOARD_READ_TIMEOUT_SECONDS,
        )) as conn:
            row = conn.execute(_OWNERSHIP_SQL, {
                "task": task,
                "run_id": run_id,
                "lock": claim_lock,
                "now": int(time.time()),
            }).fetchone()
        return row is not None
    except (OSError, ValueError, sqlite3.Error):
        # Deliberately narrow: cancellation and interrupts must propagate, not
        # be recorded as "not a worker".
        logger.debug(
            "Kanban stop guard could not verify live run ownership", exc_info=True
        )
        return False


def kanban_stop_nudge_enabled() -> bool:
    """Nudge only a verified live dispatcher run, never inherited identity.

    Read the dispatcher-pinned board without initializing or migrating it.
    On unavailable evidence, leave enforcement to the dispatcher rather than
    claiming a task is running and replacing an unrelated human reply.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return _owns_live_run()


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    owned = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if task_id is not None and task_id.strip() != owned:
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = owned or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
]
