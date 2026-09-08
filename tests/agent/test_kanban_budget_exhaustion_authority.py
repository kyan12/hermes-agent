"""Only the live worker may charge a failure to the task it is working on.

``finalize_turn`` records a terminal ``timed_out`` outcome when a kanban
worker exhausts its iteration budget. It used to select the victim with a bare
``os.environ.get("HERMES_KANBAN_TASK")`` — the same mistake the turn-end stop
guard made, with far worse consequences.

A kanban worker is an ordinary agent whose toolset includes ``cronjob``, and
``cronjob(action="run")`` executes in the worker's own process. ``delegate_task``
children do too. Either can exhaust ITS OWN iteration budget while the parent
worker is mid-run, inherit the parent's ``HERMES_KANBAN_*`` environment, and
charge a failure to the parent's card: increment ``consecutive_failures``, trip
the circuit breaker, emit ``gave_up``, block the card, and close the parent's
still-live run out from under it.

Authority is the same single predicate everything else uses
(``live_dispatcher_worker_task``), and the mutation itself carries the run id
and claim lock so it fails closed inside the write transaction too.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


logger = logging.getLogger(__name__)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """A dispatcher-spawned worker: live claim on a real board."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="long worker task", assignee="alice")
        claimed = kb.claim_task(conn, tid, claimer="alice")
        assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)
        yield {
            "conn": conn, "task_id": tid,
            "run_id": int(claimed.current_run_id),
            "claim_lock": claimed.claim_lock,
            "monkeypatch": monkeypatch,
        }
    finally:
        conn.close()


def _exhaust(task_id: str):
    from agent.turn_finalizer import _record_kanban_budget_exhausted

    return _record_kanban_budget_exhausted(task_id, 200, 200, logger)


def _state(conn, tid):
    task = kb.get_task(conn, tid)
    return {
        "status": task.status,
        "failures": task.consecutive_failures,
        "run_id": task.current_run_id,
        "claim_lock": task.claim_lock,
    }


def _gave_up(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]


def test_the_live_worker_still_records_its_own_exhaustion(worker):
    """The real behaviour must survive the authority check."""
    conn, tid = worker["conn"], worker["task_id"]
    before = _state(conn, tid)
    assert before["status"] == "running"

    _exhaust(tid)

    after = _state(conn, tid)
    assert after["failures"] == before["failures"] + 1
    run = conn.execute(
        "SELECT status, outcome, ended_at FROM task_runs WHERE id=?",
        (worker["run_id"],),
    ).fetchone()
    assert run["ended_at"] is not None
    assert run["outcome"] == "timed_out"


def test_an_in_process_cron_tick_cannot_charge_the_workers_card(worker):
    """`cronjob(action="run")` runs inside the worker's own process."""
    from agent.delegation_context import non_dispatcher_owned_context

    conn, tid = worker["conn"], worker["task_id"]
    before = _state(conn, tid)

    with non_dispatcher_owned_context():
        _exhaust(tid)

    assert _state(conn, tid) == before, (
        "a cron tick charged its own budget exhaustion to the worker's card"
    )
    assert _gave_up(conn, tid) == []
    run = conn.execute(
        "SELECT ended_at FROM task_runs WHERE id=?", (worker["run_id"],),
    ).fetchone()
    assert run["ended_at"] is None, "the worker's live run was closed under it"


def test_a_delegated_child_cannot_charge_the_parents_card(worker):
    from agent.delegation_context import delegated_child_context

    conn, tid = worker["conn"], worker["task_id"]
    before = _state(conn, tid)

    with delegated_child_context():
        _exhaust(tid)

    assert _state(conn, tid) == before
    assert _gave_up(conn, tid) == []


def test_a_stale_binding_from_a_finished_run_charges_nothing(worker):
    """A desktop follow-up inherits the env of a run that already closed."""
    conn, tid = worker["conn"], worker["task_id"]
    assert kb.complete_task(
        conn, tid, summary="done", expected_run_id=worker["run_id"],
    )
    before = _state(conn, tid)

    _exhaust(tid)

    assert _state(conn, tid) == before
    assert kb.get_task(conn, tid).status == "done"
    assert _gave_up(conn, tid) == []


def test_a_run_id_mismatch_charges_nothing(worker):
    """A newer run of the same card is not this process's authority."""
    conn, tid = worker["conn"], worker["task_id"]
    worker["monkeypatch"].setenv(
        "HERMES_KANBAN_RUN_ID", str(worker["run_id"] + 99),
    )
    before = _state(conn, tid)

    _exhaust(tid)

    assert _state(conn, tid) == before
    assert _gave_up(conn, tid) == []


def test_a_foreign_claim_lock_charges_nothing(worker):
    conn, tid = worker["conn"], worker["task_id"]
    worker["monkeypatch"].setenv("HERMES_KANBAN_CLAIM_LOCK", "someone-elses")
    before = _state(conn, tid)

    _exhaust(tid)

    assert _state(conn, tid) == before
    assert _gave_up(conn, tid) == []


def test_the_kernel_itself_refuses_a_mismatched_run(worker):
    """Belt and braces: the guard is inside the write transaction too.

    An env check alone is a check-then-act. The claim can be reaped between
    the check and the UPDATE, and the process that reclaimed the task is then
    the one whose run gets closed.
    """
    conn, tid = worker["conn"], worker["task_id"]
    before = _state(conn, tid)

    assert kb._record_task_failure(
        conn, tid, error="not mine", outcome="timed_out",
        release_claim=True, end_run=True,
        expected_run_id=worker["run_id"] + 99,
        claim_lock=worker["claim_lock"],
    ) is False
    assert _state(conn, tid) == before

    assert kb._record_task_failure(
        conn, tid, error="not mine", outcome="timed_out",
        release_claim=True, end_run=True,
        expected_run_id=worker["run_id"],
        claim_lock="someone-elses",
    ) is False
    assert _state(conn, tid) == before


def test_a_reaped_run_row_is_refused_by_the_kernel(worker):
    """``tasks`` mirrors the claim; ``task_runs`` is where it settles."""
    conn, tid = worker["conn"], worker["task_id"]
    conn.execute(
        "UPDATE task_runs SET status='reclaimed', ended_at=?, claim_lock=NULL, "
        "claim_expires=NULL WHERE id=?",
        (int(time.time()), worker["run_id"]),
    )
    conn.commit()
    before = _state(conn, tid)

    assert kb._record_task_failure(
        conn, tid, error="reaped", outcome="timed_out",
        release_claim=True, end_run=True,
        expected_run_id=worker["run_id"], claim_lock=worker["claim_lock"],
    ) is False
    assert _state(conn, tid) == before


def test_existing_dispatcher_callers_are_unaffected(worker):
    """Passing no run/claim keeps the pre-existing unconditional behaviour.

    The dispatcher's own reap paths legitimately act on a task whose claim
    they have already released; they must not start failing closed.
    """
    conn, tid = worker["conn"], worker["task_id"]

    assert kb._record_task_failure(
        conn, tid, error="dispatcher reap", outcome="crashed",
        force_trip=True, release_claim=True, end_run=True,
    ) is True
    assert kb.get_task(conn, tid).status == "blocked"
