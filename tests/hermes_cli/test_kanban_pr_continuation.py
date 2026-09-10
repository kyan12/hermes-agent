"""Prose that merely *mentions* a pull request is not this card's lineage.

``_respawn_guard_reason`` decides the 24-hour ``active_pr`` hold by scanning
comment bodies for a GitHub PR URL. Any body will do: a quote of someone else's
card, a status digest reposted onto this one, a link offered as an example.
None of those say "a worker for THIS task opened THIS pull request", yet each
parks the card for a day — and the receipt path only lifts the hold after a
human looks at it.

The refinement: a PR URL is a discovery hint, never ownership. Without
structured association — a completion contract naming the PR, ``published_pr``
on a run, a recorded ``pr_acceptance`` event — unassociated prose imposes no
hold at all. The test below fabricates no association; the point is precisely
that there is none to find.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pr_association as kba
from hermes_cli import kanban_pr_reconcile as kpr

PR = "https://github.com/NousResearch/hermes-agent/pull/4242"
OTHER_PR = "https://github.com/NousResearch/hermes-agent/pull/9999"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: tmp_path / "kanban.db")
    # The dispatcher refuses to spawn an assignee that is not a real profile.
    (tmp_path / "profiles" / "a").mkdir(parents=True, exist_ok=True)
    kb.init_db()
    conn = kbc.connect()
    try:
        yield conn
    finally:
        conn.close()


def _crashed_on_no_pr_of_its_own(conn, body: str) -> str:
    """A card whose worker died, carrying one comment that merely names ``PR``.

    Terminal run, back on the ready lane — the shape the dispatcher re-examines
    on the next tick. Nothing here associates the card with the pull request.
    """
    task_id = kb.create_task(conn, title="mid-run crash", assignee="a")
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, 'crashed', ?, ?, 'crashed')",
        (task_id, now - 600, now - 300),
    )
    kb.add_comment(conn, task_id, "worker", body)
    conn.execute(
        "UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?",
        (task_id,),
    )
    return task_id


@pytest.mark.parametrize("body", [
    pytest.param(f"> continuing in {PR}\n\nthat quote is from the other card, not ours",
                 id="quoted_from_another_card"),
    pytest.param(f"fleet digest: t_aaaa opened {PR}; t_bbbb still running",
                 id="reposted_digest"),
    pytest.param(f"follow the review-comment pattern from {PR} when you get there",
                 id="unrelated_reference"),
])
def test_unassociated_pr_prose_does_not_park_the_card(board, body):
    task_id = _crashed_on_no_pr_of_its_own(board, body)

    # The premise: nothing on this card claims that pull request.
    assert board.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["completion_contract"] == "local-only"
    assert board.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone()["metadata"] is None
    assert board.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance'",
        (task_id,),
    ).fetchone()[0] == 0

    assert kbd._respawn_guard_reason(board, task_id) is None
    decision = kbd.evaluate_respawn_guard(board, task_id)
    assert decision.reason is None
    # And it runs on its own account: no receipt was needed to get here.
    assert decision.resume_receipt_id is None


# --- structured provenance is association, and it does not age out -----------
#
# The old guard could only see a PR that was mentioned in a comment inside the
# 24-hour window. A card whose *contract* names a pull request, whose run
# published one, or which carries an acceptance receipt for one, is that PR's
# card indefinitely — but on the day after the comment aged out it read as
# unguarded and the next tick was free to open a second PR.

def _structured(conn, source: str) -> str:
    """A card that owns ``PR`` through one structured provenance source, with
    no PR-bearing comment inside the guard window."""
    contract = PR if source == "completion_contract" else None
    task_id = kb.create_task(conn, title="owns a PR", assignee="a",
                             completion_contract=contract)
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, 'crashed', ?, ?, 'crashed')",
        (task_id, now - 600, now - 300),
    )
    run_id = int(cur.lastrowid)
    if source == "published_pr":
        conn.execute("UPDATE task_runs SET metadata = ? WHERE id = ?",
                     (json.dumps({"published_pr": PR}), run_id))
    if source == "pr_acceptance":
        kb._append_event(conn, task_id, "pr_acceptance",
                         {"ok": False, "classification": "pending", "pr_url": PR},
                         run_id=run_id)
    # The comment that named it has aged out of the guard's 24-hour window.
    kb.add_comment(conn, task_id, "worker", f"opened {PR}")
    conn.execute(
        "UPDATE task_comments SET created_at = ? WHERE task_id = ?",
        (now - 3 * 86400, task_id),
    )
    conn.execute(
        "UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?", (task_id,))
    return task_id


@pytest.mark.parametrize("source", ["completion_contract", "published_pr", "pr_acceptance"])
def test_structured_provenance_holds_after_the_comment_window(board, source):
    task_id = _structured(board, source)

    # The premise: no PR-bearing comment survives inside the window.
    assert not kba.prose_urls(board, task_id, window_seconds=86400)

    assert [a.source for a in kba.structured_associations(board, task_id)] == [source]
    # Association is recorded canonically: GitHub owner/repo casing is display,
    # not identity, so two spellings of one PR are never two lineages.
    assert kba.associated_pr(board, task_id) == PR.lower()
    assert kbd._respawn_guard_reason(board, task_id) == "active_pr"


# --- automatic reconciliation: the card resumes without a click --------------
#
# Holding the card is only half the contract. A worker that died mid-PR must be
# able to continue that exact PR on the next tick, decided from evidence the
# dispatcher can collect under its own authenticated principal. The operator
# receipt stays available, but routine recovery must not need one.

def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def checkout(tmp_path):
    """A real repository with a real linked worktree, as the dispatcher finds it."""
    origin = tmp_path / "repo"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "README").write_text("x")
    _git(origin, "add", "README")
    _git(origin, "commit", "-qm", "base")
    return origin


def _worktree(origin, branch):
    target = origin.parent / f"wt-{branch.replace('/', '-')}"
    _git(origin, "worktree", "add", "-q", "-b", branch, str(target))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(target),
                          capture_output=True, text=True, check=True).stdout.strip()
    return target, head


def _pr_payload(*, state="open", merged=False, head_ref, head_sha,
                head_repo="NousResearch/hermes-agent",
                base_repo="NousResearch/hermes-agent", base_ref="main"):
    return {"state": state, "merged": merged,
            "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": head_repo}},
            "base": {"ref": base_ref, "repo": {"full_name": base_repo}}}


def _fake_api(payloads):
    """A bounded stand-in for ``gh api``; records what was asked for."""
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        value = payloads.get(endpoint)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise kpr.TransportError("no such endpoint")
        return value

    api.calls = calls
    return api


def _mid_pr_crash(conn, checkout, *, branch=None, pr=PR):
    """A card that owns a worktree on its own branch and mentions ``pr``."""
    task_id = kb.create_task(conn, title="mid-PR crash", assignee="a")
    branch = branch or f"wt/{task_id}"
    target, head = _worktree(checkout, branch)
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome) "
        "VALUES (?, 'crashed', ?, ?, 'crashed')", (task_id, now - 600, now - 300))
    kb.add_comment(conn, task_id, "worker", f"opened {pr}")
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, workspace_kind='worktree', "
        "workspace_path=?, branch_name=? WHERE id=?", (str(target), branch, task_id))
    return task_id, branch, head


def test_an_exact_open_pr_resumes_automatically_on_its_own_checkout(board, checkout):
    """No attestation, no dashboard call: the dispatcher's own principal."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(head_ref=branch, head_sha=head)})

    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)
    assert observation.classification == kpr.OPEN
    assert observation.pr_url == PR.lower()

    receipt = kpr.admit(board, snapshot, observation)
    assert receipt is not None
    decision = kbd.evaluate_respawn_guard(board, task_id)
    assert decision.reason is None
    assert decision.resume_receipt_id == receipt

    claimed = kb.claim_task(board, task_id, resume_receipt_id=receipt)
    assert claimed is not None
    # The successor is told which PR it continues, and told not to open another.
    context = kb.build_worker_context(board, task_id)
    assert PR.lower() in context.lower()
    assert "Do NOT open a new one." in context


def test_a_reconciled_receipt_is_not_an_operator_attestation(board, checkout):
    """Automatic authority is a distinct kind, auditable as such."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(head_ref=branch, head_sha=head)})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    receipt = kpr.admit(board, snapshot, kpr.observe(
        snapshot, deadline=time.time() + 5, principal="hermes:dispatcher", api=api))

    row = board.execute("SELECT * FROM task_resume_receipts WHERE id = ?", (receipt,)).fetchone()
    assert row["authority_kind"] == "reconciled"
    assert row["issued_by"] == "hermes:dispatcher"
    evidence = json.loads(row["reconciliation"])
    assert evidence["version"] == kpr.RECONCILIATION_VERSION
    # Both repository identities, so a fork head is never read as the base.
    assert evidence["head_repo"] == "NousResearch/hermes-agent"
    assert evidence["base_repo"] == "NousResearch/hermes-agent"
    assert evidence["head_sha"] == head
    assert evidence["branch"] == branch


def test_a_merged_pr_continues_closeout_without_marking_done(board, checkout):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(state="closed", merged=True, head_ref=branch, head_sha=head)})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)
    assert observation.classification == kpr.MERGED

    receipt = kpr.admit(board, snapshot, observation)
    assert receipt is not None
    assert board.execute(
        "SELECT continuation_mode FROM task_resume_receipts WHERE id = ?",
        (receipt,)).fetchone()["continuation_mode"] == kpr.MERGED_CLOSEOUT
    claimed = kb.claim_task(board, task_id, resume_receipt_id=receipt)
    assert claimed is not None
    assert claimed.status != "done"
    assert board.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] != "done"


@pytest.mark.parametrize("payloads,expected", [
    pytest.param(
        {"repos/nousresearch/hermes-agent/pulls/4242":
         {"state": "closed", "merged": False,
          "head": {"ref": "BRANCH", "sha": "HEAD", "repo": {"full_name": "NousResearch/hermes-agent"}},
          "base": {"ref": "main", "repo": {"full_name": "NousResearch/hermes-agent"}}}},
        "closed_unmerged", id="closed_unmerged"),
    pytest.param({}, "unavailable", id="unavailable"),
])
def test_an_unresolvable_pr_gets_a_specific_bounded_recovery(board, checkout, payloads, expected):
    """Not a silent day-long wait: a named classification and a retry schedule."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    resolved = {k: json.loads(json.dumps(v).replace('"BRANCH"', json.dumps(branch))
                              .replace('"HEAD"', json.dumps(head)))
                for k, v in payloads.items()}
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=_fake_api(resolved))
    assert observation.classification == expected
    assert observation.recovery

    assert kpr.admit(board, snapshot, observation) is None
    kpr.record_unresolved(board, snapshot, observation)
    state = board.execute(
        "SELECT * FROM task_pr_reconcile_state WHERE task_id = ?", (task_id,)).fetchone()
    assert state["classification"] == expected
    assert state["attempts"] == 1
    assert state["next_at"] > int(time.time())
    # The card is still held: an unresolved observation is not permission.
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_two_associated_pull_requests_stay_ambiguous(board, checkout):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    kb.add_comment(board, task_id, "worker", f"and {OTHER_PR}")
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=_fake_api({}))
    assert observation.classification == kpr.AMBIGUOUS
    assert kpr.admit(board, snapshot, observation) is None


@pytest.mark.parametrize("mutate,reason", [
    pytest.param(lambda pr, branch, head: pr.update(head={"ref": "someone-elses-branch",
                 "sha": head, "repo": {"full_name": "NousResearch/hermes-agent"}}),
                 "branch", id="head_ref_is_not_this_tasks_branch"),
    pytest.param(lambda pr, branch, head: pr["head"].update(sha="0" * 40),
                 "head", id="remote_head_is_not_this_checkouts_head"),
])
def test_a_pr_this_checkout_did_not_push_is_not_this_tasks_pr(board, checkout, mutate, reason):
    """The prose named a PR; the branch and head decide whether it is ours."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    payload = _pr_payload(head_ref=branch, head_sha=head)
    mutate(payload, branch, head)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=_fake_api(
                                  {"repos/nousresearch/hermes-agent/pulls/4242": payload}))
    assert observation.classification == kpr.UNAVAILABLE
    assert reason in observation.detail.lower()
    assert kpr.admit(board, snapshot, observation) is None


def test_a_fork_head_is_not_confused_with_the_base_repository(board, checkout):
    """A PR from a fork is still this branch's PR, and both identities persist."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(
        head_ref=branch, head_sha=head, head_repo="contributor/hermes-agent")})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)
    assert observation.classification == kpr.OPEN
    assert observation.evidence["head_repo"] == "contributor/hermes-agent"
    assert observation.evidence["base_repo"] == "NousResearch/hermes-agent"


def test_evidence_moving_between_observation_and_admission_refuses(board, checkout):
    """The window between collecting and admitting is the one that must not leak."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(head_ref=branch, head_sha=head)})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)

    kb.add_comment(board, task_id, "human", "hold off, the design changed")
    assert kpr.admit(board, snapshot, observation) is None
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"


def test_a_live_writer_refuses_admission(board, checkout):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(head_ref=branch, head_sha=head)})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)
    board.execute(
        "INSERT INTO task_runs (task_id, status, started_at, worker_pid) "
        "VALUES (?, 'running', ?, ?)", (task_id, int(time.time()), os.getpid()))
    assert kpr.admit(board, snapshot, observation) is None


def test_observation_does_not_hold_a_sqlite_transaction(board, checkout):
    """Network waits must leave the board writable for everyone else."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)

    def api(endpoint):
        # A concurrent writer, on its own connection, during the "network" wait.
        other = kbc.connect()
        try:
            with kbc.write_txn(other):
                other.execute("UPDATE tasks SET priority = 7 WHERE id = ?", (task_id,))
        finally:
            other.close()
        return _pr_payload(head_ref=branch, head_sha=head)

    observation = kpr.observe(snapshot, deadline=time.time() + 5,
                              principal="hermes:dispatcher", api=api)
    assert observation.classification == kpr.OPEN
    assert board.execute(
        "SELECT priority FROM tasks WHERE id = ?", (task_id,)).fetchone()[0] == 7


def test_a_merged_continuation_is_not_told_to_push_to_the_pull_request(board, checkout):
    """"Continue this PR" and "close this out" are different instructions.

    A merged pull request has no branch left to push to and merging is not
    completion, so a successor handed the open-PR wording either pushes into a
    closed lineage or reads the merge as done.
    """
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(state="closed", merged=True, head_ref=branch, head_sha=head)})
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    receipt = kpr.admit(board, snapshot, kpr.observe(
        snapshot, deadline=time.time() + 5, principal="hermes:dispatcher", api=api))
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is not None

    context = kb.build_worker_context(board, task_id)
    assert "merged" in context.lower()
    assert "Do NOT open a new one." not in context
    assert "does not complete this task" in context


# --- the dispatcher does this by itself --------------------------------------

def _dispatch(board, **kw):
    spawned = []
    result = kbd.dispatch_once(
        board, spawn_fn=lambda *a, **k: spawned.append(a) or 4242,
        reconcile_orphans=False, **kw)
    return result, spawned


def test_the_dispatcher_resumes_an_open_pr_with_no_operator_involvement(
        board, checkout, monkeypatch):
    """The whole point: routine recovery without a click."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242":
                     _pr_payload(head_ref=branch, head_sha=head)})
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: api(endpoint))

    result, spawned = _dispatch(board)
    assert [t for t, _ in result.respawn_guarded] == []
    assert [t for t, _, _ in result.spawned] == [task_id]
    assert spawned, "the successor never started"
    # Exactly one receipt, spent by exactly this run, and not reusable.
    row = board.execute(
        "SELECT authority_kind, consumed_at, consumed_run_id "
        "FROM task_resume_receipts WHERE task_id = ?", (task_id,)).fetchone()
    assert row["authority_kind"] == "reconciled"
    assert row["consumed_at"] is not None
    assert row["consumed_run_id"] is not None


def test_a_dry_run_reconciles_nothing_and_reaches_no_network(board, checkout, monkeypatch):
    """A preview must not spend budget, issue authority or touch GitHub."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    calls = []
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: calls.append(endpoint))

    result, spawned = _dispatch(board, dry_run=True)
    assert calls == []
    assert spawned == []
    assert [t for t, _ in result.respawn_guarded] == [task_id]
    assert board.execute(
        "SELECT COUNT(*) FROM task_resume_receipts").fetchone()[0] == 0


def test_an_unresolvable_card_stays_held_and_records_a_bounded_retry(
        board, checkout, monkeypatch):
    """No spawn, no quota damage, and a schedule instead of a day of silence."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    monkeypatch.setattr(kpr, "_gh_api",
                        lambda endpoint, **kw: (_ for _ in ()).throw(kpr.TransportError("boom")))

    result, spawned = _dispatch(board)
    assert spawned == []
    assert [t for t, r in result.respawn_guarded] == [task_id]
    state = board.execute(
        "SELECT classification, attempts, next_at FROM task_pr_reconcile_state "
        "WHERE task_id = ?", (task_id,)).fetchone()
    assert state["classification"] == kpr.UNAVAILABLE
    assert state["attempts"] == 1
    # An unreadable pull request is not a task failure: the breaker is untouched.
    task = board.execute(
        "SELECT consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
        (task_id,)).fetchone()
    assert task["consecutive_failures"] == 0
    assert task["last_failure_error"] is None


def test_the_retry_schedule_is_not_re_observed_every_tick(board, checkout, monkeypatch):
    """Backoff, so an unreachable API is not hammered once per tick."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    calls = []

    def boom(endpoint, **kw):
        calls.append(endpoint)
        raise kpr.TransportError("boom")

    monkeypatch.setattr(kpr, "_gh_api", boom)
    _dispatch(board)
    first = len(calls)
    assert first >= 1
    _dispatch(board)
    assert len(calls) == first, "the second tick re-observed inside the backoff"


def test_a_quota_blocked_card_is_never_reconciled(board, checkout, monkeypatch):
    """Authority to continue a PR is not authority to burn a rate-limited key."""
    task_id, branch, head = _mid_pr_crash(board, checkout)
    board.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                  ("Error: 403 forbidden - quota exhausted", task_id))
    calls = []
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: calls.append(endpoint))

    result, spawned = _dispatch(board)
    assert calls == []
    assert spawned == []
    assert [r for _, r in result.respawn_guarded] == ["blocker_auth"]


def test_an_unassociated_card_is_never_reconciled(board, monkeypatch):
    """Nothing is held, so there is nothing to spend a request on."""
    _crashed_on_no_pr_of_its_own(board, f"see {PR} for the pattern")
    calls = []
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: calls.append(endpoint))
    _dispatch(board)
    assert calls == []


def test_a_board_that_predates_the_reconciliation_payload_is_migrated(tmp_path, monkeypatch):
    """``CREATE TABLE IF NOT EXISTS`` is a no-op on a table that already exists.

    A board created before the reconciliation columns existed keeps its old
    ``task_resume_receipts`` forever unless the columns are added explicitly —
    and every insert against it fails at runtime, on exactly the boards that
    have been running longest.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: tmp_path / "kanban.db")
    kb.init_db()

    legacy = kbc.connect()
    try:
        with kbc.write_txn(legacy):
            legacy.execute("DROP TABLE task_resume_receipts")
            legacy.execute(
                "CREATE TABLE task_resume_receipts ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
                " board_identity TEXT NOT NULL, run_id INTEGER NOT NULL,"
                " occurrence_event_id INTEGER NOT NULL, pr_url TEXT NOT NULL,"
                " comment_digest TEXT NOT NULL, lifecycle TEXT NOT NULL,"
                " assignee TEXT, workspace_kind TEXT, workspace_path TEXT,"
                " issued_by TEXT NOT NULL, issued_at INTEGER NOT NULL,"
                " expires_at INTEGER NOT NULL, consumed_at INTEGER,"
                " consumed_run_id INTEGER, revoked_at INTEGER)")
        assert "authority_kind" not in {
            r["name"] for r in legacy.execute("PRAGMA table_info(task_resume_receipts)")}
    finally:
        legacy.close()

    kb.init_db()

    conn = kbc.connect()
    try:
        columns = {r["name"] for r in conn.execute(
            "PRAGMA table_info(task_resume_receipts)")}
        assert {"authority_kind", "reconciliation", "continuation_mode"} <= columns
        # Receipts written before the column existed were operator attestations;
        # that is the only safe reading of the backfilled default.
        assert conn.execute(
            "SELECT dflt_value FROM pragma_table_info('task_resume_receipts') "
            "WHERE name = 'authority_kind'").fetchone()[0] == "'attested'"
    finally:
        conn.close()
