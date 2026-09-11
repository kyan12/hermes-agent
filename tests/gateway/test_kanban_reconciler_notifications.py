from __future__ import annotations

from gateway.kanban_watchers_notifier import should_notify_kanban_event


def test_legacy_notification_behavior_is_unchanged_when_reconciler_disabled() -> None:
    for kind in ("blocked", "gave_up", "crashed", "timed_out", "block_loop_detected"):
        assert should_notify_kanban_event(kind, {}, reconciler_enabled=False)
    assert should_notify_kanban_event(
        "reconciliation_outcome",
        {"outcome": "genuine_human_gate"},
        reconciler_enabled=False,
    )


def test_reconciler_suppresses_raw_recovery_events() -> None:
    for kind in (
        "blocked",
        "block_loop_detected",
        "gave_up",
        "crashed",
        "timed_out",
        "spawn_failed",
        "protocol_violation",
        "rate_limited",
    ):
        assert not should_notify_kanban_event(kind, {}, reconciler_enabled=True)


def test_only_affirmed_human_gate_outcome_notifies() -> None:
    for outcome in (
        "cleared/resumed",
        "continuation_created",
        "dependency_wait",
        "backoff_scheduled",
        "reconciliation_failed",
    ):
        assert not should_notify_kanban_event(
            "reconciliation_outcome",
            {"outcome": outcome},
            reconciler_enabled=True,
        )
    assert should_notify_kanban_event(
        "reconciliation_outcome",
        {"outcome": "genuine_human_gate"},
        reconciler_enabled=True,
    )
    assert should_notify_kanban_event("completed", {}, reconciler_enabled=True)


import asyncio
import json
from pathlib import Path
import pytest
from gateway.config import GatewayConfig, Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_cli import kanban_blocker_reconcile as reconcile


class Transport:
    supports_async_delivery = True

    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, **kwargs):
        self.sent.append(text)

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from hermes_cli import config
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    settings = {"enabled": True, "profile": "default", "max_active": 2}
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"blocker_reconciler": settings}})
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: Transport()}
    runner.config = GatewayConfig()
    runner._kanban_dispatcher_lock_handle = object()
    runner._primary_profile_name = "default"
    return runner, settings


def collect(runner):
    return _notifier_collect(runner, kb, notifier_profile=None, gc_due=False, gc_retention_days=30)


def deliver(runner, rows):
    async def run():
        for row in rows:
            await _KanbanNotification(runner, row, platform_cls=Platform, sub_fail_counts={}).deliver()
    asyncio.run(run())


def source_task(conn):
    task = kb.create_task(conn, title="disposable canary", assignee="default")
    kbn.add_notify_sub(conn, task_id=task, platform="telegram", chat_id="canary")
    claim = kb.claim_task(conn, task, claimer="canary")
    assert claim and kb.heartbeat_claim(conn, task, claimer="canary")
    # Exercise native spawn bookkeeping with a sentinel only; no process is started.
    from hermes_cli.kanban_db_dispatch import _set_worker_pid
    _set_worker_pid(conn, task, 987654321)
    return task, claim


@pytest.mark.parametrize("kind", ["transient", "needs_input"])
def test_disposable_canary_native_claim_spawn_heartbeat_and_one_delivery(rig, kind):
    runner, settings = rig
    with kbc.connect_closing() as conn:
        task, claim = source_task(conn)
        kb.block_task(conn, task, kind=kind, reason="Approve the paid deployment." if kind == "needs_input" else "iteration budget", expected_run_id=claim.current_run_id)
        recoveries = [t for t in kb.list_tasks(conn) if (t.idempotency_key or '').startswith('kanban-reconcile:')]
        rows = collect(runner)
        deliver(runner, rows)
        transport = runner.adapters[Platform.TELEGRAM]
        if kind == "transient":
            assert len(recoveries) == 1
            assert transport.sent == []
            recovery = kb.claim_task(conn, recoveries[0].id, claimer="canary-recovery")
            assert recovery and kb.heartbeat_claim(conn, recovery.id, claimer="canary-recovery")
            from hermes_cli.kanban_db_dispatch import _set_worker_pid
            _set_worker_pid(conn, recovery.id, 987654322)
            event_id = int(recovery.idempotency_key.rsplit(':', 1)[1])
            assert kb.complete_task(conn, recovery.id, expected_run_id=recovery.current_run_id, metadata={"reconciliation": {
                "outcome": "cleared/resumed", "source_task_id": task, "source_event_id": event_id,
            }})
            assert kb.get_task(conn, task).status == "ready"
        else:
            assert recoveries == []
            assert kb.get_task(conn, task).status == "blocked"
            assert len(transport.sent) == 1
            assert "Approve the paid deployment." in transport.sent[0]
        # Retry/restart and config toggles cannot change the gate's identity.
        settings["enabled"] = False
        deliver(runner, collect(runner))
        settings["enabled"] = True
        deliver(runner, collect(runner))
        assert len(transport.sent) == (1 if kind == "needs_input" else 0)
        receipt = {
            "fixture": "disposable SQLite, recording transport, no model or child process",
            "input_kind": kind, "source_task_id": task, "source_run_id": claim.current_run_id,
            "source_status": kb.get_task(conn, task).status, "recovery_count": len(recoveries),
            "alerts": len(transport.sent), "claim": True, "spawn_bookkeeping_only": True, "heartbeat": True,
            "outcomes": [e.payload for e in kb.list_events(conn, task) if e.kind == "reconciliation_outcome"],
        }
        path = Path(__file__).resolve().parents[2] / 'restoration-evidence' / f'canary-{kind}.json'
        path.write_text(json.dumps(receipt, indent=2) + '\n')


@pytest.mark.parametrize("transition", ["complete", "unblock", "archive"])
def test_claimed_gate_revalidated_before_delivery(rig, transition):
    runner, _ = rig
    with kbc.connect_closing() as conn:
        task, claim = source_task(conn)
        kb.block_task(conn, task, kind="needs_input", reason="Approve deployment", expected_run_id=claim.current_run_id)
        rows = collect(runner)
        assert rows
        {"complete": kb.complete_task, "unblock": kb.unblock_task, "archive": kb.archive_task}[transition](conn, task)
    deliver(runner, rows)
    assert runner.adapters[Platform.TELEGRAM].sent == []


def test_gate_context_comment_does_not_erase_the_one_pending_notice(rig):
    runner, _ = rig
    with kbc.connect_closing() as conn:
        task, claim = source_task(conn)
        kb.block_task(conn, task, kind='needs_input', reason='Approve deployment', expected_run_id=claim.current_run_id)
        rows = collect(runner)
        kb.add_comment(conn, task, author='default', body='Additional context for the decision.')
    deliver(runner, rows)
    assert len(runner.adapters[Platform.TELEGRAM].sent) == 1


@pytest.mark.parametrize('delivery_mode', ['notify', 'notify+wake', 'wake'])
def test_raw_and_affirmed_gate_share_ping_identity_across_enablement(rig, delivery_mode):
    runner, settings = rig
    settings['enabled'] = False
    with kbc.connect_closing() as conn:
        task, claim = source_task(conn)
        kbn.add_notify_sub(conn, task_id=task, platform='telegram', chat_id='canary', delivery_mode=delivery_mode)
        kb.block_task(conn, task, kind='needs_input', reason='Approve deployment', expected_run_id=claim.current_run_id)
        event = [e for e in kb.list_events(conn, task) if e.kind == 'blocked'][-1]
        deliver(runner, collect(runner))
        assert len(runner.adapters[Platform.TELEGRAM].sent) == (delivery_mode != 'wake')
        settings['enabled'] = True
        reconcile.enqueue_blocker_reconciliation(conn, event.id)
    deliver(runner, collect(runner))
    assert len(runner.adapters[Platform.TELEGRAM].sent) == (delivery_mode != 'wake')
    assert len(runner.adapters[Platform.TELEGRAM].handled) == (delivery_mode != 'notify')


@pytest.mark.parametrize('producer', ['native', 'raw'])
def test_new_explicit_gate_after_managed_recovery_still_notifies_when_disabled(rig, producer):
    from hermes_cli.kanban_blocker_policy import sync_dispatcher_policy
    runner, settings = rig
    with kbc.connect_closing() as conn:
        source, claim = source_task(conn)
        kb.block_task(conn, source, kind='transient', reason='failure', expected_run_id=claim.current_run_id)
        recovery = next(t for t in kb.list_tasks(conn) if (t.idempotency_key or '').startswith('kanban-reconcile:'))
        claim = kb.claim_task(conn, recovery.id)
        kb.complete_task(conn, recovery.id, expected_run_id=claim.current_run_id, metadata={'reconciliation': {
            'outcome': 'cleared/resumed', 'source_task_id': source,
            'source_event_id': int(recovery.idempotency_key.rsplit(':', 1)[1]),
        }})
        settings['enabled'] = False
        sync_dispatcher_policy(conn)
        if producer == 'native':
            claim = kb.claim_task(conn, source)
            kb.block_task(conn, source, kind='needs_input', reason='Approve a new deployment', expected_run_id=claim.current_run_id)
        else:
            # Simulate an older writer that has no Python transaction hook.
            conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (source,))
            conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'blocked', ?, 1)",
                         (source, json.dumps({'kind': 'needs_input', 'reason': 'Approve a new deployment'})))
            from hermes_cli.kanban_blocker_capture import drain_pending
            # The native tick publishes its disabled policy before draining.
            sync_dispatcher_policy(conn)
            drain_pending(conn)
            assert reconcile.attention_class(conn, source, reconciler_enabled=False) == 'human_input'
            assert len([t for t in kb.list_tasks(conn) if (t.idempotency_key or '').startswith('kanban-reconcile:')]) == 1
    deliver(runner, collect(runner))
    deliver(runner, collect(runner))
    assert len(runner.adapters[Platform.TELEGRAM].sent) == 1
    assert 'Approve a new deployment' in runner.adapters[Platform.TELEGRAM].sent[0]
