"""Legacy loop repair must park work without manufacturing an execution."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def legacy_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="human decision", assignee="worker")
        for _ in range(kb.BLOCK_RECURRENCE_LIMIT):
            assert kb.claim_task(conn, tid, claimer="worker")
            assert kb.block_task(conn, tid, reason="missing approval", kind="needs_input")
            if kb.get_task(conn, tid).block_recurrences < kb.BLOCK_RECURRENCE_LIMIT:
                assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        yield conn, tid


def test_repair_preserves_evidence_and_is_atomic_idempotent(legacy_card, monkeypatch):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    runs = kb.list_runs(conn, tid)
    events = kb.list_events(conn, tid)
    def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(kb, "_append_event", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            repair_block_loop_task(conn, tid, actor="operator", reason="restore human handoff")
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before
    assert kb.list_events(conn, tid) == events
    assert repair_block_loop_task(conn, tid, actor="operator", reason="restore human handoff")
    after = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert after == {**before, "status": "blocked"}
    assert kb.list_runs(conn, tid) == runs
    repaired = kb.list_events(conn, tid)
    assert repaired[:len(events)] == events
    audit = repaired[-1]
    assert audit.kind == "blocked"  # Old readers only know blocked/unblocked.
    assert audit.payload["repair"] == "block_loop_triage"
    assert audit.payload["actor"] == "operator"
    assert audit.payload["repair_reason"] == "restore human handoff"
    assert audit.payload["escalation_event_id"] == events[-1].id
    assert audit.payload["reason"] == "missing approval"
    assert audit.payload["recurrences"] == before["block_recurrences"]
    assert conn.execute("SELECT kind FROM task_events WHERE task_id=? AND kind IN ('blocked','unblocked') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0] == "blocked"
    assert not repair_block_loop_task(conn, tid, actor=" operator ", reason=" restore human handoff ")
    assert kb.list_events(conn, tid) == repaired
    assert kb.recompute_ready(conn) == 0
    assert kb.claim_task(conn, tid, claimer="worker") is None


@pytest.mark.parametrize("unsafe", ["missing", "intake", "ready", "running", "done", "review", "archived", "claim", "pid", "expiry", "run", "evidence", "count", "kind", "stale", "actor", "reason"])
def test_repair_refuses_unsafe_targets_without_writes(legacy_card, unsafe):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    with kb.write_txn(conn):
        if unsafe in {"ready", "running", "done", "review", "archived"}:
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (unsafe, tid))
        elif unsafe == "intake":
            tid = kb.create_task(conn, title="intake", triage=True)
        elif unsafe in {"claim", "pid", "expiry", "count", "kind"}:
            column, value = {"claim": ("claim_lock", "owner"), "pid": ("worker_pid", 123), "expiry": ("claim_expires", 1), "count": ("block_recurrences", 0), "kind": ("block_kind", "dependency")}[unsafe]
            conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))
        elif unsafe == "run":
            conn.execute("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (tid,))
        elif unsafe == "evidence":
            conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='block_loop_detected'", (tid,))
        elif unsafe == "stale":
            kb._append_event(conn, tid, "status", {"status": "triage"})
    before = conn.serialize()
    with pytest.raises(ValueError):
        repair_block_loop_task(conn, "absent" if unsafe == "missing" else tid,
                               actor="" if unsafe == "actor" else "operator",
                               reason="" if unsafe == "reason" else "restore handoff")
    assert conn.serialize() == before


def test_operator_cli_repairs_only_legacy_escalation(legacy_card, capsys):
    import argparse
    from hermes_cli import kanban as cli
    from hermes_cli.kanban_parser import build_parser

    conn, tid = legacy_card
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "repair-block-loop", tid,
                              "--actor", "operator", "--reason", "restore handoff"])
    runs = kb.list_runs(conn, tid)
    assert cli.kanban_command(args) == 0
    assert "blocked" in capsys.readouterr().out.lower()
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.list_runs(conn, tid) == runs
    events = kb.list_events(conn, tid)
    assert cli.kanban_command(args) == 0
    assert kb.list_events(conn, tid) == events
    args.task_id = kb.create_task(conn, title="intentional intake", triage=True)
    assert cli.kanban_command(args) == 1
    assert kb.get_task(conn, args.task_id).status == "triage"


@pytest.mark.parametrize("field,value", [
    ("kind", "missing"), ("kind", {}), ("kind", 1), ("kind", "unknown"),
    ("reason", {}), ("reason", []), ("reason", 1), ("reason", False),
    ("reason", "missing"), ("recurrences", True), ("recurrences", 2.0),
    ("recurrences", 0), ("limit", True), ("limit", 2.0), ("limit", 0),
    ("limit", -1), ("limit", 100),
])
def test_initial_repair_requires_strict_escalation_payload(legacy_card, field, value):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    event = kb.list_events(conn, tid)[-1]
    payload = {**event.payload, "kind": None}
    if value == "missing":
        payload.pop(field)
    else:
        payload[field] = value
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event.id))
    before = conn.serialize()
    with pytest.raises(ValueError):
        repair_block_loop_task(conn, tid, actor="operator", reason="restore handoff")
    assert conn.serialize() == before


def test_initial_repair_accepts_explicit_untyped_optional_reason(legacy_card):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    event = kb.list_events(conn, tid)[-1]
    payload = {**event.payload, "kind": None, "reason": None}
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind=NULL WHERE id=?", (tid,))
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event.id))
    assert repair_block_loop_task(conn, tid, actor=" operator ", reason=" restore handoff ")
    audit = kb.list_events(conn, tid)[-1]
    assert audit.kind == "blocked"
    assert audit.payload["reason"] is None
    assert audit.payload["kind"] is None
    assert audit.payload["actor"] == "operator"
    assert audit.payload["repair_reason"] == "restore handoff"


@pytest.mark.parametrize("tamper", [
    "task_kind", "task_count", "original_reason", "original_kind_missing",
    "original_count_bool", "original_limit_bool", "audit_reason", "audit_kind",
    "audit_count_float", "audit_limit", "audit_source", "audit_status",
    "audit_from_status", "audit_actor", "audit_note", "reference_missing",
    "reference_absent", "reference_other_task", "reference_wrong_kind",
    "reference_string", "reference_bool", "reference_future", "intervening",
    "later_lifecycle", "retry_actor", "retry_reason", "ownership", "open_run",
    "status",
])
def test_retry_revalidates_original_evidence_and_audit(legacy_card, tamper):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    original = kb.list_events(conn, tid)[-1]
    assert repair_block_loop_task(conn, tid, actor=" operator ", reason=" restore handoff ")
    audit = kb.list_events(conn, tid)[-1]
    payload = dict(audit.payload)
    original_payload = dict(original.payload)
    with kb.write_txn(conn):
        task_changes = {
            "task_kind": ("block_kind", "dependency"), "task_count": ("block_recurrences", 0),
            "ownership": ("claim_lock", "owner"), "status": ("status", "ready"),
        }
        audit_changes = {
            "audit_reason": ("reason", "different blocker"), "audit_kind": ("kind", None),
            "audit_count_float": ("recurrences", float(payload["recurrences"])),
            "audit_limit": ("limit", 1), "audit_source": ("source_status", "review"),
            "audit_status": ("status", "ready"), "audit_from_status": ("from_status", "ready"),
            "audit_actor": ("actor", "somebody else"), "audit_note": ("repair_reason", "different note"),
            "reference_absent": ("escalation_event_id", 999999),
            "reference_wrong_kind": ("escalation_event_id", kb.list_events(conn, tid)[0].id),
            "reference_string": ("escalation_event_id", str(original.id)),
            "reference_bool": ("escalation_event_id", True),
        }
        if tamper in task_changes:
            field, value = task_changes[tamper]
            conn.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, tid))
        if tamper in audit_changes:
            field, value = audit_changes[tamper]
            payload[field] = value
        if tamper == "original_reason":
            original_payload["reason"] = {}
        if tamper == "original_kind_missing":
            original_payload.pop("kind")
        if tamper == "original_count_bool":
            original_payload["recurrences"] = True
        if tamper == "original_limit_bool":
            original_payload["limit"] = True
        if tamper == "reference_missing":
            payload.pop("escalation_event_id")
        if tamper == "reference_other_task":
            other = kb.create_task(conn, title="other task", triage=True)
            kb._append_event(conn, other, "block_loop_detected", original.payload)
            payload["escalation_event_id"] = kb.list_events(conn, other)[-1].id
        if tamper == "reference_future":
            kb._append_event(conn, tid, "block_loop_detected", original.payload)
            payload["escalation_event_id"] = kb.list_events(conn, tid)[-1].id
        if tamper == "open_run":
            conn.execute("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (tid,))
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(original_payload), original.id))
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), audit.id))
        if tamper in {"intervening", "later_lifecycle"}:
            kb._append_event(conn, tid, "promoted", {"status": "ready"})
        if tamper == "intervening":
            # A copied marker must not hide intervening work or a newer escalation.
            kb._append_event(conn, tid, "blocked", payload)
    before = conn.serialize()
    with pytest.raises(ValueError):
        repair_block_loop_task(conn, tid,
                               actor="other" if tamper == "retry_actor" else "operator",
                               reason="other" if tamper == "retry_reason" else "restore handoff")
    assert conn.serialize() == before


def test_exact_retry_uses_trimmed_identity_and_preserves_all_state(legacy_card):
    from hermes_cli.kanban_block_repair import repair_block_loop_task

    conn, tid = legacy_card
    assert repair_block_loop_task(conn, tid, actor=" operator ", reason=" restore handoff ")
    kb.add_comment(conn, tid, "operator", "Still waiting for a decision")
    before = conn.serialize()
    assert repair_block_loop_task(conn, tid, actor="operator", reason="restore handoff") is False
    assert repair_block_loop_task(conn, tid, actor=" operator\n", reason=" restore handoff\t") is False
    assert conn.serialize() == before
    assert kb.list_events(conn, tid)[-2].kind == "blocked"
