"""Process-start isolation for human-facing servers launched by a worker.

Call only at dedicated interactive server entrypoints, before agents or worker
threads start. Never clear process-wide worker identity inside a tool or turn.
"""
from __future__ import annotations

import os
from typing import MutableMapping, Optional


def clear_inherited_worker_identity(
    env: Optional[MutableMapping[str, str]] = None,
) -> None:
    """Detach a new interactive process from its launching Kanban run.

    Standalone board selection and explicitly chosen profile/cwd stay intact.
    The dispatcher supplies fresh worker identity to its own subprocesses; this
    does not change lifecycle checks or permissions in the launching worker.

    Idempotent, so a restart / in-place update path may call it as often as it
    likes; safe to call in a process that never had worker identity at all.
    """
    target = os.environ if env is None else env
    _drop_delegated_child_lineage(target)
    if not target.get("HERMES_KANBAN_TASK"):
        return
    workspace = target.get("HERMES_KANBAN_WORKSPACE")
    for key in tuple(target):
        if key.startswith("HERMES_KANBAN_"):
            target.pop(key, None)
    if target.get("HERMES_SESSION_SOURCE") == "kanban":
        target.pop("HERMES_SESSION_SOURCE", None)
    if workspace and target.get("TERMINAL_CWD") == workspace:
        target.pop("TERMINAL_CWD", None)


def _drop_delegated_child_lineage(env: MutableMapping[str, str]) -> None:
    """Forget a delegate_task lineage marker inherited across the spawn.

    ``scrub_kanban_env`` strips the HERMES_KANBAN_* vars from a child's env but
    stamps ``HERMES_DELEGATED_CHILD_CONTEXT`` so the lineage survives the fork.
    An interactive server launched from inside such a child would otherwise
    spend its whole life as that child: ``is_delegated_child_process_context``
    stays true, and every board mutation the human asks for fails closed.
    Handled independently of ``HERMES_KANBAN_TASK`` precisely because the
    scrub that sets the marker is the same one that removes the task var.
    """
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
