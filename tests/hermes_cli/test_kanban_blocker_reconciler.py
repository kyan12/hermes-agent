from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _enable(monkeypatch: pytest.MonkeyPatch, *, profile: str = "default", max_active: int = 2) -> None:
    from hermes_cli import config as config_module

    monkeypatch.setattr(
        config_module,
        "load_config",
        lambda: {
            "kanban": {
                "blocker_reconciler": {
                    "enabled": True,
                    "profile": profile,
                    "max_active": max_active,
                }
            }
        },
    )


def _running(conn, *, title: str = "source", **kwargs) -> str:
    task_id = kb.create_task(conn, title=title, assignee="code-crab", **kwargs)
    claimed = kb.claim_task(conn, task_id, claimer="test")
    assert claimed is not None
    return task_id


def _reconciliation_tasks(conn) -> list[kb.Task]:
    return [
        task
        for task in kb.list_tasks(conn, include_archived=True)
        if (task.idempotency_key or "").startswith(kb.RECONCILIATION_IDEMPOTENCY_PREFIX)
    ]


def test_iteration_budget_block_enqueues_one_reconciliation_without_human_gate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        task_id = _running(
            conn,
            body=(
                "Relationship mode: internal. Principal: default. "
                "Legal scope: Hermes infrastructure only."
            ),
            workspace_kind="worktree",
            workspace_path=str(isolated_home / "repo" / ".worktrees" / "source"),
            branch_name="fix/source",
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="goal/iteration budget exhausted before finalization",
            kind="transient",
        )

        reconciliations = _reconciliation_tasks(conn)
        assert len(reconciliations) == 1
        recovery = reconciliations[0]
        assert recovery.assignee == "default"
        assert recovery.status == "ready"
        assert recovery.workspace_kind == "scratch"
        assert recovery.idempotency_key is not None
        source_event = [e for e in kb.list_events(conn, task_id) if e.kind == "blocked"][-1]
        assert f":{task_id}:{source_event.id}" in recovery.idempotency_key
        assert f"source_event_id: {source_event.id}" in (recovery.body or "")
        assert '"path":' in (recovery.body or "")
        assert kb.attention_class(conn, task_id, reconciler_enabled=True) == "automation_recovery"


def test_explicit_needs_input_is_preflighted_and_only_affirmation_is_human_gate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(
            conn,
            source_id,
            reason="Kevin must approve a paid public deployment",
            kind="needs_input",
        )
        assert kb.attention_class(conn, source_id, reconciler_enabled=True) == "automation_recovery"

        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event = [e for e in kb.list_events(conn, source_id) if e.kind == "blocked"][-1]
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="Source verified; paid public deployment requires Kevin authorization.",
            metadata={
                "reconciliation": {
                    "outcome": "genuine_human_gate",
                    "source_task_id": source_id,
                    "source_event_id": source_event.id,
                    "human_action": "Approve the paid public deployment.",
                }
            },
            expected_run_id=claimed.current_run_id,
        )

        assert kb.attention_class(conn, source_id, reconciler_enabled=True) == "human_input"
        affirmed = [e for e in kb.list_events(conn, source_id) if e.kind == "reconciliation_outcome"]
        assert len(affirmed) == 1
        assert affirmed[0].payload == {
            "outcome": "genuine_human_gate",
            "source_event_id": source_event.id,
            "reconciliation_task_id": recovery.id,
            "human_action": "Approve the paid public deployment.",
            "continuation_task_id": None,
        }


def test_duplicate_event_replay_and_concurrent_idempotency_do_not_duplicate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="worker crashed", kind="transient")
        source_event = [e for e in kb.list_events(conn, source_id) if e.kind == "blocked"][-1]
        first = kb.enqueue_blocker_reconciliation(conn, source_event.id)
        second = kb.enqueue_blocker_reconciliation(conn, source_event.id)
        assert first == second
        assert len(_reconciliation_tasks(conn)) == 1


def test_repeated_occurrence_coalesces_into_active_reconciliation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        with kb.write_txn(conn):
            first_event = kb._append_event(conn, source_id, "crashed", {"error": "boom"})
        with kb.write_txn(conn):
            second_event = kb._append_event(conn, source_id, "timed_out", {"error": "slow"})

        reconciliations = _reconciliation_tasks(conn)
        assert len(reconciliations) == 1
        coalesced = [e for e in kb.list_events(conn, source_id) if e.kind == "reconciliation_coalesced"]
        assert len(coalesced) == 1
        assert coalesced[0].payload["source_event_id"] == second_event
        assert coalesced[0].payload["reconciliation_task_id"] == reconciliations[0].id
        assert first_event != second_event


def test_reconciliation_task_failures_do_not_recurse(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="routing failed", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        assert kb.block_task(
            conn,
            recovery.id,
            reason="reconciliation itself failed",
            kind="transient",
            expected_run_id=claimed.current_run_id,
        )
        assert len(_reconciliation_tasks(conn)) == 1


def test_quota_and_workspace_routing_are_automation_recovery() -> None:
    assert kb.classify_blocker_occurrence(
        "rate_limited", {"error": "quota wall; reset in 30m"}, block_kind=None,
    ) == "automation_recovery"
    assert kb.classify_blocker_occurrence(
        "spawn_failed", {"error": "workspace path routing failed"}, block_kind=None,
    ) == "automation_recovery"


def test_config_disabled_preserves_legacy_block_and_notification_behavior(
    isolated_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="legacy", kind=None)
        assert _reconciliation_tasks(conn) == []
        assert kb.attention_class(conn, source_id, reconciler_enabled=False) == "human_input"


def test_multiple_boards_keep_reconciliation_tasks_isolated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    kb.create_board("alpha")
    kb.create_board("beta")
    source_ids: dict[str, str] = {}
    for board in ("alpha", "beta"):
        with kb.connect_closing(board=board) as conn:
            source_id = _running(conn, title=f"{board} source")
            source_ids[board] = source_id
            assert kb.block_task(conn, source_id, reason="iteration budget", kind="transient")
            tasks = _reconciliation_tasks(conn)
            assert len(tasks) == 1
            assert f"kanban-reconcile:{board}:" in (tasks[0].idempotency_key or "")

    with kb.connect_closing(board="alpha") as conn:
        assert kb.get_task(conn, source_ids["beta"]) is None
    with kb.connect_closing(board="beta") as conn:
        assert kb.get_task(conn, source_ids["alpha"]) is None


def test_preserved_dirty_worktree_continuation_lineage_is_in_envelope(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    worktree = isolated_home / "repo" / ".worktrees" / "dirty"
    worktree.mkdir(parents=True)
    with kb.connect_closing() as conn:
        source_id = _running(
            conn,
            workspace_kind="worktree",
            workspace_path=str(worktree),
            branch_name="wt/dirty",
            project_id=None,
        )
        kb.add_comment(conn, source_id, author="worker", body="Preserve unstaged src/a.py exactly.")
        assert kb.block_task(conn, source_id, reason="workspace routing failure", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        envelope_marker = "```json\n"
        body = recovery.body or ""
        envelope = json.loads(body.split(envelope_marker, 1)[1].split("\n```", 1)[0])
        assert envelope["workspace"]["path"] == str(worktree)
        assert envelope["workspace"]["branch"] == "wt/dirty"
        assert envelope["lineage"]["source_task_id"] == source_id
        assert envelope["comments"][-1]["body"] == "Preserve unstaged src/a.py exactly."


def test_reconciliation_claims_respect_configured_active_cap(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, max_active=2)
    with kb.connect_closing() as conn:
        for index in range(3):
            source_id = _running(conn, title=f"source {index}")
            assert kb.block_task(conn, source_id, reason="iteration budget", kind="transient")
        recoveries = _reconciliation_tasks(conn)
        assert len(recoveries) == 3
        assert kb.claim_task(conn, recoveries[0].id, claimer="reconciler-1") is not None
        assert kb.claim_task(conn, recoveries[1].id, claimer="reconciler-2") is not None
        assert kb.claim_task(conn, recoveries[2].id, claimer="reconciler-3") is None
        queued = kb.get_task(conn, recoveries[2].id)
        assert queued is not None
        assert queued.status == "ready"
