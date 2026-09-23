from __future__ import annotations

import json

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_blocker_reconcile as reconcile
from hermes_cli import kanban_blocker_outcomes as outcomes
from hermes_cli import kanban_db_connect as connection


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
        if (task.idempotency_key or "").startswith("kanban-reconcile:")
    ]

def test_iteration_budget_block_enqueues_one_reconciliation_without_human_gate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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



def test_reconciliation_claims_respect_configured_active_cap(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, max_active=2)
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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


def test_reconciliation_completion_requires_valid_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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


def test_duplicate_event_replay_and_concurrent_idempotency_do_not_duplicate(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="worker crashed", kind="transient")
        source_event = [e for e in kb.list_events(conn, source_id) if e.kind == "blocked"][-1]
        first = reconcile.enqueue_blocker_reconciliation(conn, source_event.id)
        second = reconcile.enqueue_blocker_reconciliation(conn, source_event.id)
        assert first == second
        assert len(_reconciliation_tasks(conn)) == 1



def test_repeated_occurrence_coalesces_into_active_reconciliation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        with kb.write_txn(conn):
            kb._end_run(conn, source_id, outcome="crashed")
            conn.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?", (source_id,))
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
    with connection.connect_closing() as conn:
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
        # An unfinished run is still an active writer: no parallel recovery.
        assert _reconciliation_tasks(conn) == []



def test_reconciliation_task_failures_do_not_recurse(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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
    assert reconcile.classify_blocker_occurrence(
        "rate_limited", {"error": "quota wall; reset in 30m"}, block_kind=None,
    ) == "automation_recovery"
    assert reconcile.classify_blocker_occurrence(
        "spawn_failed", {"error": "workspace path routing failed"}, block_kind=None,
    ) == "automation_recovery"



def test_config_disabled_preserves_legacy_block_and_notification_behavior(
    isolated_home: Path,
) -> None:
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="legacy", kind=None)
        assert _reconciliation_tasks(conn) == []
        assert reconcile.attention_class(conn, source_id, reconciler_enabled=False) == "human_input"



def test_multiple_boards_keep_reconciliation_tasks_isolated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    kb.create_board("alpha")
    kb.create_board("beta")
    source_ids: dict[str, str] = {}
    for board in ("alpha", "beta"):
        with connection.connect_closing(board=board) as conn:
            source_id = _running(conn, title=f"{board} source")
            source_ids[board] = source_id
            assert kb.block_task(conn, source_id, reason="iteration budget", kind="transient")
            tasks = _reconciliation_tasks(conn)
            assert len(tasks) == 1
            assert f"kanban-reconcile:{board}:" in (tasks[0].idempotency_key or "")

    with connection.connect_closing(board="alpha") as conn:
        assert kb.get_task(conn, source_ids["beta"]) is None
    with connection.connect_closing(board="beta") as conn:
        assert kb.get_task(conn, source_ids["alpha"]) is None



def test_preserved_dirty_worktree_continuation_lineage_is_in_envelope(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    worktree = isolated_home / "repo" / ".worktrees" / "dirty"
    worktree.mkdir(parents=True)
    with connection.connect_closing() as conn:
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



def test_connection_path_wins_over_mismatched_board_environment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    kb.create_board("alpha")
    kb.create_board("beta")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")
    with connection.connect_closing(board="alpha") as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        assert recovery.idempotency_key is not None
        assert recovery.idempotency_key.startswith(f"kanban-reconcile:alpha:{source_id}:")



def test_archived_exact_reconciliation_key_is_never_replayed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        event = next(e for e in reversed(kb.list_events(conn, source_id)) if e.kind == "blocked")
        assert kb.archive_task(conn, recovery.id)
        replayed = reconcile.enqueue_blocker_reconciliation(conn, event.id)
        assert replayed == recovery.id
        assert len([
            t for t in kb.list_tasks(conn, include_archived=True)
            if t.idempotency_key == recovery.idempotency_key
        ]) == 1



def test_trigger_and_recovery_enqueue_are_one_transaction(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        monkeypatch.setattr(
            reconcile,
            "enqueue_blocker_reconciliation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic enqueue failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic enqueue failure"):
            kb.block_task(conn, source_id, reason="retry me", kind="transient")
        source = kb.get_task(conn, source_id)
        assert source is not None
        assert source.status == "running"
        assert not any(e.kind == "blocked" for e in kb.list_events(conn, source_id))



@pytest.mark.parametrize("invalid_event_id", [True, 1.0, "1"])
def test_reconciliation_verdict_rejects_coerced_event_ids(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_event_id: object,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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



def test_manual_triage_without_active_recovery_is_human_input(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source_id = kb.create_task(conn, title="manual triage", assignee="code-crab")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (source_id,))
        assert reconcile.attention_class(conn, source_id, reconciler_enabled=True) == "human_input"



def test_outcome_specific_metadata_is_validated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        parent_id = kb.create_task(conn, title="recovery parent", assignee="code-crab")
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", recovery.id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        kb.link_tasks(conn, parent_id, source_id)
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



def test_backoff_outcome_rejects_non_future_deadline(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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
    with connection.connect_closing() as conn:
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
        assert kb.schedule_task(conn, source_id, reason="wait for external window")
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
    with connection.connect_closing() as conn:
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
            kb._end_run(conn, source_id, outcome="protocol_violation")
            conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?", (source_id,))
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
    redacted = reconcile._redact_reconciliation_text(("x" * 3900) + private_key, limit=4000)
    assert "secret-material" not in redacted
    assert "BEGIN PRIVATE KEY" not in redacted
    assert "[REDACTED PRIVATE KEY]" in redacted



def test_coalesced_generation_rejects_stale_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
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



def test_explicit_human_gate_is_atomic_and_stale_gate_cannot_survive_unblock(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind="needs_input", reason="Approve the paid deployment.")
        assert kb.get_task(conn, source).status == "blocked"
        assert _reconciliation_tasks(conn) == []
        gate = reconcile.current_human_gate(conn, source)
        assert gate.payload["human_action"] == "Approve the paid deployment."
        assert reconcile.attention_class(conn, source, reconciler_enabled=True) == "human_input"
        kb.unblock_task(conn, source)
        kb.block_task(conn, source, kind="transient", reason="worker crashed")
        assert reconcile.current_human_gate(conn, source) is None
        assert reconcile.attention_class(conn, source, reconciler_enabled=True) == "automation_recovery"


def test_failed_recovery_records_machine_failure_without_resuming_or_human_gate(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind="transient", reason="crashed")
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claim.current_run_id))
        kb.add_comment(conn, source, author='default', body='Workspace inspected before worker failure')
        kb.block_task(conn, recovery.id, kind="needs_input", reason="model unavailable", expected_run_id=claim.current_run_id)
        assert kb.get_task(conn, source).status == "blocked"
        assert len(_reconciliation_tasks(conn)) == 1
        events = [e for e in kb.list_events(conn, source) if e.kind == "reconciliation_outcome"]
        assert len(events) == 1
        assert events[0].payload["outcome"] == "reconciliation_failed"
        assert reconcile.attention_class(conn, source, reconciler_enabled=True) == "automation_recovery"
        assert reconcile.attention_class(conn, recovery.id, reconciler_enabled=True) == "automation_recovery"


def test_replay_coalesced_event_after_restart_and_archive_does_not_create_recovery(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind="transient", reason="crashed")
        recovery = _reconciliation_tasks(conn)[0]
        with kb.write_txn(conn):
            event_id = kb._append_event(conn, source, "timed_out", {"error": "late terminal event"})
        reconcile.enqueue_blocker_reconciliation(conn, event_id)
        assert len([e for e in kb.list_events(conn, source) if e.kind == "reconciliation_coalesced"]) == 1
        kb.archive_task(conn, recovery.id)
    with connection.connect_closing() as conn:
        assert reconcile.enqueue_blocker_reconciliation(conn, event_id) == recovery.id
        assert len(_reconciliation_tasks(conn)) == 1


def test_rolled_back_nested_failure_event_cannot_enqueue_reused_event_id(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        with kb.write_txn(conn):
            with pytest.raises(RuntimeError):
                with kb.write_txn(conn, allow_nested=True):
                    kb._append_event(conn, source, "crashed", {})
                    raise RuntimeError("rollback savepoint")
            kb._append_event(conn, source, "commented", {})
        assert _reconciliation_tasks(conn) == []


def test_typed_config_defaults_and_invalid_flags_do_not_enable_recovery(isolated_home, monkeypatch):
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from hermes_cli import config
    raw = DEFAULT_CONFIG["kanban"]["blocker_reconciler"]
    assert reconcile.BlockerReconcilerConfig.from_mapping(raw).enabled is False
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"blocker_reconciler": {"enabled": "false"}}})
    assert reconcile.blocker_reconciler_enabled() is False
    assert reconcile.BlockerReconcilerConfig.from_mapping({"enabled": True, "profile": "../other"}).enabled is False


def test_recovery_outcome_requires_claimed_run_and_native_enqueue_provenance(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind="transient", reason="crashed")
        recovery = _reconciliation_tasks(conn)[0]
        event = int(recovery.idempotency_key.rsplit(':', 1)[1])
        verdict = {"reconciliation": {"source_task_id": source, "source_event_id": event, "outcome": "cleared/resumed"}}
        monkeypatch.setattr(kb, "_merge_completion_prose_artifacts", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("staging before run fence")))
        with pytest.raises(ValueError, match="run"):
            kb.complete_task(conn, recovery.id, metadata=verdict)
        assert kb.get_task(conn, source).status == "blocked"
        forged = kb.create_task(conn, title="forged", assignee="default", idempotency_key=f"kanban-reconcile:default:{source}:999999")
        assert kb.claim_task(conn, forged) is None


def test_successful_looking_generations_exhaust_in_blocked_without_a_human_gate(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        for attempt in range(3):
            if attempt:
                assert kb.claim_task(conn, source) is not None
            kb.block_task(conn, source, kind="transient", reason="iteration limit")
            recovery = [t for t in _reconciliation_tasks(conn) if t.status == 'ready'][0]
            claim = kb.claim_task(conn, recovery.id)
            event = int(recovery.idempotency_key.rsplit(':', 1)[1])
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={"reconciliation": {
                "outcome": "cleared/resumed", "source_task_id": source, "source_event_id": event,
            }})
        assert kb.get_task(conn, source).status == "blocked"
        assert reconcile.current_human_gate(conn, source) is None
        kb.unblock_task(conn, source)
        kb.block_task(conn, source, kind="transient", reason="same failure")
        assert len(_reconciliation_tasks(conn)) == 3
        assert kb.get_task(conn, source).status == "blocked"


def test_envelope_preserves_redacted_topology_and_run_handoff(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        kb.complete_task(conn, parent, summary="parent handoff")
        source = _running(conn, parents=[parent])
        kb.block_task(conn, source, kind="transient", reason="failure")
        body = _reconciliation_tasks(conn)[0].body
        envelope = json.loads(body.split('```json\n')[1].split('\n```')[0])
        assert envelope['topology']['parents'][0]['id'] == parent
        assert envelope['topology']['parents'][0]['status'] == 'done'
        assert envelope['runs'][-1]['id'] == envelope['lineage']['source_run_id']


def test_machine_terminal_event_is_parked_for_one_recovery_even_before_breaker(isolated_home, monkeypatch):
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        _record_task_failure(conn, source, "cannot spawn", outcome="spawn_failed", release_claim=True, end_run=True, failure_limit=10)
        assert kb.get_task(conn, source).status == "blocked"
        assert len(_reconciliation_tasks(conn)) == 1
        kb.recompute_ready(conn)
        assert kb.get_task(conn, source).status == "blocked"


@pytest.mark.parametrize('outcome', ['cleared/resumed', 'backoff_scheduled'])
def test_review_origin_recovery_preserves_review_lane(isolated_home, monkeypatch, outcome):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        task = kb.create_task(conn, title='review source', assignee='default')
        kb.request_review(conn, task, summary='implementation ready')
        claim = kb.claim_review_task(conn, task)
        assert claim
        kb.block_task(conn, task, kind='transient', reason='review failed', expected_run_id=claim.current_run_id)
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id)
        payload = {'outcome': outcome, 'source_task_id': task, 'source_event_id': int(recovery.idempotency_key.rsplit(':',1)[1])}
        if outcome == 'backoff_scheduled': payload['resume_at'] = int(kb.time.time()) + 30
        kb.complete_task(conn, recovery.id, expected_run_id=claimed.current_run_id, metadata={'reconciliation': payload})
        if outcome == 'backoff_scheduled':
            monkeypatch.setattr(kb.time, 'time', lambda: payload['resume_at'] + 1)
            kb.recompute_ready(conn)
        assert kb.get_task(conn, task).status == 'review'


def test_native_dispatcher_policy_applies_to_worker_profile_without_setting(isolated_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli.kanban_db_dispatch import dispatch_once
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        dispatch_once(conn, max_spawn=0, spawn_fn=lambda *_a, **_k: pytest.fail('no spawn budget'))
        # Another worker profile shares the board, but has no local reconciler setting.
        monkeypatch.setattr(config, 'load_config', lambda: {'kanban': {}})
        source = _running(conn)
        kb.block_task(conn, source, kind='transient', reason='worker crash')
        assert len(_reconciliation_tasks(conn)) == 1


def test_only_provenanced_recovery_comments_and_links_can_cross_source_fence(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id)
        event = int(recovery.idempotency_key.rsplit(':',1)[1])
        monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claimed.current_run_id))
        kb.add_comment(conn, source, author='default', body='Verified the source workspace.')
        parent = kb.create_task(conn, title='continuation', assignee='default')
        kb.link_tasks(conn, parent, source)
        assert kb.complete_task(conn, recovery.id, expected_run_id=claimed.current_run_id, metadata={'reconciliation': {
            'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': event,
        }})
        assert kb.get_task(conn, source).status == 'todo'


def test_foreign_link_cannot_be_excused_by_naming_it_in_verdict(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        parent = kb.create_task(conn, title='foreign requirement', assignee='default')
        kb.link_tasks(conn, parent, source)
        with pytest.raises(ValueError, match='source advanced'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': 'dependency_wait', 'source_task_id': source,
                'source_event_id': int(recovery.idempotency_key.rsplit(':', 1)[1]),
                'dependency_task_id': parent,
            }})
        assert kb.get_task(conn, source).status == 'blocked'


def test_multiple_terminal_events_in_one_transaction_assign_each_occurrence_once(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        with kb.write_txn(conn):
            run = kb._end_run(conn, source, outcome='crashed')
            conn.execute("UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, worker_pid = NULL WHERE id = ?", (source,))
            first = kb._append_event(conn, source, 'crashed', {}, run_id=run)
            second = kb._append_event(conn, source, 'gave_up', {}, run_id=run)
        recovery = _reconciliation_tasks(conn)[0]
        assigned = [e.payload['source_event_id'] for e in kb.list_events(conn, source) if e.kind in {'reconciliation_enqueued', 'reconciliation_coalesced'}]
        assert assigned == [first, second]
        assert reconcile.enqueue_blocker_reconciliation(conn, first) == recovery.id
        assert reconcile.enqueue_blocker_reconciliation(conn, second) == recovery.id


@pytest.mark.parametrize('kind', ['stale', 'reclaimed', 'reconciled'])
def test_other_machine_terminal_paths_use_native_recovery(isolated_home, monkeypatch, kind):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        task = kb.create_task(conn, title='lost worker', assignee='default')
        with kb.write_txn(conn):
            kb._append_event(conn, task, kind, {'reason': 'worker cannot continue'})
        assert kb.get_task(conn, task).status == 'blocked'
        assert len(_reconciliation_tasks(conn)) == 1


def test_recovery_on_project_board_has_no_ownership_of_source_worktree(isolated_home, monkeypatch):
    from hermes_cli import projects_db
    _enable(monkeypatch)
    repo = isolated_home / 'repo'
    repo.mkdir()
    with projects_db.connect_closing() as conn:
        project = projects_db.create_project(conn, name='source project', primary_path=str(repo))
    kb.create_board('scoped', project_id=project)
    with connection.connect_closing(board='scoped') as conn:
        source_id = kb.create_task(conn, title='source', assignee='default', board='scoped')
        source = kb.get_task(conn, source_id)
        kb.block_task(conn, source_id, reason='failure', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        assert recovery.workspace_kind == 'scratch'
        assert recovery.project_id is None
        assert recovery.workspace_path is None
        assert kb.get_task(conn, source_id).workspace_path == source.workspace_path
        assert kb.get_task(conn, source_id).branch_name == source.branch_name


def test_raw_writer_capture_drains_bounded_batches_once_and_respects_kill_switch(isolated_home, monkeypatch):
    from hermes_cli.kanban_blocker_policy import sync_dispatcher_policy
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        sync_dispatcher_policy(conn)
        for index in range(18):
            task = kb.create_task(conn, title=f'legacy {index}', assignee='default')
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task,))
            conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'crashed', '{}', 1)", (task,))
        assert conn.execute('SELECT COUNT(*) FROM blocker_reconciler_pending').fetchone()[0] == 18
    from hermes_cli.kanban_blocker_capture import drain_pending
    with connection.connect_closing() as conn:
        assert len(drain_pending(conn)) == 16
        assert len(drain_pending(conn)) == 2
        before = conn.total_changes
        assert drain_pending(conn) == []
        assert conn.total_changes == before
        assert len(_reconciliation_tasks(conn)) == 18
        task = kb.create_task(conn, title='kill switch source', assignee='default')
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task,))
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'crashed', '{}', 1)", (task,))
        from hermes_cli import config
        monkeypatch.setattr(config, 'load_config', lambda: {'kanban': {'blocker_reconciler': {'enabled': False}}})
        sync_dispatcher_policy(conn)
        assert drain_pending(conn) == []
        _enable(monkeypatch)
        sync_dispatcher_policy(conn)
        assert drain_pending(conn) == []
        assert len(_reconciliation_tasks(conn)) == 18
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_capture_repairs_trigger_atomically_and_never_reseeds_settled_work(isolated_home, monkeypatch):
    import sqlite3
    from hermes_cli.kanban_blocker_capture import install_capture, drain_pending
    from hermes_cli.kanban_blocker_policy import sync_dispatcher_policy
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        sync_dispatcher_policy(conn)
        conn.execute('DROP TRIGGER blocker_reconcile_capture_v2')
        conn.execute("CREATE TRIGGER blocker_reconcile_capture_v2 AFTER INSERT ON task_events BEGIN SELECT 1; END")
        old = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'blocker_reconcile_capture_v2'").fetchone()[0]
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TRIGGER else sqlite3.SQLITE_OK)
        with pytest.raises(sqlite3.DatabaseError):
            install_capture(conn)
        conn.set_authorizer(None)
        assert not conn.in_transaction
        assert conn.execute("SELECT sql FROM sqlite_master WHERE name = 'blocker_reconcile_capture_v2'").fetchone()[0] == old
        install_capture(conn)
        source = kb.create_task(conn, title='raw writer', assignee='default')
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (source,))
        conn.execute("INSERT INTO task_events(task_id, kind, created_at) VALUES (?, 'crashed', 1)", (source,))
        assert len(drain_pending(conn)) == 1
        before = conn.total_changes
        install_capture(conn)
        assert drain_pending(conn) == []
        assert conn.total_changes == before


def test_worker_process_uses_board_policy_without_its_own_setting(isolated_home, monkeypatch):
    import os
    import subprocess
    import sys
    from hermes_cli.kanban_blocker_policy import sync_dispatcher_policy
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        sync_dispatcher_policy(conn)
        worker_home = isolated_home / 'other-profile'
        worker_home.mkdir()
        worker_tmpdir = isolated_home / 'worker-tmp'
        worker_tmpdir.mkdir()
        script = '''from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
with kbc.connect_closing() as conn:
    task = kb.create_task(conn, title="other profile source", assignee="default")
    kb.block_task(conn, task, reason="failed in another profile", kind="transient")
    print(task)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script], cwd=Path(__file__).resolve().parents[2],
            env={'HOME': str(worker_home), 'HERMES_HOME': str(worker_home),
                 'HERMES_KANBAN_DB': str(kb.kanban_db_path()), 'PATH': os.defpath,
                 'TMPDIR': str(worker_tmpdir), 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert len(_reconciliation_tasks(conn)) == 1


def test_transaction_capture_initialization_failure_rolls_back_without_schema(monkeypatch):
    import sqlite3
    conn = sqlite3.connect(':memory:', isolation_level=None)
    monkeypatch.setattr(reconcile, 'begin_events', lambda _conn: (_ for _ in ()).throw(RuntimeError('capture init failed')))
    try:
        with pytest.raises(RuntimeError, match='capture init failed'):
            with connection.write_txn(conn):
                pytest.fail('body must not run')
        assert not conn.in_transaction
    finally:
        conn.close()


def test_inflight_recovery_failure_remains_machine_owned_after_disable(isolated_home, monkeypatch):
    from hermes_cli import config
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = kb.create_task(conn, title='source', assignee='default')
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        monkeypatch.setattr(config, 'load_config', lambda: {'kanban': {'blocker_reconciler': {'enabled': False}}})
        kb.block_task(conn, recovery.id, kind='needs_input', reason='recovery crashed', expected_run_id=claim.current_run_id)
        assert reconcile.attention_class(conn, recovery.id, reconciler_enabled=False) == 'automation_recovery'
        assert [e.payload['outcome'] for e in kb.list_events(conn, source) if e.kind == 'reconciliation_outcome'] == ['reconciliation_failed']


def test_raw_replay_does_not_coalesce_past_a_new_human_occurrence(isolated_home, monkeypatch):
    from hermes_cli.kanban_blocker_capture import drain_pending
    from hermes_cli.kanban_blocker_policy import sync_dispatcher_policy
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        sync_dispatcher_policy(conn)
        source = kb.create_task(conn, title='legacy source', assignee='default')
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?", (source,))
        conn.execute("INSERT INTO task_events(task_id, kind, created_at) VALUES (?, 'crashed', 1)", (source,))
        conn.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (source,))
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, 2)",
                     (source, json.dumps({'kind': 'needs_input', 'reason': 'Approve a new deployment'})))
        drain_pending(conn)
        assert _reconciliation_tasks(conn) == []
        assert reconcile.current_human_gate(conn, source) is not None


def test_identical_settled_completion_replay_is_a_zero_write_noop(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = kb.create_task(conn, title='source', assignee='default')
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        metadata = {'reconciliation': {'outcome': 'cleared/resumed', 'source_task_id': source,
                    'source_event_id': int(recovery.idempotency_key.rsplit(':', 1)[1])}}
        assert kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=claim.current_run_id)
        before = conn.total_changes
        assert kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=claim.current_run_id) is False
        assert conn.total_changes == before
        with pytest.raises(ValueError):
            kb.complete_task(conn, recovery.id, metadata={'reconciliation': {**metadata['reconciliation'], 'unexpected': True}})


@pytest.mark.parametrize('late_write', [False, True])
def test_prior_run_evidence_requires_durable_run_window(isolated_home, monkeypatch, late_write):
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = kb.create_task(conn, title='source', assignee='default')
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        first = kb.claim_task(conn, recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(first.current_run_id))
        kb.add_comment(conn, source, author='default', body='Verified workspace')
        stamped = [e.payload for e in kb.list_events(conn, source) if e.kind == 'commented'][-1]
        _record_task_failure(conn, recovery.id, 'retryable worker failure', outcome='spawn_failed', release_claim=True, end_run=True, failure_limit=10)
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
        if late_write:
            with kb.write_txn(conn):
                kb._append_event(conn, source, 'commented', stamped)
        second = kb.claim_task(conn, recovery.id)
        metadata = {'reconciliation': {'outcome': 'cleared/resumed', 'source_task_id': source,
                    'source_event_id': int(recovery.idempotency_key.rsplit(':', 1)[1])}}
        if late_write:
            with pytest.raises(ValueError, match='source advanced'):
                kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=second.current_run_id)
        else:
            assert kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=second.current_run_id)


def test_prior_blocked_run_evidence_survives_synthesized_terminal_event(isolated_home, monkeypatch):
    """A reasoned block closes the claimed run but attributes its event to a synthesized run."""
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = kb.create_task(conn, title='source', assignee='default')
        kb.block_task(conn, source, kind='transient', reason='failure')
        recovery = _reconciliation_tasks(conn)[0]
        first = kb.claim_task(conn, recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(first.current_run_id))
        kb.add_comment(conn, source, author='default', body='Verified workspace')
        stamped = [e for e in kb.list_events(conn, source) if e.kind == 'commented'][-1]
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
        assert kb.block_task(conn, recovery.id, kind='transient', reason='retry later',
                             expected_run_id=first.current_run_id)
        # Model the historical reclaim path: the closed attempt has a
        # different outcome from the terminal event recorded by the park.
        # Only the disposable board is edited; no live event is rewritten.
        conn.execute("UPDATE task_runs SET outcome = 'crashed' WHERE id = ?",
                     (first.current_run_id,))
        assert not [e for e in kb.list_events(conn, recovery.id)
                    if e.run_id == first.current_run_id and e.kind == 'crashed']
        assert kb.unblock_task(conn, recovery.id)
        second = kb.claim_task(conn, recovery.id)
        assert second is not None
        from hermes_cli.kanban_blocker_evidence import matches
        source_event_id = int(recovery.idempotency_key.rsplit(':', 1)[1])
        assert matches(conn, stamped.payload, recovery.id, source, source_event_id, stamped.id)
        with kb.write_txn(conn):
            late = kb._append_event(conn, source, 'commented', stamped.payload)
        assert not matches(conn, stamped.payload, recovery.id, source, source_event_id, late)


# --- Benign park-artifact chain: advance guard + drifted-source settlement ---


def test_park_chain_accepts_evidence_from_prior_recovery_attempt(isolated_home, monkeypatch):
    """Disposable-board canary for the pinned occurrence and retried recovery."""
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        first = kb.claim_task(conn, recovery.id)
        event = int(recovery.idempotency_key.rsplit(':', 1)[1])
        _park_then_evidence(conn, monkeypatch, source, recovery, first)
        _record_task_failure(conn, recovery.id, 'retryable worker failure', outcome='spawn_failed',
                             release_claim=True, end_run=True, failure_limit=10)
        second = kb.claim_task(conn, recovery.id)
        assert kb.complete_task(conn, recovery.id, expected_run_id=second.current_run_id,
                                metadata={'reconciliation': {'outcome': 'cleared/resumed',
                                          'source_task_id': source, 'source_event_id': event}})
        assert kb.get_task(conn, source).status == 'todo'


def _park_then_evidence(conn, monkeypatch, source, recovery, claim):
    """Record the misfire shape: evidence-free park note + its park, then
    recovery-owned evidence comment/link writes (crossing the fence via
    matches()), mirroring the live t_a6e9cf69 chain."""
    kb.add_comment(conn, source, author='default', body='Park note citing an unrelated task address')
    assert kb.schedule_task(conn, source, reason='misfiring park citing the wrong address')
    monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claim.current_run_id))
    parent = kb.create_task(conn, title='continuation', assignee='default')
    kb.link_tasks(conn, parent, source)
    kb.add_comment(conn, source, author='default', body='Recovery evidence: zero-loss verified')
    monkeypatch.delenv('HERMES_KANBAN_TASK')
    monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
    return parent


@pytest.mark.parametrize('outcome,extra', [
    ('cleared/resumed', {}),
    ('continuation_created', 'PARENT'),
    ('dependency_wait', 'PARENT'),
    ('backoff_scheduled', 'BACKOFF'),
    ('genuine_human_gate', {'human_action': 'Run the one unfenced unblock command'}),
    ('reconciliation_failed', {'error': 'sanitized failure'}),
])
def test_park_artifact_chain_settles_every_outcome_schema(isolated_home, monkeypatch, outcome, extra):
    import time

    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        source_event_id = int((recovery.idempotency_key or '').rsplit(':', 1)[1])
        parent = _park_then_evidence(conn, monkeypatch, source, recovery, claim)
        assert kb.get_task(conn, source).status == 'scheduled'
        fields = {'outcome': outcome, 'source_task_id': source, 'source_event_id': source_event_id}
        if extra == 'PARENT':
            fields['continuation_task_id' if outcome == 'continuation_created' else 'dependency_task_id'] = parent
        elif extra == 'BACKOFF':
            fields['resume_at'] = int(time.time()) + 3600
        elif isinstance(extra, dict):
            fields.update(extra)
        assert kb.complete_task(
            conn, recovery.id, summary='verdict',
            metadata={'reconciliation': fields}, expected_run_id=claim.current_run_id,
        )
        settled = kb.get_task(conn, source)
        if outcome == 'genuine_human_gate':
            assert settled.status == 'scheduled'
            assert settled.block_kind == 'needs_input'
        elif outcome == 'reconciliation_failed':
            # The parked state stands; no progress transition, no human gate.
            assert settled.status == 'scheduled'
            assert settled.block_kind == 'transient'
        elif outcome == 'backoff_scheduled':
            assert settled.status == 'scheduled'
            assert settled.block_kind is None
        else:
            # Progress outcomes resume normally; the linked continuation is
            # still open, so _resume_status_from_events yields 'todo'.
            assert settled.status == 'todo'
            assert settled.block_kind is None
        outcomes = [e for e in kb.list_events(conn, source) if e.kind == 'reconciliation_outcome']
        assert [e.payload['outcome'] for e in outcomes] == [outcome]


def test_park_chain_bookkeeping_event_id_remains_stale_occurrence(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        _park_then_evidence(conn, monkeypatch, source, recovery, claim)
        benign_event_id = [e.id for e in kb.list_events(conn, source) if e.kind == 'commented'][0]
        # The stale-check runs before the occurrence-kind check, so the
        # stale error wins regardless of which branch would reject next.
        with pytest.raises(ValueError, match='stale'):
            kb.complete_task(
                conn, recovery.id, summary='bookkeeping id',
                metadata={'reconciliation': {
                    'outcome': 'cleared/resumed', 'source_task_id': source,
                    'source_event_id': benign_event_id,
                }},
                expected_run_id=claim.current_run_id,
            )


def test_genuine_later_blocker_still_invalidates_pinned_occurrence(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        source_event_id = int((recovery.idempotency_key or '').rsplit(':', 1)[1])
        _park_then_evidence(conn, monkeypatch, source, recovery, claim)
        # A genuine later blocker occurrence (needs_input) after the artifacts.
        # Appended raw because block_task's status guard targets running/ready;
        # the native capture coalesces it onto the active recovery exactly as
        # in production, advancing the pinned occurrence.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (source,))
            kb._append_event(conn, source, 'blocked', {
                'reason': 'a genuine new human gate', 'block_kind': 'needs_input',
            })
        genuine = [e.id for e in kb.list_events(conn, source)
                   if e.kind == 'blocked' and (e.payload or {}).get('block_kind') == 'needs_input'][-1]
        # The pinned pre-artifacts verdict is now stale — the recovery must
        # acknowledge the new occurrence; the park chain cannot hide it.
        with pytest.raises(ValueError, match='stale; expected newest coalesced event'):
            kb.complete_task(
                conn, recovery.id, summary='stale after genuine occurrence',
                metadata={'reconciliation': {
                    'outcome': 'genuine_human_gate', 'source_task_id': source,
                    'source_event_id': source_event_id, 'human_action': 'decide',
                }},
                expected_run_id=claim.current_run_id,
            )
        assert not any(
            e.kind == 'reconciliation_outcome' for e in kb.list_events(conn, source)
        )
        # Citing the genuine occurrence settles the verdict against it; the
        # source keeps its human-gate state instead of resuming.
        assert kb.complete_task(
            conn, recovery.id, summary='verdict on the genuine occurrence',
            metadata={'reconciliation': {
                'outcome': 'genuine_human_gate', 'source_task_id': source,
                'source_event_id': genuine, 'human_action': 'decide',
            }},
            expected_run_id=claim.current_run_id,
        )
        settled = kb.get_task(conn, source)
        assert settled.status == 'blocked'
        assert settled.block_kind == 'needs_input'


def test_drift_settlement_rejects_blocked_source_with_active_run(isolated_home, monkeypatch):
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        source_event_id = int((recovery.idempotency_key or '').rsplit(':', 1)[1])
        _park_then_evidence(conn, monkeypatch, source, recovery, claim)
        # An operator re-gated the source and a worker picked it up again.
        # Raw status/run flips write no guard-relevant events (claimed is
        # guard-excluded), so the chain detection is intact and the drift
        # branch must still refuse to settle a source with an active run.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = 424242 WHERE id = ?",
                (source,),
            )
        assert kb.get_task(conn, source).current_run_id == 424242
        with pytest.raises(ValueError, match='active run'):
            kb.complete_task(
                conn, recovery.id, summary='must not settle',
                metadata={'reconciliation': {
                    'outcome': 'cleared/resumed', 'source_task_id': source,
                    'source_event_id': source_event_id,
                }},
                expected_run_id=claim.current_run_id,
            )


def test_drift_settlement_rejects_unexplained_status(isolated_home, monkeypatch):
    """Only the recorded park chain earns drift settlement: any other
    observed drift must be rejected. E2E this is unreachable past the
    advance guard — every event-visible drift (comment/link/scheduled/status)
    either matches the chain or breaks it and raises 'source advanced' — so
    assert the settlement contract directly on apply_completion's verdict,
    exactly as complete_task invokes it inside the native transaction."""
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='crashed', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        source_event_id = int((recovery.idempotency_key or '').rsplit(':', 1)[1])
        _park_then_evidence(conn, monkeypatch, source, recovery, claim)
        verdict = outcomes.validate_completion(conn, recovery.id, {'reconciliation': {
            'outcome': 'cleared/resumed', 'source_task_id': source,
            'source_event_id': source_event_id,
        }})
        assert verdict is not None and verdict['source_drifted_by_recorded_park_chain']
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (source,))
        with pytest.raises(ValueError, match='drifted outside the recorded park chain'):
            outcomes.apply_completion(conn, recovery.id, {'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source,
                'source_event_id': source_event_id,
            }})
        assert not any(
            e.kind == 'reconciliation_outcome' for e in kb.list_events(conn, source)
        )


def test_evidence_only_history_without_park_chain_keeps_blocked_settlement(isolated_home, monkeypatch):
    """No park pair: evidence-bearing writes exempt, source still 'blocked',
    settled from 'blocked' exactly as before the drift contract."""
    _enable(monkeypatch)
    with connection.connect_closing() as conn:
        source = _running(conn)
        assert kb.block_task(conn, source, reason='failure', kind='transient')
        recovery = _reconciliation_tasks(conn)[0]
        claim = kb.claim_task(conn, recovery.id)
        source_event_id = int((recovery.idempotency_key or '').rsplit(':', 1)[1])
        monkeypatch.setenv('HERMES_KANBAN_TASK', recovery.id)
        monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(claim.current_run_id))
        kb.add_comment(conn, source, author='default', body='Verified the source workspace.')
        monkeypatch.delenv('HERMES_KANBAN_TASK')
        monkeypatch.delenv('HERMES_KANBAN_RUN_ID')
        assert kb.complete_task(
            conn, recovery.id, summary='verdict',
            metadata={'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source,
                'source_event_id': source_event_id,
            }},
            expected_run_id=claim.current_run_id,
        )
        assert kb.get_task(conn, source).status == 'ready'
def _natively_recover_and_complete(conn, source):
    """Mirror t_21e46f20: promoted -> claimed -> spawned -> completed after the occurrence."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = ?", (source,))
        kb._append_event(conn, source, 'promoted', None)
    run = kb.claim_task(conn, source)
    assert run is not None
    assert kb.complete_task(conn, source, summary='finished natively', expected_run_id=run.current_run_id)


def _settled_setup(conn, monkeypatch):
    _enable(monkeypatch)
    source = _running(conn)
    assert kb.block_task(conn, source, kind='transient', reason='worker crashed')
    recovery = _reconciliation_tasks(conn)[0]
    event = int(recovery.idempotency_key.rsplit(':', 1)[1])
    return source, recovery, event


def test_source_settled_after_native_recovery_records_cleared_without_transition(isolated_home, monkeypatch):
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        kb.add_comment(conn, source, author='worker', body='progress note without evidence')
        _natively_recover_and_complete(conn, source)
        claim = kb.claim_task(conn, recovery.id)
        before = kb.get_task(conn, source)
        metadata = {'reconciliation': {'outcome': 'cleared/resumed', 'source_task_id': source,
                                       'source_event_id': event}}
        assert kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=claim.current_run_id)
        after = kb.get_task(conn, source)
        assert (after.status, after.completed_at, after.block_kind) == (before.status, before.completed_at, before.block_kind)
        outcome = [e for e in kb.list_events(conn, source) if e.kind == 'reconciliation_outcome'][-1]
        completed = [e.id for e in kb.list_events(conn, source) if e.kind == 'completed'][-1]
        assert outcome.payload['outcome'] == 'cleared/resumed'
        assert outcome.payload['source_settled_event_id'] == completed
        assert outcome.payload['reconciliation_task_id'] == recovery.id
        assert kb.get_task(conn, recovery.id).status == 'done'
        # Exact replay stays a zero-write no-op.
        changes = conn.total_changes
        assert kb.complete_task(conn, recovery.id, metadata=metadata, expected_run_id=claim.current_run_id) is False
        assert conn.total_changes == changes


def test_source_settled_then_archived_still_records_cleared(isolated_home, monkeypatch):
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        _natively_recover_and_complete(conn, source)
        assert kb.archive_task(conn, source)
        claim = kb.claim_task(conn, recovery.id)
        assert kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
            'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': event}})
        assert kb.get_task(conn, source).status == 'archived'


@pytest.mark.parametrize('outcome,extra', [
    ('genuine_human_gate', {'human_action': 'Pick one'}),
    ('reconciliation_failed', {'error': 'could not recover'}),
    ('backoff_scheduled', None),
])
def test_settled_source_rejects_non_cleared_verdicts(isolated_home, monkeypatch, outcome, extra):
    import time as _time
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        _natively_recover_and_complete(conn, source)
        claim = kb.claim_task(conn, recovery.id)
        extra = extra or {'resume_at': int(_time.time()) + 600}
        with pytest.raises(ValueError, match='advanced after source event'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': outcome, 'source_task_id': source, 'source_event_id': event, **extra}})
        assert not any(e.kind == 'reconciliation_outcome' for e in kb.list_events(conn, source))
        assert kb.get_task(conn, source).status == 'done'


def test_settled_source_rejects_when_reopened_after_completion(isolated_home, monkeypatch):
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        _natively_recover_and_complete(conn, source)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (source,))
            kb._append_event(conn, source, 'status', {'status': 'blocked'})
        claim = kb.claim_task(conn, recovery.id)
        with pytest.raises(ValueError, match='advanced after source event'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': event}})


def test_settled_source_rejects_after_genuine_later_blocker_occurrence(isolated_home, monkeypatch):
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        with kb.write_txn(conn):
            kb._append_event(conn, source, 'gave_up', {'error': 'a later, unassigned occurrence'})
        _natively_recover_and_complete(conn, source)
        from hermes_cli.kanban_blocker_outcomes import _settled_source_completion
        assert _settled_source_completion(conn, source, event) is None
        claim = kb.claim_task(conn, recovery.id)
        with pytest.raises(ValueError, match='stale'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': event}})


def test_settled_after_newest_coalesced_gave_up_mirrors_t_a94c519b(isolated_home, monkeypatch):
    """crashed -> coalesced gave_up -> promoted -> claimed -> completed: newest id is accepted."""
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        with kb.write_txn(conn):
            gave_up = kb._append_event(conn, source, 'gave_up', {'error': 'pid not alive'})
        assert any(e.kind == 'reconciliation_coalesced' and e.payload.get('source_event_id') == gave_up
                   for e in kb.list_events(conn, source))
        _natively_recover_and_complete(conn, source)
        claim = kb.claim_task(conn, recovery.id)
        assert kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
            'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': gave_up}})
        assert kb.get_task(conn, source).status == 'done'


def test_settled_path_does_not_admit_non_newest_event_id(isolated_home, monkeypatch):
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        _natively_recover_and_complete(conn, source)
        promoted = [e.id for e in kb.list_events(conn, source) if e.kind == 'promoted'][-1]
        claim = kb.claim_task(conn, recovery.id)
        with pytest.raises(ValueError, match='stale'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': promoted}})


def test_blocked_source_with_benign_promoted_still_fenced(isolated_home, monkeypatch):
    """No regression: a live (not settled) source that advanced is still stale."""
    with connection.connect_closing() as conn:
        source, recovery, event = _settled_setup(conn, monkeypatch)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (source,))
            kb._append_event(conn, source, 'promoted', None)
        claim = kb.claim_task(conn, recovery.id)
        with pytest.raises(ValueError, match='advanced after source event'):
            kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
                'outcome': 'cleared/resumed', 'source_task_id': source, 'source_event_id': event}})
