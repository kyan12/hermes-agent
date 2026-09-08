"""Interactive server entrypoints must not inherit their launching worker."""
import os

import pytest


class ReachedRuntime(Exception):
    pass


@pytest.mark.parametrize("surface", ["desktop", "dashboard", "tui"])
def test_interactive_entrypoint_drops_worker_identity_before_runtime(monkeypatch, surface):
    inherited = {
        "HERMES_KANBAN_TASK": "t_finished",
        "HERMES_KANBAN_RUN_ID": "2495",
        "HERMES_KANBAN_DB": "/tmp/worker-board.db",
        "HERMES_KANBAN_BOARD": "worker-board",
        "HERMES_KANBAN_WORKSPACE": "/tmp/worker-worktree",
        "HERMES_KANBAN_BRANCH": "worker-branch",
        "HERMES_KANBAN_CLAIM_LOCK": "worker-lock",
        "HERMES_KANBAN_GOAL_MODE": "1",
        "HERMES_KANBAN_GOAL_MAX_TURNS": "20",
        "HERMES_SESSION_SOURCE": "kanban",
        "TERMINAL_CWD": "/tmp/worker-worktree",
    }
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", "/tmp/explicit-interactive-profile")

    def stop(*args, **kwargs):
        raise ReachedRuntime

    with pytest.raises(ReachedRuntime):
        if surface == "tui":
            from tui_gateway import entry
            monkeypatch.setattr(entry, "_install_sidecar_publisher", stop)
            entry.main()
        else:
            from hermes_cli import web_server
            monkeypatch.setattr(web_server, "_apply_ssh_session_token", stop)
            web_server.start_server(headless=surface == "desktop")
    assert {key: os.environ[key] for key in inherited if key in os.environ} == {}
    assert os.environ["HERMES_HOME"] == "/tmp/explicit-interactive-profile"


def test_standalone_board_and_explicit_cwd_are_preserved(monkeypatch):
    from tui_gateway.interactive_env import clear_inherited_worker_identity

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/operator-board.db")
    monkeypatch.setenv("TERMINAL_CWD", "/tmp/user-project")
    clear_inherited_worker_identity()
    assert os.environ["HERMES_KANBAN_DB"] == "/tmp/operator-board.db"
    assert os.environ["TERMINAL_CWD"] == "/tmp/user-project"


def test_worker_cleanup_preserves_explicit_cwd_and_is_idempotent(monkeypatch):
    from tui_gateway.interactive_env import clear_inherited_worker_identity

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_finished")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/tmp/worker-project")
    monkeypatch.setenv("TERMINAL_CWD", "/tmp/user-project")
    clear_inherited_worker_identity()
    cleared = dict(os.environ)
    clear_inherited_worker_identity()
    assert dict(os.environ) == cleared
    assert os.environ["TERMINAL_CWD"] == "/tmp/user-project"


def test_delegated_child_lineage_is_dropped_at_interactive_startup(monkeypatch):
    """A desktop launched from inside a delegate_task child is not that child.

    ``scrub_kanban_env`` strips HERMES_KANBAN_* but stamps
    ``HERMES_DELEGATED_CHILD_CONTEXT=1``, so the marker reaches an interactive
    backend without any task var beside it.  Left in place it makes the whole
    human-facing process fail closed on board mutations forever.
    """
    from agent.delegation_context import (
        DELEGATED_CHILD_ENV_MARKER,
        is_delegated_child_process_context,
    )
    from tui_gateway.interactive_env import clear_inherited_worker_identity

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
    clear_inherited_worker_identity()
    assert DELEGATED_CHILD_ENV_MARKER not in os.environ
    assert not is_delegated_child_process_context()


def test_dashboard_command_scrubs_before_touching_its_arguments(monkeypatch):
    """`hermes dashboard` / `hermes serve` scrub ahead of all startup work.

    ``cmd_dashboard`` starts the background MCP discovery thread — which spawns
    stdio MCP servers with the process environment — before it ever reaches
    ``start_server``.  The scrub has to happen before any of that.
    """
    from hermes_cli import main as cli_main

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_finished")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "2495")
    monkeypatch.setenv("HERMES_KANBAN_DB", "/tmp/worker-board.db")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")

    class TripOnFirstRead:
        def __getattr__(self, name):
            raise ReachedRuntime(name)

    with pytest.raises(ReachedRuntime):
        cli_main.cmd_dashboard(TripOnFirstRead())
    assert "HERMES_KANBAN_TASK" not in os.environ
    assert "HERMES_KANBAN_RUN_ID" not in os.environ
    assert "HERMES_KANBAN_DB" not in os.environ
    assert "HERMES_SESSION_SOURCE" not in os.environ


def test_worker_identity_is_only_cleared_at_dedicated_entrypoints():
    """No tool, turn, or agent path may delete process-wide worker identity.

    Clearing ``os.environ`` mid-turn would race the worker's own claim
    heartbeat and the gateway's board watchers; in-process isolation belongs to
    ``agent.delegation_context``'s ContextVars instead.
    """
    import pathlib
    import subprocess

    repo = pathlib.Path(__file__).resolve().parents[2]
    hits = subprocess.run(
        ["git", "grep", "-l", "--untracked", "clear_inherited_worker_identity", "--", "*.py"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.split()
    callers = {h for h in hits if not h.startswith("tests/")}
    assert callers == {
        "hermes_cli/main.py",
        "hermes_cli/main_tui_launch.py",
        "hermes_cli/web_server.py",
        "tui_gateway/entry.py",
        "tui_gateway/interactive_env.py",
    }
