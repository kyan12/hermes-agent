from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "_blocker_reconciler_config", lambda: {
        "enabled": True, "profile": "default", "max_active": 2,
    })
    kb.init_db()
    return home


def _running(conn) -> str:
    tid = kb.create_task(conn, title="source", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def test_genuine_gate_rejects_non_kevin_owner(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="approval", kind="needs_input")
        recovery = next(
            task for task in kb.list_tasks(conn)
            if (task.idempotency_key or "").startswith(kb.RECONCILIATION_IDEMPOTENCY_PREFIX)
        )
        claimed = kb.claim_task(conn, recovery.id, claimer="default")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        kb.add_comment(
            conn, source_id, author="default", body="Current gate evidence.",
            origin_task_id=recovery.id, origin_run_id=claimed.current_run_id,
        )

        with pytest.raises(ValueError, match="attention_owner must be exactly Kevin Yan"):
            kb.complete_task(conn, recovery.id, summary="gate", metadata={"reconciliation": {
                "outcome": "genuine_human_gate", "source_task_id": source_id,
                "source_event_id": source_event_id, "attention_owner": "someone else",
                "human_action": "Approve once.",
                "why_automation_cannot_perform": "Owner authorization is required.",
                "current_evidence": "Policy currently requires owner approval.",
            }}, expected_run_id=claimed.current_run_id)


def test_genuine_gate_retains_complete_kevin_metadata(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        source_id = _running(conn)
        assert kb.block_task(conn, source_id, reason="approval", kind="needs_input")
        recovery = next(
            task for task in kb.list_tasks(conn)
            if (task.idempotency_key or "").startswith(kb.RECONCILIATION_IDEMPOTENCY_PREFIX)
        )
        claimed = kb.claim_task(conn, recovery.id, claimer="default")
        assert claimed is not None
        source_event_id = int((recovery.idempotency_key or "").rsplit(":", 1)[1])
        kb.add_comment(
            conn, source_id, author="default", body="Current gate evidence.",
            origin_task_id=recovery.id, origin_run_id=claimed.current_run_id,
        )
        gate = {
            "outcome": "genuine_human_gate", "source_task_id": source_id,
            "source_event_id": source_event_id, "attention_owner": "Kevin Yan",
            "human_action": "Approve once.",
            "why_automation_cannot_perform": "Owner authorization is required.",
            "current_evidence": "Policy currently requires owner approval.",
        }

        assert kb.complete_task(
            conn, recovery.id, summary="gate", metadata={"reconciliation": gate},
            expected_run_id=claimed.current_run_id,
        )
        source = kb.get_task(conn, source_id)
        assert source is not None and source.status == "blocked"
        event = [e for e in kb.list_events(conn, source_id) if e.kind == "reconciliation_outcome"][-1]
        assert {key: event.payload[key] for key in (
            "attention_owner", "human_action", "why_automation_cannot_perform", "current_evidence",
        )} == {key: gate[key] for key in (
            "attention_owner", "human_action", "why_automation_cannot_perform", "current_evidence",
        )}
        assert kb.attention_class(conn, source_id, reconciler_enabled=True) == "human_input"


def test_reconciliation_prompt_requires_complete_kevin_gate_schema() -> None:
    prompt = kb._reconciliation_prompt({
        "lineage": {"source_task_id": "t_source", "source_event_id": 7},
    })
    assert '"attention_owner":"Kevin Yan"' in prompt
    assert '"human_action":"one atomic action"' in prompt
    assert '"why_automation_cannot_perform":"' in prompt
    assert '"current_evidence":"' in prompt


def test_operator_affirmation_requires_recovery_and_records_provenance(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running(conn)
        with pytest.raises(ValueError, match="automation_recovery"):
            kb.affirm_human_gate(
                conn, tid, attention_owner="Kevin Yan", human_action="Approve once.",
                why_automation_cannot_perform="Authorization is personal.",
                current_evidence="The current policy requires Kevin's approval.",
                affirmed_by="operator:test",
            )
        assert kb.block_task(conn, tid, reason="approval", kind="needs_input")
        assert kb.affirm_human_gate(
            conn, tid, attention_owner="Kevin Yan", human_action="Approve once.",
            why_automation_cannot_perform="Authorization is personal.",
            current_evidence="The current policy requires Kevin's approval.",
            affirmed_by="operator:test",
        )
        assert kb.get_task(conn, tid).status == "blocked"
        event = [e for e in kb.list_events(conn, tid) if e.kind == "human_gate_affirmed"][-1]
        assert event.payload["affirmed_by"] == "operator:test"
        assert kb.attention_class(conn, tid, reconciler_enabled=True) == "human_input"


def test_operator_affirmation_rejects_non_kevin_and_blank_fields(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason="approval", kind="needs_input")
        for owner, action in (("Other Person", "Approve once."), ("Kevin Yan", "  ")):
            with pytest.raises(ValueError):
                kb.affirm_human_gate(
                    conn, tid, attention_owner=owner, human_action=action,
                    why_automation_cannot_perform="Authorization is personal.",
                    current_evidence="Current evidence.", affirmed_by="operator:test",
                )
        assert kb.get_task(conn, tid).status == "automation_recovery"


def test_dependency_block_without_parent_is_rejected(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running(conn)
        with pytest.raises(ValueError, match="parent"):
            kb.block_task(conn, tid, reason="waiting", kind="dependency")
        assert kb.get_task(conn, tid).status == "running"


def test_historical_unaffirmed_block_is_automation_not_human(isolated_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked', block_kind=NULL WHERE id=?", (tid,))
        assert kb.attention_class(conn, tid, reconciler_enabled=True) == "automation_recovery"
