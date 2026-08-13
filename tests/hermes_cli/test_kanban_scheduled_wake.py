"""Regression coverage for durable typed Kanban scheduled holds."""
from __future__ import annotations

from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def test_due_timed_hold_wakes_and_dispatches_same_tick(kanban_home, monkeypatch):
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    spawned = []
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missed checkpoint", assignee="worker")
        assert kb.schedule_task(conn, task_id, schedule_kind="timed", wake_at=now - 60)
        result = kb.dispatch_once(
            conn, spawn_fn=lambda task, _workspace: spawned.append(task.id) or 12345,
            max_in_progress=2,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert task.wake_at is None and task.schedule_kind is None
        assert result.woken == [task_id]
        assert spawned == [task_id]


def test_dependency_hold_wakes_only_after_all_parents_terminal(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        assert kb.schedule_task(conn, child, schedule_kind="dependency")
        assert kb.reconcile_scheduled(conn) == []
        assert kb.complete_task(conn, parent, result="done")
        assert kb.reconcile_scheduled(conn) == [child]
        assert kb.get_task(conn, child).status == "ready"


def test_legacy_scheduled_child_with_done_parents_wakes_exactly_once(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="accepted parent", assignee="worker")
        child = kb.create_task(conn, title="legacy child", assignee="worker", parents=[parent])
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (child,))
        assert kb.complete_task(conn, parent, result="accepted")

        assert kb.reconcile_scheduled(conn) == [child]
        assert kb.reconcile_scheduled(conn) == []
        assert kb.get_task(conn, child).status == "ready"


def test_dependency_hold_does_not_wake_for_archived_unaccepted_parent(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="rejected parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
        assert kb.schedule_task(conn, child, schedule_kind="dependency")
        assert kb.archive_task(conn, parent)

        assert kb.reconcile_scheduled(conn) == []
        assert kb.get_task(conn, child).status == "scheduled"
        report = kb.audit_scheduled_tasks(conn)
        assert report["counts"]["dependency_unaccepted"] == 1


def test_ambiguous_and_incomplete_holds_are_rejected(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="hold", assignee="worker")
        with pytest.raises(ValueError, match="schedule_kind"):
            kb.schedule_task(conn, task_id)
        with pytest.raises(ValueError, match="wake_at"):
            kb.schedule_task(conn, task_id, schedule_kind="timed")
        with pytest.raises(ValueError, match="wake_job_id"):
            kb.schedule_task(conn, task_id, schedule_kind="external", checkpoint_at=9_000_000)
        with pytest.raises(ValueError, match="checkpoint_at"):
            kb.schedule_task(conn, task_id, schedule_kind="physical", wake_job_id="job_123")


def test_external_hold_persists_wake_job_and_checkpoint(kanban_home, monkeypatch):
    monkeypatch.setattr("cron.jobs.get_job", lambda job_id: {"id": job_id, "enabled": True})
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external", assignee="worker")
        assert kb.schedule_task(
            conn, task_id, schedule_kind="external", wake_job_id="job_123",
            checkpoint_at=9_000_000,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert (task.schedule_kind, task.wake_job_id, task.checkpoint_at) == (
            "external", "job_123", 9_000_000,
        )


def test_external_hold_rejects_missing_or_disabled_wake_job(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external", assignee="worker")
        monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: None)
        with pytest.raises(ValueError, match="enabled durable cron job"):
            kb.schedule_task(conn, task_id, schedule_kind="external",
                             wake_job_id="missing", checkpoint_at=9_000_000)


@pytest.mark.parametrize("job_state,execution,category", [
    (None, None, "wake_broken"),
    ({"id": "job_123", "enabled": False}, None, "wake_broken"),
    ({"id": "job_123", "enabled": True}, {"status": "failed"}, "wake_broken"),
    ({"id": "job_123", "enabled": True}, {"status": "completed"}, "wake_broken"),
    ({"id": "job_123", "enabled": True}, {"status": "running"}, "healthy_hold"),
])
def test_audit_classifies_external_wake_health(
    kanban_home, monkeypatch, job_state, execution, category,
):
    monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: {"id": "job_123", "enabled": True})
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external", assignee="worker")
        assert kb.schedule_task(
            conn, task_id, schedule_kind="external", wake_job_id="job_123",
            checkpoint_at=9_000_000,
        )
        monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: job_state)
        monkeypatch.setattr("cron.executions.latest_execution", lambda _job_id: execution)
        report = kb.audit_scheduled_tasks(conn, now=8_000_000)
        assert report["tasks"][0]["category"] == category
        assert report["counts"][category] == 1


def test_audit_ignores_pre_hold_completion_when_future_run_is_live(
    kanban_home, monkeypatch,
):
    now = 8_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    monkeypatch.setattr(
        "cron.jobs.get_job",
        lambda _job_id: {"id": "job_123", "enabled": True, "next_run_at": "1970-04-04T15:06:40+00:00"},
    )
    monkeypatch.setattr(
        "cron.executions.latest_execution",
        lambda _job_id: {"status": "completed", "finished_at": "1970-03-01T00:00:00+00:00"},
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="future recurring wake", assignee="worker")
        assert kb.schedule_task(
            conn, task_id, schedule_kind="external", wake_job_id="job_123",
            checkpoint_at=now + 200_000,
        )
        report = kb.audit_scheduled_tasks(conn, now=now)
        assert report["tasks"][0]["category"] == "healthy_hold"


def test_audit_marks_elapsed_external_checkpoint_actionable(kanban_home, monkeypatch):
    monkeypatch.setattr(
        "cron.jobs.get_job", lambda _job_id: {"id": "job_123", "enabled": True},
    )
    monkeypatch.setattr(
        "cron.executions.latest_execution", lambda _job_id: {"status": "running"},
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missed external checkpoint", assignee="worker")
        assert kb.schedule_task(
            conn, task_id, schedule_kind="external", wake_job_id="job_123",
            checkpoint_at=7_999_999,
        )
        report = kb.audit_scheduled_tasks(conn, now=8_000_000)
        assert report["tasks"][0]["category"] == "wake_broken"


def test_board_health_exposes_gated_todo_capacity_and_broken_wake(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr("cron.jobs.get_job", lambda job_id: {"id": job_id, "enabled": True})
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="gated child", assignee="worker", parents=[parent])
        # Model a stranded nonterminal source with no executable path.
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
        held = kb.create_task(conn, title="broken wake", assignee="worker")
        assert kb.schedule_task(
            conn, held, schedule_kind="external", wake_job_id="job_123",
            checkpoint_at=9_000_000,
        )
        monkeypatch.setattr("cron.jobs.get_job", lambda _job_id: None)
        monkeypatch.setattr("cron.executions.latest_execution", lambda _job_id: None)

        report = kb.board_health(conn, now=8_000_000, max_in_progress=2)

        assert report["healthy"] is False
        assert report["capacity"]["full"] is False
        assert report["todo"]["gated"] == [{"id": child, "blocking_parents": [parent]}]
        assert report["scheduled"]["counts"]["wake_broken"] == 1
        assert held in report["actionable_task_ids"]


def test_board_health_flags_blocked_and_unspawnable_ready_even_with_running_work(
    kanban_home, monkeypatch,
):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "worker")
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="worker")
        assert kb.claim_task(conn, running) is not None
        blocked = kb.create_task(conn, title="blocked", assignee="worker")
        assert kb.block_task(conn, blocked, reason="input", kind="needs_input")
        unspawnable = kb.create_task(conn, title="no profile", assignee="missing")
        report = kb.board_health(conn, max_in_progress=2)
        assert report["healthy"] is False
        assert set(report["actionable_task_ids"]) >= {blocked, unspawnable}


def test_board_health_uses_configured_capacity_when_not_overridden(kanban_home, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"kanban": {"max_in_progress": 1}},
    )
    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="worker")
        assert kb.claim_task(conn, running) is not None
        report = kb.board_health(conn)
        assert report["capacity"] == {
            "running": 1, "limit": 1, "available": 0, "full": True,
        }


def test_completed_is_not_a_public_schedule_kind(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="stale wrapper", assignee="worker")
        with pytest.raises(ValueError, match="schedule_kind"):
            kb.schedule_task(conn, task_id, schedule_kind="completed")


def test_schedule_audit_never_infers_legacy_intent(kanban_home, monkeypatch):
    now = 7_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kb.connect() as conn:
        due = kb.create_task(conn, title="due", assignee="worker")
        legacy = kb.create_task(conn, title="legacy", assignee="worker")
        kb.schedule_task(conn, due, schedule_kind="timed", wake_at=now - 1)
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (legacy,))
        report = kb.audit_scheduled_tasks(conn, now=now, repair=True)
        assert report["counts"]["timed_due"] == 1
        assert report["counts"]["legacy"] == 1
        assert report["repaired"] == [due]
        assert kb.get_task(conn, legacy).status == "scheduled"
