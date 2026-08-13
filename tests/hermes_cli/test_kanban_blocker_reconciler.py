from __future__ import annotations

import json
from datetime import datetime, timezone
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


def _add_legacy_comment(conn, task_id: str, *, author: str, body: str) -> None:
    """Insert the pre-provenance comment/event shape used by live old rows."""
    now = int(kb.time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, author, body, now),
        )
        kb._append_event(
            conn, task_id, "commented", {"author": author, "len": len(body)},
        )


def test_terminal_source_with_active_replacement_coalesces_stale_wrapper(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live regression: archived source + running replacement is no human gate."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn, title="spawn-failed source")
        assert kb.block_task(conn, source_id, reason="workspace unresolved", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        replacement_id = kb.create_task(conn, title="executable replacement", assignee="code-crab")
        assert kb.claim_task(conn, replacement_id, claimer="replacement") is not None
        assert kb.archive_task(conn, source_id)
        kb.link_tasks(conn, replacement_id, source_id)

        closed = kb.reconcile_stale_reconciliation_wrappers(conn)

        assert closed == [recovery.id]
        assert kb.get_task(conn, recovery.id).status == "archived"
        outcome = [
            e for e in kb.list_events(conn, source_id)
            if e.kind == "reconciliation_outcome"
        ][-1]
        assert outcome.payload["outcome"] == "cleared/resumed"
        assert outcome.payload["stale"] is True
        assert outcome.payload["replacement_task_id"] == replacement_id
        assert outcome.payload.get("human_action") is None


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
        for required_field in (
            "continuation_task_id",
            "dependency_task_id",
            "resume_at",
            "human_action",
            "error",
        ):
            assert required_field in (recovery.body or "")
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


def test_dispatcher_retryable_crash_does_not_spawn_parallel_recovery(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (source_id,),
            )
            kb._append_event(
                conn,
                source_id,
                "crashed",
                {"error": "worker exited", "retry_status": "ready"},
            )
        assert _reconciliation_tasks(conn) == []


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


def test_disabling_reconciler_stops_queued_recovery_claims(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="iteration budget", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]

        from hermes_cli import config as config_module

        monkeypatch.setattr(
            config_module,
            "load_config",
            lambda: {"kanban": {"blocker_reconciler": {"enabled": False}}},
        )
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is None
        queued = kb.get_task(conn, recovery.id)
        assert queued is not None
        assert queued.status == "ready"


def test_connection_path_wins_over_mismatched_board_environment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")
    with kb.connect_closing(board="alpha") as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert recovery.idempotency_key is not None
        assert recovery.idempotency_key.startswith(f"kanban-reconcile:alpha:{source_id}:")


def test_archived_exact_reconciliation_key_is_never_replayed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        event = next(e for e in reversed(kb.list_events(conn, source_id)) if e.kind == "blocked")
        assert kb.archive_task(conn, recovery.id)
        replayed = kb.enqueue_blocker_reconciliation(conn, event.id)
        assert replayed == recovery.id
        assert len([
            t for t in kb.list_tasks(conn, include_archived=True)
            if t.idempotency_key == recovery.idempotency_key
        ]) == 1


def test_trigger_and_recovery_enqueue_are_one_transaction(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        monkeypatch.setattr(
            kb,
            "enqueue_blocker_reconciliation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic enqueue failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
            kb.block_task(conn, source_id, reason="retry me", kind="transient")
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "running"
        assert not any(e.kind == "blocked" for e in kb.list_events(conn, source_id))


def test_reconciliation_completion_requires_valid_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with pytest.raises(ValueError, match="reconciliation"):
            kb.complete_task(conn, recovery.id, summary="missing verdict", metadata={})
        current = kb.get_task(conn, recovery.id)
        assert current is not None
        assert current.status == "running"


@pytest.mark.parametrize("invalid_event_id", [True, 1.0, "1"])
def test_reconciliation_verdict_rejects_coerced_event_ids(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_event_id: object,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        with pytest.raises(ValueError, match="must be an integer"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="coerced lineage",
                metadata={"reconciliation": {
                    "outcome": "cleared/resumed",
                    "source_task_id": source_id,
                    "source_event_id": invalid_event_id,
                }},
            )


def test_reconciliation_verdict_rejects_cross_outcome_fields(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with pytest.raises(ValueError, match="unexpected fields"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="mixed schema",
                metadata={"reconciliation": {
                    "outcome": "cleared/resumed",
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "human_action": "not valid for this outcome",
                }},
            )


def test_stale_reconciliation_cannot_emit_human_gate_after_source_done(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert kb.complete_task(conn, source_id, summary="completed externally")
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="stale verdict",
                metadata={
                    "reconciliation": {
                        "source_task_id": source_id,
                        "source_event_id": source_event_id,
                        "outcome": "genuine_human_gate",
                        "human_action": "Choose one option",
                    }
                },
            )
        assert not any(
            e.kind == "reconciliation_outcome" and (e.payload or {}).get("outcome") == "genuine_human_gate"
            for e in kb.list_events(conn, source_id)
        )


def test_stale_reconciliation_rejects_same_status_round_trip(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="temporary", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (source_id,))
            kb._append_event(conn, source_id, "status", {"status": "ready"})
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (source_id,))
            kb._append_event(conn, source_id, "status", {"status": "blocked"})
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="obsolete human gate",
                metadata={"reconciliation": {
                    "outcome": "genuine_human_gate",
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "human_action": "This stale verdict must not notify",
                }},
            )
        assert not any(
            event.kind == "reconciliation_outcome"
            for event in kb.list_events(conn, source_id)
        )


def test_stale_reconciliation_cannot_regress_manually_resumed_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="temporary", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        assert kb.unblock_task(conn, source_id)
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="stale backoff",
                metadata={
                    "reconciliation": {
                        "source_task_id": source_id,
                        "source_event_id": source_event_id,
                        "outcome": "backoff_scheduled",
                        "resume_at": int(kb.time.time()) + 60,
                    }
                },
                expected_run_id=claimed.current_run_id,
            )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "ready"
        assert not any(
            event.kind == "reconciliation_outcome"
            for event in kb.list_events(conn, source_id)
        )


def test_terminal_reconciliation_failure_resumes_source_automatically(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        assert kb.block_task(
            conn,
            recovery.id,
            reason="reconciler infrastructure failed",
            kind="transient",
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "ready"
        assert any(
            e.kind == "reconciliation_outcome" and (e.payload or {}).get("outcome") == "reconciliation_failed"
            for e in kb.list_events(conn, source_id)
        )


def test_successful_looking_recovery_generations_are_bounded(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    block_kinds = ("transient", "capability", "needs_input")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        for index, block_kind in enumerate(block_kinds, start=1):
            assert kb.block_task(
                conn,
                source_id,
                reason=f"repeating blocker {index}",
                kind=block_kind,
            )
            recovery = next(
                task for task in _reconciliation_tasks(conn) if task.status == "ready"
            )
            claimed = kb.claim_task(conn, recovery.id, claimer=f"reconciler-{index}")
            assert claimed is not None
            source_event_id = next(
                event.id
                for event in reversed(kb.list_events(conn, source_id))
                if event.kind in kb.RECONCILIATION_EVENT_KINDS
            )
            assert kb.complete_task(
                conn,
                recovery.id,
                summary="claimed the blocker was cleared",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
            source = kb.get_task(conn, source_id)
            assert source is not None
            if index < len(block_kinds):
                assert source.status == "ready"
                assert kb.claim_task(conn, source_id, claimer=f"source-{index}") is not None
            else:
                assert source.status == "triage"

        gates = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
            and (event.payload or {}).get("outcome") == "genuine_human_gate"
        ]
        assert len(gates) == 1
        assert gates[0]["fallback"] == "automation_exhausted"


def test_block_loop_triage_cannot_be_auto_resumed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="same blocker", kind="capability")
        assert kb.unblock_task(conn, source_id)
        assert kb.claim_task(conn, source_id, claimer="source-again") is not None
        assert kb.block_task(conn, source_id, reason="same blocker", kind="capability")
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "triage"

        recovery = next(task for task in _reconciliation_tasks(conn) if task.status == "ready")
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = next(
            event.id
            for event in reversed(kb.list_events(conn, source_id))
            if event.kind == "block_loop_detected"
        )
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="incorrectly claimed the loop was clear",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "triage"
        gates = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
            and (event.payload or {}).get("outcome") == "genuine_human_gate"
        ]
        assert len(gates) == 1


def test_failed_reconciliation_cannot_resume_block_loop_triage(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="same blocker", kind="capability")
        assert kb.unblock_task(conn, source_id)
        assert kb.claim_task(conn, source_id, claimer="source-again") is not None
        assert kb.block_task(conn, source_id, reason="same blocker", kind="capability")
        recovery = next(task for task in _reconciliation_tasks(conn) if task.status == "ready")
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = next(
            event.id
            for event in reversed(kb.list_events(conn, source_id))
            if event.kind == "block_loop_detected"
        )

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="reconciler could not diagnose the loop",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "reconciliation_failed",
                "error": "diagnosis failed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "triage"
        outcomes = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert outcomes[-1]["outcome"] == "genuine_human_gate"


def test_manual_triage_without_active_recovery_is_human_input(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = kb.create_task(conn, title="manual triage", assignee="code-crab")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (source_id,))
        assert kb.attention_class(conn, source_id, reconciler_enabled=True) == "human_input"


def test_repeated_reconciliation_failures_escalate_once_instead_of_looping(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        for attempt in range(1, kb.RECONCILIATION_SOURCE_FAILURE_LIMIT + 1):
            source = kb.get_task(conn, source_id)
            assert source is not None
            assert source.status == ("running" if attempt == 1 else "ready")
            assert kb.block_task(conn, source_id, reason=f"failure {attempt}", kind="transient")
            recovery = next(
                task for task in _reconciliation_tasks(conn) if task.status == "ready"
            )
            claimed = kb.claim_task(conn, recovery.id, claimer=f"reconciler-{attempt}")
            assert claimed is not None
            assert kb.block_task(
                conn,
                recovery.id,
                reason=f"reconciler failure {attempt}",
                kind="transient",
                expected_run_id=claimed.current_run_id,
            )
            source = kb.get_task(conn, source_id)
            assert source is not None
            expected = (
                "triage"
                if attempt == kb.RECONCILIATION_SOURCE_FAILURE_LIMIT
                else "ready"
            )
            assert source.status == expected

        outcomes = [
            event.payload or {} for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert [payload.get("outcome") for payload in outcomes] == [
            "reconciliation_failed",
            "reconciliation_failed",
            "genuine_human_gate",
        ]
        assert outcomes[-1].get("failure_count") == kb.RECONCILIATION_SOURCE_FAILURE_LIMIT


def test_outcome_specific_metadata_is_validated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        for outcome, missing in (
            ("continuation_created", "continuation_task_id"),
            ("dependency_wait", "dependency_task_id"),
            ("backoff_scheduled", "resume_at"),
            ("genuine_human_gate", "human_action"),
            ("reconciliation_failed", "error"),
        ):
            with pytest.raises(ValueError, match=missing):
                kb.complete_task(
                    conn,
                    recovery.id,
                    summary="invalid verdict",
                    metadata={
                        "reconciliation": {
                            "source_task_id": source_id,
                            "source_event_id": source_event_id,
                            "outcome": outcome,
                        }
                    },
                )


@pytest.mark.parametrize(
    ("outcome", "id_field"),
    (
        ("continuation_created", "continuation_task_id"),
        ("dependency_wait", "dependency_task_id"),
    ),
)
def test_task_outcomes_require_linked_parent_lineage(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    id_field: str,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        unrelated_id = kb.create_task(conn, title="unrelated", assignee="code-crab")
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with pytest.raises(ValueError, match="linked parent"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="invalid lineage",
                metadata={
                    "reconciliation": {
                        "source_task_id": source_id,
                        "source_event_id": source_event_id,
                        "outcome": outcome,
                        id_field: unrelated_id,
                    }
                },
            )


@pytest.mark.parametrize(
    ("outcome", "id_field"),
    (
        ("continuation_created", "continuation_task_id"),
        ("dependency_wait", "dependency_task_id"),
    ),
)
def test_task_outcomes_accept_link_created_by_supported_api(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    id_field: str,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        parent_id = kb.create_task(conn, title="recovery parent", assignee="code-crab")
        kb.link_tasks(conn, parent_id, source_id)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="linked recovery route created",
            metadata={
                "reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": outcome,
                    id_field: parent_id,
                }
            },
            expected_run_id=claimed.current_run_id,
        )
        emitted = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert emitted[-1]["outcome"] == outcome
        assert emitted[-1][id_field] == parent_id


def test_dependency_verdict_accepts_reconciler_evidence_comment_and_resumes_after_parent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        parent_id = kb.create_task(conn, title="recovery parent", assignee="code-crab")
        kb.link_tasks(conn, parent_id, source_id)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body=f"Reconciliation evidence for source event {source_event_id}: waiting on {parent_id}.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="dependency evidence recorded",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "dependency_wait",
                "dependency_task_id": parent_id,
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"
        assert kb.complete_task(conn, parent_id, summary="dependency finished")
        kb.recompute_ready(conn)
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "ready"


@pytest.mark.parametrize("outcome", ["continuation_created", "genuine_human_gate"])
def test_reconciliation_owned_comment_does_not_invalidate_original_occurrence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="operator decision", kind="needs_input")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        reconciliation = {
            "source_task_id": source_id,
            "source_event_id": source_event_id,
            "outcome": outcome,
        }
        if outcome == "continuation_created":
            continuation = kb.create_task(conn, title="continuation", assignee="code-crab")
            kb.link_tasks(conn, continuation, source_id)
            reconciliation["continuation_task_id"] = continuation
        else:
            reconciliation["human_action"] = "Approve the bounded action."
        kb.add_comment(
            conn, source_id, author="default",
            body="Mandatory recovery evidence recorded from current source truth.",
            origin_task_id=recovery.id, origin_run_id=claimed.current_run_id,
        )

        assert kb.complete_task(
            conn, recovery.id, summary="verdict accepted",
            metadata={"reconciliation": reconciliation},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == ("todo" if outcome == "continuation_created" else "blocked")
        assert not any(
            event.kind == "reconciliation_verdict_discarded"
            for event in kb.list_events(conn, source_id)
        )


def test_legacy_reconciler_evidence_comment_is_tied_to_active_recovery_run(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        _add_legacy_comment(
            conn,
            source_id,
            author="default",
            body=f"Reconciliation evidence for source event {source_event_id}: automation recovered.",
        )

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="legacy evidence accepted",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )


def test_legacy_reconciler_topology_comment_requires_mirrored_parent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        parent_id = kb.create_task(conn, title="recovery route", assignee="code-crab")
        kb.link_tasks(conn, parent_id, source_id)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        kb.link_tasks(conn, parent_id, recovery.id)
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        _add_legacy_comment(
            conn,
            source_id,
            author="default",
            body=f"Reconcile this source through canonical remediation {parent_id}; preserve its evidence.",
        )
        assert kb.complete_task(conn, parent_id, summary="canonical remediation finished")
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="legacy topology evidence accepted",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )


def test_live_lineage_shape_accepts_mirrored_links_and_both_legacy_evidence_comments(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="workspace routing failure", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        canonical_id = kb.create_task(conn, title="canonical remediation", assignee="code-crab")
        guard_fix_id = kb.create_task(conn, title="guard remediation", assignee="code-crab")
        kb.link_tasks(conn, canonical_id, source_id)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        kb.link_tasks(conn, canonical_id, recovery.id)
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        _add_legacy_comment(
            conn,
            source_id,
            author="default",
            body=(
                f"Reconcile this card as superseded by canonical remediation {canonical_id}; "
                "do not launch duplicate implementation."
            ),
        )
        _add_legacy_comment(
            conn,
            source_id,
            author="default",
            body=(
                f"Reconciliation evidence for source event {source_event_id}: canonical "
                f"remediation {canonical_id} covers the exact scope."
            ),
        )
        kb.link_tasks(conn, guard_fix_id, source_id)
        kb.link_tasks(conn, guard_fix_id, recovery.id)
        assert kb.complete_task(conn, canonical_id, summary="canonical remediation finished")
        assert kb.complete_task(conn, guard_fix_id, summary="guard remediation finished")

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="live lineage reconciled",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "ready"


@pytest.mark.parametrize("advance", ("operator_comment", "status", "different_link"))
def test_reconciliation_evidence_exemption_rejects_unrelated_advances(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    advance: str,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        parent_id = kb.create_task(conn, title="declared parent", assignee="code-crab")
        kb.link_tasks(conn, parent_id, source_id)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        if advance == "operator_comment":
            kb.add_comment(conn, source_id, author="operator", body="Change direction now.")
        elif advance == "status":
            with kb.write_txn(conn):
                kb._append_event(conn, source_id, "status", {"status": "blocked"})
        else:
            other_parent_id = kb.create_task(conn, title="unrelated parent", assignee="operator")
            kb.link_tasks(conn, other_parent_id, source_id)

        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="stale verdict",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "dependency_wait",
                    "dependency_task_id": parent_id,
                }},
                expected_run_id=claimed.current_run_id,
            )


def test_current_comment_without_origin_provenance_remains_material(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body=f"Reconciliation evidence for source event {source_event_id}: forged externally.",
        )
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )


def test_ambiguous_legacy_comment_match_remains_material(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    monkeypatch.setattr(kb.time, "time", lambda: 1_700_000_000)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        evidence = f"Reconciliation evidence for source event {source_event_id}: valid."
        operator = "X" * len(evidence)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (source_id, "default", evidence, 1_700_000_000),
            )
            kb._append_event(
                conn, source_id, "commented", {"author": "default", "len": len(evidence)},
            )
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (source_id, "default", operator, 1_700_000_000),
            )
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject ambiguity",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )


@pytest.mark.parametrize("malformed_comment_id", (None, "12", True, 1.5))
def test_malformed_present_comment_id_never_uses_legacy_fallback(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_comment_id: object,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        body = f"Reconciliation evidence for source event {source_event_id}: malformed id."
        now = int(kb.time.time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (source_id, "default", body, now),
            )
            kb._append_event(
                conn,
                source_id,
                "commented",
                {
                    "author": "default",
                    "len": len(body),
                    "comment_id": malformed_comment_id,
                },
            )
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject malformed comment id",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )


@pytest.mark.parametrize("malformed_run_id", ("2", True, 2.0))
def test_append_event_rejects_malformed_run_id(
    isolated_home: Path,
    malformed_run_id: object,
) -> None:
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="event type guard", assignee="default")
        with pytest.raises(ValueError, match="event run_id must be an integer or None"):
            kb._append_event(
                conn,
                task_id,
                "commented",
                {"author": "default", "len": 1},
                run_id=malformed_run_id,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(
    ("origin_field", "malformed_value"),
    (
        ("origin_run_id", None),
        ("origin_run_id", "2"),
        ("origin_run_id", True),
        ("origin_run_id", 2.0),
        ("origin_task_id", None),
        ("origin_task_id", 123),
        ("origin_task_id", True),
    ),
)
def test_malformed_explicit_origin_provenance_remains_material(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_field: str,
    malformed_value: object,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        comment_id = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (
                source_id,
                "default",
                f"Reconciliation evidence for source event {source_event_id}: malformed origin.",
                int(kb.time.time()),
            ),
        ).lastrowid
        payload = {
            "author": "default",
            "len": 0,
            "comment_id": comment_id,
            "origin_task_id": recovery.id,
            "origin_run_id": claimed.current_run_id,
        }
        payload[origin_field] = malformed_value
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                source_id,
                "commented",
                payload,
                run_id=claimed.current_run_id,
            )
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject malformed origin",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )


@pytest.mark.parametrize("mismatch", ("wrong_run", "wrong_author", "ended_run"))
def test_explicit_comment_provenance_must_match_active_recovery_run(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        origin_run_id = int(claimed.current_run_id)
        author = "default"
        if mismatch == "wrong_run":
            origin_run_id = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, ?, ?, ?)",
                (recovery.id, "default", "running", int(kb.time.time())),
            ).lastrowid
        elif mismatch == "wrong_author":
            author = "operator"
        else:
            conn.execute(
                "UPDATE task_runs SET ended_at = ? WHERE id = ?",
                (int(kb.time.time()) - 1, origin_run_id),
            )
        kb.add_comment(
            conn,
            source_id,
            author=author,
            body=f"Reconciliation evidence for source event {source_event_id}: invalid provenance.",
            origin_task_id=recovery.id,
            origin_run_id=int(origin_run_id),
        )
        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject provenance mismatch",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )


def test_backoff_outcome_rejects_non_future_deadline(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="quota reset", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with pytest.raises(ValueError, match="future unix timestamp"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="invalid expired backoff",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "backoff_scheduled",
                    "resume_at": int(kb.time.time()) - 1,
                }},
            )


def test_backoff_outcome_resumes_when_deadline_elapses(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(
        "cron.jobs.get_job",
        lambda job_id: {
            "id": job_id, "enabled": True,
            "next_run_at": datetime.fromtimestamp(
                int(kb.time.time()) + 120, tz=timezone.utc,
            ).isoformat(),
        },
    )
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="quota reset", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        resume_at = int(kb.time.time()) + 60
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="wait for quota reset",
            metadata={
                "reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "backoff_scheduled",
                    "resume_at": resume_at,
                }
            },
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "scheduled"
        assert kb.recompute_ready(conn) == 0
        monkeypatch.setattr(kb.time, "time", lambda: resume_at + 1)
        assert kb.recompute_ready(conn) == 1
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "ready"

        # A later, unrelated operator/cron park must not be released by the
        # stale backoff outcome that already elapsed above.
        assert kb.schedule_task(
            conn,
            source_id,
            reason="wait for external window",
            schedule_kind="external",
            wake_job_id="test-window-wake",
            checkpoint_at=resume_at + 3600,
        )
        assert kb.recompute_ready(conn) == 0
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "scheduled"


def test_reconciliation_envelope_redacts_secret_shaped_values(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    private_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "very-secret-key-material\n"
        "-----END RSA PRIVATE KEY-----"
    )
    with kb.connect_closing() as conn:
        source_id = _running(
            conn,
            title="https://alice:password@example.test/private",
            body=(
                "OPENAI_API_KEY=openai-secret\n"
                "AWS_SECRET_ACCESS_KEY=aws-secret\n"
                "GITHUB_TOKEN=github-secret\n"
                f"token=very-secret\n{private_key}"
            ),
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                source_id,
                "protocol_violation",
                {
                    "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
                    "detail": private_key,
                },
            )
        body = _reconciliation_tasks(conn)[0].body or ""
        assert "password" not in body
        assert "very-secret" not in body
        assert "Bearer abcdef" not in body
        assert "openai-secret" not in body
        assert "aws-secret" not in body
        assert "github-secret" not in body
        assert "[REDACTED]" in body


def test_reconciliation_redacts_private_key_before_truncation() -> None:
    private_key = (
        "-----BEGIN PRIVATE KEY-----\n"
        + ("secret-material" * 50)
        + "\n-----END PRIVATE KEY-----"
    )
    redacted = kb._redact_reconciliation_text(("x" * 3900) + private_key, limit=4000)
    assert "secret-material" not in redacted
    assert "BEGIN PRIVATE KEY" not in redacted
    assert "[REDACTED PRIVATE KEY]" in redacted


def test_coalesced_generation_rejects_stale_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="first failure", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        original_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with kb.write_txn(conn):
            newest_event_id = kb._append_event(
                conn, source_id, "timed_out", {"error": "new generation"},
            )
        assert newest_event_id != original_event_id
        assert kb.claim_task(conn, recovery.id, claimer="reconciler") is not None
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="obsolete result",
                metadata={
                    "reconciliation": {
                        "source_task_id": source_id,
                        "source_event_id": original_event_id,
                        "outcome": "cleared/resumed",
                    }
                },
            )
        current = kb.get_task(conn, recovery.id)
        assert current is not None
        assert current.status == "running"
