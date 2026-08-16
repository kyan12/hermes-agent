from __future__ import annotations

import json
import sqlite3
import threading
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


def _fulfilled_source_topology(conn) -> tuple[str, str, kb.Task, int]:
    """Build the live regression shape: an unroutable wrapper already fulfilled by a direct parent."""
    source_id = _running(
        conn,
        title="unroutable source wrapper",
        workspace_kind="worktree",
        workspace_path=None,
    )
    continuation_id = kb.create_task(
        conn, title="valid continuation", assignee="code-crab",
    )
    kb.link_tasks(conn, continuation_id, source_id)
    claimed_continuation = kb.claim_task(conn, continuation_id, claimer="continuation")
    assert claimed_continuation is not None
    assert kb.complete_task(
        conn,
        continuation_id,
        summary="Continuation shipped and independently reviewed.",
        metadata={"commit": "abc123", "review": {"approved": True}},
        expected_run_id=claimed_continuation.current_run_id,
    )
    assert kb.block_task(
        conn,
        source_id,
        reason="worktree route missing",
        kind="transient",
    )
    recovery = _reconciliation_tasks(conn)[0]
    source_event = [
        event for event in kb.list_events(conn, source_id) if event.kind == "blocked"
    ][-1]
    return source_id, continuation_id, recovery, source_event.id


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


def test_cleared_completed_closes_fulfilled_unroutable_source_and_promotes_child(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        child_id = kb.create_task(
            conn, title="downstream rollout", assignee="code-crab", parents=[source_id],
        )
        assert kb.get_task(conn, child_id).status == "todo"
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="Accepted the fulfilled continuation handoff.",
            metadata={"reconciliation": {
                "outcome": "cleared/completed",
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }},
            expected_run_id=claimed.current_run_id,
        )

        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "done"
        assert source.completed_at is not None
        assert source.current_run_id is None
        assert kb.get_task(conn, child_id).status == "ready"
        assert kb.recompute_ready(conn) == 0
        assert len([
            event for event in kb.list_events(conn, child_id) if event.kind == "promoted"
        ]) == 1
        source_run = kb.list_runs(conn, source_id)[-1]
        assert source_run.summary == "Continuation shipped and independently reviewed."
        assert source_run.metadata == {
            "accepted_continuation": {
                "task_id": continuation_id,
                "run_id": claimed_continuation_run_id(conn, continuation_id),
                "summary": "Continuation shipped and independently reviewed.",
                "metadata": {"commit": "abc123", "review": {"approved": True}},
            },
            "accepted_by_reconciliation": {
                "task_id": recovery.id,
                "source_event_id": source_event_id,
            },
        }
        events = kb.list_events(conn, source_id)
        completed = [event for event in events if event.kind == "completed"]
        outcomes = [event for event in events if event.kind == "reconciliation_outcome"]
        assert len(completed) == 1
        assert outcomes[-1].payload["outcome"] == "cleared/completed"
        assert outcomes[-1].payload["continuation_task_id"] == continuation_id


def claimed_continuation_run_id(conn, continuation_id: str) -> int:
    row = conn.execute(
        "SELECT id FROM task_runs WHERE task_id=? AND outcome='completed' ORDER BY id DESC LIMIT 1",
        (continuation_id,),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def test_cleared_completed_rejects_stale_lineage(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with kb.write_txn(conn):
            newer_event_id = kb._append_event(
                conn, source_id, "gave_up", {"error": "new occurrence"},
            )
            kb._append_event(conn, source_id, "reconciliation_coalesced", {
                "source_event_id": newer_event_id,
                "source_status": "automation_recovery",
                "reconciliation_task_id": recovery.id,
            })
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(conn, recovery.id, metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }}, expected_run_id=claimed.current_run_id)
        assert kb.get_task(conn, source_id).status == "automation_recovery"


def test_cleared_completed_rejects_wrong_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        _source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        wrong_source_id = kb.create_task(conn, title="wrong source", assignee="code-crab")
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with pytest.raises(ValueError, match="does not match recovery lineage"):
            kb.complete_task(conn, recovery.id, metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": wrong_source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }}, expected_run_id=claimed.current_run_id)


@pytest.mark.parametrize("tamper", ["creator", "profile"])
def test_cleared_completed_rejects_unauthorized_reconciler(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, tamper: str,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with kb.write_txn(conn):
            column = "created_by" if tamper == "creator" else "assignee"
            conn.execute(f"UPDATE tasks SET {column}='ordinary-worker' WHERE id=?", (recovery.id,))
        with pytest.raises(ValueError, match="authorized blocker reconciler"):
            kb.complete_task(conn, recovery.id, metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }}, expected_run_id=claimed.current_run_id)
        assert kb.get_task(conn, source_id).status == "automation_recovery"


def test_cleared_completed_rejects_active_source_writer(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        now = int(kb.time.time())
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'code-crab', 'running', ?)", (source_id, now),
            )
            conn.execute(
                "UPDATE tasks SET current_run_id=?, claim_lock='writer', "
                "claim_expires=?, worker_pid=123 WHERE id=?",
                (cur.lastrowid, now + 900, source_id),
            )
        with pytest.raises(ValueError, match="active source writer"):
            kb.complete_task(conn, recovery.id, metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }}, expected_run_id=claimed.current_run_id)


def test_cleared_completed_requires_completed_direct_parent(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        continuation_id = kb.create_task(conn, title="unfinished continuation", assignee="code-crab")
        kb.link_tasks(conn, continuation_id, source_id)
        assert kb.block_task(conn, source_id, reason="route missing", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        with pytest.raises(ValueError, match="completed direct continuation"):
            kb.complete_task(conn, recovery.id, metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }}, expected_run_id=claimed.current_run_id)


def test_redrive_creates_one_new_live_wrapper_for_unfinished_fulfilled_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        repair_id = kb.create_task(conn, title="kernel repair", assignee="code-crab")
        kb.link_tasks(conn, repair_id, source_id)
        kb.add_comment(conn, source_id, author="default", body="Prior recovery evidence.")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (recovery.id,))
        redriven_id = kb.redrive_blocker_reconciliation(conn, source_id, source_event_id)
        assert redriven_id is not None and redriven_id != recovery.id
        redriven = kb.get_task(conn, redriven_id)
        assert redriven is not None and redriven.status == "ready"
        assert redriven.created_by == "blocker-reconciler"
        assert kb.redrive_blocker_reconciliation(conn, source_id, source_event_id) == redriven_id
        claimed = kb.claim_task(conn, redriven_id, claimer="reconciler")
        assert claimed is not None
        assert kb.complete_task(
            conn, redriven_id,
            metadata={"reconciliation": {
                "outcome": "cleared/completed", "source_task_id": source_id,
                "source_event_id": source_event_id,
                "continuation_task_id": continuation_id,
            }},
            expected_run_id=claimed.current_run_id,
        )
        assert kb.get_task(conn, source_id).status == "done"


def test_redrive_normalizes_dependency_gated_fulfilled_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live topology: an operator parked the recovery source in todo behind the kernel repair."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, _continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        repair_id = kb.create_task(conn, title="active kernel repair", assignee="code-crab")
        kb.link_tasks(conn, repair_id, source_id)
        assert kb.claim_task(conn, repair_id, claimer="repair") is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (recovery.id,))
        assert kb.unblock_task(conn, source_id)
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"

        redriven_id = kb.redrive_blocker_reconciliation(conn, source_id, source_event_id)

        assert redriven_id is not None
        redriven = kb.get_task(conn, redriven_id)
        source = kb.get_task(conn, source_id)
        assert redriven is not None and redriven.status == "ready"
        assert source is not None and source.status == "automation_recovery"
        envelope_marker = "```json\n"
        assert redriven.body is not None
        envelope = json.loads(
            redriven.body.split(envelope_marker, 1)[1].split("\n```", 1)[0]
        )
        assert envelope["source"]["status"] == "automation_recovery"


def test_redrive_rejects_ordinary_dependency_gated_todo_without_recovery_assignment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        continuation_id = kb.create_task(conn, title="completed parent", assignee="code-crab")
        continuation_claim = kb.claim_task(conn, continuation_id, claimer="continuation")
        assert continuation_claim is not None
        assert kb.complete_task(
            conn, continuation_id, summary="done",
            expected_run_id=continuation_claim.current_run_id,
        )
        repair_id = kb.create_task(conn, title="active parent", assignee="code-crab")
        assert kb.claim_task(conn, repair_id, claimer="repair") is not None
        source_id = kb.create_task(
            conn, title="ordinary dependency gated task", assignee="code-crab",
            parents=[continuation_id, repair_id],
        )
        with kb.write_txn(conn):
            source_event_id = kb._append_event(
                conn, source_id, "gave_up", {"error": "synthetic old failure"},
            )

        with pytest.raises(ValueError, match="requires an automation_recovery source"):
            kb.redrive_blocker_reconciliation(conn, source_id, source_event_id)
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"


def test_redrive_still_rejects_source_change_after_assignment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (recovery.id,))
        redriven_id = kb.redrive_blocker_reconciliation(conn, source_id, source_event_id)
        assert redriven_id is not None
        claimed = kb.claim_task(conn, redriven_id, claimer="reconciler")
        assert claimed is not None
        kb.add_comment(conn, source_id, author="operator", body="New source truth.")
        with pytest.raises(ValueError, match="source advanced"):
            kb.complete_task(
                conn, redriven_id,
                metadata={"reconciliation": {
                    "outcome": "cleared/completed", "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "continuation_task_id": continuation_id,
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, source_id).status == "automation_recovery"


def test_redrive_holds_write_transaction_through_wrapper_creation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, _continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (recovery.id,))
        original_create = kb.create_task

        def create_while_locked(*args, **kwargs):
            assert conn.in_transaction, "redrive eligibility and assignment must share BEGIN IMMEDIATE"
            return original_create(*args, **kwargs)

        monkeypatch.setattr(kb, "create_task", create_while_locked)
        assert kb.redrive_blocker_reconciliation(conn, source_id, source_event_id) is not None


def test_cleared_completed_rejects_continuation_recompletion_race(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        original_validate = kb._validate_reconciliation_verdict

        def validate_then_recomplete(*args, **kwargs):
            verdict = original_validate(*args, **kwargs)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET result='newer result', completed_at=? WHERE id=?",
                    (int(kb.time.time()) + 1, continuation_id),
                )
                kb._synthesize_ended_run(
                    conn, continuation_id, outcome="completed",
                    summary="Newer completion handoff.", metadata={"generation": 2},
                )
            return verdict

        monkeypatch.setattr(kb, "_validate_reconciliation_verdict", validate_then_recomplete)
        with pytest.raises(ValueError, match="completion generation changed"):
            kb.complete_task(
                conn, recovery.id,
                metadata={"reconciliation": {
                    "outcome": "cleared/completed", "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "continuation_task_id": continuation_id,
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, source_id).status == "automation_recovery"


def test_cleared_completed_replay_is_idempotent_and_source_never_redrives(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, continuation_id, recovery, source_event_id = _fulfilled_source_topology(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        verdict = {"reconciliation": {
            "outcome": "cleared/completed", "source_task_id": source_id,
            "source_event_id": source_event_id,
            "continuation_task_id": continuation_id,
        }}
        assert kb.complete_task(
            conn, recovery.id, metadata=verdict,
            expected_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn, recovery.id, metadata=verdict,
            expected_run_id=claimed.current_run_id,
        ) is False
        assert kb.enqueue_blocker_reconciliation(conn, source_event_id) is None
        assert kb.redrive_blocker_reconciliation(conn, source_id, source_event_id) is None
        assert kb.claim_task(conn, source_id, claimer="dispatcher") is None
        kb.recompute_ready(conn)
        assert kb.get_task(conn, source_id).status == "done"
        assert len([e for e in kb.list_events(conn, source_id) if e.kind == "completed"]) == 1
        assert len([
            run for run in kb.list_runs(conn, source_id) if run.outcome == "completed"
        ]) == 1


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
        kb.add_comment(
            conn, source_id, author="default",
            body="Verified current human-only gate evidence.",
            origin_task_id=recovery.id, origin_run_id=claimed.current_run_id,
        )
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
                    "attention_owner": "Kevin Yan",
                    "why_automation_cannot_perform": "Only the owner can authorize payment.",
                    "current_evidence": "The deployment is paid and public.",
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
            "attention_owner": "Kevin Yan",
            "why_automation_cannot_perform": "Only the owner can authorize payment.",
            "current_evidence": "The deployment is paid and public.",
            "continuation_task_id": None,
        }
        human_gate_events = [
            e for e in kb.list_events(conn, source_id)
            if e.kind == "human_gate_affirmed"
        ]
        assert len(human_gate_events) == 1
        assert human_gate_events[0].payload == {
            "attention_owner": "Kevin Yan",
            "human_action": "Approve the paid public deployment.",
            "why_automation_cannot_perform": "Only the owner can authorize payment.",
            "current_evidence": "The deployment is paid and public.",
            "affirmed_by": f"reconciler:{recovery.id}",
            "source_event_id": source_event.id,
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


def test_dispatch_tick_backfills_recovery_event_missed_by_stale_daemon(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon loaded before the recovery hook must not strand the source forever."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn, title="missed recovery hook", max_retries=1)
        real_enqueue = kb.enqueue_blocker_reconciliation
        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
        assert kb._record_task_failure(
            conn,
            source_id,
            "Iteration budget exhausted (140/140)",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "automation_recovery"
        assert _reconciliation_tasks(conn) == []

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", real_enqueue)
        kb._dispatch_once_locked(conn, dry_run=True, max_spawn=0)

        recoveries = _reconciliation_tasks(conn)
        assert len(recoveries) == 1
        assert recoveries[0].status == "ready"
        assert recoveries[0].created_by == "blocker-reconciler"
        assert recoveries[0].idempotency_key and source_id in recoveries[0].idempotency_key
        assert any(
            event.kind == "reconciliation_enqueued"
            for event in kb.list_events(conn, source_id)
        )

        kb._dispatch_once_locked(conn, dry_run=True, max_spawn=0)
        assert len(_reconciliation_tasks(conn)) == 1


def test_recovery_backfill_is_bounded_and_drains_across_ticks(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        real_enqueue = kb.enqueue_blocker_reconciliation
        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
        total = kb.RECONCILIATION_BACKFILL_BATCH_LIMIT + 2
        source_ids: list[str] = []
        for index in range(total):
            source_id = _running(
                conn,
                title=f"missed recovery hook {index}",
                max_retries=1,
            )
            source_ids.append(source_id)
            assert kb._record_task_failure(
                conn,
                source_id,
                "Iteration budget exhausted (140/140)",
                outcome="timed_out",
                release_claim=True,
                end_run=True,
            )
        assert _reconciliation_tasks(conn) == []

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", real_enqueue)
        first = kb.reconcile_orphaned_automation_recovery(conn)
        second = kb.reconcile_orphaned_automation_recovery(conn)
        events_before_third = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events"
        ).fetchone()["n"]
        third = kb.reconcile_orphaned_automation_recovery(conn)
        events_after_third = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events"
        ).fetchone()["n"]

        assert len(first) == kb.RECONCILIATION_BACKFILL_BATCH_LIMIT
        assert len(second) == 2
        assert third == []
        assert events_after_third == events_before_third
        assert len(_reconciliation_tasks(conn)) == total

        for source_id in source_ids:
            cursor = conn.execute(
                "SELECT recovery_backfill_after FROM tasks WHERE id=?",
                (source_id,),
            ).fetchone()["recovery_backfill_after"]
            scan = conn.execute(
                "SELECT MAX(id) AS id FROM task_events WHERE task_id=? "
                "AND kind='reconciliation_backfill_repaired'",
                (source_id,),
            ).fetchone()["id"]
            assert cursor == scan


def test_database_trigger_marks_event_written_by_pre_hook_process(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn, title="event from stale daemon", max_retries=1)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='automation_recovery' WHERE id=?",
                (source_id,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'gave_up', '{}', 1)",
                (source_id,),
            )

        pending = conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"]
        repaired = kb.reconcile_orphaned_automation_recovery(conn)

        assert pending == 1
        assert len(repaired) == 1


def test_database_trigger_does_not_recurse_on_reconciliation_wrapper(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        wrapper_id = kb.create_task(
            conn,
            title="reconciliation wrapper",
            idempotency_key=f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}board:source:1",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='automation_recovery' WHERE id=?",
                (wrapper_id,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'gave_up', '{}', 1)",
                (wrapper_id,),
            )

        pending = conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (wrapper_id,),
        ).fetchone()["recovery_backfill_pending"]

        assert pending == 0
        assert kb.reconcile_orphaned_automation_recovery(conn) == []


def test_recovery_backfill_selection_uses_partial_bounded_index(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM tasks "
            "INDEXED BY idx_tasks_recovery_source_scan "
            "WHERE status='automation_recovery' AND recovery_backfill_pending=1 "
            "AND (idempotency_key IS NULL "
            "OR idempotency_key NOT LIKE 'kanban-reconcile:%') "
            "ORDER BY recovery_backfill_after, created_at, id LIMIT ?",
            (kb.RECONCILIATION_BACKFILL_BATCH_LIMIT,),
        ).fetchall()
        event_kinds = sorted(kb.RECONCILIATION_EVENT_KINDS)
        placeholders = ",".join("?" for _ in event_kinds)
        event_plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT MAX(id) AS id FROM task_events "
            f"INDEXED BY idx_events_task_kind_id WHERE task_id=? "
            f"AND kind IN ({placeholders})",
            ("t_source", *event_kinds),
        ).fetchall()

        assert any(
            "idx_tasks_recovery_source_scan" in row["detail"] for row in plan
        )
        assert any(
            "idx_events_task_kind_id" in row["detail"] for row in event_plan
        )


def test_recovery_backfill_settles_coalesced_candidate_without_starvation(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        real_enqueue = kb.enqueue_blocker_reconciliation
        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
        total = kb.RECONCILIATION_BACKFILL_BATCH_LIMIT + 2
        source_ids: list[str] = []
        for index in range(total):
            source_id = _running(
                conn,
                title=f"missed recovery hook {index}",
                max_retries=1,
            )
            source_ids.append(source_id)
            assert kb._record_task_failure(
                conn,
                source_id,
                "Iteration budget exhausted (140/140)",
                outcome="timed_out",
                release_claim=True,
                end_run=True,
            )

        with kb.write_txn(conn):
            for position, source_id in enumerate(source_ids, start=1):
                conn.execute(
                    "UPDATE tasks SET created_at=? WHERE id=?",
                    (position, source_id),
                )

        stubborn_id = source_ids[0]
        stubborn_event_ids = {
            event.id for event in kb.list_events(conn, stubborn_id)
            if event.kind in kb.RECONCILIATION_EVENT_KINDS
        }

        def coalesce_one(db: sqlite3.Connection, event_id: int) -> str | None:
            if event_id in stubborn_event_ids:
                return "t_existing_active_reconciliation"
            return real_enqueue(db, event_id)

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", coalesce_one)
        first = kb.reconcile_orphaned_automation_recovery(conn)
        second = kb.reconcile_orphaned_automation_recovery(conn)
        events_before_third = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events"
        ).fetchone()["n"]
        third = kb.reconcile_orphaned_automation_recovery(conn)
        events_after_third = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events"
        ).fetchone()["n"]

        assert len(first) == kb.RECONCILIATION_BACKFILL_BATCH_LIMIT - 1
        assert len(second) == 2
        assert third == []
        assert events_after_third == events_before_third
        assert len(_reconciliation_tasks(conn)) == total - 1
        deferred = [
            event for event in kb.list_events(conn, stubborn_id)
            if event.kind == "reconciliation_backfill_deferred"
        ]
        assert deferred
        assert all(
            event.payload
            and event.payload.get("reason") == "coalesced_without_exact_occurrence"
            for event in deferred
        )
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (stubborn_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


def test_newer_trigger_arriving_during_backfill_remains_pending(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        real_enqueue = kb.enqueue_blocker_reconciliation
        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
        source_id = _running(conn, title="concurrent recovery event", max_retries=1)
        assert kb._record_task_failure(
            conn,
            source_id,
            "Iteration budget exhausted (140/140)",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
        )

        def append_newer_event(db: sqlite3.Connection, _event_id: int) -> str:
            with kb.write_txn(db, allow_nested=True):
                db.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'gave_up', '{\"reason\":\"newer\"}', 2)",
                    (source_id,),
                )
            return "t_existing_active_reconciliation"

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", append_newer_event)
        assert kb.reconcile_orphaned_automation_recovery(conn) == []
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 1
        latest_event_id = conn.execute(
            "SELECT MAX(id) AS id FROM task_events WHERE task_id=? "
            "AND kind IN ('gave_up', 'timed_out')",
            (source_id,),
        ).fetchone()["id"]
        assert conn.execute(
            "SELECT recovery_backfill_after FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_after"] == latest_event_id
        assert not any(
            event.kind.startswith("reconciliation_backfill_")
            for event in kb.list_events(conn, source_id)
        )

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", real_enqueue)
        repaired = kb.reconcile_orphaned_automation_recovery(conn)

        assert len(repaired) == 1
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


def test_concurrent_backfills_settle_pending_source_once(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    real_enqueue = kb.enqueue_blocker_reconciliation
    monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
    with kb.connect_closing() as conn:
        source_id = _running(conn, title="concurrent backfill source", max_retries=1)
        assert kb._record_task_failure(
            conn,
            source_id,
            "Iteration budget exhausted (140/140)",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
        )

    barrier = threading.Barrier(2)
    monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", real_enqueue)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run_backfill() -> None:
        try:
            barrier.wait(timeout=10)
            with kb.connect_closing() as db:
                results.append(kb.reconcile_orphaned_automation_recovery(db))
        except BaseException as exc:  # surfaced below with both thread outcomes
            errors.append(exc)

    threads = [threading.Thread(target=run_backfill) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    with kb.connect_closing() as conn:
        settlement_events = [
            event for event in kb.list_events(conn, source_id)
            if event.kind in {
                "reconciliation_backfill_repaired",
                "reconciliation_backfill_deferred",
            }
        ]
        assert len(settlement_events) == 1
        assert sum(len(result) for result in results) == 1
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


def test_backfill_does_not_enqueue_before_acquiring_write_lock(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    real_enqueue = kb.enqueue_blocker_reconciliation
    monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
    with kb.connect_closing() as holder, kb.connect_closing() as contender:
        source_id = _running(holder, title="write-lock backfill source", max_retries=1)
        assert kb._record_task_failure(
            holder,
            source_id,
            "Iteration budget exhausted (140/140)",
            outcome="timed_out",
            release_claim=True,
            end_run=True,
        )
        enqueue_calls = 0

        def counted_enqueue(db: sqlite3.Connection, event_id: int) -> str | None:
            nonlocal enqueue_calls
            enqueue_calls += 1
            return real_enqueue(db, event_id)

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", counted_enqueue)
        monkeypatch.setattr(kb, "_BUSY_MAX_RETRIES", 0)
        contender.execute("PRAGMA busy_timeout=0")
        holder.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                kb.reconcile_orphaned_automation_recovery(contender)
            assert enqueue_calls == 0
            assert holder.execute(
                "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
                (source_id,),
            ).fetchone()["recovery_backfill_pending"] == 1
            assert not any(
                event.kind.startswith("reconciliation_backfill_")
                for event in kb.list_events(holder, source_id)
            )
        finally:
            holder.rollback()

        settlement_updates: list[str] = []
        contender.set_trace_callback(
            lambda statement: settlement_updates.append(statement)
            if (
                "UPDATE tasks SET recovery_backfill_after=" in statement
                and "recovery_backfill_pending=" in statement
            ) else None
        )
        recovered = kb.reconcile_orphaned_automation_recovery(contender)
        assert len(recovered) == 1
        assert enqueue_calls == 1
        assert len(settlement_updates) == 1
        settlement_events = [
            event for event in kb.list_events(contender, source_id)
            if event.kind in {
                "reconciliation_backfill_repaired",
                "reconciliation_backfill_deferred",
            }
        ]
        assert len(settlement_events) == 1
        assert contender.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


def test_full_hot_batch_rotates_before_stable_pending_source(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        real_enqueue = kb.enqueue_blocker_reconciliation
        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", lambda *_args: None)
        source_ids: list[str] = []
        for index in range(kb.RECONCILIATION_BACKFILL_BATCH_LIMIT + 1):
            source_id = _running(
                conn,
                title=f"hot recovery source {index}",
                max_retries=1,
            )
            source_ids.append(source_id)
            assert kb._record_task_failure(
                conn,
                source_id,
                "Iteration budget exhausted (140/140)",
                outcome="timed_out",
                release_claim=True,
                end_run=True,
            )

        hot_ids = set(source_ids[:kb.RECONCILIATION_BACKFILL_BATCH_LIMIT])
        stable_id = source_ids[-1]
        with kb.write_txn(conn):
            for position, source_id in enumerate(source_ids, start=1):
                conn.execute(
                    "UPDATE tasks SET created_at=? WHERE id=?",
                    (position, source_id),
                )

        def retrigger_hot(db: sqlite3.Connection, event_id: int) -> str | None:
            source_id = db.execute(
                "SELECT task_id FROM task_events WHERE id=?", (event_id,),
            ).fetchone()["task_id"]
            if source_id in hot_ids:
                with kb.write_txn(db, allow_nested=True):
                    db.execute(
                        "INSERT INTO task_events (task_id, kind, payload, created_at) "
                        "VALUES (?, 'gave_up', '{\"reason\":\"hot\"}', 2)",
                        (source_id,),
                    )
                return "t_existing_active_reconciliation"
            return real_enqueue(db, event_id)

        monkeypatch.setattr(kb, "enqueue_blocker_reconciliation", retrigger_hot)
        assert kb.reconcile_orphaned_automation_recovery(conn) == []
        repaired = kb.reconcile_orphaned_automation_recovery(conn)

        assert len(repaired) == 1
        wrapper = kb.get_task(conn, repaired[0])
        assert wrapper is not None
        assert wrapper.idempotency_key and stable_id in wrapper.idempotency_key
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (stable_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


def test_recovery_pending_trigger_is_refreshed_on_connect(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = kb.create_task(conn, title="partially migrated recovery source")
        with kb.write_txn(conn):
            conn.execute(
                f"DROP TRIGGER {kb.RECONCILIATION_BACKFILL_TRIGGER_NAME}"
            )
            conn.execute(
                f"CREATE TRIGGER {kb.RECONCILIATION_BACKFILL_TRIGGER_NAME} "
                "AFTER INSERT ON task_events BEGIN SELECT 1; END"
            )
            conn.execute(
                "CREATE TRIGGER trg_recovery_backfill_pending "
                "AFTER INSERT ON task_events BEGIN SELECT 1; END"
            )
            conn.execute(
                "UPDATE tasks SET status='automation_recovery', "
                "recovery_backfill_pending=0 WHERE id=?",
                (source_id,),
            )

    kb.init_db()
    with kb.connect_closing() as conn:
        trigger_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name=?",
            (kb.RECONCILIATION_BACKFILL_TRIGGER_NAME,),
        ).fetchone()["sql"]

        assert "recovery_backfill_pending=1" in trigger_sql
        assert "NEW.kind IN" in trigger_sql
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_recovery_backfill_pending'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 1
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET recovery_backfill_pending=0 WHERE id=?",
                (source_id,),
            )

    kb.init_db()
    with kb.connect_closing() as conn:
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?",
            (source_id,),
        ).fetchone()["recovery_backfill_pending"] == 0


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


def test_config_disabled_keeps_raw_block_machine_owned(
    isolated_home: Path,
) -> None:
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="legacy", kind=None)
        assert _reconciliation_tasks(conn) == []
        assert kb.attention_class(conn, source_id, reconciler_enabled=False) == "automation_recovery"


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
                        "attention_owner": "Kevin Yan",
                        "why_automation_cannot_perform": "Only Kevin can choose.",
                        "current_evidence": "A current choice is still required.",
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
                    "attention_owner": "Kevin Yan",
                    "why_automation_cannot_perform": "Only Kevin can act.",
                    "current_evidence": "This evidence is stale.",
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
                assert source.status == "automation_recovery"

        gates = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
            and (event.payload or {}).get("outcome") == "genuine_human_gate"
        ]
        assert gates == []
        assert all(
            (event.payload or {}).get("outcome") != "genuine_human_gate"
            for event in kb.list_events(conn, source_id)
        )


def test_block_loop_stays_machine_owned_recovery(
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
        assert source.status == "automation_recovery"

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
        assert source.status == "automation_recovery"
        gates = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
            and (event.payload or {}).get("outcome") == "genuine_human_gate"
        ]
        assert gates == []


def test_failed_reconciliation_keeps_block_loop_machine_owned(
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
        assert source.status == "automation_recovery"
        outcomes = [
            event.payload or {}
            for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert outcomes[-1]["outcome"] == "reconciliation_failed"


def test_manual_triage_without_affirmed_gate_is_not_human_input(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = kb.create_task(conn, title="manual triage", assignee="code-crab")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (source_id,))
        assert kb.attention_class(conn, source_id, reconciler_enabled=True) is None


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
                "automation_recovery"
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
            "reconciliation_failed",
        ]
        assert outcomes[-1].get("failure_count") == kb.RECONCILIATION_SOURCE_FAILURE_LIMIT
        assert outcomes[-1].get("fallback") == "automation_exhausted"


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
        if outcome == "continuation_created":
            kb.add_comment(
                conn, source_id, author="default",
                body="Verified linked continuation from current source truth.",
                origin_task_id=recovery.id, origin_run_id=claimed.current_run_id,
            )

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
            reconciliation.update({
                "human_action": "Approve the bounded action.",
                "attention_owner": "Kevin Yan",
                "why_automation_cannot_perform": "Only Kevin can approve it.",
                "current_evidence": "The approval requirement is current.",
            })
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


def test_prior_run_reconciler_evidence_comment_remains_non_material_on_retry(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="release closeout", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])

        continuation_id = kb.create_task(conn, title="bounded closeout", assignee="code-crab")
        kb.link_tasks(conn, continuation_id, source_id)
        continuation = kb.claim_task(conn, continuation_id, claimer="code-crab")
        assert continuation is not None
        assert kb.complete_task(
            conn, continuation_id, summary="closeout passed",
            expected_run_id=continuation.current_run_id,
        )

        prior = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert prior is not None and prior.current_run_id is not None
        rollout_id = kb.create_task(conn, title="guard rollout", assignee="code-crab")
        kb.link_tasks(conn, rollout_id, recovery.id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body=(
                f"Reconciliation evidence for source event {source_event_id}: "
                f"continuation {continuation_id} completed."
            ),
            origin_task_id=recovery.id,
            origin_run_id=prior.current_run_id,
        )
        evidence_event = [
            event for event in kb.list_events(conn, source_id) if event.kind == "commented"
        ][-1]
        assert evidence_event.run_id == prior.current_run_id
        assert kb.block_task(
            conn,
            recovery.id,
            reason="waiting for guard rollout",
            kind="dependency",
            expected_run_id=prior.current_run_id,
        )
        assert kb.complete_task(conn, rollout_id, summary="guard activated")
        kb.recompute_ready(conn)

        current = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert current is not None and current.current_run_id is not None
        assert current.current_run_id != prior.current_run_id
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="prior-run evidence remains valid",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/completed",
                "continuation_task_id": continuation_id,
            }},
            expected_run_id=current.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "done"


def test_prior_run_comment_from_previous_recovery_profile_remains_material(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable(monkeypatch, profile="default")
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        prior = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert prior is not None and prior.current_run_id is not None

        rollout_id = kb.create_task(conn, title="guard rollout", assignee="code-crab")
        kb.link_tasks(conn, rollout_id, recovery.id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body=f"Reconciliation evidence for source event {source_event_id}: prior profile.",
            origin_task_id=recovery.id,
            origin_run_id=prior.current_run_id,
        )
        assert kb.block_task(
            conn,
            recovery.id,
            reason="waiting for rollout",
            kind="dependency",
            expected_run_id=prior.current_run_id,
        )
        assert kb.assign_task(conn, recovery.id, "operator")
        assert kb.complete_task(conn, rollout_id, summary="guard activated")
        kb.recompute_ready(conn)
        current = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert current is not None and current.current_run_id is not None

        with pytest.raises(ValueError, match="advanced after source event"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="must reject previous profile evidence",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=current.current_run_id,
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


def _settle_source_via_backfill(conn, source_id: str, recovery_id: str) -> int:
    """Run the real dispatcher backfill over a pending recovery source.

    Returns the id of the benign ``reconciliation_backfill_repaired``
    bookkeeping event the pass records on the source when the exact
    occurrence already has its wrapper (the live t_b98c600a shape).
    """
    recovered = kb.reconcile_orphaned_automation_recovery(conn)
    assert recovered == [recovery_id]
    repairs = [
        event
        for event in kb.list_events(conn, source_id)
        if event.kind == "reconciliation_backfill_repaired"
    ]
    assert len(repairs) == 1
    payload = repairs[0].payload or {}
    assert payload.get("reason") == "exact_occurrence_present"
    return int(repairs[0].id)


def test_backfill_repair_after_enqueue_accepts_human_gate_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A benign exact_occurrence_present repair must not invalidate the verdict."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="operator decision", kind="needs_input")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        repair_event_id = _settle_source_via_backfill(conn, source_id, recovery.id)
        assert repair_event_id > source_event_id

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Mandatory recovery evidence recorded from current source truth.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="human gate affirmed",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "genuine_human_gate",
                "human_action": "Choose the region strategy.",
                "attention_owner": "Kevin Yan",
                "why_automation_cannot_perform": "Only Kevin can make the compliance call.",
                "current_evidence": "The provider constraint is still present on current main.",
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "blocked"
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        outcome_payload = outcomes[0].payload or {}
        assert outcome_payload.get("outcome") == "genuine_human_gate"
        assert outcome_payload.get("stale") is not True


def test_backfill_repair_after_enqueue_accepts_reconciliation_failed_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciliation_failed outcome path must survive the same repair."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        repair_event_id = _settle_source_via_backfill(conn, source_id, recovery.id)
        assert repair_event_id > source_event_id

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="automation could not clear it",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "reconciliation_failed",
                "error": "sanitized automation failure",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        outcome_payload = outcomes[0].payload or {}
        assert outcome_payload.get("outcome") == "reconciliation_failed"
        assert outcome_payload.get("error") == "sanitized automation failure"
        assert outcome_payload.get("stale") is not True


def test_backfill_repair_event_is_not_a_citable_occurrence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Citing the repair bookkeeping id itself stays rejected as stale."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        repair_event_id = _settle_source_via_backfill(conn, source_id, recovery.id)

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="wrong occurrence cited",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": repair_event_id,
                    "outcome": "reconciliation_failed",
                    "error": "must stay rejected",
                }},
                expected_run_id=claimed.current_run_id,
            )
        current = kb.get_task(conn, recovery.id)
        assert current is not None and current.status == "running"


def test_backfill_repair_does_not_mask_genuine_later_occurrence(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real later blocker after the repair still rejects the stale verdict."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="first failure", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        original_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        _settle_source_via_backfill(conn, source_id, recovery.id)

        with kb.write_txn(conn):
            newest_event_id = kb._append_event(
                conn, source_id, "gave_up", {"error": "new terminal failure"},
            )
        assert newest_event_id != original_event_id
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="obsolete result",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": original_event_id,
                    "outcome": "reconciliation_failed",
                    "error": "superseded by a genuine later occurrence",
                }},
            )
        current = kb.get_task(conn, recovery.id)
        assert current is not None and current.status == "running"

        # The newest coalesced occurrence is the valid replacement lineage.
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="new occurrence reconciled",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": newest_event_id,
                "outcome": "reconciliation_failed",
                "error": "new terminal failure remains current",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("source_event_id") == newest_event_id

def test_backfill_repair_after_gave_up_accepts_continuation_created_verdict(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live t_270fa73e sequence: gave_up -> enqueued -> benign repair ->
    linked/commented -> continuation_created passes with the original id."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        with kb.write_txn(conn):
            source_event_id = kb._append_event(
                conn,
                source_id,
                "gave_up",
                {"error": "iteration budget exhausted", "retry_status": "ready"},
            )
            # Reproduce the dispatcher circuit-breaker state from the live
            # occurrence. The event hook creates the exact wrapper; the pending
            # bit makes the subsequent backfill pass settle that occurrence.
            conn.execute(
                "UPDATE tasks SET status='automation_recovery', "
                "recovery_backfill_pending=1 WHERE id=?",
                (source_id,),
            )
        recovery = _reconciliation_tasks(conn)[0]
        assert int((recovery.idempotency_key or "").rsplit(":", 1)[1]) == source_event_id
        repair_event_id = _settle_source_via_backfill(conn, source_id, recovery.id)
        assert repair_event_id > source_event_id

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = kb.create_task(
            conn, title="business continuation", assignee="code-crab",
        )
        kb.link_tasks(conn, continuation_id, source_id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Continuation linked and running from current source truth.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="continuation created",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "continuation_created",
                "continuation_task_id": continuation_id,
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        outcome_payload = outcomes[0].payload or {}
        assert outcome_payload.get("outcome") == "continuation_created"
        assert outcome_payload.get("source_event_id") == source_event_id
        assert outcome_payload.get("continuation_task_id") == continuation_id
        assert outcome_payload.get("stale") is not True

def _applied_continuation_topology(conn) -> tuple[str, kb.Task, int, str, int]:
    """Drive one source through the full repaired-backfill continuation path.

    Returns (source_id, recovery, source_event_id, continuation_id, run_id)
    after the verdict was durably applied and the recovery completed.
    """
    source_id = _running(conn)
    assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
    recovery = _reconciliation_tasks(conn)[0]
    source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
    _settle_source_via_backfill(conn, source_id, recovery.id)
    claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
    assert claimed is not None
    continuation_id = kb.create_task(
        conn, title="business continuation", assignee="code-crab",
    )
    kb.link_tasks(conn, continuation_id, source_id)
    kb.add_comment(
        conn,
        source_id,
        author="default",
        body="Continuation linked and running from current source truth.",
        origin_task_id=recovery.id,
        origin_run_id=claimed.current_run_id,
    )
    metadata = {"reconciliation": {
        "source_task_id": source_id,
        "source_event_id": source_event_id,
        "outcome": "continuation_created",
        "continuation_task_id": continuation_id,
    }}
    assert kb.complete_task(
        conn,
        recovery.id,
        summary="continuation created",
        metadata=metadata,
        expected_run_id=claimed.current_run_id,
    )
    return source_id, recovery, source_event_id, continuation_id, int(claimed.current_run_id)

def test_applied_verdict_duplicate_completion_is_an_idempotent_noop(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replaying an already-applied verdict must no-op, not raise on the
    provenance-bearing reconciliation_outcome event the first pass recorded."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id, continuation_id, _run_id = (
            _applied_continuation_topology(conn)
        )
        replay_metadata = {"reconciliation": {
            "source_task_id": source_id,
            "source_event_id": source_event_id,
            "outcome": "continuation_created",
            "continuation_task_id": continuation_id,
        }}
        assert not kb.complete_task(
            conn, recovery.id, summary="duplicate replay", metadata=replay_metadata,
        )
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"
        current = kb.get_task(conn, recovery.id)
        assert current is not None and current.status == "done"

        # Required fields are validated before replay matching; a subset cannot
        # vacuously match an already-applied verdict.
        with pytest.raises(ValueError, match="continuation_task_id"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="malformed replay",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "continuation_created",
                }},
            )

        # Same lineage/outcome but a different continuation is not the same
        # durable verdict and must not be swallowed by the replay guard.
        different_continuation_id = kb.create_task(
            conn, title="different continuation", assignee="code-crab",
        )
        kb.link_tasks(conn, different_continuation_id, source_id)
        with pytest.raises(ValueError):
            kb.complete_task(
                conn,
                recovery.id,
                summary="not an identical replay",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "continuation_created",
                    "continuation_task_id": different_continuation_id,
                }},
            )
        assert len([
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]) == 1


def test_applied_verdict_replay_matches_canonical_redacted_text(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact retry is compared to the canonical persisted payload, not the
    raw credential-bearing text that was intentionally normalized on apply."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="retry me", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        raw_error = "provider rejected api_key=not-a-real-secret during retry"
        metadata = {"reconciliation": {
            "source_task_id": source_id,
            "source_event_id": source_event_id,
            "outcome": "reconciliation_failed",
            "error": raw_error,
        }}
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="failed safely",
            metadata=metadata,
            expected_run_id=claimed.current_run_id,
        )
        assert not kb.complete_task(
            conn,
            recovery.id,
            summary="exact raw replay",
            metadata=metadata,
        )
        outcomes = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("error") == (
            "provider rejected api_key=[REDACTED] during retry"
        )


def test_stale_discarded_verdict_replay_still_rejects(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verdict the kernel discarded as stale is not an applied verdict: its
    replay must re-validate and re-reject rather than no-op."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="first failure", kind="transient")
        recovery = _reconciliation_tasks(conn)[0]
        original_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        # A genuine later occurrence makes the first verdict stale. It is
        # rejected before apply, and repeating it remains rejected rather than
        # being mistaken for an idempotent replay.
        with kb.write_txn(conn):
            newest_event_id = kb._append_event(
                conn, source_id, "gave_up", {"error": "new terminal failure"},
            )
        assert newest_event_id != original_event_id
        stale_metadata = {"reconciliation": {
            "source_task_id": source_id,
            "source_event_id": original_event_id,
            "outcome": "reconciliation_failed",
            "error": "superseded by a genuine later occurrence",
        }}
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="obsolete result",
                metadata=stale_metadata,
                expected_run_id=claimed.current_run_id,
            )
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="obsolete replay",
                metadata=stale_metadata,
                expected_run_id=claimed.current_run_id,
            )
        assert not [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_outcome"
        ]


def _gave_up_source_with_settled_recovery(conn) -> tuple[str, kb.Task, int]:
    """Mirror the live t_270fa73e occurrence 27798 shape.

    gave_up occurrence -> automation_recovery + recovery wrapper -> benign
    exact_occurrence_present backfill repair. Returns (source_id, recovery,
    source_event_id).
    """
    source_id = _running(conn)
    with kb.write_txn(conn):
        source_event_id = kb._append_event(
            conn,
            source_id,
            "gave_up",
            {"error": "iteration budget exhausted", "retry_status": "ready"},
        )
        conn.execute(
            "UPDATE tasks SET status='automation_recovery', "
            "recovery_backfill_pending=1 WHERE id=?",
            (source_id,),
        )
    recovery = [
        task for task in _reconciliation_tasks(conn)
        if source_id in (task.idempotency_key or "")
    ][0]
    repair_event_id = _settle_source_via_backfill(conn, source_id, recovery.id)
    assert repair_event_id > source_event_id
    return source_id, recovery, source_event_id


def _occurrence_keyed_continuation(conn, source_id: str, source_event_id: int) -> str:
    """Create the reconciliation-owned continuation shape live workers produce."""
    return kb.create_task(
        conn,
        title="business continuation",
        assignee="code-crab",
        created_by="default",
        idempotency_key=(
            f"reconcile-{source_id}-event-{source_event_id}-continuation"
        ),
    )


def _last_linked_event_id(conn, source_id: str) -> int:
    linked = [
        event for event in kb.list_events(conn, source_id) if event.kind == "linked"
    ]
    assert linked
    return int(linked[-1].id)


def _reconciliation_outcomes(conn, source_id: str) -> list:
    return [
        event for event in kb.list_events(conn, source_id)
        if event.kind == "reconciliation_outcome"
    ]


def _dependency_block_recovery(conn, recovery_id: str) -> str:
    """End the recovery run as a dependency wait (the live run-1146 shape).

    A dependency block requires at least one parent, so park the recovery
    behind a throwaway holder; the holder edge lands on the recovery, never
    on the source, so it cannot exercise the mirrored-parent exemption.
    """
    holder_id = kb.create_task(conn, title="repair hold", assignee="code-crab")
    kb.link_tasks(conn, holder_id, recovery_id)
    assert kb.block_task(
        conn, recovery_id, reason="waiting on validator repair", kind="dependency"
    )
    return holder_id


def _release_recovery_for_retry(conn, recovery_id: str, holder_id: str) -> None:
    """Finish the dependency hold so the recovery can be legitimately re-claimed."""
    assert kb.complete_task(conn, holder_id, summary="repair shipped")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (recovery_id,))


def test_cleared_resumed_accepts_same_run_required_parent_link(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The protocol-required direct-parent link must not self-invalidate a
    cleared/resumed verdict for the same reconciliation occurrence.

    Live t_3226491c failure: source_event_id=27798 rejected as advanced via
    linked:27822 while 27822 was rejected as stale (newest coalesced 27798).
    """
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Continuation linked and running from current source truth.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )

        # Substituting the required link's event id stays stale: the newest
        # coalesced occurrence is still the original gave_up event.
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="wrong occurrence id",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": link_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )

        assert kb.complete_task(
            conn,
            recovery.id,
            summary="source resumed from current truth",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        # The linked continuation is not done yet, so the resumed source waits
        # behind its required parent (todo), exactly like continuation_created.
        assert source is not None and source.status == "todo"
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        payload = outcomes[0].payload or {}
        assert payload.get("outcome") == "cleared/resumed"
        assert payload.get("source_event_id") == source_event_id
        assert payload.get("stale") is not True


def test_cleared_resumed_accepts_prior_run_required_parent_link(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact live t_3226491c shape: the required link was recorded by an
    earlier (now blocked) run of the same recovery task; the retry run's
    cleared/resumed verdict for the original occurrence must pass."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Continuation linked and running from current source truth.",
            origin_task_id=recovery.id,
            origin_run_id=first.current_run_id,
        )
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None
        assert second.current_run_id != first.current_run_id
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="source resumed from current truth",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=second.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == "cleared/resumed"


def test_reconciliation_failed_accepts_required_parent_link(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reconciliation_failed carries no topology field either; the owned
    required link must not invalidate it (live run attempted this shape)."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="bounded automation failed",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "reconciliation_failed",
                "error": "sanitized failure",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == "reconciliation_failed"


def test_unowned_link_still_invalidates_cleared_resumed(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated link recorded outside the recovery task's runs and naming
    no occurrence-scoped continuation is still a material source advance."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        # Linked BEFORE the recovery task ever ran: no run window covers it and
        # the parent carries no occurrence provenance.
        unrelated_id = kb.create_task(conn, title="unrelated parent", assignee="code-crab")
        kb.link_tasks(conn, unrelated_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        with pytest.raises(
            ValueError, match=rf"source advanced after source event {source_event_id} via linked:{link_event_id}"
        ):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_keyed_link_outside_recovery_run_windows_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an occurrence-keyed continuation link is material when it was not
    recorded inside a run window of THIS recovery task (concurrent actor)."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        # Link lands between runs: no recovery run window covers it.  Durable
        # timestamps are second-precision, so make the gap explicit in the
        # disposable fixture instead of racing the wall clock.
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)
        with kb.write_txn(conn):
            run_a = conn.execute(
                "SELECT ended_at FROM task_runs WHERE id=?",
                (first_run_id,),
            ).fetchone()
            assert run_a is not None and run_a["ended_at"] is not None
            gap = int(run_a["ended_at"])
            conn.execute(
                "UPDATE task_events SET created_at=? WHERE id=?",
                (gap + 5, link_event_id),
            )

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at=? WHERE id=?",
                (gap + 10, int(second.current_run_id)),
            )
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_stamped_link_with_recovery_run_provenance_accepted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New writes carry exact run provenance: a link stamped with the recovery
    task's own active run is reconciliation-owned even without a keyed parent."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = kb.create_task(
            conn, title="unkeyed continuation", assignee="code-crab",
        )
        kb.link_tasks(
            conn,
            continuation_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Continuation linked and running from current source truth.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="source resumed from current truth",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == "cleared/resumed"


def test_stamped_link_with_other_source_occurrence_key_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Occurrence-shaped keys bind no matter WHICH source they name: a
    stamped link whose parent key carries an event-N marker for a different
    occurrence (here: another source's reconciliation continuation) holds
    wrong-occurrence provenance for the cited occurrence and stays
    material.  Run provenance alone cannot launder it."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        other_source_id = _running(conn, title="unrelated other source")
        foreign_keyed_id = kb.create_task(
            conn,
            title="other source continuation",
            assignee="code-crab",
            idempotency_key=(
                f"reconcile-{other_source_id}-event-{source_event_id + 1000}-continuation"
            ),
        )
        kb.link_tasks(
            conn,
            foreign_keyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


@pytest.mark.parametrize(
    ("outcome", "extra"),
    (
        ("backoff_scheduled", {"resume_at": 1999999999}),
        ("genuine_human_gate", {
            "human_action": "operator must rotate the key",
            "attention_owner": "Kevin Yan",
            "why_automation_cannot_perform": "credential rotation needs the human's 1Password unlock",
            "current_evidence": "provider rejects the stored key with 401",
        }),
    ),
)
def test_no_topology_outcomes_accept_same_run_required_parent_link(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    extra: dict,
) -> None:
    """backoff_scheduled and genuine_human_gate also name no
    continuation/dependency in their verdict metadata; the protocol-required
    direct-parent link must not self-invalidate them either."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        kb.add_comment(
            conn,
            source_id,
            author="default",
            body="Reconciliation evidence: source re-read from current truth.",
            origin_task_id=recovery.id,
            origin_run_id=claimed.current_run_id,
        )
        verdict = {
            "source_task_id": source_id,
            "source_event_id": source_event_id,
            "outcome": outcome,
            **extra,
        }
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="verdict with required link",
            metadata={"reconciliation": verdict},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == outcome


def test_cleared_resumed_accepts_prior_run_stamped_required_parent_link(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stamped prior-run shape: the required link was recorded by an earlier
    (now dependency-blocked) run of the same recovery task WITH exact run
    provenance; the retry run's cleared/resumed verdict for the original
    occurrence must pass on the stamped branch, not the legacy fallback."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(
            conn,
            continuation_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first.current_run_id,
        )
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None
        assert second.current_run_id != first.current_run_id
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="source resumed from current truth",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=second.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == "cleared/resumed"


def test_stamped_link_with_foreign_provenance_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link stamped with another task's run is not this reconciliation's own
    topology mutation and must stay material (wrong reconciliation task)."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        foreign_id = _running(conn, title="foreign worker task")
        foreign = kb.get_task(conn, foreign_id)
        assert foreign is not None and foreign.current_run_id is not None

        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(
            conn,
            continuation_id,
            source_id,
            origin_task_id=foreign_id,
            origin_run_id=int(foreign.current_run_id),
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_stamped_link_outside_stamped_run_window_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run provenance must cover the event: a link stamped with an ended
    recovery run but recorded after that run closed stays material."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(
            conn,
            continuation_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first.current_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        with kb.write_txn(conn):
            # Forge the durable timestamp past the stamped run's end: the
            # provenance tuple no longer covers the event.
            conn.execute(
                "UPDATE task_events SET created_at = created_at + 100000 WHERE id=?",
                (link_event_id,),
            )
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_genuine_later_occurrence_still_rejects_with_owned_link(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine later blocker occurrence still invalidates the stale verdict.
    The pre-occurrence owned link then satisfies the retried verdict for the
    newest coalesced occurrence, because only post-occurrence mutations are
    material for it."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)

        with kb.write_txn(conn):
            newer_event_id = kb._append_event(
                conn,
                source_id,
                "gave_up",
                {"error": "iteration budget exhausted again", "retry_status": "ready"},
            )
        coalesced = [
            event for event in kb.list_events(conn, source_id)
            if event.kind == "reconciliation_coalesced"
        ]
        assert coalesced and coalesced[-1].payload.get("source_event_id") == newer_event_id

        # The original occurrence is now stale.
        with pytest.raises(ValueError, match="stale"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="old occurrence verdict",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        # The retried verdict for the newest coalesced occurrence passes: the
        # owned link predates the cited occurrence and nothing material
        # advanced the source after it.
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="resumed from newest occurrence",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": newer_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        payload = outcomes[0].payload or {}
        assert payload.get("outcome") == "cleared/resumed"
        assert payload.get("source_event_id") == newer_event_id


def test_post_occurrence_link_with_wrong_event_provenance_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link recorded AFTER the cited occurrence whose parent names a
    different (older) occurrence is not this occurrence's required topology:
    wrong source-event provenance stays a material advance."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        # Continuation keyed to a DIFFERENT occurrence than the cited one.
        stale_keyed_id = kb.create_task(
            conn,
            title="continuation for an older occurrence",
            assignee="code-crab",
            created_by="default",
            idempotency_key=f"reconcile-{source_id}-event-{source_event_id - 100}-continuation",
        )
        kb.link_tasks(conn, stale_keyed_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="verdict citing the assigned occurrence",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_settled_cleared_resumed_duplicate_is_zero_write(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the owned-link verdict settles, an exact replay is an idempotent
    no-op: no new outcome event, no source status change."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        verdict = {"reconciliation": {
            "source_task_id": source_id,
            "source_event_id": source_event_id,
            "outcome": "cleared/resumed",
        }}
        assert kb.complete_task(
            conn, recovery.id, summary="source resumed", metadata=verdict,
            expected_run_id=claimed.current_run_id,
        )
        assert not kb.complete_task(conn, recovery.id, summary="duplicate replay", metadata=verdict)
        assert len(_reconciliation_outcomes(conn, source_id)) == 1
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "todo"
        current = kb.get_task(conn, recovery.id)
        assert current is not None and current.status == "done"


def test_simultaneous_wrappers_keep_owned_links_isolated(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two live reconciliations completing in interleaved order: each wrapper's
    owned link satisfies only its own source verdict; neither cross-exempts."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_a, recovery_a, event_a = _gave_up_source_with_settled_recovery(conn)
        source_b, recovery_b, event_b = _gave_up_source_with_settled_recovery(conn)

        claimed_a = kb.claim_task(conn, recovery_a.id, claimer="reconciler-a")
        claimed_b = kb.claim_task(conn, recovery_b.id, claimer="reconciler-b")
        assert claimed_a is not None and claimed_b is not None

        continuation_a = _occurrence_keyed_continuation(conn, source_a, event_a)
        kb.link_tasks(conn, continuation_a, source_a)
        continuation_b = _occurrence_keyed_continuation(conn, source_b, event_b)
        kb.link_tasks(conn, continuation_b, source_b)

        assert kb.complete_task(
            conn, recovery_a.id, summary="A resumed",
            metadata={"reconciliation": {
                "source_task_id": source_a,
                "source_event_id": event_a,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed_a.current_run_id,
        )
        assert kb.complete_task(
            conn, recovery_b.id, summary="B resumed",
            metadata={"reconciliation": {
                "source_task_id": source_b,
                "source_event_id": event_b,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed_b.current_run_id,
        )
        assert len(_reconciliation_outcomes(conn, source_a)) == 1
        assert len(_reconciliation_outcomes(conn, source_b)) == 1


def test_partial_link_provenance_never_uses_legacy_fallback(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A linked event naming origin_task_id without a matching origin_run_id
    is current-format with partial provenance: material, never legacy."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (continuation_id, source_id),
            )
            partial_event_id = kb._append_event(
                conn,
                source_id,
                "linked",
                {
                    "parent": continuation_id,
                    "child": source_id,
                    "origin_task_id": recovery.id,
                },
            )
        with pytest.raises(ValueError, match=rf"via linked:{partial_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_mismatched_link_run_provenance_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payload origin run and the durable row run_id must agree exactly;
    a forged mismatch stays material even for the recovery's own run."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = int(claimed.current_run_id)
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (continuation_id, source_id),
            )
            forged_event_id = kb._append_event(
                conn,
                source_id,
                "linked",
                {
                    "parent": continuation_id,
                    "child": source_id,
                    "origin_task_id": recovery.id,
                    "origin_run_id": run_id + 1000,
                },
                run_id=run_id,
            )
        with pytest.raises(ValueError, match=rf"via linked:{forged_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_occurrence_key_prefix_collision_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Digit-boundary citation: a parent keyed to event-<occ><digit> must not
    vouch for occurrence <occ>, and a task-id prefix run-on must not either."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        digit_collision_id = kb.create_task(
            conn,
            title="continuation keyed to a run-on occurrence",
            assignee="code-crab",
            created_by="default",
            idempotency_key=(
                f"reconcile-{source_id}-event-{source_event_id}9-continuation"
            ),
        )
        kb.link_tasks(conn, digit_collision_id, source_id)
        first_link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{first_link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_concurrent_unprovenanced_keyed_link_inside_window_is_accepted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documented trust boundary: an UNPROVENANCED link whose parent cites the
    exact occurrence, with creation AND link inside recovery run windows, is
    indistinguishable from a pre-provenance durable row and recognized as
    reconciliation-owned.  Local non-tool writers (CLI/dashboard/direct DB)
    are trusted cooperative actors in this model — they could rewrite history
    directly regardless — while the worker tool path now always stamps exact
    provenance, and every un-keyed or out-of-window link stays material."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="source resumed from current truth",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": source_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=claimed.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        assert (outcomes[0].payload or {}).get("outcome") == "cleared/resumed"


def test_stamped_link_with_wrong_occurrence_key_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run provenance cannot vouch for occurrence-shaped keys: a stamped link
    whose parent cites this source with a DIFFERENT event marker carries the
    wrong source-event provenance and stays material (stale coalesced worker
    linking an old-occurrence continuation after a newer occurrence)."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        stale_keyed_id = kb.create_task(
            conn,
            title="continuation for an older occurrence",
            assignee="code-crab",
            created_by="default",
            idempotency_key=f"reconcile-{source_id}-event-{source_event_id + 100}-continuation",
        )
        kb.link_tasks(
            conn,
            stale_keyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=int(claimed.current_run_id),
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_occurrence_key_task_id_run_on_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary citation for the task id too: a key containing the source id
    only as an alphanumeric run-on (<source_task_id>x) is not a citation."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None
        run_on_id = kb.create_task(
            conn,
            title="continuation with a run-on source id",
            assignee="code-crab",
            created_by="default",
            idempotency_key=(
                f"reconcile-{source_id}x-event-{source_event_id}-continuation"
            ),
        )
        kb.link_tasks(conn, run_on_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def _coalesce_newer_occurrence(conn, source_id: str) -> int:
    with kb.write_txn(conn):
        newer_event_id = kb._append_event(
            conn,
            source_id,
            "gave_up",
            {"error": "iteration budget exhausted again", "retry_status": "ready"},
        )
    coalesced = [
        event for event in kb.list_events(conn, source_id)
        if event.kind == "reconciliation_coalesced"
    ]
    assert coalesced and coalesced[-1].payload.get("source_event_id") == newer_event_id
    return int(newer_event_id)


def test_pre_occurrence_run_unkeyed_stamped_link_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent-review scenario: R1 starts for E1; a genuine later E2
    coalesces mid-run; stale R1 stamps an UNKEYED parent link after E2; R1
    blocks; R2's verdict for E2 must still reject — a run that predates the
    cited occurrence cannot vouch for topology without occurrence-key
    evidence."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        # No timestamp manipulation: R1 and the coalesced E2 share the same
        # wall-clock second, the exact frozen-clock case. Rejection must come
        # from durable event ordering, not clock granularity.
        newer_event_id = _coalesce_newer_occurrence(conn, source_id)
        unkeyed_id = kb.create_task(
            conn, title="unkeyed continuation", assignee="code-crab",
        )
        kb.link_tasks(
            conn,
            unkeyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="retry verdict for the newest occurrence",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": newer_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_pre_occurrence_run_keyed_stamped_link_is_accepted(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same coalesced sequence, but the pre-occurrence run's stamped link names
    a parent keyed to the CITED (newest) occurrence: occurrence-key evidence
    backs the pre-occurrence run and the retried verdict passes."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        newer_event_id = _coalesce_newer_occurrence(conn, source_id)
        keyed_id = kb.create_task(
            conn,
            title="continuation for the newest occurrence",
            assignee="code-crab",
            created_by="default",
            idempotency_key=f"reconcile-{source_id}-event-{newer_event_id}-continuation",
        )
        kb.link_tasks(
            conn,
            keyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first_run_id,
        )
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        assert kb.complete_task(
            conn,
            recovery.id,
            summary="retry verdict for the newest occurrence",
            metadata={"reconciliation": {
                "source_task_id": source_id,
                "source_event_id": newer_event_id,
                "outcome": "cleared/resumed",
            }},
            expected_run_id=second.current_run_id,
        )
        outcomes = _reconciliation_outcomes(conn, source_id)
        assert len(outcomes) == 1
        payload = outcomes[0].payload or {}
        assert payload.get("outcome") == "cleared/resumed"
        assert payload.get("source_event_id") == newer_event_id


def test_foreign_profile_prior_run_stamped_link_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reassignment must not launder provenance: a link stamped by a run of
    this recovery task while it was assigned to a FOREIGN profile is not the
    authorized reconciler's topology mutation and stays material after the
    task is reassigned to the configured reconciler profile."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee='rogue-profile' WHERE id=?", (recovery.id,)
            )
        first = kb.claim_task(conn, recovery.id, claimer="rogue-profile")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        unkeyed_id = kb.create_task(
            conn, title="unkeyed continuation", assignee="code-crab",
        )
        kb.link_tasks(
            conn,
            unkeyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee='default' WHERE id=?", (recovery.id,)
            )

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with kb.write_txn(conn):
            # Second-precision durable clock: pin the retry run's start past
            # the link so ONLY the foreign-profile run window covers it.
            conn.execute(
                "UPDATE task_runs SET started_at = started_at + 10 WHERE id=?",
                (int(second.current_run_id),),
            )
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="retry verdict after reassignment",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_foreign_profile_prior_run_legacy_keyed_link_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same reassignment binding for the legacy branch: an occurrence-keyed
    link from a foreign-profile run window must not vouch after reassignment."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee='rogue-profile' WHERE id=?", (recovery.id,)
            )
        first = kb.claim_task(conn, recovery.id, claimer="rogue-profile")
        assert first is not None
        continuation_id = _occurrence_keyed_continuation(conn, source_id, source_event_id)
        kb.link_tasks(conn, continuation_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee='default' WHERE id=?", (recovery.id,)
            )

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with kb.write_txn(conn):
            # Second-precision durable clock: pin the retry run's start past
            # the link so ONLY the foreign-profile run window covers it.
            conn.execute(
                "UPDATE task_runs SET started_at = started_at + 10 WHERE id=?",
                (int(second.current_run_id),),
            )
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="retry verdict after reassignment",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_multi_marker_occurrence_key_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key citing the source with multiple occurrence markers must name the
    cited occurrence UNANIMOUSLY: `event-<cited>-event-<other>` is ambiguous
    wrong-occurrence provenance and stays material in both branches."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        ambiguous_id = kb.create_task(
            conn,
            title="continuation with a two-marker key",
            assignee="code-crab",
            created_by="default",
            idempotency_key=(
                f"reconcile-{source_id}-event-{source_event_id}"
                f"-event-{source_event_id + 7}-continuation"
            ),
        )
        # Legacy branch: unprovenanced link inside the run window.
        kb.link_tasks(conn, ambiguous_id, source_id)
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_multi_marker_occurrence_key_stamped_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same ambiguity through the stamped branch: first marker matches, second
    does not — run provenance cannot rescue a split-verdict key."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        claimed = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert claimed is not None and claimed.current_run_id is not None
        ambiguous_id = kb.create_task(
            conn,
            title="continuation with a two-marker key",
            assignee="code-crab",
            created_by="default",
            idempotency_key=(
                f"reconcile-{source_id}-event-{source_event_id}"
                f"-event-{source_event_id + 7}-continuation"
            ),
        )
        kb.link_tasks(
            conn,
            ambiguous_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=int(claimed.current_run_id),
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="source resumed from current truth",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=claimed.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_stamped_link_from_settled_run_without_ending_event_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a settled stamped run with NO durable ending event
    cannot vouch for a link — the recorded run window alone is weaker
    evidence than durable event ordering, so a run row claiming to be
    settled without its ending event (deleted, lost, or forged) must not
    have its provenance accepted on the timestamp fence alone."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        unkeyed_id = kb.create_task(
            conn, title="unkeyed continuation", assignee="code-crab",
        )
        kb.link_tasks(
            conn,
            unkeyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        # Forge the anomaly: the run row says settled, but its durable
        # ending event is gone — only the recorded window remains.
        with kb.write_txn(conn):
            conn.execute(
                "DELETE FROM task_events WHERE task_id = ? AND run_id = ? "
                "AND kind = 'dependency_wait'",
                (recovery.id, first_run_id),
            )
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="retry verdict",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []


def test_stamped_link_recorded_after_run_end_still_invalidates(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen-clock run-end fence: a link stamped with run R1 but RECORDED
    after R1's durable run-ending event (same wall-clock second) is a
    post-mortem write, not R1's topology mutation, and stays material."""
    _enable(monkeypatch)
    with kb.connect_closing() as conn:
        source_id, recovery, source_event_id = _gave_up_source_with_settled_recovery(conn)
        first = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert first is not None and first.current_run_id is not None
        first_run_id = int(first.current_run_id)
        holder_id = _dependency_block_recovery(conn, recovery.id)
        # Post-mortem stamped write: recorded after the run-ending event but
        # inside the same wall-clock second, so only durable event ordering
        # can reject it.
        unkeyed_id = kb.create_task(
            conn, title="unkeyed continuation", assignee="code-crab",
        )
        kb.link_tasks(
            conn,
            unkeyed_id,
            source_id,
            origin_task_id=recovery.id,
            origin_run_id=first_run_id,
        )
        link_event_id = _last_linked_event_id(conn, source_id)
        _release_recovery_for_retry(conn, recovery.id, holder_id)

        second = kb.claim_task(conn, recovery.id, claimer="reconciler")
        assert second is not None and second.current_run_id is not None
        with pytest.raises(ValueError, match=rf"via linked:{link_event_id}"):
            kb.complete_task(
                conn,
                recovery.id,
                summary="retry verdict",
                metadata={"reconciliation": {
                    "source_task_id": source_id,
                    "source_event_id": source_event_id,
                    "outcome": "cleared/resumed",
                }},
                expected_run_id=second.current_run_id,
            )
        assert _reconciliation_outcomes(conn, source_id) == []
