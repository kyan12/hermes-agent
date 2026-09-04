"""Adversarial regressions for health-loop authority and races."""
from __future__ import annotations

import json
import argparse
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh
from hermes_cli import kanban_sentinel as ks


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


def _mk(conn, title="card"):
    return kb.create_task(conn, title=title, assignee="alice")


def _evidence(**overrides):
    value = {
        "type": "human_decision",
        "action": "Sign the named contract",
        "affirmed_by": "Kevin Yan",
        "affirmed_at": int(time.time()),
    }
    value.update(overrides)
    return value


def test_evidence_rejects_untrusted_affirmer_and_missing_timestamp():
    assert kh.parse_evidence(_evidence(affirmed_by="mallory")) is None
    evidence = _evidence()
    evidence.pop("affirmed_at")
    assert kh.parse_evidence(evidence) is None


def test_generic_transport_identities_are_never_a_verified_human_principal():
    """``operator:cli`` / ``operator:dashboard`` describe HOW a request
    arrived, not WHO sent it. Accepting them as Kevin lets anyone holding a
    dashboard session token project a card into the human-attention column."""
    for transport in ("operator", "operator:cli", "operator:dashboard", "cli",
                      "dashboard", "worker", "system"):
        assert kh.parse_evidence(_evidence(affirmed_by=transport)) is None, transport
        assert kh.is_verified_human_principal(transport) is False, transport
    assert kh.parse_evidence(_evidence(affirmed_by="Kevin Yan")) is not None
    assert kh.is_verified_human_principal("kevin") is True


def test_human_gate_principal_allowlist_is_configurable(monkeypatch):
    from hermes_cli import config as hcfg

    monkeypatch.setattr(
        hcfg, "load_config",
        lambda: {"kanban": {"human_gate_principals": ["kevin.yan@example.test"]}},
    )
    kh.human_gate_principals.cache_clear()
    try:
        assert kh.is_verified_human_principal("Kevin.Yan@example.test") is True
        # An explicit allowlist replaces the default identities …
        assert kh.is_verified_human_principal("kevin") is False
        # … but can never re-admit a transport identity.
        assert kh.is_verified_human_principal("operator:dashboard") is False
    finally:
        kh.human_gate_principals.cache_clear()


def test_evidence_must_carry_exactly_one_structured_atomic_action():
    """"Atomic" as a nonempty string lets one record smuggle several asks."""
    for compound in (
        "Sign the contract and wire the deposit",
        "Sign the contract; file the addendum",
        "Sign the contract\nFile the addendum",
        "Sign the contract, then file it",
        "1. Sign the contract 2. File it",
    ):
        assert kh.parse_evidence(_evidence(action=compound)) is None, compound
    # A bare verb with no object is not an action either.
    assert kh.parse_evidence(_evidence(action="Approve")) is None

    parsed = kh.parse_evidence(_evidence(action="Sign the named contract"))
    assert parsed.atomic_action == {"verb": "Sign", "object": "the named contract"}
    structured = kh.parse_evidence(
        _evidence(action={"verb": "Sign", "object": "the named contract"})
    )
    assert structured.action == "Sign the named contract"
    assert structured.atomic_action == parsed.atomic_action


def test_block_projection_fails_closed_on_a_stale_mixed_version_reblock(board):
    """A writer that predates the typed columns can unblock and re-block a
    card while leaving ``block_kind`` / ``gate_evidence`` untouched. Reading
    the row alone would project the PREVIOUS occurrence's Kevin affirmation
    as though it were current."""
    tid = _mk(board)
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    assert kh.classify_block(board, kb.get_task(board, tid)).visible is True

    # The old writer's shape: a fresh ``blocked`` occurrence, same columns.
    with kb.write_txn(board):
        kb._append_event(board, tid, "unblocked", {"by": "legacy"})
        kb._append_event(board, tid, "blocked", {"reason": "legacy re-block"})

    task = kb.get_task(board, tid)
    assert task.gate_evidence is not None  # the stale column survived …
    projection = kh.classify_block(board, task)
    assert projection.visible is False     # … and proves nothing.
    assert projection.reason_code == kh.REASON_UNAFFIRMED_GATE


def test_block_projection_without_a_connection_fails_closed(board):
    """The occurrence check is not optional: a caller that cannot consult the
    event log has not established that the affirmation is current."""
    tid = _mk(board)
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    assert kh.classify_block(None, kb.get_task(board, tid)).visible is False


def test_gate_affirmation_binds_current_block_occurrence_and_reblock_clears_it(board):
    tid = _mk(board)
    # An untrusted/machine block request is recovery work, never a visible
    # Kevin gate.  Only the atomic trusted operator transition below may put
    # the card in ``blocked``.
    assert kb.block_task(board, tid, reason="claim", kind="needs_input")
    assert kb.get_task(board, tid).status == "triage"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    bound = json.loads(kb.get_task(board, tid).gate_evidence)
    event = board.execute(
        "SELECT id FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert bound["task_id"] == tid
    assert bound["occurrence_event_id"] == event["id"]

    assert kb.unblock_task(board, tid)
    assert kb.get_task(board, tid).gate_evidence is None
    assert kb.block_task(board, tid, reason="new claim", kind="needs_input")
    task = kb.get_task(board, tid)
    assert task.status == "triage"
    assert task.gate_evidence is None
    assert kh.classify_block(board, task).visible is False


def test_gate_and_hold_fields_clear_on_complete_and_archive(board):
    tid = _mk(board, "complete")
    assert kh.affirm_human_gate(board, tid, evidence=_evidence())
    assert kb.complete_task(board, tid, result="done")
    done = kb.get_task(board, tid)
    assert (done.gate_evidence, done.hold_kind, done.hold_wake_at) == (None, None, None)

    other = _mk(board, "archive")
    assert kh.set_hold(board, other, kind="wake", wake_at=int(time.time()) + 60)
    assert kb.archive_task(board, other)
    archived = kb.get_task(board, other)
    assert (archived.gate_evidence, archived.hold_kind, archived.hold_wake_at) == (None, None, None)


def test_intentional_park_requires_current_trusted_evidence(board):
    tid = _mk(board)
    assert kh.set_hold(board, tid, kind="external", apply=True)
    health = kh.board_health(board)
    hold = next(row for row in health["holds"] if row["task_id"] == tid)
    assert hold["healthy"] is False
    assert tid in {row["task_id"] for row in health["no_forward_path"]}


def test_set_hold_rejects_parentless_dependency(board):
    tid = _mk(board)

    assert kh.set_hold(board, tid, kind="dependency", apply=True) is False
    assert kb.get_task(board, tid).status == "ready"


def test_parentless_dependency_hold_is_broken_and_never_resumed(board):
    tid = _mk(board)
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET status='scheduled', hold_kind='dependency' WHERE id=?",
            (tid,),
        )

    state = kh.classify_hold(
        kb.get_task(board, tid),
        now=int(time.time()),
        wake_health={"healthy": True},
        parents_done=kh._parents_done(board, tid),
    )
    assert state.healthy is False
    assert state.resumable is False
    assert state.reason_code == kh.REASON_DEPENDENCY_BROKEN

    report = kh.reconcile_board(board, apply=True)
    assert tid in {row["task_id"] for row in report.diagnosed}
    assert not any(row["task_id"] == tid for row in report.resumed)
    assert kb.get_task(board, tid).status == "scheduled"


def test_dead_running_row_has_no_forward_path(board, monkeypatch):
    tid = _mk(board)
    with kb.write_txn(board):
        board.execute(
            "UPDATE tasks SET status='running', worker_pid=999999, claim_lock='x', claim_expires=? WHERE id=?",
            (int(time.time()) + 60, tid),
        )
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    path = next(p for p in kh.forward_paths(board) if p.task_id == tid)
    assert path.ok is False


def test_dispatch_result_carries_complete_reconciliation_report(board, monkeypatch, all_assignees_spawnable):
    expected = kh.ReconcileReport(errors=[{"task_id": None, "error": "boom"}])
    monkeypatch.setattr(kh, "reconcile_board", lambda conn: expected)
    result = kb.dispatch_once(board, spawn_fn=lambda *a, **k: 1)
    assert result.health_reconciliation == expected.to_dict()
    assert result.health_reconciliation["errors"] == [{"task_id": None, "error": "boom"}]


def test_board_enumeration_failure_is_not_silently_defaulted(monkeypatch):
    monkeypatch.setattr(kb, "list_boards", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("catalog down")))
    with pytest.raises(RuntimeError, match="catalog down"):
        kh.all_board_slugs()


def test_sentinel_alert_dedupe_is_transactional(board, monkeypatch, tmp_path):
    monkeypatch.setattr(ks, "_state_path", lambda: tmp_path / "sentinel-state.json")
    action = {"title": "one", "action": "do one thing", "evidence": {"task_ids": ["t_x"]}}
    reports = [ks.SentinelReport(generated_at=100, kevin_action=action) for _ in range(8)]
    barrier = threading.Barrier(len(reports))
    emitted = []

    def invoke(report):
        barrier.wait()
        emitted.append(ks._maybe_emit(report, 100, apply=True))

    threads = [threading.Thread(target=invoke, args=(report,)) for report in reports]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert emitted.count(True) == 1


def test_set_hold_rolls_back_status_when_typing_event_fails(board, monkeypatch):
    tid = _mk(board)
    original = kb._append_event

    def fail_typed(conn, task_id, kind, payload=None, **kwargs):
        if kind == "hold_typed":
            raise RuntimeError("event write failed")
        return original(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_typed)
    with pytest.raises(RuntimeError, match="event write failed"):
        kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) + 60)
    task = kb.get_task(board, tid)
    assert task.status == "ready"
    assert task.hold_kind is None


def test_reconcile_rolls_back_resume_when_audit_event_fails(board):
    tid = _mk(board)
    assert kh.set_hold(board, tid, kind="wake", wake_at=int(time.time()) - 1)
    board.execute(
        "CREATE TRIGGER reject_hold_resume BEFORE INSERT ON task_events "
        "WHEN NEW.kind='hold_resumed' BEGIN SELECT RAISE(ABORT, 'audit failed'); END"
    )
    report = kh.reconcile_board(board)
    assert report.errors
    task = kb.get_task(board, tid)
    assert task.status == "scheduled"
    assert task.hold_kind == "wake"


def test_reconcile_classification_failure_replaces_fresh_ok_checkpoint(
    board, monkeypatch
):
    now = int(time.time())
    parent = _mk(board, "unfinished parent")
    tid = _mk(board)
    kb.link_tasks(board, parent_id=parent, child_id=tid)
    assert kh.set_hold(board, tid, kind="dependency")
    kh.record_checkpoint(
        board, kh.CHECKPOINT_RECONCILE, status="ok", now=now
    )
    monkeypatch.setattr(
        kh,
        "_parents_done",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dependency read failed")),
    )

    report = kh.reconcile_board(board, now=now + 1)
    assert report.errors == [{"task_id": tid, "error": "dependency read failed"}]
    checkpoint = kh.read_checkpoint(board, kh.CHECKPOINT_RECONCILE)
    assert checkpoint["status"] == "failed"
    assert checkpoint["updated_at"] == now + 1


def test_dashboard_health_get_is_read_only_and_post_owns_reconcile():
    from plugins.kanban.dashboard import plugin_api

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in plugin_api.router.routes
    }
    assert ("/board-health", ("GET",)) in routes
    assert ("/board-health/reconcile", ("POST",)) in routes
    assert "reconcile" not in plugin_api.get_board_health.__annotations__


def test_update_canary_fails_closed_when_probe_crashes(tmp_path, monkeypatch):
    from hermes_cli import update_cmd
    from hermes_cli import kanban_capabilities as kc

    monkeypatch.setattr(kc, "preactivation_canary", lambda root: (_ for _ in ()).throw(RuntimeError("boom")))
    assert update_cmd._run_capability_canary(tmp_path, label="candidate") is False


def test_affirm_human_gate_is_atomic_when_audit_event_fails(board, monkeypatch):
    tid = _mk(board)
    original = kb._append_event

    def fail_gate(conn, task_id, kind, payload=None, **kwargs):
        if kind == "blocked":
            raise RuntimeError("gate audit failed")
        return original(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_gate)
    with pytest.raises(RuntimeError, match="gate audit failed"):
        kh.affirm_human_gate(board, tid, evidence=_evidence())
    task = kb.get_task(board, tid)
    assert task.status == "ready"
    assert task.gate_evidence is None


def test_dashboard_exposes_typed_gate_and_hold_mutation_routes():
    from plugins.kanban.dashboard import plugin_api

    routes = {
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in plugin_api.router.routes
    }
    assert ("/tasks/{task_id}/affirm-gate", ("POST",)) in routes
    assert ("/tasks/{task_id}/hold", ("POST",)) in routes


def _block_args(tid, **overrides):
    args = dict(
        task_id=tid,
        ids=[],
        reason=["Kevin must decide"],
        kind="needs_input",
        action="Approve the named contract",
        evidence_type="human_decision",
        affirmed_by=None,
    )
    args.update(overrides)
    return argparse.Namespace(**args)


def test_worker_context_cannot_self_affirm_a_human_gate_via_cli(
    board, monkeypatch
):
    from hermes_cli import kanban as kcli

    tid = _mk(board)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "kevin")

    assert kcli._cmd_block(_block_args(tid)) == 2
    assert kb.get_task(board, tid).status == "ready"


def test_cli_refuses_to_affirm_without_a_bound_human_principal(board, monkeypatch):
    """``operator:cli`` proved only that someone could run a command on this
    host. With no verified principal bound, the CLI has no identity to affirm
    with and must refuse rather than manufacture one."""
    from hermes_cli import kanban as kcli

    tid = _mk(board)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_OPERATOR", raising=False)
    monkeypatch.setattr(kh, "operator_principal", lambda: None)

    assert kcli._cmd_block(_block_args(tid)) == 2
    assert kb.get_task(board, tid).status == "ready"


def test_cli_affirmation_is_stamped_with_the_verified_principal(board, monkeypatch):
    from hermes_cli import kanban as kcli

    tid = _mk(board)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "Kevin Yan")

    assert kcli._cmd_block(_block_args(tid)) == 0
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    evidence = json.loads(task.gate_evidence)
    assert evidence["affirmed_by"] == "Kevin Yan"
    assert evidence["atomic_action"] == {
        "verb": "Approve", "object": "the named contract"
    }
    assert kh.classify_block(board, task).visible is True


def test_cli_rejects_an_affirmed_by_outside_the_verified_allowlist(
    board, monkeypatch
):
    from hermes_cli import kanban as kcli

    tid = _mk(board)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "Kevin Yan")

    assert kcli._cmd_block(_block_args(tid, affirmed_by="mallory")) == 2
    assert kb.get_task(board, tid).status == "ready"


def test_cli_rejects_a_compound_action_before_touching_the_board(
    board, monkeypatch
):
    from hermes_cli import kanban as kcli

    tid = _mk(board)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_OPERATOR", "Kevin Yan")

    args = _block_args(tid, action="Sign the contract and wire the deposit")
    assert kcli._cmd_block(args) == 2
    assert kb.get_task(board, tid).status == "ready"


def test_blocked_hook_fires_only_for_affirmed_gate_after_commit(board, monkeypatch):
    observed = []

    def record(event, task_id, **fields):
        observed.append((event, task_id, board.in_transaction, fields))

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", record)

    dependency = _mk(board, "dependency")
    assert kb.block_task(board, dependency, kind="dependency", reason="parent")
    assert observed == []

    gate = _mk(board, "gate")
    assert kh.affirm_human_gate(
        board, gate, evidence=_evidence(), reason="Kevin must decide"
    )
    assert len(observed) == 1
    event, task_id, in_transaction, fields = observed[0]
    assert event == "kanban_task_blocked"
    assert task_id == gate
    assert in_transaction is False
    assert fields["reason"] == "Kevin must decide"
