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

import os
from typing import Any, Iterable, Optional


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2

#: Marker the conversation loop stamps on the synthetic nudge it injects.
#: Owned here so the guard can count its own prior nudges in a resumed
#: transcript, and so the loop and the guard cannot drift on the spelling.
SYNTHETIC_NUDGE_FLAG = "_kanban_stop_synthetic"


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_STOP_NUDGE`` does not disable it AND the board still
    shows this process holding a live claim on the task it names.

    The environment variable alone used to be the whole test, and it is only a
    routing hint: a desktop follow-up, an in-process cron tick, a delegated
    child or a replayed transcript inherits the same strings long after that run
    settled.  The guard then fabricated "task X is still running" from nothing —
    the t_74e92ef2 / run 2499 regression, where an ordinary answer to an
    ordinary question was discarded and replaced with a demand to call
    ``kanban_complete`` on a card that had been done for hours.

    Genuine worker completion enforcement is untouched: a live claim still
    enables the guard.  See
    :func:`agent.delegation_context.live_dispatcher_worker_task`, which is the
    one place this authority is defined.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return live_worker_task_id() is not None


def live_worker_task_id() -> Optional[str]:
    """The task this process is genuinely the live worker for, else ``None``.

    Fails closed on any lookup failure: an unprovable claim must never take a
    real final answer away from the user.
    """
    if not (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return None
    try:
        from agent.delegation_context import live_dispatcher_worker_task

        return live_dispatcher_worker_task()
    except Exception:
        return None


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


def _synthetic_nudges_in(messages: Iterable[dict] | None) -> int:
    """How many stop-guard nudges this conversation already carries."""
    if not messages:
        return 0
    return sum(
        1 for msg in messages
        if isinstance(msg, dict) and msg.get(SYNTHETIC_NUDGE_FLAG)
    )


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire: this process is not the
    live worker for the task its environment names, the session already
    completed/blocked, or the nudge budget is exhausted.
    """
    live_task = live_worker_task_id()
    if live_task is None or not kanban_stop_nudge_enabled():
        return None
    # A resumed transcript can already carry nudges from an earlier turn, while
    # ``attempts`` lives on the agent object and starts at zero for the new
    # session. Counting what is actually in the conversation keeps the budget a
    # property of the conversation rather than of the process that resumed it.
    spent = max(int(attempts), _synthetic_nudges_in(messages))
    if spent >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or live_task).strip() or "this task"
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
    "SYNTHETIC_NUDGE_FLAG",
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "live_worker_task_id",
    "session_called_kanban_terminal",
]
