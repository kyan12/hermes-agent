"""The control loop must be wired into the surfaces that actually run.

A health module nothing calls is decoration. These tests pin the seams:

* the dispatcher tick runs reconciliation and reports what it moved,
* the dispatcher tick is what stamps the durable wake checkpoint (so a
  ``wake`` hold on an attended board is provably wakeable),
* a card that comes due is resumed AND promoted in the same tick,
* the dashboard exposes the same payload the CLI prints,
* the CLI commands exit non-zero on an unhealthy board.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dispatcher tick
# ---------------------------------------------------------------------------


def test_dispatch_tick_stamps_the_durable_wake_checkpoint(board, all_assignees_spawnable):
    """Without this the wake subsystem can never report healthy on a live
    board — the checkpoint IS the proof that something is running."""
    assert kh.read_checkpoint(board, kh.CHECKPOINT_RECONCILE) is None
    kb.dispatch_once(board, spawn_fn=lambda *a, **k: 4321)
    checkpoint = kh.read_checkpoint(board, kh.CHECKPOINT_RECONCILE)
    assert checkpoint is not None
    assert checkpoint["status"] == "ok"


def test_dispatch_tick_resumes_a_due_typed_wake_and_reports_it(
    board, all_assignees_spawnable
):
    tid = kb.create_task(board, title="due wake", assignee="alice")
    kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 60, apply=True)

    result = kb.dispatch_once(board, spawn_fn=lambda *a, **k: 4321)
    assert tid in result.health_resumed
    assert kb.get_task(board, tid).status in ("ready", "running")


def test_dispatch_tick_reports_holds_it_refused_to_resume(
    board, all_assignees_spawnable
):
    tid = kb.create_task(board, title="legacy hold", assignee="alice")
    kb.schedule_task(board, tid, reason="prose only")

    result = kb.dispatch_once(board, spawn_fn=lambda *a, **k: 4321)
    assert tid in result.health_needs_attention
    assert tid not in result.health_resumed
    assert kb.get_task(board, tid).status == "scheduled"


def test_a_hold_that_comes_due_is_spawned_in_the_same_tick(
    board, all_assignees_spawnable
):
    """Reconciliation runs BEFORE promotion so a due card does not idle for a
    whole dispatcher interval."""
    spawned = []
    tid = kb.create_task(board, title="due now", assignee="alice")
    kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 1, apply=True)

    def _spawn(task, workspace, *a, **k):
        spawned.append(task.id)
        return 4321

    kb.dispatch_once(board, spawn_fn=_spawn)
    assert tid in spawned


def test_intentional_parks_survive_a_dispatcher_tick(board, all_assignees_spawnable):
    tid = kb.create_task(board, title="waiting on counsel", assignee="alice")
    kh.set_hold(board, tid, kind="external", apply=True)

    kb.dispatch_once(board, spawn_fn=lambda *a, **k: 4321)
    assert kb.get_task(board, tid).status == "scheduled"
    assert kb.get_task(board, tid).hold_kind == "external"


def test_dry_run_tick_does_not_reconcile(board, all_assignees_spawnable):
    tid = kb.create_task(board, title="due wake", assignee="alice")
    kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 60, apply=True)

    kb.dispatch_once(board, spawn_fn=lambda *a, **k: 4321, dry_run=True)
    assert kb.get_task(board, tid).status == "scheduled"


def test_reconciler_can_be_switched_off_without_breaking_dispatch(
    board, all_assignees_spawnable, monkeypatch
):
    monkeypatch.setattr(kh, "reconcile_enabled", lambda: False)
    tid = kb.create_task(board, title="due wake", assignee="alice")
    kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 60, apply=True)
    ordinary = kb.create_task(board, title="ordinary", assignee="alice")

    spawned = []
    kb.dispatch_once(
        board, spawn_fn=lambda t, w, *a, **k: (spawned.append(t.id), 4321)[1]
    )
    # Dispatch still works …
    assert ordinary in spawned
    # … but nothing woke, and the board says so rather than staying silent.
    assert kb.get_task(board, tid).status == "scheduled"
    health = kh.board_health(board)
    assert health["wake"]["reason_code"] == kh.REASON_WAKE_DISABLED


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv):
    import argparse

    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kcli.build_parser(sub)
    return kcli.kanban_command(parser.parse_args(argv))


def test_board_health_cli_exits_nonzero_on_an_unhealthy_board(board, capsys):
    tid = kb.create_task(board, title="legacy", assignee="alice")
    kb.schedule_task(board, tid, reason="prose")

    assert _run_cli(["kanban", "board-health"]) == 1
    out = capsys.readouterr().out
    assert "UNHEALTHY" in out
    assert kh.REASON_LEGACY_UNTYPED in out


def test_board_health_cli_exits_zero_on_a_healthy_board(board, capsys):
    kb.create_task(board, title="ordinary ready card", assignee="alice")
    assert _run_cli(["kanban", "board-health"]) == 0
    assert "OK" in capsys.readouterr().out


def test_scheduled_wake_cli_reconciles_and_reports(board, capsys):
    tid = kb.create_task(board, title="due", assignee="alice")
    kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 5, apply=True)

    _run_cli(["kanban", "scheduled-wake", "--reconcile"])
    assert kb.get_task(board, tid).status == "ready"


def test_schedule_cli_refuses_an_unarmed_wake_hold(board):
    tid = kb.create_task(board, title="x", assignee="alice")
    assert _run_cli(["kanban", "schedule", tid, "--kind", "wake"]) == 2
    assert kb.get_task(board, tid).status == "ready"


def test_schedule_cli_types_the_hold(board):
    tid = kb.create_task(board, title="x", assignee="alice")
    assert _run_cli(
        ["kanban", "schedule", tid, "later", "--kind", "wake", "--wake-at", "+3600"]
    ) == 0
    task = kb.get_task(board, tid)
    assert task.status == "scheduled"
    assert task.hold_kind == "wake"
    assert task.hold_wake_at > int(time.time())


def test_sentinel_cli_runs_and_reports(board, capsys):
    assert _run_cli(["kanban", "sentinel", "--dry-run"]) in (0, 1)
    assert "kanban sentinel" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------


def test_dashboard_board_health_matches_the_cli_payload(board):
    from plugins.kanban.dashboard import plugin_api

    tid = kb.create_task(board, title="legacy", assignee="alice")
    kb.schedule_task(board, tid, reason="prose")

    response = plugin_api.get_board_health(
        board=None, all_boards=False, ready_queue=False
    )
    assert response["healthy"] is False
    assert response["control_loop_version"] == kh.CONTROL_LOOP_VERSION
    payload = response["boards"][0]
    assert tid in {h["task_id"] for h in payload["holds"]}
    assert payload == kh.board_health(board, board=payload["board"], now=payload["generated_at"])
