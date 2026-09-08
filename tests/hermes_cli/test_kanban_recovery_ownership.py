"""Adversarial contract for exact-occurrence recovery ownership.

Restores and pins the guarantees of the historical blocker reconciler
(40b307357d) inside this tree's lifecycle:

* exactly one owner per occurrence, across repeat ticks and concurrent resolvers;
* authority lives at the kernel/claim boundary — the reserved recovery
  namespace cannot be forged, and the kill switch bites where ownership is
  acquired and where outcomes are applied, not merely at enqueue;
* a dead or settled owner never suppresses the forward path;
* every non-success outcome leaves the source moving — machine failure never
  becomes human attention;
* the source's title/body/assignee/project/workspace survive byte-for-byte, an
  affirmed human gate stays stopped, and external/physical holds stay held.

Every test runs against a throwaway ``HERMES_HOME``/board.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


ENABLED_CONFIG = {
    "kanban": {
        "blocker_reconciler": {"enabled": True, "profile": "code-crab", "max_active": 2}
    }
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def set_reconciler(monkeypatch):
    """Install a reconciler config; returns a setter so tests can flip it."""
    import hermes_cli.config as config_module

    state = {"config": json.loads(json.dumps(ENABLED_CONFIG))}
    monkeypatch.setattr(
        config_module, "load_config",
        lambda *a, **k: json.loads(json.dumps(state["config"])),
    )

    def _set(**overrides):
        state["config"] = {"kanban": {"blocker_reconciler": dict(overrides)}}

    return _set


@pytest.fixture
def board(home, set_reconciler):
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SOURCE_BODY = (
    "relationship_mode: internal\n"
    "principal: Kevin Yan\n"
    "legal_scope: ProteusX internal only\n"
    "APPROVED ENVELOPE: subject=2026-09-07 canary; do not alter.\n"
    + ("long approved prose. " * 600)
)


def _machine_stall(conn, *, title="stalled source", kind="transient", **kw):
    tid = kb.create_task(
        conn, title=title, assignee="alice", body=SOURCE_BODY, **kw
    )
    assert kb.claim_task(conn, tid, claimer="alice") is not None
    assert kb.block_task(conn, tid, reason="provider timed out", kind=kind) is True
    return tid


def _occurrence(conn, task_id, kind="automation_recovery_requested"):
    row = conn.execute(
        "SELECT id, run_id FROM task_events WHERE task_id=? AND kind=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    assert row is not None
    return int(row["id"]), row["run_id"]


def _owners(conn, source_id):
    return conn.execute(
        "SELECT id, idempotency_key, status FROM tasks WHERE idempotency_key LIKE ? "
        "ORDER BY created_at, id",
        (f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}%:{source_id}:%",),
    ).fetchall()


def _outcomes(conn, source_id):
    return [
        e.payload or {}
        for e in kb.list_events(conn, source_id)
        if e.kind == "reconciliation_outcome"
    ]


def _later_occurrence(conn, task_id, kind="gave_up"):
    """A second machine occurrence on a source that is already parked."""
    with kb.write_txn(conn):
        return kb._append_event(conn, task_id, kind, {"reason": "still failing"})


def _complete_owner(conn, owner_id, reconciliation):
    """Run one owner through claim → complete with a machine-readable verdict."""
    owner = kb.claim_task(conn, owner_id, claimer="code-crab")
    assert owner is not None, "the owner must be claimable / dispatchable"
    return kb.complete_task(
        conn, owner_id, result="reconciled", summary="reconciled",
        metadata={"reconciliation": reconciliation},
        expected_run_id=owner.current_run_id,
    )


# ---------------------------------------------------------------------------
# 1 — the occurrence obtains exactly one bounded, dispatchable owner
# ---------------------------------------------------------------------------


def test_the_owner_binds_the_exact_source_task_run_and_event(board):
    source_id = _machine_stall(board)
    event_id, run_id = _occurrence(board, source_id)

    owners = _owners(board, source_id)
    assert len(owners) == 1
    assert owners[0]["idempotency_key"].endswith(f":{source_id}:{event_id}")

    enqueued = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "reconciliation_enqueued"
    ]
    assert enqueued and enqueued[-1]["source_event_id"] == event_id
    assert enqueued[-1]["source_run_id"] == run_id
    assert enqueued[-1]["reconciliation_task_id"] == owners[0]["id"]


def test_the_owner_is_bounded_and_carries_the_scope_envelope(board):
    source_id = _machine_stall(board, tenant="proteusx")
    owner = kb.get_task(board, _owners(board, source_id)[0]["id"])

    assert owner.assignee == "code-crab"
    assert owner.max_runtime_seconds == 1800
    assert owner.max_retries == 2
    assert owner.tenant == "proteusx"
    assert "ProteusX internal only" in owner.body
    assert "Kevin Yan" in owner.body
    # Classification, not re-specification: the owner is told what it may not do.
    assert "not authorized" in owner.body.lower()
    assert "decompose" in owner.body.lower()


def test_repeat_ticks_and_concurrent_resolvers_yield_exactly_one_owner(board, home):
    source_id = _machine_stall(board)
    event_id, _ = _occurrence(board, source_id)

    other = kb.connect()
    try:
        for _ in range(3):
            kb.enqueue_blocker_reconciliation(board, event_id)
            kb.enqueue_blocker_reconciliation(other, event_id)
            kb.reconcile_orphaned_automation_recovery(board)
    finally:
        other.close()

    assert len(_owners(board, source_id)) == 1


def test_a_repeat_occurrence_coalesces_into_the_active_owner(board):
    source_id = _machine_stall(board)
    first = _owners(board, source_id)[0]["id"]

    _later_occurrence(board, source_id)

    assert [row["id"] for row in _owners(board, source_id)] == [first]
    coalesced = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "reconciliation_coalesced"
    ]
    assert coalesced and coalesced[-1]["reconciliation_task_id"] == first


def test_the_occurrence_and_its_owner_share_one_transaction(board, monkeypatch):
    """A crash between the two must leave neither, never a stranded stall."""
    real_create = kb.create_task

    def _boom(*a, **kw):
        if kb.is_recovery_owner_key(kw.get("idempotency_key")):
            raise RuntimeError("owner creation failed")
        return real_create(*a, **kw)

    monkeypatch.setattr(kb, "create_task", _boom)
    tid = kb.create_task(board, title="atomic", assignee="alice", body=SOURCE_BODY)
    assert kb.claim_task(board, tid, claimer="alice") is not None
    with pytest.raises(RuntimeError):
        kb.block_task(board, tid, reason="boom", kind="transient")

    monkeypatch.undo()
    assert kb.get_task(board, tid).status == "running"
    assert not _owners(board, tid)
    assert not [
        e for e in kb.list_events(board, tid)
        if e.kind == "automation_recovery_requested"
    ]


# ---------------------------------------------------------------------------
# 2 — authority: the reserved namespace and the kill switch
# ---------------------------------------------------------------------------


def test_the_reserved_recovery_namespace_cannot_be_forged(board):
    source_id = _machine_stall(board)
    event_id, _ = _occurrence(board, source_id)
    forged = f"{kb.RECONCILIATION_IDEMPOTENCY_PREFIX}default:{source_id}:{event_id + 1}"

    with pytest.raises(ValueError, match="reserved"):
        kb.create_task(
            board, title="forged owner", assignee="code-crab",
            idempotency_key=forged,
        )
    assert len(_owners(board, source_id)) == 1


def test_the_kill_switch_stops_an_existing_owner_at_the_claim_boundary(
    board, set_reconciler
):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]

    set_reconciler(enabled=False)
    assert kb.claim_task(board, owner_id, claimer="code-crab") is None
    rejected = [
        e.payload for e in kb.list_events(board, owner_id)
        if e.kind == "claim_rejected"
    ]
    assert rejected[-1]["reason"] == "blocker_reconciler_disabled"


def test_the_kill_switch_stops_outcomes_from_being_applied(board, set_reconciler):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)
    owner = kb.claim_task(board, owner_id, claimer="code-crab")

    set_reconciler(enabled=False)
    with pytest.raises(ValueError, match="disabled"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": {
                "outcome": "cleared/resumed",
                "source_task_id": source_id,
                "source_event_id": event_id,
            }},
            expected_run_id=owner.current_run_id,
        )
    assert kb.get_task(board, source_id).status == "triage"


@pytest.mark.parametrize("raw", [False, 0, "", "no", "off", None, "maybe", {}])
def test_malformed_or_false_kill_switch_values_fail_closed(
    board, set_reconciler, raw
):
    set_reconciler(enabled=raw)
    tid = kb.create_task(board, title="off", assignee="alice", body=SOURCE_BODY)
    assert kb.claim_task(board, tid, claimer="alice") is not None
    assert kb.block_task(board, tid, reason="stall", kind="transient")
    assert not _owners(board, tid)


# ---------------------------------------------------------------------------
# 3 — what recovery must refuse to own
# ---------------------------------------------------------------------------


def test_an_affirmed_human_gate_is_never_owned_and_stays_stopped(board):
    tid = kb.create_task(board, title="human only", assignee="alice", body=SOURCE_BODY)
    assert kb.claim_task(board, tid, claimer="alice") is not None
    assert kh.affirm_human_gate(
        board, tid,
        evidence={
            "type": "human_decision",
            "action": "sign the physical form",
            "affirmed_by": "Kevin Yan",
            "affirmed_at": int(time.time()),
        },
        kind="needs_input", reason="sign the physical form",
    )
    assert kb.get_task(board, tid).status == "blocked"

    kb.reconcile_orphaned_automation_recovery(board)
    assert not _owners(board, tid)
    assert kb.get_task(board, tid).status == "blocked"


def test_an_external_hold_stays_held(board):
    tid = kb.create_task(board, title="waiting on a courier", assignee="alice")
    assert kh.set_hold(board, tid, kind="external", reason="courier in transit")
    kb.reconcile_orphaned_automation_recovery(board)
    task = kb.get_task(board, tid)
    assert task.status == "scheduled" and task.hold_kind == "external"
    assert not _owners(board, tid)


def test_a_terminal_source_never_obtains_an_owner(board):
    tid = kb.create_task(board, title="already done", assignee="alice")
    assert kb.claim_task(board, tid, claimer="alice") is not None
    assert kb.block_task(board, tid, reason="stall", kind="transient")
    event_id, _ = _occurrence(board, tid)
    owner_id = _owners(board, tid)[0]["id"]
    board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    board.commit()

    # Terminal source truth is final: no owner is minted or re-minted.
    assert kb.enqueue_blocker_reconciliation(board, event_id) is None
    board.execute("DELETE FROM tasks WHERE id=?", (owner_id,))
    board.commit()
    assert kb.enqueue_blocker_reconciliation(board, event_id) is None
    assert not _owners(board, tid)


def test_a_terminal_source_retires_its_stale_owner(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    board.execute("UPDATE tasks SET status='done' WHERE id=?", (source_id,))
    board.commit()

    assert kb.reconcile_stale_reconciliation_wrappers(board) == [owner_id]
    assert kb.get_task(board, owner_id).status == "archived"


def test_a_fresh_intake_triage_card_is_never_owned(board):
    tid = kb.create_task(board, title="fresh intake", assignee="alice", triage=True)
    kb.reconcile_orphaned_automation_recovery(board)
    assert not _owners(board, tid)


# ---------------------------------------------------------------------------
# 4 — a dead or settled owner never suppresses the forward path
# ---------------------------------------------------------------------------


def test_a_running_owner_with_a_dead_worker_is_not_a_live_owner(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    assert kb.claim_task(board, owner_id, claimer="code-crab") is not None
    assert kb.active_recovery_owner(board, source_id) == owner_id

    board.execute(
        "UPDATE tasks SET worker_pid=?, claim_expires=? WHERE id=?",
        (2 ** 22 - 1, int(time.time()) + 3600, owner_id),
    )
    board.commit()
    assert kb.active_recovery_owner(board, source_id) is None


def test_a_running_owner_with_an_expired_claim_is_not_a_live_owner(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    assert kb.claim_task(board, owner_id, claimer="code-crab") is not None
    board.execute(
        "UPDATE tasks SET claim_expires=? WHERE id=?",
        (int(time.time()) - 10, owner_id),
    )
    board.commit()
    assert kb.active_recovery_owner(board, source_id) is None


def test_a_settled_owner_is_not_a_live_owner(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    board.execute("UPDATE tasks SET status='archived' WHERE id=?", (owner_id,))
    board.commit()
    assert kb.active_recovery_owner(board, source_id) is None


# ---------------------------------------------------------------------------
# 5 — every non-success owner outcome retains a forward path
# ---------------------------------------------------------------------------


def test_a_cleared_resumed_verdict_returns_the_source_to_the_queue(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    assert _complete_owner(board, owner_id, {
        "outcome": "cleared/resumed",
        "source_task_id": source_id,
        "source_event_id": event_id,
    })
    source = kb.get_task(board, source_id)
    assert source.status == "ready"
    assert source.block_kind is None
    assert _outcomes(board, source_id)[-1]["outcome"] == "cleared/resumed"


def test_a_reconciliation_failure_still_leaves_the_source_moving(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    assert _complete_owner(board, owner_id, {
        "outcome": "reconciliation_failed",
        "source_task_id": source_id,
        "source_event_id": event_id,
        "error": "provider still refusing",
    })
    source = kb.get_task(board, source_id)
    assert source.status in ("ready", "todo")
    payload = _outcomes(board, source_id)[-1]
    assert payload["fallback"] == "source_resumed"
    assert payload["human_action"] is None


def test_a_dead_owner_hands_the_source_back_instead_of_dead_ending(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]

    assert kb.claim_task(board, owner_id, claimer="code-crab") is not None
    # The owner itself machine-fails. It cannot spawn another owner, so the
    # source must be handed back to automation, not to a human.
    assert kb.block_task(board, owner_id, reason="owner crashed", kind="transient")

    source = kb.get_task(board, source_id)
    assert source.status in ("ready", "todo")
    payload = _outcomes(board, source_id)[-1]
    assert payload["outcome"] == "reconciliation_failed"
    assert payload["fallback"] == "source_resumed"
    assert payload["human_action"] is None


def test_a_dependency_wait_verdict_parks_the_source_behind_its_parent(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)
    dep = kb.create_task(board, title="real dependency", assignee="alice")
    kb.link_tasks(board, dep, source_id)

    assert _complete_owner(board, owner_id, {
        "outcome": "dependency_wait",
        "source_task_id": source_id,
        "source_event_id": event_id,
        "dependency_task_id": dep,
    })
    assert kb.get_task(board, source_id).status == "todo"


def test_a_dependency_verdict_without_a_real_parent_edge_is_rejected(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)
    dep = kb.create_task(board, title="unlinked", assignee="alice")

    with pytest.raises(ValueError, match="linked parent"):
        _complete_owner(board, owner_id, {
            "outcome": "dependency_wait",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "dependency_task_id": dep,
        })
    assert kb.get_task(board, source_id).status == "triage"


def test_a_backoff_verdict_becomes_a_typed_wake_hold(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)
    resume_at = int(time.time()) + 900

    assert _complete_owner(board, owner_id, {
        "outcome": "backoff_scheduled",
        "source_task_id": source_id,
        "source_event_id": event_id,
        "resume_at": resume_at,
    })
    source = kb.get_task(board, source_id)
    assert source.status == "scheduled"
    assert source.hold_kind == "wake" and source.hold_wake_at == resume_at


def test_a_past_resume_at_is_rejected(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    with pytest.raises(ValueError, match="future unix timestamp"):
        _complete_owner(board, owner_id, {
            "outcome": "backoff_scheduled",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "resume_at": int(time.time()) - 10,
        })


# ---------------------------------------------------------------------------
# 6 — a machine verdict may surface a gate, never affirm one
# ---------------------------------------------------------------------------


def test_a_genuine_human_gate_verdict_surfaces_one_action_without_affirming_it(board):
    source_id = _machine_stall(board, kind="capability")
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    assert _complete_owner(board, owner_id, {
        "outcome": "genuine_human_gate",
        "source_task_id": source_id,
        "source_event_id": event_id,
        "human_action": "sign the paper form in the office safe",
        "why_automation_cannot_perform": "requires a wet signature",
        "current_evidence": "the notary rejected the digital copy on 2026-09-07",
    })
    source = kb.get_task(board, source_id)
    # The trusted affirmation boundary is kanban_health.affirm_human_gate. A
    # machine verdict must not manufacture the blocked state.
    assert source.status != "blocked"
    assert source.gate_evidence is None
    surfaced = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "recovery_human_action"
    ]
    assert surfaced and surfaced[-1]["affirmed"] is False
    assert surfaced[-1]["human_action"].startswith("sign the paper form")


def test_a_gate_verdict_missing_its_justification_is_rejected(board):
    source_id = _machine_stall(board, kind="needs_input")
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    with pytest.raises(ValueError, match="why_automation_cannot_perform"):
        _complete_owner(board, owner_id, {
            "outcome": "genuine_human_gate",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "human_action": "ask Kevin",
        })


# ---------------------------------------------------------------------------
# 7 — exact binding: stale source advancement rejects mutation
# ---------------------------------------------------------------------------


def test_a_stale_source_event_id_is_rejected(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    _later_occurrence(board, source_id)

    with pytest.raises(ValueError, match="stale"):
        _complete_owner(board, owner_id, {
            "outcome": "cleared/resumed",
            "source_task_id": source_id,
            "source_event_id": event_id,
        })


def test_a_source_that_advances_while_resolving_discards_the_verdict(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)
    owner = kb.claim_task(board, owner_id, claimer="code-crab")

    # Another writer materially advances the source after the verdict was formed.
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (source_id,))
    board.commit()
    with kb.write_txn(board):
        kb._append_event(board, source_id, "unblocked", {"by": "operator"})

    with pytest.raises(ValueError, match="advanced"):
        kb.complete_task(
            board, owner_id, result="x",
            metadata={"reconciliation": {
                "outcome": "cleared/resumed",
                "source_task_id": source_id,
                "source_event_id": event_id,
            }},
            expected_run_id=owner.current_run_id,
        )


def test_an_owner_cannot_report_an_outcome_for_a_foreign_source(board):
    source_id = _machine_stall(board, title="mine")
    victim_id = _machine_stall(board, title="victim")
    owner_id = _owners(board, source_id)[0]["id"]
    victim_event_id, _ = _occurrence(board, victim_id)

    with pytest.raises(ValueError, match="lineage"):
        _complete_owner(board, owner_id, {
            "outcome": "cleared/resumed",
            "source_task_id": victim_id,
            "source_event_id": victim_event_id,
        })
    assert kb.get_task(board, victim_id).status == "triage"


def test_an_owner_cannot_complete_without_a_verdict(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]

    with pytest.raises(ValueError, match="reconciliation metadata is required"):
        _complete_owner(board, owner_id, None)


def test_an_unknown_verdict_field_is_rejected(board):
    source_id = _machine_stall(board)
    owner_id = _owners(board, source_id)[0]["id"]
    event_id, _ = _occurrence(board, source_id)

    with pytest.raises(ValueError, match="unexpected fields"):
        _complete_owner(board, owner_id, {
            "outcome": "cleared/resumed",
            "source_task_id": source_id,
            "source_event_id": event_id,
            "archive_source": True,
        })


# ---------------------------------------------------------------------------
# 8 — the source envelope survives byte-for-byte
# ---------------------------------------------------------------------------


def test_the_source_envelope_is_preserved_byte_for_byte(board):
    tid = kb.create_task(
        board, title="AV Chemist canary", assignee="alice", body=SOURCE_BODY,
        tenant="proteusx", workspace_kind="scratch",
    )
    before = kb.get_task(board, tid)
    assert kb.claim_task(board, tid, claimer="alice") is not None
    assert kb.block_task(board, tid, reason="preflight unresolved", kind="needs_input")
    owner_id = _owners(board, tid)[0]["id"]
    event_id, _ = _occurrence(board, tid)
    _complete_owner(board, owner_id, {
        "outcome": "cleared/resumed",
        "source_task_id": tid,
        "source_event_id": event_id,
    })

    after = kb.get_task(board, tid)
    assert after.title == before.title
    assert after.body == before.body
    assert after.assignee == before.assignee
    assert after.project_id == before.project_id
    assert after.workspace_kind == before.workspace_kind
    assert after.workspace_path == before.workspace_path
    assert "APPROVED ENVELOPE: subject=2026-09-07 canary" in after.body


def test_the_long_source_body_is_excerpted_not_re_specified(board):
    source_id = _machine_stall(board)
    owner = kb.get_task(board, _owners(board, source_id)[0]["id"])
    envelope = json.loads(owner.body.split("```json", 1)[1].rsplit("```", 1)[0])

    assert envelope["source"]["body_is_truncated"] is True
    assert len(envelope["source"]["body_excerpt"]) <= 4000
    assert envelope["lineage"]["source_task_id"] == source_id
    assert envelope["workspace"]["preserve_dirty_work"] is True


def test_credentials_never_reach_the_owner_prompt(board):
    tid = kb.create_task(
        board, title="leaky", assignee="alice",
        body="api_key=sk-abcdefghijklmnopqrstuvwxyz012345\nremote: https://u:p@example.com/x",
    )
    assert kb.claim_task(board, tid, claimer="alice") is not None
    assert kb.block_task(board, tid, reason="ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaa", kind="transient")
    owner = kb.get_task(board, _owners(board, tid)[0]["id"])

    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in owner.body
    assert "u:p@example.com" not in owner.body
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaa" not in owner.body


# ---------------------------------------------------------------------------
# 9 — bounded generations, backfill, and mixed deployments
# ---------------------------------------------------------------------------


def test_recovery_stops_after_its_generation_limit(board):
    """Bounded automation: a fixed number of owners, then one escalation."""
    source_id = _machine_stall(board)
    seen = []
    for _ in range(kb.RECONCILIATION_SOURCE_FAILURE_LIMIT + 2):
        owner_id = kb.active_recovery_owner(board, source_id)
        if owner_id is None:
            break
        seen.append(owner_id)
        event_id, _ = _occurrence(board, source_id)
        assert _complete_owner(board, owner_id, {
            "outcome": "cleared/resumed",
            "source_task_id": source_id,
            "source_event_id": event_id,
        })
        if kb.get_task(board, source_id).status not in ("ready", "todo"):
            break
        assert kb.claim_task(board, source_id, claimer="alice") is not None
        assert kb.block_task(board, source_id, reason="again", kind="transient")

    assert len(seen) == len(set(seen)) <= kb.RECONCILIATION_SOURCE_FAILURE_LIMIT
    exhausted = [
        p for p in _outcomes(board, source_id)
        if p.get("fallback") == "automation_exhausted"
    ]
    assert len(exhausted) == 1, "the bounded lane must escalate exactly once"
    assert exhausted[0]["discarded_outcome"] == "cleared/resumed"
    # Parked with its precise state, not silently retried forever and not
    # converted into human attention by the machine.
    assert kb.get_task(board, source_id).status == "triage"
    assert kb.active_recovery_owner(board, source_id) is None


def test_the_backfill_pass_is_idempotent_across_repeat_ticks(board):
    source_id = _machine_stall(board)
    for row in _owners(board, source_id):
        board.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    board.execute(
        "UPDATE tasks SET recovery_backfill_pending=1 WHERE id=?", (source_id,)
    )
    board.commit()

    first = kb.reconcile_orphaned_automation_recovery(board)
    assert len(first) == 1
    for _ in range(3):
        assert kb.reconcile_orphaned_automation_recovery(board) == []
    assert len(_owners(board, source_id)) == 1

    repaired = [
        e.payload for e in kb.list_events(board, source_id)
        if e.kind == "reconciliation_backfill_repaired"
    ]
    assert len(repaired) == 1


def test_the_backfill_cursor_is_durable_so_a_hot_source_cannot_starve_others(board):
    hot = _machine_stall(board, title="hot")
    for row in _owners(board, hot):
        board.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    board.execute("UPDATE tasks SET recovery_backfill_pending=1 WHERE id=?", (hot,))
    board.commit()

    real_enqueue = kb.enqueue_blocker_reconciliation

    def _advance(conn, event_id):
        out = real_enqueue(conn, event_id)
        # Simulate the source re-blocking mid-scan.
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'block_loop_detected', NULL, ?)",
            (hot, int(time.time())),
        )
        return out

    kb.enqueue_blocker_reconciliation = _advance
    try:
        kb.reconcile_orphaned_automation_recovery(board)
    finally:
        kb.enqueue_blocker_reconciliation = real_enqueue

    row = board.execute(
        "SELECT recovery_backfill_pending, recovery_backfill_after FROM tasks WHERE id=?",
        (hot,),
    ).fetchone()
    assert row["recovery_backfill_pending"] == 1
    assert row["recovery_backfill_after"] > 0


def test_a_board_created_before_the_lane_still_migrates_and_reconciles(
    home, set_reconciler, tmp_path
):
    """Mixed-deployment path: an older DB gains the cursor + trigger in place."""
    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    raw.close()
    conn = kb.connect(legacy)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert {"recovery_backfill_pending", "recovery_backfill_after"} <= cols
        triggers = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert kb.RECONCILIATION_BACKFILL_TRIGGER_NAME in triggers

        tid = kb.create_task(conn, title="legacy stall", assignee="alice")
        assert kb.claim_task(conn, tid, claimer="alice") is not None
        assert kb.block_task(conn, tid, reason="stall", kind="transient")
        assert _owners(conn, tid)
        # The trigger armed the durable cursor for the pre-hook path too.
        assert conn.execute(
            "SELECT recovery_backfill_pending FROM tasks WHERE id=?", (tid,)
        ).fetchone()["recovery_backfill_pending"] == 1
    finally:
        conn.close()


def test_an_ordinary_dispatch_tick_drives_the_recovery_passes(board, monkeypatch):
    source_id = _machine_stall(board)
    for row in _owners(board, source_id):
        board.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    board.execute(
        "UPDATE tasks SET recovery_backfill_pending=1 WHERE id=?", (source_id,)
    )
    board.commit()

    kb.dispatch_once(board, spawn_fn=lambda *a, **kw: None, max_spawn=0)
    assert len(_owners(board, source_id)) == 1
