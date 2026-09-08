"""Operator-facing projection: no Triage/Review lanes, honest dependencies.

Triage and Review are internal lifecycle stages. Triage is transient intake
that automation assigns and consumes; Review is a stage of work in flight. An
operator asked to look at either of them is being asked to do the machine's
routing — which is what "triage no longer drains" looked like from the desk.

So the operator sees one projection of the durable lifecycle:

  * ``review`` and a live-owned ``triage`` recovery are **In Progress**;
  * a ``triage`` occurrence with no live owner is **Blocked**, owned by the
    machine, with a next action — never "Kevin must classify this";
  * only an affirmed typed gate is **Blocked / needs Kevin**;
  * scheduled holds stay their own waiting lane;
  * the durable status is never rewritten, only projected, and travels with
    the payload as ``lifecycle_status`` so the board stays inspectable.

Dependency labels get the same treatment: a parent is shown by title with a
real state, "Unknown" is never a label, and the transitive walk is bounded.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_health as kh


ENABLED = {"enabled": True, "profile": "code-crab", "max_active": 2}


@pytest.fixture
def set_reconciler(monkeypatch):
    import hermes_cli.config as config_module

    state = {"cfg": {"kanban": {"blocker_reconciler": dict(ENABLED)}}}
    monkeypatch.setattr(
        config_module, "load_config", lambda *a, **k: dict(state["cfg"]),
    )

    def _set(**overrides):
        merged = dict(ENABLED)
        merged.update(overrides)
        state["cfg"] = {"kanban": {"blocker_reconciler": merged}}

    return _set


@pytest.fixture
def board(tmp_path, monkeypatch, set_reconciler):
    from hermes_cli import profiles as profiles_module

    monkeypatch.setattr(
        profiles_module, "profile_exists",
        lambda name: str(name).strip().lower() in {"code-crab", "alice", "default"},
    )
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
    ):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        yield conn
    finally:
        conn.close()


def _project(conn, task_id):
    return kh.project_operator_state(conn, kb.get_task(conn, task_id))


def _stall(conn, *, title="stalled", kind="transient"):
    tid = kb.create_task(conn, title=title, assignee="alice", body="src")
    assert kb.claim_task(conn, tid, claimer="alice") is not None
    assert kb.block_task(conn, tid, reason="provider timed out", kind=kind)
    return tid


# ---------------------------------------------------------------------------
# 1 — Triage and Review are not operator columns
# ---------------------------------------------------------------------------


def test_triage_and_review_are_never_operator_columns():
    assert "triage" not in kh.OPERATOR_COLUMNS
    assert "review" not in kh.OPERATOR_COLUMNS
    # The lanes that remain are the ones an operator can actually reason about.
    assert kh.OPERATOR_COLUMNS == (
        "todo", "scheduled", "ready", "running", "blocked", "done",
    )


def test_a_card_in_review_is_an_in_progress_stage(board):
    tid = kb.create_task(board, title="implemented", assignee="alice")
    claimed = kb.claim_task(board, tid, claimer="alice")
    assert kb.request_review(
        board, tid, summary="please review", expected_run_id=claimed.current_run_id,
    )

    projection = _project(board, tid)
    assert projection.column == "running"
    assert projection.state == "in_progress"
    assert projection.stage == "review"
    assert projection.owner == "machine"
    # The durable row is untouched and still inspectable.
    assert projection.lifecycle_status == "review"
    assert kb.get_task(board, tid).status == "review"


def test_every_stored_status_projects_into_an_operator_lane(board):
    """No lifecycle status may fall through into an invented lane."""
    tid = kb.create_task(board, title="probe", assignee="alice")
    for status in kb.VALID_STATUSES:
        board.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        board.commit()
        projection = _project(board, tid)
        assert projection.lifecycle_status == status
        if status == "archived":
            assert projection.column == "archived"
        else:
            assert projection.column in kh.OPERATOR_COLUMNS, status
        assert projection.state in {
            "in_progress", "blocked", "waiting", "done", "archived",
        }


def test_an_unknown_future_status_fails_closed_into_blocked(board):
    """A status this projection has never seen is not silently 'todo'."""
    tid = kb.create_task(board, title="probe", assignee="alice")
    board.execute("UPDATE tasks SET status='quantum' WHERE id=?", (tid,))
    board.commit()

    projection = _project(board, tid)
    assert projection.column == "blocked"
    assert projection.owner == "machine"
    assert projection.lifecycle_status == "quantum"
    assert projection.action


# ---------------------------------------------------------------------------
# 2 — machine failure is never "Needs Kevin"
# ---------------------------------------------------------------------------


def test_a_recovery_source_with_a_live_owner_shows_recovery_in_progress(board):
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    assert owner_id is not None
    owner = kb.claim_task(board, owner_id, claimer="code-crab")
    assert owner is not None

    projection = _project(board, source_id)
    assert projection.column == "running"
    assert projection.state == "in_progress"
    assert projection.stage == "recovery"
    assert projection.owner == "machine"
    assert projection.reason_code == kh.REASON_RECOVERY_IN_PROGRESS
    # Evidence, not a hidden count or a fabricated source run.
    assert projection.evidence["recovery_task_id"] == owner_id
    assert projection.evidence["recovery_run_id"] == int(owner.current_run_id)
    assert projection.lifecycle_status == "triage"


def test_a_recovery_source_without_a_live_owner_is_machine_owned_blocked(board):
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    board.execute("UPDATE tasks SET status='archived' WHERE id=?", (owner_id,))
    board.commit()

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.state == "blocked"
    assert projection.owner == "machine", (
        "a stalled machine recovery was presented as human attention"
    )
    assert projection.action, "a machine-owned block must name its next action"
    assert projection.next_owner
    assert projection.lifecycle_status == "triage"


def test_a_queued_recovery_owner_is_blocked_not_recovery_in_progress(board):
    """Owning an occurrence is not executing it.

    A minted-but-unclaimed owner means the lane accepted the occurrence; it
    does not mean anything is running. Rendering it "Recovery in progress"
    told the operator a worker was on the card when no claim existed at all.
    """
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    assert kb.get_task(board, owner_id).status in {"ready", "todo"}

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.state == "blocked"
    assert projection.stage == "recovery"
    assert projection.owner == "machine"
    assert projection.reason_code == kh.REASON_RECOVERY_QUEUED
    assert projection.next_owner == "blocker-reconciler"
    # The owner card is named, so the operator can open the exact card.
    assert projection.evidence["recovery_task_id"] == owner_id
    assert owner_id in (projection.action or "")


def test_a_queued_owner_still_owns_the_occurrence_for_dedupe(board):
    """Projection honesty must not weaken duplicate-owner exclusion."""
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    owner = kb.get_task(board, owner_id)
    assert owner.status in {"ready", "todo"}
    # Still a live owner for coalescing: a repeat occurrence must land on this
    # card rather than minting a second one.
    assert kb._recovery_owner_is_live(board, owner) is True
    assert _project(board, source_id).column == "blocked"


def test_a_held_recovery_owner_is_blocked_with_a_machine_reason(board):
    """A scheduled hold is a stopped card, however it got there."""
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    board.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (owner_id,))
    board.commit()

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.owner == "machine"
    assert projection.reason_code == kh.REASON_RECOVERY_HELD
    assert projection.next_owner == "blocker-reconciler"
    assert projection.evidence["recovery_task_id"] == owner_id
    assert projection.action


def test_disabling_the_lane_after_enqueue_stops_reading_as_in_progress(
    board, set_reconciler
):
    """The kill switch scenario: an owner exists, but nothing can claim it."""
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    assert owner_id is not None
    set_reconciler(enabled=False)
    # Proof there is no forward path: the claim boundary refuses it.
    assert kb.claim_task(board, owner_id, claimer="code-crab") is None

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.state == "blocked"
    assert projection.owner == "machine"
    assert projection.reason_code == kh.REASON_RECOVERY_DISABLED
    assert projection.next_owner == "blocker-reconciler"
    assert projection.action
    # The queued owner card is described, never cancelled.
    assert kb.get_task(board, owner_id).status in {"ready", "todo"}


def test_a_running_owner_whose_claim_holds_is_the_only_recovery_in_progress(board):
    source_id = _stall(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    owner = kb.claim_task(board, owner_id, claimer="code-crab")
    assert owner is not None and kb.get_task(board, owner_id).status == "running"

    projection = _project(board, source_id)
    assert projection.column == "running"
    assert projection.state == "in_progress"
    assert projection.reason_code == kh.REASON_RECOVERY_IN_PROGRESS
    assert projection.evidence["recovery_run_id"] == int(owner.current_run_id)


def test_a_plain_intake_triage_card_is_never_left_for_kevin_to_classify(board):
    """Transient intake with nobody to consume it is a machine-owned block."""
    tid = kb.create_task(board, title="fresh intake", assignee="alice", triage=True)

    projection = _project(board, tid)
    assert projection.column in {"running", "blocked"}
    assert projection.owner == "machine"
    assert projection.column != "triage"


def test_only_an_affirmed_typed_gate_reads_as_needs_kevin(board):
    tid = kb.create_task(board, title="needs a decision", assignee="alice")
    claimed = kb.claim_task(board, tid, claimer="alice")
    assert kb.block_task(
        board, tid, reason="which envelope should ship?", kind="needs_input",
        expected_run_id=claimed.current_run_id,
    )
    # Unaffirmed: a worker's claim is not a gate. The card is machine-owned
    # whichever lane it is showing in — automation still owns the next move.
    unaffirmed = _project(board, tid)
    assert unaffirmed.owner == "machine"
    assert unaffirmed.column != "blocked" or unaffirmed.owner != "kevin"

    assert kh.affirm_human_gate(board, tid, evidence={
        "type": "human_decision",
        "action": "Confirm the approved subject line",
        "affirmed_by": "Kevin Yan",
        "affirmed_at": int(time.time()),
    }, reason="which envelope should ship?")
    affirmed = _project(board, tid)
    assert affirmed.column == "blocked"
    assert affirmed.owner == "kevin"
    assert affirmed.action == "Confirm the approved subject line"
    assert affirmed.reason_code == kh.REASON_AFFIRMED_GATE


def test_a_scheduled_hold_keeps_its_own_waiting_lane(board):
    tid = kb.create_task(board, title="wake later", assignee="alice")
    board.execute(
        "UPDATE tasks SET status='scheduled', hold_kind='wake', hold_wake_at=? "
        "WHERE id=?",
        (int(time.time()) + 3600, tid),
    )
    board.commit()

    projection = _project(board, tid)
    assert projection.column == "scheduled"
    assert projection.state == "waiting"
    assert projection.owner != "kevin"


# ---------------------------------------------------------------------------
# 3 — columns, counts, filters and the detail view agree
# ---------------------------------------------------------------------------


def _board_shape(conn):
    """Every non-archived card, bucketed exactly as the operator sees it."""
    columns = {name: [] for name in kh.OPERATOR_COLUMNS}
    for task in kb.list_tasks(conn, include_archived=False):
        projection = kh.project_operator_state(conn, task)
        columns[projection.column].append(task.id)
    return columns


def test_no_card_is_hidden_and_no_lane_is_invented(board):
    review_id = kb.create_task(board, title="in review", assignee="alice")
    claimed = kb.claim_task(board, review_id, claimer="alice")
    kb.request_review(
        board, review_id, summary="x", expected_run_id=claimed.current_run_id,
    )
    source_id = _stall(board)
    kb.create_task(board, title="waiting", assignee="alice")

    columns = _board_shape(board)
    placed = [tid for ids in columns.values() for tid in ids]
    live = [t.id for t in kb.list_tasks(board, include_archived=False)]
    assert sorted(placed) == sorted(live), (
        "merely hiding stranded cards is not acceptance"
    )
    assert set(columns) == set(kh.OPERATOR_COLUMNS)
    assert review_id in columns["running"]
    # The stalled source has a queued owner, which is ownership without
    # execution: visible in Blocked, machine-owned, never hidden.
    assert source_id in columns["blocked"]


def test_the_counts_the_operator_sees_are_the_columns_they_see(board):
    _stall(board)
    kb.create_task(board, title="waiting", assignee="alice")

    columns = _board_shape(board)
    counts = kh.operator_column_counts(board)
    assert counts == {name: len(ids) for name, ids in columns.items()}
    assert "triage" not in counts and "review" not in counts


# ---------------------------------------------------------------------------
# 4 — dependency labels tell the truth, by title, bounded
# ---------------------------------------------------------------------------


def _labels(conn, task_id):
    return kh.dependency_labels(conn, task_id)


def test_a_dependency_is_shown_by_title_not_by_bare_id(board):
    parent = kb.create_task(board, title="Restore the provider credential",
                            assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])

    labels = _labels(board, child)["parents"]
    assert [entry["title"] for entry in labels] == [
        "Restore the provider credential"
    ]
    assert labels[0]["task_id"] == parent


def test_a_running_parent_reads_in_progress_and_never_unknown(board):
    parent = kb.create_task(board, title="upstream work", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    assert kb.claim_task(board, parent, claimer="alice") is not None

    entry = _labels(board, child)["parents"][0]
    assert entry["state"] == "in_progress"
    assert entry["label"] == "In progress"
    assert entry["available"] is True
    assert "unknown" not in entry["label"].lower()


def test_a_todo_behind_an_unfinished_parent_is_waiting_on_dependency(board):
    parent = kb.create_task(board, title="upstream work", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    assert kb.get_task(board, child).status == "todo"

    labels = _labels(board, child)
    assert labels["label"] == "Waiting on dependency"
    assert labels["blocking"] == [parent]
    projection = _project(board, child)
    assert projection.column == "todo"
    assert projection.state == "waiting"


def test_a_parent_in_review_reads_as_in_progress_for_its_child(board):
    parent = kb.create_task(board, title="upstream work", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    claimed = kb.claim_task(board, parent, claimer="alice")
    kb.request_review(
        board, parent, summary="x", expected_run_id=claimed.current_run_id,
    )

    entry = _labels(board, child)["parents"][0]
    assert entry["state"] == "in_progress"
    assert entry["label"] == "In progress"


def test_an_archived_parent_is_distinguishably_unavailable(board):
    parent = kb.create_task(board, title="retired work", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    board.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
    board.commit()

    entry = _labels(board, child)["parents"][0]
    assert entry["available"] is False
    assert entry["state"] == "unavailable"
    assert entry["reason"] == "parent_archived"
    assert entry["title"] == "retired work"
    assert entry["label"] != "Waiting on dependency"


def test_a_missing_parent_is_distinguishable_from_an_archived_one(board):
    parent = kb.create_task(board, title="deleted work", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    board.execute("DELETE FROM tasks WHERE id=?", (parent,))
    board.commit()

    entries = _labels(board, child)["parents"]
    assert len(entries) == 1
    assert entries[0]["available"] is False
    assert entries[0]["reason"] == "parent_missing"
    assert entries[0]["task_id"] == parent


def test_the_transitive_root_is_bounded_and_says_so(board):
    chain = [kb.create_task(board, title="root", assignee="alice")]
    for depth in range(1, 12):
        chain.append(kb.create_task(
            board, title=f"level {depth}", assignee="alice",
            parents=[chain[-1]],
        ))
    labels = _labels(board, chain[-1])

    assert labels["root"] is not None
    assert labels["root"]["title"]
    assert labels["root_truncated"] is True, (
        "an unbounded ancestor walk is what a deep graph turns into a hang"
    )
    assert labels["root_depth"] <= kh.MAX_DEPENDENCY_ROOT_DEPTH


def test_a_short_chain_reports_its_real_root(board):
    root = kb.create_task(board, title="the real root", assignee="alice")
    middle = kb.create_task(board, title="middle", assignee="alice", parents=[root])
    leaf = kb.create_task(board, title="leaf", assignee="alice", parents=[middle])

    labels = _labels(board, leaf)
    assert labels["root"]["task_id"] == root
    assert labels["root"]["title"] == "the real root"
    assert labels["root_truncated"] is False


def test_a_dependency_cycle_cannot_hang_the_ancestor_walk(board):
    a = kb.create_task(board, title="a", assignee="alice")
    b = kb.create_task(board, title="b", assignee="alice", parents=[a])
    # Forged directly: link_tasks rejects cycles, but a legacy row can carry one.
    board.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (b, a),
    )
    board.commit()

    labels = _labels(board, b)
    assert labels["root"] is not None
    assert labels["root_truncated"] is True


def test_a_task_with_no_parents_reports_no_dependency_wait(board):
    tid = kb.create_task(board, title="standalone", assignee="alice")

    labels = _labels(board, tid)
    assert labels["parents"] == []
    assert labels["blocking"] == []
    assert labels["label"] is None
    assert labels["root"] is None


# ---------------------------------------------------------------------------
# 5 — a circuit-broken source with a live owner is recovery, not a dead stop
# ---------------------------------------------------------------------------
#
# ``_record_task_failure`` trips the breaker by setting the source ``blocked``
# and emitting ``gave_up`` in ONE transaction — and that ``gave_up`` is a
# reconciliation occurrence, so the pre-COMMIT drain mints a recovery owner in
# the same transaction. The source therefore lands ``blocked`` *with* a live
# owner already working it.
#
# Projecting that as a flat Blocked is the same lie the triage lane used to
# tell, wearing a different status: automation is demonstrably on it, and the
# board says nothing is happening. Recovery evidence already decides this for a
# ``triage`` source; a ``blocked`` source with no affirmed gate is the same
# question and must get the same answer.


def _gave_up(conn, *, title="circuit broken"):
    tid = kb.create_task(conn, title=title, assignee="alice")
    assert kb.claim_task(conn, tid, claimer="alice") is not None
    assert kb._record_task_failure(
        conn, tid, error="provider refused six times", outcome="crashed",
        force_trip=True, release_claim=True, end_run=True,
    ) is True
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def test_a_gave_up_source_with_a_queued_owner_is_machine_owned_blocked(board):
    """The breaker mints an owner in the same txn — but nothing is running."""
    source_id = _gave_up(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    assert owner_id is not None, "gave_up minted no recovery owner"
    assert kb.get_task(board, owner_id).status in {"ready", "todo"}

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.state == "blocked"
    assert projection.stage == "recovery"
    assert projection.owner == "machine", (
        "a machine failure with a queued owner must not read as Needs Kevin"
    )
    assert projection.reason_code == kh.REASON_RECOVERY_QUEUED
    assert projection.evidence["recovery_task_id"] == owner_id
    assert projection.action
    # The durable row is untouched and still inspectable.
    assert projection.lifecycle_status == "blocked"
    assert kb.get_task(board, source_id).status == "blocked"


def test_a_gave_up_source_with_a_claimed_owner_reads_as_recovery(board):
    source_id = _gave_up(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    owner = kb.claim_task(board, owner_id, claimer="code-crab")
    assert owner is not None

    projection = _project(board, source_id)
    assert projection.column == "running"
    assert projection.stage == "recovery"
    assert projection.evidence["recovery_run_id"] == int(owner.current_run_id)
    assert projection.next_owner == owner.assignee


def test_a_gave_up_source_whose_owner_died_falls_back_to_blocked(board):
    """No live owner is a real stop, and it names a next action."""
    source_id = _gave_up(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    board.execute("UPDATE tasks SET status='archived' WHERE id=?", (owner_id,))
    board.commit()

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.owner == "machine"
    assert projection.action
    assert projection.next_owner


def test_an_affirmed_gate_outranks_a_live_recovery_owner(board):
    """A held gate stays stopped even while an owner card is still alive.

    Recovery evidence must never be able to project an operator's own decision
    back into "in progress".
    """
    source_id = _stall(board, kind="needs_input")
    owner_id = kb.active_recovery_owner(board, source_id)
    assert owner_id is not None, "the stalled source has no live owner to outrank"
    assert kh.affirm_human_gate(board, source_id, evidence={
        "type": "human_decision",
        "action": "Confirm the approved subject line",
        "affirmed_by": "Kevin Yan",
        "affirmed_at": int(time.time()),
    }, reason="needs a decision")
    # The owner card is untouched and still live.
    assert kb.get_task(board, owner_id).status in {"ready", "todo"}

    projection = _project(board, source_id)
    assert projection.column == "blocked"
    assert projection.owner == "kevin"
    assert projection.action == "Confirm the approved subject line"


def test_the_board_places_a_recovering_gave_up_source_in_progress(board):
    """Columns and counts follow the projection, as everywhere else."""
    source_id = _gave_up(board)
    owner_id = kb.active_recovery_owner(board, source_id)
    assert kb.claim_task(board, owner_id, claimer="code-crab") is not None

    columns = _board_shape(board)
    assert source_id in columns["running"]
    assert kh.operator_column_counts(board)["running"] >= 1


def test_the_board_leaves_a_queued_gave_up_source_in_blocked(board):
    """Counts must not manufacture progress out of a queued owner card."""
    source_id = _gave_up(board)

    columns = _board_shape(board)
    assert source_id in columns["blocked"]
    assert source_id not in columns["running"]
