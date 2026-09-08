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
