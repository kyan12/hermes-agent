"""Process-start isolation for human-facing servers launched by a worker.

Call only at dedicated interactive server entrypoints, before agents or worker
threads start. Never clear process-wide worker identity inside a tool or turn.
"""
from __future__ import annotations

import os


def clear_inherited_worker_identity() -> None:
    """Detach a new interactive process from its launching Kanban run.

    Standalone board selection and explicitly chosen profile/cwd stay intact.
    The dispatcher supplies fresh worker identity to its own subprocesses; this
    does not change lifecycle checks or permissions in the launching worker.
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE")
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            os.environ.pop(key, None)
    if os.environ.get("HERMES_SESSION_SOURCE") == "kanban":
        os.environ.pop("HERMES_SESSION_SOURCE", None)
    if workspace and os.environ.get("TERMINAL_CWD") == workspace:
        os.environ.pop("TERMINAL_CWD", None)
