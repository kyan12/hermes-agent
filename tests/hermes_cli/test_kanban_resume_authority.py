"""Explicit, single-use authority to resume one task on one existing PR.

``check_respawn_guard`` refuses to respawn a task whose recent comments carry a
GitHub PR URL. That guard has no exit, so a worker that died *mid-PR* — branch
pushed, PR open, work unfinished — parks its card forever. The only escapes were
to weaken the guard for every task or to do the work off the board.

A resume receipt is the third option, and its whole value is in what it does NOT
do. It bypasses ``active_pr`` and nothing else; it names one board, one task, one
terminal run and one pull request; it is spent inside the claim that uses it; and
it stops being valid the moment the evidence it was issued against changes.

The negative cases below are the specification. Each one is a way the authority
could leak — a stale receipt, another task's, another run's, another board's, a
second PR appearing, a writer still running — and each must refuse.
"""

from __future__ import annotations

import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_resume as kr

PR = "https://github.com/NousResearch/hermes-agent/pull/4242"
OTHER_PR = "https://github.com/NousResearch/hermes-agent/pull/9999"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: tmp_path / "kanban.db")
    kb.init_db()
    conn = kbc.connect()
    try:
        yield conn
    finally:
        conn.close()


def _parked_on_a_pr(conn, *, pr: str = PR) -> tuple[str, int]:
    """A task whose worker opened ``pr`` and then died: run ended, task ready."""
    task_id = kb.create_task(conn, title="mid-PR crash", assignee="a")
    run_id = _finished_run(conn, task_id)
    kb.add_comment(conn, task_id, "worker", f"opened {pr}")
    conn.execute("UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?", (task_id,))
    return task_id, run_id


def _finished_run(conn, task_id: str, *, outcome: str = "crashed") -> int:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, 'crashed', ?, ?, ?)",
        (task_id, now - 600, now - 300, outcome),
    )
    return int(cur.lastrowid)


def _issue(conn, task_id, run_id=None, pr=PR, *, attested=True, mutate=None, **kw):
    """Issue the way the authenticated surface does: preview, then confirm it."""
    expected = dict(kr.preview(conn, task_id)["expected"])
    if run_id is not None:
        expected["run_id"] = run_id
    if pr is not None:
        expected["pr_url"] = pr
    if mutate:
        expected.update(mutate)
    return kr.issue(conn, task_id, expected=expected,
                    issued_by="dashboard:test:operator", attested=attested, **kw)


# --- the guard this authorises ----------------------------------------------

def test_a_parked_card_is_guarded_without_a_receipt(board):
    """The premise. If this ever stops firing, everything below tests nothing."""
    task_id, _ = _parked_on_a_pr(board)
    assert kbd.check_respawn_guard(board, task_id) == "active_pr"
    assert kbd.evaluate_respawn_guard(board, task_id) == (("active_pr"), None)


def test_the_guards_own_verdict_ignores_the_receipt(board):
    """``check_respawn_guard`` answers "is this held, and why", not "may it run".

    Keeping the two apart is what lets diagnostics keep saying "parked on a PR"
    while the spawn path acts on an authorisation, instead of the card silently
    reading as unguarded to everyone.
    """
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    assert kbd.check_respawn_guard(board, task_id) == "active_pr"
    assert kbd.evaluate_respawn_guard(board, task_id).reason is None


def test_a_receipt_clears_only_the_active_pr_guard(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    decision = kbd.evaluate_respawn_guard(board, task_id)
    assert decision.reason is None
    assert decision.resume_receipt_id == receipt


def test_a_receipt_does_not_clear_a_quota_blocker(board):
    """Authority to continue a PR is not authority to burn a rate-limited key."""
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    board.execute(
        "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
        ("Error: 403 forbidden - quota exhausted", task_id),
    )
    assert kbd.check_respawn_guard(board, task_id) == "blocker_auth"


def test_a_receipt_does_not_clear_the_recent_success_window(board):
    """Nor authority to redo work that just succeeded."""
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    now = int(time.time())
    board.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, 'done', ?, ?, 'completed')", (task_id, now - 60, now - 30),
    )
    assert kbd.check_respawn_guard(board, task_id) == "recent_success"


# --- what a receipt refuses to be issued for --------------------------------

def test_a_running_task_cannot_be_authorised(board):
    """A live writer is the one case where a second worker is guaranteed harm."""
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    with pytest.raises(kr.ResumeAuthorityError, match="running"):
        _issue(board, task_id, run_id)


def test_a_claimed_task_cannot_be_authorised(board):
    task_id, run_id = _parked_on_a_pr(board)
    board.execute(
        "UPDATE tasks SET claim_lock = 'someone', claim_expires = ? WHERE id = ?",
        (int(time.time()) + 600, task_id),
    )
    with pytest.raises(kr.ResumeAuthorityError, match="claimed"):
        _issue(board, task_id, run_id)


def test_a_task_with_an_unfinished_run_cannot_be_authorised(board):
    """The task row and the run rows can disagree; either one refuses."""
    task_id, run_id = _parked_on_a_pr(board)
    board.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
        (task_id, int(time.time())),
    )
    with pytest.raises(kr.ResumeAuthorityError, match="not ended"):
        _issue(board, task_id, run_id)


def test_an_unfinished_run_cannot_be_the_resumed_run(board):
    task_id, _ = _parked_on_a_pr(board)
    cur = board.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
        (task_id, int(time.time())),
    )
    with pytest.raises(kr.ResumeAuthorityError):
        _issue(board, task_id, int(cur.lastrowid))


def test_another_tasks_run_cannot_be_authorised(board):
    """Wrong-run and wrong-task are the same refusal: a run belongs to one card."""
    task_id, _ = _parked_on_a_pr(board)
    other_id = kb.create_task(board, title="unrelated", assignee="a")
    other_run = _finished_run(board, other_id)
    with pytest.raises(kr.ResumeAuthorityError, match="run_id"):
        _issue(board, task_id, other_run)


def test_a_pr_the_task_never_mentioned_cannot_be_authorised(board):
    task_id, run_id = _parked_on_a_pr(board)
    with pytest.raises(kr.ResumeAuthorityError, match="confirmation named"):
        _issue(board, task_id, run_id, pr=OTHER_PR)


def test_a_task_with_two_pull_requests_cannot_be_authorised(board):
    """A receipt exempts one lineage; the second would go unexamined."""
    task_id, run_id = _parked_on_a_pr(board)
    kb.add_comment(board, task_id, "worker", f"also {OTHER_PR}")
    with pytest.raises(kr.ResumeAuthorityError, match="more than one"):
        _issue(board, task_id, run_id)


def test_a_task_with_no_pr_evidence_cannot_be_authorised(board):
    """Nothing to bypass means the receipt would be a blank cheque."""
    task_id = kb.create_task(board, title="no pr", assignee="a")
    run_id = _finished_run(board, task_id)
    board.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
    with pytest.raises(kr.ResumeAuthorityError, match="no pull request"):
        _issue(board, task_id, run_id, pr=PR)


def test_a_non_pr_url_is_not_a_pull_request(board):
    task_id, run_id = _parked_on_a_pr(board)
    with pytest.raises(kr.ResumeAuthorityError, match="not a GitHub"):
        _issue(board, task_id, run_id, pr="https://github.com/a/b/issues/1")


# --- what invalidates an issued receipt --------------------------------------

def test_a_new_pull_request_invalidates_the_receipt(board):
    """The operator authorised the situation they looked at, not a later one."""
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    assert kbd.evaluate_respawn_guard(board, task_id).reason is None
    kb.add_comment(board, task_id, "worker", f"also opened {OTHER_PR}")
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_another_comment_on_the_same_pr_also_invalidates_the_receipt(board):
    """The digest binds to comment IDS, not to the set of URLs.

    Author and timestamp cannot identify a particular run's comment — two
    workers commenting in the same second are indistinguishable — so the receipt
    binds to the evidence rows themselves. New evidence, even about the same PR,
    is a situation the operator has not seen.
    """
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    kb.add_comment(board, task_id, "worker", f"still working {PR}")
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_an_expired_receipt_is_not_authority(board):
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id, ttl_seconds=1)
    later = int(time.time()) + 60
    assert kr.exempt_reason(board, task_id, now=later) is None


def test_a_revoked_receipt_is_not_authority(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE task_resume_receipts SET revoked_at = ? WHERE id = ?",
                  (int(time.time()), receipt))
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_a_receipt_does_not_authorise_a_different_task(board):
    """Same PR, different card: an authority is not transferable."""
    task_id, run_id = _parked_on_a_pr(board)
    _issue(board, task_id, run_id)
    sibling, _ = _parked_on_a_pr(board)
    assert kbd.evaluate_respawn_guard(board, sibling).reason == "active_pr"


def test_a_receipt_from_another_board_is_not_visible(tmp_path, monkeypatch):
    """There is no cross-board receipt: it lives in the board it authorises."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = tmp_path / "one.db"
    second = tmp_path / "two.db"
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: first)
    kb.init_db()
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: second)
    kb.init_db()

    conn_a = kbc.connect(first)
    conn_b = kbc.connect(second)
    try:
        task_a, run_a = _parked_on_a_pr(conn_a)
        _issue(conn_a, task_a, run_a)
        # The same card id, same PR, on the other board.
        task_b = kb.create_task(conn_b, title="mid-PR crash", assignee="a")
        _finished_run(conn_b, task_b)
        kb.add_comment(conn_b, task_b, "worker", f"opened {PR}")
        conn_b.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_b,))
        assert kbd.evaluate_respawn_guard(conn_b, task_b).reason == "active_pr"
    finally:
        conn_a.close()
        conn_b.close()


# --- single use, consumed in the claim ---------------------------------------

def test_the_receipt_is_spent_by_the_claim_and_not_before(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    assert kbd.evaluate_respawn_guard(board, task_id).reason is None
    spent = board.execute(
        "SELECT consumed_at FROM task_resume_receipts WHERE id = ?", (receipt,)).fetchone()
    assert spent["consumed_at"] is None, "the guard check spent the receipt"

    claimed = kb.claim_task(board, task_id, resume_receipt_id=receipt)
    assert claimed is not None
    row = board.execute(
        "SELECT consumed_at, consumed_run_id FROM task_resume_receipts WHERE id = ?",
        (receipt,)).fetchone()
    assert row["consumed_at"] is not None
    assert row["consumed_run_id"] == claimed.current_run_id


def test_a_spent_receipt_does_not_authorise_a_second_respawn(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is not None
    # Back to ready, as a crash would leave it.
    board.execute(
        "UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?",
        (task_id,))
    board.execute("UPDATE task_runs SET ended_at=?, outcome='crashed' WHERE task_id=? "
                  "AND ended_at IS NULL", (int(time.time()), task_id))
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_a_claim_with_an_invalidated_receipt_refuses_rather_than_spawning(board):
    """The window between deciding and claiming is the one that must not leak.

    The guard passed *because of* this receipt. If a comment lands before the
    claim, claiming anyway is a duplicate PR authorised by nothing — so the
    claim itself re-validates and refuses.
    """
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    assert kbd.evaluate_respawn_guard(board, task_id).reason is None

    kb.add_comment(board, task_id, "worker", f"and now {OTHER_PR}")

    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    assert board.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "ready"
    kinds = [r["kind"] for r in board.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]
    assert "claim_rejected" in kinds


def test_the_receipt_is_validated_before_a_dangling_run_is_reclaimed(board):
    """Reclaiming first would erase the state the validation reads.

    ``_reclaim_dangling_run`` marks an unfinished run terminal and clears its
    pid without proving its writer stopped. A refused claim must leave that run
    exactly as it found it, so the next look still sees a live writer.
    """
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    cur = board.execute(
        "INSERT INTO task_runs (task_id, status, started_at, worker_pid) "
        "VALUES (?, 'running', ?, 4242)", (task_id, int(time.time())))
    dangling = int(cur.lastrowid)
    board.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (dangling, task_id))
    kb.add_comment(board, task_id, "worker", f"and now {OTHER_PR}")

    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    row = board.execute(
        "SELECT status, ended_at, worker_pid FROM task_runs WHERE id = ?", (dangling,)).fetchone()
    assert row["ended_at"] is None, "the refused claim reclaimed the dangling run anyway"
    assert row["status"] == "running"
    assert row["worker_pid"] == 4242


def test_a_normal_claim_is_unchanged_by_the_receipt_parameter(board):
    """No receipt, no new behaviour: the ordinary path must not have moved."""
    task_id = kb.create_task(board, title="ordinary", assignee="a")
    board.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
    assert kb.claim_task(board, task_id) is not None


# --- claim-time revalidation (review findings 1 and 2) -----------------------

def test_an_intervening_writer_refuses_the_claim_without_touching_evidence(board):
    """A writer arriving after issuance must refuse the claim on its own.

    Deliberately no new PR comment: an evidence change would refuse for a
    different reason and hide whether the writer was ever looked at.
    """
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute(
        "INSERT INTO task_runs (task_id, status, started_at, worker_pid) "
        "VALUES (?, 'running', ?, ?)", (task_id, int(time.time()), os.getpid()))
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_a_newer_terminal_run_refuses_the_claim(board):
    """The receipt names one attempt; a later one is a different situation."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    _finished_run(board, task_id)
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_the_authorized_run_becoming_unfinished_refuses_the_claim(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE task_runs SET ended_at = NULL WHERE id = ?", (run_id,))
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_a_live_pid_on_a_terminal_run_refuses_issuance(board):
    """Terminal bookkeeping does not prove the process stopped."""
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE task_runs SET worker_pid = ? WHERE id = ?", (os.getpid(), run_id))
    with pytest.raises(kr.ResumeAuthorityError, match="still running"):
        _issue(board, task_id, run_id)


def test_an_unclassifiable_pid_refuses_issuance(board, monkeypatch):
    """Unknown is not clear."""
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET worker_pid = 424242 WHERE id = ?", (task_id,))
    monkeypatch.setattr(kr, "_pid_is_running", lambda pid: None)
    with pytest.raises(kr.ResumeAuthorityError, match="could not be classified"):
        _issue(board, task_id, run_id)


def test_a_dead_pid_does_not_block_issuance(board, monkeypatch):
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET worker_pid = 424242 WHERE id = ?", (task_id,))
    monkeypatch.setattr(kr, "_pid_is_running", lambda pid: False)
    assert _issue(board, task_id, run_id) > 0


# --- content and occurrence binding (review finding 3) -----------------------

def test_editing_a_comment_body_invalidates_the_receipt(board):
    """The digest covers full content, not just the URL it contains.

    "opened <PR>" and "DO NOT RESUME <PR>" are the same URL and opposite
    instructions.
    """
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE task_comments SET body = ? WHERE task_id = ?",
                  (f"DO NOT RESUME {PR}", task_id))
    assert kr.exempt_reason(board, task_id) is None
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_a_non_pr_comment_is_part_of_the_binding(board):
    """A comment with no URL still changes what the operator agreed to."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    kb.add_comment(board, task_id, "human", "hold off, the design changed")
    assert kr.exempt_reason(board, task_id) is None
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_an_older_run_cannot_be_authorized(board):
    """Only the latest terminal attempt, resolved by id."""
    task_id, older = _parked_on_a_pr(board)
    _finished_run(board, task_id)
    with pytest.raises(kr.ResumeAuthorityError, match="run_id"):
        _issue(board, task_id, older)


def test_a_block_unblock_cycle_invalidates_the_receipt(board):
    """A new occurrence is a new situation; the receipt bound the old one."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    kb._append_event(board, task_id, "unblocked", {"status": "ready"})
    assert kr.exempt_reason(board, task_id) is None
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_a_lifecycle_or_assignee_change_invalidates_the_receipt(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET assignee = 'someone-else' WHERE id = ?", (task_id,))
    assert kr.exempt_reason(board, task_id) is None
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_issuance_requires_an_explicit_lineage_attestation(board):
    task_id, run_id = _parked_on_a_pr(board)
    with pytest.raises(kr.ResumeAuthorityError, match="attestation"):
        _issue(board, task_id, run_id, attested=False)


def test_a_confirmation_naming_another_board_is_refused(board):
    task_id, run_id = _parked_on_a_pr(board)
    with pytest.raises(kr.ResumeAuthorityError, match="board_identity"):
        _issue(board, task_id, run_id, mutate={"board_identity": "/elsewhere/kanban.db"})


def test_a_running_card_has_no_preview(board):
    """No transition is manufactured to make a card resumable."""
    task_id, _ = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    with pytest.raises(kr.ResumeAuthorityError, match="running"):
        kr.preview(board, task_id)


# --- the successor's own instructions (review finding 5) ---------------------

def test_the_resumed_worker_is_told_which_pr_it_continues(board):
    """Comments are tail- and char-capped, so the URL can vanish from context."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    claimed = kb.claim_task(board, task_id, resume_receipt_id=receipt)
    assert claimed is not None
    context = kb.build_worker_context(board, task_id)
    assert PR in context
    assert "Do NOT open a new one." in context


def test_an_ordinary_run_says_nothing_about_resume_lineage(board):
    task_id = kb.create_task(board, title="ordinary", assignee="a")
    board.execute("UPDATE tasks SET status='ready', current_run_id=NULL WHERE id=?", (task_id,))
    assert kb.claim_task(board, task_id) is not None
    assert "Authorised resume" not in kb.build_worker_context(board, task_id)


# --- the authenticated operator surface (review finding 6) -------------------
#
# Authority is the dashboard's existing authentication boundary. There is no CLI
# issuance path: absence of a worker environment marker is not operator consent,
# and a process deny-list is not authentication.

def test_there_is_no_cli_issuance_path():
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_parser

    assert "authorize-resume" not in kcli._HANDLERS
    assert "authorize-resume" not in str(kanban_parser._SPECS)


# --- one permitted promotion, and nothing else (cross-review finding 2) ------

def _parked_todo(board):
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    return task_id, run_id


def test_a_todo_receipt_survives_an_ordinary_promotion(board):
    """The advertised todo authorisation has to actually reach a claim.

    A card authorised while waiting on a parent is promoted the ordinary way
    before the dispatcher looks at it. Binding lifecycle and occurrence exactly
    made that promotion invalidate the receipt, so the card stayed held by the
    guard the receipt exists to lift.
    """
    task_id, run_id = _parked_todo(board)
    receipt = _issue(board, task_id, run_id)
    assert kr.exempt_reason(board, task_id) == receipt

    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    kb._append_event(board, task_id, "promoted", {"reason": "parents done"})

    assert kr.exempt_reason(board, task_id) == receipt
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is not None


def test_a_manual_promotion_is_also_permitted(board):
    task_id, run_id = _parked_todo(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    kb._append_event(board, task_id, "promoted_manual", {"actor": "op"})
    assert kr.exempt_reason(board, task_id) == receipt


def test_a_block_unblock_cycle_after_promotion_cannot_reuse_the_receipt(board):
    """The permitted transition is one promotion — not any path back to ready."""
    task_id, run_id = _parked_todo(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    kb._append_event(board, task_id, "promoted", {"reason": "parents done"})
    kb._append_event(board, task_id, "blocked", {"reason": "needs input"})
    kb._append_event(board, task_id, "unblocked", {"status": "ready"})

    assert kr.exempt_reason(board, task_id) is None
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


def test_a_promotion_twice_over_cannot_reuse_the_receipt(board):
    task_id, run_id = _parked_todo(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    kb._append_event(board, task_id, "promoted", {"reason": "one"})
    kb._append_event(board, task_id, "promoted", {"reason": "two"})
    assert kr.exempt_reason(board, task_id) is None


def test_a_ready_receipt_still_refuses_any_occurrence_change(board):
    """Promotion is permitted only out of todo; a ready card has nowhere to go."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    kb._append_event(board, task_id, "promoted", {"reason": "spurious"})
    assert kr.exempt_reason(board, task_id) is None


def test_promotion_does_not_loosen_the_rest_of_the_tuple(board):
    task_id, run_id = _parked_todo(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET status = 'ready', assignee = 'someone-else' WHERE id = ?",
                  (task_id,))
    kb._append_event(board, task_id, "promoted", {"reason": "parents done"})
    assert kr.exempt_reason(board, task_id) is None


# --- a failed claim never spends the receipt (cross-review finding 3) --------

def test_a_lost_claim_cas_does_not_spend_the_receipt(board, monkeypatch):
    """Forced CAS failure, independent of every precheck.

    The consumption commits a few statements before the CAS. Returning None from
    inside the write transaction committed it anyway: single-use authority
    destroyed, no successor run, card still parked.
    """
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    monkeypatch.setattr(kb, "_claim_and_open_run", lambda *a, **kw: None)

    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None

    row = board.execute(
        "SELECT consumed_at, consumed_run_id FROM task_resume_receipts WHERE id = ?",
        (receipt,)).fetchone()
    assert row["consumed_at"] is None, "the lost claim spent the receipt anyway"
    assert row["consumed_run_id"] is None
    assert board.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == "ready"
    # Still usable: the authority survives a claim it never got to use.
    assert kr.exempt_reason(board, task_id) == receipt
    monkeypatch.undo()
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is not None


def test_a_lost_claim_cas_does_not_reclaim_a_dangling_run(board, monkeypatch):
    """The rollback takes the reclaim with it — it only ran to make room."""
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    cur = board.execute(
        "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)",
        (task_id, int(time.time())))
    dangling = int(cur.lastrowid)
    board.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (dangling, task_id))
    board.execute("UPDATE task_runs SET ended_at = ? WHERE id = ?", (int(time.time()), dangling))
    monkeypatch.setattr(kb, "_claim_and_open_run", lambda *a, **kw: None)

    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    assert board.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == dangling


def test_a_stale_claim_lock_refuses_before_the_receipt_is_spent(board):
    """The claim CAS requires ``claim_lock IS NULL``; an expired lock is not null.

    Issuing against one would guarantee a lost CAS, so it refuses at issuance
    rather than spending authority on a claim that cannot succeed.
    """
    task_id, run_id = _parked_on_a_pr(board)
    board.execute("UPDATE tasks SET claim_lock = 'ghost', claim_expires = ? WHERE id = ?",
                  (int(time.time()) - 3600, task_id))
    with pytest.raises(kr.ResumeAuthorityError, match="claimed by ghost"):
        _issue(board, task_id, run_id)


def test_a_claim_lock_appearing_after_issuance_refuses_the_claim(board):
    task_id, run_id = _parked_on_a_pr(board)
    receipt = _issue(board, task_id, run_id)
    board.execute("UPDATE tasks SET claim_lock = 'ghost', claim_expires = ? WHERE id = ?",
                  (int(time.time()) - 3600, task_id))
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    assert board.execute(
        "SELECT consumed_at FROM task_resume_receipts WHERE id = ?",
        (receipt,)).fetchone()["consumed_at"] is None
