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
REAL_GH_API = kpr._gh_api
OTHER_PR = "https://github.com/NousResearch/hermes-agent/pull/9999"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "kanban_db_path", lambda **kw: tmp_path / "kanban.db")
    # The dispatcher refuses to spawn an assignee that is not a real profile.
    (tmp_path / "profiles" / "a").mkdir(parents=True, exist_ok=True)
    kb.init_db()
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: _fake_api({})(endpoint))
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
    task_id = kb.create_task(board, title="fresh unrelated reference", assignee="a")
    kb.add_comment(board, task_id, "worker", body)

    # The premise: nothing on this card claims that pull request.
    assert board.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["completion_contract"] == "local-only"
    assert board.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ?", (task_id,)
    ).fetchone() is None
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
    _git(origin, "remote", "add", "origin", "git@github.com:NousResearch/hermes-agent.git")
    return origin


def _worktree(origin, branch):
    target = origin.parent / f"wt-{branch.replace('/', '-')}"
    _git(origin, "worktree", "add", "-q", "-b", branch, str(target))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(target),
                          capture_output=True, text=True, check=True).stdout.strip()
    return target, head


def _pr_payload(*, state="open", merged=False, number=4242, draft=False, head_ref, head_sha,
                head_repo="NousResearch/hermes-agent",
                base_repo="NousResearch/hermes-agent", base_ref="main"):
    return {"number": number, "state": state, "merged": merged, "draft": draft,
            "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": head_repo}},
            "base": {"ref": base_ref, "sha": head_sha, "repo": {"full_name": base_repo}}}


def _fake_api(payloads):
    """A bounded stand-in for ``gh api``; records what was asked for."""
    calls = []

    def api(endpoint):
        calls.append(endpoint)
        value = payloads.get(endpoint, {"id": 123, "login": "test-owner"} if endpoint == "user" else None)
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
         {"number": 4242, "state": "closed", "merged": False,
          "head": {"ref": "BRANCH", "sha": "HEAD", "repo": {"full_name": "NousResearch/hermes-agent"}},
          "base": {"ref": "main", "sha": "HEAD", "repo": {"full_name": "NousResearch/hermes-agent"}}}},
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
        principal="hermes:dispatcher", api=_fake_api({
            "repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(head_ref=branch, head_sha=head),
            "repos/nousresearch/hermes-agent/pulls/9999": _pr_payload(number=9999, head_ref=branch, head_sha=head)}))
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
    assert observation.classification == (kpr.NO_ASSOCIATED_PR if reason == "branch" else kpr.UNAVAILABLE)
    assert reason in observation.detail.lower()
    assert kpr.admit(board, snapshot, observation) is None


def test_a_fork_head_is_not_confused_with_the_base_repository(board, checkout):
    """A PR from a configured fork preserves both repository identities."""
    _git(checkout, "remote", "add", "fork", "https://github.com/contributor/hermes-agent.git")
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
    assert state["classification"] == "auth_unavailable"
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
    task_id = kb.create_task(board, title="fresh unrelated reference", assignee="a")
    kb.add_comment(board, task_id, "worker", f"see {PR} for the pattern")
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


@pytest.mark.parametrize("head_repo,base_repo,allowed", [
    ("attacker/fork", "NousResearch/hermes-agent", False),
    ("NousResearch/hermes-agent", "attacker/fork", False),
    ("NousResearch/hermes-agent", "NousResearch/hermes-agent", True),
])
def test_remote_identity_controls_pr_ownership(board, checkout, head_repo, base_repo, allowed):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
        principal="hermes:dispatcher", api=_fake_api({
            "repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(
                head_ref=branch, head_sha=head, head_repo=head_repo, base_repo=base_repo)}))
    assert (observation.classification == kpr.OPEN) is allowed
    assert (kpr.admit(board, snapshot, observation) is not None) is allowed


@pytest.mark.parametrize("mutation", ["branch_record", "head", "remote", "spawn_head"])
def test_resume_rechecks_checkout_at_claim_and_spawn(board, checkout, monkeypatch, mutation):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    payload = _pr_payload(head_ref=branch, head_sha=head)
    api = _fake_api({"repos/nousresearch/hermes-agent/pulls/4242": payload})
    receipt = kpr.admit(board, snapshot, kpr.observe(snapshot,
        deadline=time.time() + 5, principal="hermes:dispatcher", api=api))
    assert receipt is not None
    if mutation == "branch_record":
        board.execute("UPDATE tasks SET branch_name='changed' WHERE id=?", (task_id,))
    elif mutation == "remote":
        _git(checkout, "remote", "set-url", "origin", "https://github.com/attacker/fork.git")
    elif mutation == "head":
        _git(snapshot.workspace_path, "commit", "--allow-empty", "-qm", "changed")
    else:
        original = kb.claim_task
        def claim(*args, **kwargs):
            task = original(*args, **kwargs)
            _git(snapshot.workspace_path, "commit", "--allow-empty", "-qm", "changed")
            return task
        monkeypatch.setattr(kb, "claim_task", claim)
        result, spawned = _dispatch(board)
        assert spawned == []
        assert result.spawned == []
        return
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    assert board.execute("SELECT consumed_at FROM task_resume_receipts WHERE id=?",
                         (receipt,)).fetchone()[0] is None


@pytest.mark.parametrize("writer", ["running", "claim", "terminal_pid"])
def test_other_task_sharing_checkout_excludes_resume(board, checkout, writer):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    receipt = kpr.admit(board, snapshot, kpr.observe(snapshot,
        deadline=time.time() + 5, principal="hermes:dispatcher", api=_fake_api({
            "repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(head_ref=branch, head_sha=head)})))
    assert receipt is not None
    other = kb.create_task(board, title="same checkout writer", assignee="a")
    board.execute("UPDATE tasks SET workspace_path=? WHERE id=?",
                  (snapshot.workspace_path + "/.", other))
    if writer == "running":
        board.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    elif writer == "claim":
        board.execute("UPDATE tasks SET claim_lock='another-worker' WHERE id=?", (other,))
    else:
        board.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at, worker_pid) "
                      "VALUES (?, 'crashed', ?, ?, ?)",
                      (other, int(time.time()) - 60, int(time.time()), os.getpid()))
    assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None
    assert board.execute("SELECT consumed_at FROM task_resume_receipts WHERE id=?",
                         (receipt,)).fetchone()[0] is None


@pytest.mark.parametrize("cooldown", [0, 60])
def test_expired_quota_cooldown_still_requires_pr_reconciliation(board, checkout, monkeypatch, cooldown):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    board.execute("UPDATE task_runs SET outcome='rate_limited', ended_at=? WHERE task_id=?",
                  (int(time.time()) - 600, task_id))
    board.execute("UPDATE tasks SET last_failure_error='quota exhausted' WHERE id=?", (task_id,))
    monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: cooldown)
    assert kbd.evaluate_respawn_guard(board, task_id).reason == "active_pr"
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw: (_ for _ in ()).throw(kpr.TransportError("offline")))
    result, spawned = _dispatch(board)
    assert spawned == []
    assert board.execute("SELECT attempts FROM task_pr_reconcile_state WHERE task_id=?",
                         (task_id,)).fetchone()[0] == 1


# The transport must terminate its own disposable gh child at the bound.
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("case", ["deadline", "utf8_bytes"])
def test_real_gh_transport_enforces_total_deadline_and_bytes(tmp_path, monkeypatch, case):
    import sys
    shim = tmp_path / "gh"
    shim.write_text("#!" + sys.executable + "\nimport time, json\n" + (
        "time.sleep(0.4)\nprint('{}')\n" if case == "deadline" else
        "print(json.dumps('é' * 80, ensure_ascii=False))\n"))
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    if case == "utf8_bytes":
        monkeypatch.setattr(kpr, "MAX_RESPONSE_BYTES", 100)
    budget = kpr._Budget(time.time() + (0.15 if case == "deadline" else 3))
    with pytest.raises(kpr.TransportError):
        budget.call(kpr._gh_api, "user")


@pytest.mark.parametrize("changed", ["checkout_head", "principal"])
def test_retry_exhaustion_is_invalidated_by_checkout_and_principal(board, checkout, changed):
    from dataclasses import replace
    task_id, branch, head = _mid_pr_crash(board, checkout)
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    if changed == "principal":
        # Same API used by the dispatcher to supply trusted execution identity.
        snapshot = replace(snapshot, principal="github:123")
    observation = kpr.observe(snapshot, deadline=time.time() + 5,
        principal="github:123", api=_fake_api({}))
    for attempt in range(4):
        kpr.record_unresolved(board, snapshot, observation, now=1000 + attempt * 400)
    assert not kpr.due(board, snapshot, now=2201)
    if changed == "checkout_head":
        _git(snapshot.workspace_path, "commit", "--allow-empty", "-qm", "recovered head")
        recovered = kpr.capture(board, task_id, window_seconds=86400)
    else:
        recovered = replace(snapshot, principal="github:456")
    assert kpr.due(board, recovered, now=10000)


@pytest.mark.parametrize("boundary", ["admit_checkout", "claim_provenance"])
def test_reconciliation_preserves_source_and_checkout_evidence(board, checkout, boundary):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    run_id = board.execute("SELECT id FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    board.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"published_pr": PR}), run_id))
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    observation = kpr.observe(snapshot, deadline=time.time() + 5, principal="hermes:dispatcher",
        api=_fake_api({"repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(head_ref=branch, head_sha=head)}))
    if boundary == "admit_checkout":
        _git(snapshot.workspace_path, "commit", "--allow-empty", "-qm", "changed")
        assert kpr.admit(board, snapshot, observation) is None
    else:
        receipt = kpr.admit(board, snapshot, observation)
        assert receipt is not None
        board.execute("UPDATE task_runs SET metadata=NULL WHERE id=?", (run_id,))
        board.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (PR, task_id))
        assert kb.claim_task(board, task_id, resume_receipt_id=receipt) is None


@pytest.mark.parametrize("case", ["crashed_unknown", "fresh_quote", "recorded_dir", "recorded_scratch"])
def test_scratch_history_requires_evidence_not_prose(board, checkout, monkeypatch, case):
    if case == "crashed_unknown":
        task_id = _crashed_on_no_pr_of_its_own(board, f"I opened {PR}")
        board.execute("UPDATE task_comments SET created_at=1 WHERE task_id=?", (task_id,))
        expected = "history_unresolved"
    elif case == "fresh_quote":
        task_id = kb.create_task(board, title="fresh work", assignee="a")
        kb.add_comment(board, task_id, "worker", f"> unrelated example: {PR}")
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
        assert kbd.evaluate_respawn_guard(board, task_id).reason is None
        return
    else:
        task_id, branch, head = _mid_pr_crash(board, checkout)
        board.execute("UPDATE tasks SET workspace_kind=? WHERE id=?",
                      (case.removeprefix("recorded_"), task_id))
        expected = None
        monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw:
                            {"id": 123} if endpoint == "user" else _pr_payload(head_ref=branch, head_sha=head))
    result, spawned = _dispatch(board)
    if expected:
        assert spawned == []
        state = board.execute("SELECT classification, next_at FROM task_pr_reconcile_state WHERE task_id=?",
                              (task_id,)).fetchone()
        assert state is not None and state["classification"] == expected
        assert int(time.time()) < state["next_at"] < int(time.time()) + 600
        assert board.execute("SELECT COUNT(*) FROM task_resume_receipts WHERE task_id=?", (task_id,)).fetchone()[0] == 0
    else:
        assert [t for t, _, _ in result.spawned] == [task_id]
        assert board.execute("SELECT COUNT(*) FROM task_resume_receipts WHERE task_id=?", (task_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("case,expected", [("unrelated", "no_associated_pr"),
    ("mixed", "open"), ("owned", "ambiguous"), ("limit", "inspection_limit")])
def test_hint_filtering_precedes_ownership_ambiguity(board, checkout, case, expected):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    kb.add_comment(board, task_id, "worker", f"another reference {OTHER_PR}")
    payloads = {
        "repos/nousresearch/hermes-agent/pulls/4242": _pr_payload(head_ref=branch if case != "unrelated" else "elsewhere", head_sha=head),
        "repos/nousresearch/hermes-agent/pulls/9999": _pr_payload(number=9999, head_ref=branch if case == "owned" else "elsewhere", head_sha=head),
    }
    if case == "limit":
        for number in range(10, 10 + kpr.MAX_REQUESTS):
            kb.add_comment(board, task_id, "worker", f"https://github.com/NousResearch/hermes-agent/pull/{number}")
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    api = _fake_api(payloads)
    observation = kpr.observe(snapshot, deadline=time.time() + 10, principal="test-owner", api=api)
    assert observation.classification == expected
    assert len(api.calls) <= kpr.MAX_REQUESTS
    if expected == "no_associated_pr":
        assert observation.pr_url is None
        assert kpr.admit(board, snapshot, observation) is None


@pytest.mark.parametrize("mutation", ["none", "comment", "head", "auth", "other_writer"])
def test_unrelated_clearance_is_atomic_and_has_no_owned_pr_lineage(board, checkout, monkeypatch, mutation):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw:
        {"id": 123} if endpoint == "user" else _pr_payload(head_ref="unrelated-branch", head_sha=head))
    decision = kbd._reconcile_active_pr(board, task_id, lane="ready")
    assert decision.reason is None
    assert decision.resume_receipt_id is None
    assert board.execute("SELECT COUNT(*) FROM task_resume_receipts").fetchone()[0] == 0
    snapshot_path = board.execute("SELECT workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    if mutation == "comment":
        kb.add_comment(board, task_id, "owner", "new evidence")
    elif mutation == "head":
        _git(snapshot_path, "commit", "--allow-empty", "-qm", "new evidence")
    elif mutation == "auth":
        board.execute("UPDATE tasks SET last_failure_error='403 forbidden quota exhausted' WHERE id=?", (task_id,))
    elif mutation == "other_writer":
        other = kb.create_task(board, title="another writer", assignee="a")
        board.execute("UPDATE tasks SET workspace_path=?, status='running' WHERE id=?", (snapshot_path, other))
    claimed = kb.claim_task(board, task_id, pr_clearance=decision.pr_clearance)
    assert (claimed is not None) == (mutation == "none")
    if claimed:
        assert kb.claim_task(board, task_id, pr_clearance=decision.pr_clearance) is None
        context = kb.build_worker_context(board, task_id)
        assert "Do NOT open a new one." not in context
    assert board.execute("SELECT COUNT(*) FROM task_resume_receipts").fetchone()[0] == 0


@pytest.mark.parametrize("recovery", ["account_changed", "auth_repaired"])
def test_authenticated_recovery_uses_shared_budget_and_renews_exhaustion(board, checkout, tmp_path, monkeypatch, recovery):
    import sys
    task_id, branch, head = _mid_pr_crash(board, checkout)
    monkeypatch.setattr(kpr, "_gh_api", REAL_GH_API)
    routes = tmp_path / "responses.json"
    calls = tmp_path / "requests.txt"
    shim = tmp_path / "gh"
    db = board.execute("PRAGMA database_list").fetchone()[2]
    shim.write_text("#!" + sys.executable + "\n" +
        "import json, sqlite3, sys\n" +
        f"db = sqlite3.connect({db!r}, timeout=0.2)\n" +
        f"db.execute('UPDATE tasks SET priority=priority WHERE id=?', ({task_id!r},))\ndb.commit()\ndb.close()\n" +
        f"with open({str(calls)!r}, 'a') as f: f.write(sys.argv[2] + '\\n')\n" +
        f"routes = json.load(open({str(routes)!r}))\n" +
        "value = routes.get(sys.argv[2])\nif value is None: sys.exit(1)\nprint(json.dumps(value))\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    payload = _pr_payload(head_ref=branch, head_sha=head)
    for attempt in range(4):
        routes.write_text(json.dumps({"user": {"id": 101, "login": "old-owner"}} if recovery == "account_changed" else {}))
        result, spawned = _dispatch(board)
        assert not spawned
        state = board.execute("SELECT * FROM task_pr_reconcile_state WHERE task_id=? ORDER BY last_at DESC LIMIT 1", (task_id,)).fetchone()
        assert state is not None
        if attempt < 3:
            board.execute("UPDATE task_pr_reconcile_state SET next_at=0 WHERE task_id=?", (task_id,))
    snapshot = kpr.capture(board, task_id, window_seconds=86400)
    assert kpr.due(board, snapshot, now=int(state["next_at"]) + 1), "exhaustion must schedule authenticated recovery"
    board.execute("UPDATE task_pr_reconcile_state SET next_at=0 WHERE task_id=?", (task_id,))
    routes.write_text(json.dumps({"user": {"id": 202, "login": "recovered-owner"},
        "repos/nousresearch/hermes-agent/pulls/4242": payload}))
    before = len(calls.read_text().splitlines())
    result, spawned = _dispatch(board)
    assert [t for t, _, _ in result.spawned] == [task_id]
    receipt = board.execute("SELECT issued_by, reconciliation FROM task_resume_receipts WHERE task_id=?", (task_id,)).fetchone()
    assert receipt["issued_by"] == "github:202"
    assert json.loads(receipt["reconciliation"])["principal"] == "github:202"
    requests = calls.read_text().splitlines()[before:]
    assert requests[0] == "user" and len(requests) <= kpr.MAX_REQUESTS
    assert json.loads(receipt["reconciliation"])["requests"] == len(requests)


@pytest.mark.parametrize("kind", ["dir", "scratch"])
def test_recorded_checkout_without_branch_column_uses_real_git_lineage(board, checkout, monkeypatch, kind):
    task_id, branch, head = _mid_pr_crash(board, checkout)
    board.execute("UPDATE tasks SET workspace_kind=?, branch_name=NULL WHERE id=?", (kind, task_id))
    path = board.execute("SELECT workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    monkeypatch.setattr(kpr, "_gh_api", lambda endpoint, **kw:
        {"id": 123} if endpoint == "user" else _pr_payload(head_ref=branch, head_sha=head))
    result, spawned = _dispatch(board)
    assert [t for t, _, workspace in result.spawned if workspace == path] == [task_id]
    receipt = board.execute("SELECT reconciliation FROM task_resume_receipts WHERE task_id=?", (task_id,)).fetchone()
    assert json.loads(receipt[0])["checkout"]["branch"] == branch
    assert json.loads(receipt[0])["checkout"]["head"] == head
