"""Decide, automatically, whether a held card may continue its own pull request.

The ``active_pr`` guard is right to hold a card whose worker died mid-PR: a
successor that opens a second pull request for the same work is worse than a
stalled card. But holding was the *whole* answer, and the only exit was an
operator clicking through an attestation. Routine recovery is not an operator's
job, so cards sat for twenty-four hours waiting for a human to confirm something
GitHub already knew.

This module answers the question the board cannot: **is the pull request at the
end of this task's branch really this task's, and is it still open?** It runs in
three phases, and the split is the design:

1. :func:`capture` — one short read. Nothing is held afterwards.
2. :func:`observe` — bounded, authenticated, **no database at all**. A network
   wait here must never make the board unwritable for anyone else.
3. :func:`admit` — one write transaction that re-reads the captured tuple, and
   issues a single-use receipt only if nothing moved.

An observation is evidence, never permission. Everything it concluded is
re-proved in :func:`admit`, and re-proved again inside ``claim_task`` when the
receipt is spent, because a comment, a run or a writer can arrive in either gap.

Ownership rules, in one place:

* One candidate pull request. Two is ambiguity, and ambiguity never resolves to
  automatic authority — a receipt exempts one lineage and would leave the other
  unexamined.
* The PR's head ref must be exactly this task's branch (**case-sensitive**: Git
  branch names are), and the PR's head SHA must be exactly what this task's own
  checkout has. That pair is what makes it *this task's* PR rather than a PR
  someone linked in a comment.
* A fork head is recorded as a fork. ``head_repo`` and ``base_repo`` are stored
  separately so a contributor's fork is never read as the upstream repository.
* Anything else — closed, unmerged, unreachable, mismatched, ambiguous — gets a
  named classification and a bounded retry schedule, not a silent day of
  waiting.

No credential is created, chosen or stored here. The transport shells out to the
already-authenticated ``gh`` the acceptance collector uses, and ``gh`` stderr is
never persisted: it can carry hostnames and token hints, and the *phase* that
failed is the actionable part anyway.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from hermes_cli import kanban_pr_association as _assoc
from hermes_cli import kanban_resume as _resume

RECONCILIATION_VERSION = 1

# Classifications. ``OPEN`` and ``MERGED`` admit; the rest schedule a retry.
OPEN = "open"
MERGED = "merged"
CLOSED_UNMERGED = "closed_unmerged"
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"

# Continuation modes carried on the receipt and into the worker's instructions.
RESUME_OPEN_PR = "resume_open_pr"
MERGED_CLOSEOUT = "merged_closeout"

# Bounds on one reconciliation. Per-request timeouts are not enough: a paginated
# or slow endpoint can respect every individual timeout and still spend a tick.
MAX_REQUESTS = 4
REQUEST_TIMEOUT_SECONDS = 15
TOTAL_DEADLINE_SECONDS = 30
MAX_RESPONSE_BYTES = 1 << 20

# Attempt N waits this long before attempt N+1. After the last one the card
# stays unresolved and is retried only when its evidence changes (a new
# comment, a new run, a new head) or an operator asks again — retrying a
# permanently closed PR every five minutes forever is event spam, not recovery.
RETRY_BACKOFF_SECONDS = (60, 120, 300)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_pr_reconcile_state (
    task_id        TEXT NOT NULL,
    evidence_key   TEXT NOT NULL,
    classification TEXT NOT NULL,
    detail         TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    first_at       INTEGER NOT NULL,
    last_at        INTEGER NOT NULL,
    next_at        INTEGER NOT NULL,
    PRIMARY KEY (task_id, evidence_key)
);
"""


class TransportError(Exception):
    """A bounded request could not produce usable evidence."""


@dataclass(frozen=True)
class Snapshot:
    """What the board said, at one instant, with no transaction still open."""

    board_identity: str
    task_id: str
    run_id: int
    occurrence_event_id: int
    comment_digest: str
    lifecycle: str
    assignee: Optional[str]
    workspace_kind: Optional[str]
    workspace_path: Optional[str]
    branch_name: Optional[str]
    candidates: tuple[str, ...]
    association_kind: str
    provenance: tuple[tuple[str, str, str], ...]
    captured_at: int

    def evidence_key(self) -> str:
        """Identity of the situation this snapshot describes.

        A changed key is a different situation, so its retry schedule starts
        fresh rather than inheriting a backoff earned by evidence that no longer
        exists.
        """
        import hashlib

        material = json.dumps([
            self.board_identity, self.task_id, self.run_id, self.occurrence_event_id,
            self.comment_digest, self.lifecycle, self.assignee, self.workspace_kind,
            self.workspace_path, self.branch_name, sorted(self.candidates),
        ], separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Observation:
    """What GitHub and the checkout said. Evidence, not permission."""

    classification: str
    pr_url: Optional[str]
    detail: str
    recovery: str
    evidence: dict[str, Any]
    principal: str
    observed_at: int

    @property
    def continuation_mode(self) -> Optional[str]:
        if self.classification == OPEN:
            return RESUME_OPEN_PR
        if self.classification == MERGED:
            return MERGED_CLOSEOUT
        return None


# --- phase 1: capture --------------------------------------------------------

def capture(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
    now: Optional[int] = None,
) -> Snapshot:
    """One short read of everything the decision depends on.

    Raises :class:`~hermes_cli.kanban_resume.ResumeAuthorityError` when the card
    is not in a position to be resumed at all, so the caller never spends a
    network budget on a running or nonexistent task.
    """
    now = int(time.time()) if now is None else now
    base = _resume.base_snapshot(conn, task_id)
    kind = _assoc.classify(conn, task_id, window_seconds=window_seconds, now=now)
    provenance = tuple(
        (a.pr_url, a.source, a.source_id)
        for a in _assoc.structured_associations(conn, task_id))
    if provenance:
        candidates = {p[0] for p in provenance}
    else:
        candidates = _assoc.prose_urls(
            conn, task_id, window_seconds=window_seconds, now=now)
    row = conn.execute(
        "SELECT branch_name FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Snapshot(
        board_identity=base["board_identity"], task_id=task_id,
        run_id=base["run_id"], occurrence_event_id=base["occurrence_event_id"],
        comment_digest=base["comment_digest"], lifecycle=base["lifecycle"],
        assignee=base["assignee"], workspace_kind=base["workspace_kind"],
        workspace_path=base["workspace_path"],
        branch_name=(row["branch_name"] if row is not None else None),
        candidates=tuple(sorted(candidates)), association_kind=kind,
        provenance=provenance, captured_at=now,
    )


# --- phase 2: observe (no database) -----------------------------------------

def _gh_api(endpoint: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> Any:
    """One bounded ``gh api`` call under the caller's existing OAuth session.

    Same transport the acceptance collector uses. No credential is selected
    here, and stderr is dropped rather than surfaced: it can carry the host and
    token hints, and the caller only needs to know the request failed.
    """
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--hostname", "github.com"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TransportError(type(exc).__name__) from None
    if len(result.stdout) > MAX_RESPONSE_BYTES:
        raise TransportError("response exceeded the size bound")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise TransportError("response was not JSON") from None


def _checkout_identity(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read-only inspection of the task's existing checkout.

    Nothing is created, switched or repaired: a resume that quietly built a
    second worktree would be a new checkout pretending to be the old one.
    """
    if not path:
        return None
    from hermes_cli import kanban_db_workspace as _kbw
    from pathlib import Path

    target = Path(path)
    if not target.is_dir():
        return None
    git_dir = _kbw._git_dir(target)
    common_dir = _kbw._git_common_dir(target)
    if git_dir is None or common_dir is None:
        return None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(target), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return {
        "realpath": os.path.realpath(str(target)),
        "git_dir": str(git_dir), "common_dir": str(common_dir),
        "linked_worktree": str(git_dir) != str(common_dir),
        "branch": _kbw._git_current_branch(target),
        "head": head.stdout.strip() if head.returncode == 0 else None,
    }


def _unresolved(kind: str, detail: str, recovery: str, snapshot: Snapshot,
                principal: str, pr_url: Optional[str] = None,
                evidence: Optional[dict[str, Any]] = None) -> Observation:
    payload = {"version": RECONCILIATION_VERSION, "classification": kind,
               "task_id": snapshot.task_id, "pr_url": pr_url,
               "association_kind": snapshot.association_kind,
               "provenance": [list(p) for p in snapshot.provenance],
               "principal": principal, **(evidence or {})}
    return Observation(kind, pr_url, detail, recovery, payload, principal,
                       int(time.time()))


def observe(
    snapshot: Snapshot, *, deadline: float, principal: str,
    api: Optional[Callable[[str], Any]] = None,
) -> Observation:
    """Collect bounded evidence about ``snapshot``'s candidate pull request.

    Touches no database. ``api`` is injected so the transport can be exercised
    without a network; the default is the authenticated ``gh`` CLI.
    """
    api = _gh_api if api is None else api
    if not snapshot.candidates:
        return _unresolved(
            UNAVAILABLE, "the card names no pull request to reconcile",
            "Nothing to continue; the hold is not an active_pr hold.",
            snapshot, principal)
    if len(snapshot.candidates) > 1:
        return _unresolved(
            AMBIGUOUS,
            "the card is associated with more than one pull request: "
            + ", ".join(snapshot.candidates),
            "Resolve which pull request this task continues (set completion_contract "
            "to the exact PR URL), then the dispatcher resumes it automatically.",
            snapshot, principal, evidence={"candidates": list(snapshot.candidates)})

    pr_url = snapshot.candidates[0]
    parsed = _assoc.repo_and_number(pr_url)
    if parsed is None:
        return _unresolved(UNAVAILABLE, f"{pr_url} is not a pull request URL",
                           "Correct the recorded pull request URL.", snapshot,
                           principal, pr_url)
    repo, number = parsed

    checkout = _checkout_identity(snapshot.workspace_path)
    if checkout is None:
        return _unresolved(
            UNAVAILABLE,
            "this task has no inspectable checkout of its own, so no branch "
            "can prove the pull request is its",
            "Restore the task's worktree, or record the pull request on the task "
            "(completion_contract) so its lineage is structured.",
            snapshot, principal, pr_url)

    budget = _Budget(deadline)
    try:
        detail = budget.call(api, f"repos/{repo}/pulls/{number}")
        state, merged, head_ref, head_sha, head_repo, base_ref, base_repo = _fields(detail)
    except (TransportError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unresolved(
            UNAVAILABLE, f"pull request evidence unavailable ({type(exc).__name__})",
            "Check `gh auth status` and API reachability; the dispatcher retries "
            "on a bounded schedule without further input.",
            snapshot, principal, pr_url)

    evidence = {
        "pr_url": pr_url, "repo": repo, "number": number, "state": state,
        "merged": bool(merged), "head_repo": head_repo, "head_ref": head_ref,
        "head_sha": head_sha, "base_repo": base_repo, "base_ref": base_ref,
        "checkout": checkout, "branch": snapshot.branch_name,
        "observed_at": int(time.time()), "requests": budget.used,
    }

    # Branch names are case-sensitive in Git, so this comparison must be too.
    if not snapshot.branch_name or head_ref != snapshot.branch_name:
        return _unresolved(
            UNAVAILABLE,
            f"the pull request's head branch is {head_ref!r}, not this task's "
            f"branch {snapshot.branch_name!r}",
            "This pull request belongs to another branch; it is a mention, not "
            "this task's lineage.",
            snapshot, principal, pr_url, evidence)
    if checkout["branch"] != snapshot.branch_name:
        return _unresolved(
            UNAVAILABLE,
            f"the checkout is on branch {checkout['branch']!r}, not the task's "
            f"branch {snapshot.branch_name!r}",
            "Return the task's worktree to its own branch before resuming.",
            snapshot, principal, pr_url, evidence)
    if not checkout["head"] or checkout["head"] != head_sha:
        return _unresolved(
            UNAVAILABLE,
            "the pull request head SHA is not the head this checkout has "
            f"({head_sha!r} vs {checkout['head']!r})",
            "Fetch or push so the checkout and the pull request agree on a head, "
            "then the dispatcher resumes automatically.",
            snapshot, principal, pr_url, evidence)

    if merged:
        return Observation(
            MERGED, pr_url,
            "the pull request is merged; unfinished deploy/acceptance work remains",
            "Continue closeout only. Merging does not complete the task, and the "
            "existing completion gates still collect their own fresh evidence.",
            {"version": RECONCILIATION_VERSION, "classification": MERGED,
             "task_id": snapshot.task_id, "association_kind": snapshot.association_kind,
             "provenance": [list(p) for p in snapshot.provenance],
             "principal": principal, **evidence},
            principal, int(time.time()))
    if str(state).lower() != "open":
        return _unresolved(
            CLOSED_UNMERGED,
            f"the pull request is {state} and was not merged",
            "Reopen the existing pull request or decide its replacement through "
            "the ordinary owner path; a resumed run must not substitute a new PR.",
            snapshot, principal, pr_url, evidence)

    # Re-read after the checkout inspection: everything above was collected over
    # time, and a head that moved while we looked is a different pull request
    # than the one we are about to authorise.
    try:
        fresh = budget.call(api, f"repos/{repo}/pulls/{number}")
        f_state, f_merged, f_head_ref, f_head_sha, *_ = _fields(fresh)
    except (TransportError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unresolved(
            UNAVAILABLE, f"the confirming re-read failed ({type(exc).__name__})",
            "Retry; the dispatcher does this on a bounded schedule.",
            snapshot, principal, pr_url, evidence)
    if (f_head_sha, f_head_ref, bool(f_merged), str(f_state).lower()) != (
            head_sha, head_ref, False, "open"):
        return _unresolved(
            UNAVAILABLE, "the pull request changed while it was being observed",
            "Retry; the dispatcher does this on a bounded schedule.",
            snapshot, principal, pr_url, evidence)

    return Observation(
        OPEN, pr_url, "one open pull request, on this task's branch and head",
        "Resume the existing pull request; do not open another.",
        {"version": RECONCILIATION_VERSION, "classification": OPEN,
         "task_id": snapshot.task_id, "association_kind": snapshot.association_kind,
         "provenance": [list(p) for p in snapshot.provenance],
         "principal": principal, **evidence},
        principal, int(time.time()))


def _fields(detail: dict[str, Any]):
    head, base = detail["head"], detail["base"]
    return (detail["state"], detail.get("merged", False), head["ref"], head["sha"],
            (head.get("repo") or {}).get("full_name"), base["ref"],
            (base.get("repo") or {}).get("full_name"))


class _Budget:
    """Total requests and total elapsed time, not just per-request timeouts."""

    def __init__(self, deadline: float) -> None:
        self.deadline = min(deadline, time.time() + TOTAL_DEADLINE_SECONDS)
        self.used = 0

    def call(self, api: Callable[[str], Any], endpoint: str) -> Any:
        if self.used >= MAX_REQUESTS:
            raise TransportError("request budget exhausted")
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise TransportError("deadline exhausted")
        self.used += 1
        return api(endpoint)


# --- phase 3: admit ----------------------------------------------------------

def admit(
    conn: sqlite3.Connection, snapshot: Snapshot, observation: Observation,
    *, now: Optional[int] = None,
) -> Optional[int]:
    """Issue a single-use receipt for a resolved observation, or return None.

    The captured tuple is compared again inside the write transaction. Anything
    that moved between :func:`capture` and here — a comment, a newer run, a
    promotion, a writer — refuses, because the observation was about a card that
    no longer exists in that state.
    """
    now = int(time.time()) if now is None else now
    if observation.classification not in (OPEN, MERGED) or not observation.pr_url:
        return None
    from hermes_cli.kanban_db_connect import write_txn

    try:
        with write_txn(conn):
            if not _tuple_unchanged(conn, snapshot):
                return None
            return _resume.issue_reconciled(
                conn, snapshot.task_id, pr_url=observation.pr_url,
                principal=observation.principal, evidence=observation.evidence,
                continuation_mode=observation.continuation_mode, now=now)
    except _resume.ResumeAuthorityError:
        return None


def _tuple_unchanged(conn: sqlite3.Connection, snapshot: Snapshot) -> bool:
    try:
        current = capture(conn, snapshot.task_id,
                          window_seconds=_assoc_window(), now=int(time.time()))
    except _resume.ResumeAuthorityError:
        return False
    return (current.board_identity, current.run_id, current.occurrence_event_id,
            current.comment_digest, current.lifecycle, current.assignee,
            current.workspace_kind, current.workspace_path, current.branch_name,
            current.candidates) == (
        snapshot.board_identity, snapshot.run_id, snapshot.occurrence_event_id,
        snapshot.comment_digest, snapshot.lifecycle, snapshot.assignee,
        snapshot.workspace_kind, snapshot.workspace_path, snapshot.branch_name,
        snapshot.candidates)


def _assoc_window() -> int:
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_WINDOW
    return _RESPAWN_GUARD_PR_WINDOW


# --- bounded retry bookkeeping ----------------------------------------------

def record_unresolved(
    conn: sqlite3.Connection, snapshot: Snapshot, observation: Observation,
    *, now: Optional[int] = None,
) -> None:
    """Persist a classified attempt and when the next one is due.

    Upserted on ``(task_id, evidence_key)``, so a card whose situation has not
    changed accumulates one row and a growing backoff rather than an event per
    tick. Nothing here touches ``consecutive_failures``,
    ``last_failure_error`` or any quota state: a pull request we could not read
    is not a task failure and must not trip the breaker.
    """
    now = int(time.time()) if now is None else now
    key = snapshot.evidence_key()
    from hermes_cli.kanban_db_connect import write_txn

    with write_txn(conn):
        row = conn.execute(
            "SELECT attempts FROM task_pr_reconcile_state WHERE task_id = ? "
            "AND evidence_key = ?", (snapshot.task_id, key)).fetchone()
        attempts = (int(row["attempts"]) if row is not None else 0) + 1
        index = min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
        next_at = now + RETRY_BACKOFF_SECONDS[index]
        if row is None:
            conn.execute(
                "INSERT INTO task_pr_reconcile_state "
                "(task_id, evidence_key, classification, detail, attempts, "
                " first_at, last_at, next_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot.task_id, key, observation.classification,
                 observation.detail, attempts, now, now, next_at))
        else:
            conn.execute(
                "UPDATE task_pr_reconcile_state SET classification = ?, detail = ?, "
                "attempts = ?, last_at = ?, next_at = ? "
                "WHERE task_id = ? AND evidence_key = ?",
                (observation.classification, observation.detail, attempts, now,
                 next_at, snapshot.task_id, key))


def exhausted(attempts: int) -> bool:
    return attempts > len(RETRY_BACKOFF_SECONDS)


def due(conn: sqlite3.Connection, snapshot: Snapshot, *, now: Optional[int] = None) -> bool:
    """Whether this exact situation may be observed again.

    Unknown evidence is due immediately: a key with no row is a situation the
    reconciler has never seen. An exhausted schedule stays undue until the
    evidence itself changes, which produces a different key and therefore a
    fresh row.
    """
    now = int(time.time()) if now is None else now
    row = conn.execute(
        "SELECT attempts, next_at FROM task_pr_reconcile_state WHERE task_id = ? "
        "AND evidence_key = ?", (snapshot.task_id, snapshot.evidence_key())).fetchone()
    if row is None:
        return True
    if exhausted(int(row["attempts"])):
        return False
    return now >= int(row["next_at"])
