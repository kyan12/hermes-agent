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

* Inspect bounded hints before deciding ownership. Multiple verified lineages
  are ambiguous; unrelated-only observations use separate atomic claim evidence.
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
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Optional

from hermes_cli import kanban_pr_association as _assoc
from hermes_cli import kanban_resume as _resume

RECONCILIATION_VERSION = 2

# Classifications. ``OPEN`` and ``MERGED`` admit; the rest schedule a retry.
OPEN = "open"
MERGED = "merged"
CLOSED_UNMERGED = "closed_unmerged"
AMBIGUOUS = "ambiguous"
UNAVAILABLE = "unavailable"
NO_ASSOCIATED_PR = "no_associated_pr"
INSPECTION_LIMIT = "inspection_limit"

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
# retries on changed evidence or a slower scheduled authenticated observation.
# The slower interval permits auth/remote recovery without a per-attempt click.
RETRY_BACKOFF_SECONDS = (60, 120, 300)
RENEWED_OBSERVATION_SECONDS = 3600

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
    provenance: tuple[tuple[str, str, str, str], ...]
    captured_at: int
    checkout: Optional[dict[str, Any]] = None
    principal: str = ""

    def evidence_key(self, *, include_principal: bool = True) -> str:
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
            self.checkout, self.provenance,
        ], separators=(",", ":"), sort_keys=True)
        local_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if not include_principal:
            return local_key
        return local_key + ":" + hashlib.sha256(self.principal.encode("utf-8")).hexdigest()


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


@dataclass(frozen=True)
class PRClearance:
    """A no-associated-PR observation; never an owned-PR resume receipt."""

    snapshot: Snapshot
    observation: Observation


def validate_clearance(conn, task_id: str, clearance: PRClearance) -> bool:
    from hermes_cli.kanban_db_dispatch import check_respawn_guard

    snapshot, observation = clearance.snapshot, clearance.observation
    return (snapshot.task_id == task_id
            and observation.classification == NO_ASSOCIATED_PR
            and observation.pr_url is None
            and observation.principal == snapshot.principal
            and _tuple_unchanged(conn, snapshot)
            and checkout_matches(conn, task_id, observation.evidence)
            and _resume.writer_state(conn, task_id, int(time.time())) is None
            and check_respawn_guard(conn, task_id) in (None, "active_pr"))


def clearance_workspace(conn, task_id: str, clearance: PRClearance, *, run_id: int, claim_lock: str) -> str:
    if (not _resume.post_claim_matches(conn, task_id, asdict(clearance.snapshot),
                                       run_id=run_id, claim_lock=claim_lock)
            or clearance.observation.principal != clearance.snapshot.principal
            or not checkout_matches(conn, task_id, clearance.observation.evidence)
            or _resume.shared_checkout_writer(conn, task_id, int(time.time()))):
        raise _resume.ResumeAuthorityError("no-associated-PR checkout evidence changed")
    return clearance.snapshot.workspace_path


# --- phase 1: capture --------------------------------------------------------

def capture(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
    now: Optional[int] = None, principal: str = "",
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
        (a.pr_url, a.source, a.source_id, a.payload_digest)
        for a in _assoc.structured_associations(conn, task_id))
    if provenance:
        candidates = {p[0] for p in provenance}
    else:
        candidates = _assoc.historical_hints(conn, task_id)
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
        checkout=_checkout_identity(base["workspace_path"]), principal=principal,
    )


# --- phase 2: observe (no database) -----------------------------------------

def _gh_api(endpoint: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> Any:
    """Read at most the byte cap plus one sentinel byte, under one deadline.

    Builds without nonblocking subprocess pipes fail closed with TransportError;
    no blocking reader is left behind as a platform fallback.
    """
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            ["gh", "api", endpoint, "--hostname", "github.com"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise TransportError(type(exc).__name__) from None
    try:
        try:
            os.set_blocking(process.stdout.fileno(), False)
        except (OSError, AttributeError):
            raise TransportError("nonblocking subprocess pipes are unsupported by this Python/platform") from None
        raw = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError("request deadline exhausted")
            try:
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
            except BlockingIOError:
                time.sleep(min(0.01, remaining))
                continue
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TransportError("response exceeded the byte bound")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode or time.monotonic() >= deadline:
            raise TransportError("request failed or exceeded deadline")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise TransportError("response was not JSON") from None
    except subprocess.TimeoutExpired:
        raise TransportError("request deadline exhausted") from None
    finally:
        # Closing our end is sufficient even when a descendant owns the writer.
        # Only the immediate Popen child is ours to terminate; no process-group kill.
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            raise TransportError("owned gh child did not exit within cleanup grace") from None


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
    remotes = subprocess.run(
        ["git", "config", "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
        cwd=str(target), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=REQUEST_TIMEOUT_SECONDS,
    )
    repositories = set()
    for line in remotes.stdout.splitlines():
        _, _, url = line.partition(" ")
        match = re.fullmatch(
            r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
            r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url)
        if match:
            repositories.add(match.group(1).lower())
    return {
        "repositories": sorted(repositories),
        "realpath": os.path.realpath(str(target)),
        "git_dir": str(git_dir), "common_dir": str(common_dir),
        "linked_worktree": str(git_dir) != str(common_dir),
        "branch": _kbw._git_current_branch(target),
        "head": head.stdout.strip() if head.returncode == 0 else None,
    }


def checkout_matches(conn, task_id: str, evidence: dict[str, Any]) -> bool:
    """Reinspect local authority at claim and immediately before spawn."""
    row = conn.execute(
        "SELECT workspace_path, branch_name FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or row["branch_name"] != evidence.get("branch"):
        return False
    provenance = [[a.pr_url, a.source, a.source_id, a.payload_digest]
                  for a in _assoc.structured_associations(conn, task_id)]
    if provenance != evidence.get("provenance"):
        return False
    if not 0 <= time.time() - evidence.get("observed_at", 0) <= TOTAL_DEADLINE_SECONDS:
        return False
    try:
        return _checkout_identity(row["workspace_path"]) == evidence.get("checkout")
    except (OSError, subprocess.SubprocessError):
        return False


def resume_workspace(conn, task_id: str, receipt_id: int, *, run_id: int, claim_lock: str) -> str:
    """A consumed receipt can only launch in the checkout it verified."""
    row = conn.execute(
        "SELECT * FROM task_resume_receipts "
        "WHERE id=? AND task_id=? AND consumed_run_id=("
        "SELECT current_run_id FROM tasks WHERE id=?)", (receipt_id, task_id, task_id)
    ).fetchone()
    if (row is None or row["consumed_run_id"] != run_id or row["revoked_at"] is not None
            or row["expires_at"] <= int(time.time())
            or not _resume.post_claim_matches(conn, task_id, row, run_id=run_id, claim_lock=claim_lock)):
        raise _resume.ResumeAuthorityError("resume receipt no longer authorizes the intended claim")
    writer = _resume.shared_checkout_writer(conn, task_id, int(time.time()))
    if writer:
        raise _resume.ResumeAuthorityError(writer)
    if row["authority_kind"] == _resume.RECONCILED:
        evidence = json.loads(row["reconciliation"])
        if (evidence.get("principal") != row["issued_by"] or evidence.get("task_id") != task_id
                or not checkout_matches(conn, task_id, evidence)):
            raise _resume.ResumeAuthorityError("resume checkout evidence changed or expired")
    if not row["workspace_path"] or not os.path.isdir(row["workspace_path"]):
        raise _resume.ResumeAuthorityError("resume checkout is unavailable")
    return row["workspace_path"]


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


def collect(snapshot: Snapshot, *, deadline: float) -> tuple[Snapshot, Observation]:
    """Resolve the existing authenticated account without reading credentials."""
    budget = _Budget(deadline)
    try:
        user = budget.call(_gh_api, "user")
        account_id = user["id"]
        if type(account_id) is not int or account_id <= 0:
            raise TransportError("authenticated account identity missing")
    except (TransportError, KeyError, TypeError):
        snapshot = replace(snapshot, principal="github:unavailable")
        return snapshot, _unresolved("auth_unavailable", "existing gh account could not be authenticated",
            "Restore authentication through the existing authenticated owner path. "
            "A scheduled renewed observation retries automatically after cooldown.",
            snapshot, snapshot.principal)
    snapshot = replace(snapshot, principal=f"github:{account_id}")
    return snapshot, observe(snapshot, deadline=deadline, principal=snapshot.principal, budget=budget)


def observe(
    snapshot: Snapshot, *, deadline: float, principal: str,
    api: Optional[Callable[[str], Any]] = None, budget: Optional[_Budget] = None,
) -> Observation:
    """Collect bounded evidence about ``snapshot``'s candidate pull request.

    Touches no database. ``api`` is injected so the transport can be exercised
    without a network; the default is the authenticated ``gh`` CLI.
    """
    api = _gh_api if api is None else api
    if snapshot.checkout is None:
        return _unresolved(
            _assoc.HISTORY_UNRESOLVED,
            "interrupted publication history has no inspectable recorded checkout",
            "The authenticated task owner must restore the existing checkout path/branch "
            "and, if known, the exact completion_contract through the existing task "
            "editing path. The dispatcher retries; no replacement PR is authorized.",
            snapshot, principal)
    if not snapshot.candidates:
        return _unresolved(
            UNAVAILABLE, "the card names no pull request to reconcile",
            "Nothing to continue; the hold is not an active_pr hold.",
            snapshot, principal)
    checkout = _checkout_identity(snapshot.workspace_path)
    if checkout != snapshot.checkout or not checkout["branch"] or (
            snapshot.branch_name and checkout["branch"] != snapshot.branch_name):
        return _unresolved(UNAVAILABLE, "recorded checkout branch or identity changed",
                           "Restore the recorded task checkout before retrying.", snapshot, principal)
    budget = budget if budget is not None else _Budget(deadline)
    if len(snapshot.candidates) > MAX_REQUESTS - budget.used:
        return _unresolved(INSPECTION_LIMIT, "candidate set exceeds bounded inspection limit",
            "The authenticated task owner can supply the exact completion_contract; "
            "otherwise bounded reconciliation will retry.", snapshot, principal)
    expected_branch = snapshot.branch_name or checkout["branch"]
    associated = []
    inspected = []
    try:
        for candidate in snapshot.candidates:
            parsed = _assoc.repo_and_number(candidate)
            if parsed is None:
                raise TransportError("invalid PR identity")
            candidate_repo, candidate_number = parsed
            detail = budget.call(api, f"repos/{candidate_repo}/pulls/{candidate_number}")
            fields = _fields(detail)
            _, _, ref, _, head_repository, _, base_repository = fields
            if not head_repository or not base_repository or base_repository.lower() != candidate_repo.lower():
                raise TransportError("missing or inconsistent repository identity")
            owns = (ref == expected_branch and
                    {head_repository.lower(), base_repository.lower()}.issubset(checkout["repositories"]))
            inspected.append({"pr_url": candidate, "head_ref": ref,
                              "head_repo": head_repository, "base_repo": base_repository})
            if owns:
                associated.append((candidate, candidate_repo, candidate_number, fields))
            elif snapshot.provenance:
                return _unresolved(UNAVAILABLE, "structured PR branch or repositories differ from checkout",
                    "Reconcile the existing contract and recorded checkout through the authenticated owner.",
                    snapshot, principal, candidate)
    except (TransportError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unresolved(UNAVAILABLE, f"pull request evidence unavailable ({type(exc).__name__})",
            "Check existing gh authentication and API reachability; bounded reconciliation retries.",
            snapshot, principal)
    if not associated:
        return _unresolved(NO_ASSOCIATED_PR, "all bounded hints belong to other repositories or branches",
            "Ordinary execution requires atomic revalidation of this observation.", snapshot, principal,
            evidence={"checkout": checkout, "branch": snapshot.branch_name,
                      "observed_at": int(time.time()), "inspected": inspected, "requests": budget.used})
    if len(associated) > 1:
        return _unresolved(AMBIGUOUS, "multiple PRs match this checkout's repository and branch",
            "The authenticated task owner must reconcile the competing PR lineages.", snapshot, principal)
    pr_url, repo, number, fields = associated[0]
    state, merged, head_ref, head_sha, head_repo, base_ref, base_repo = fields

    evidence = {
        "pr_url": pr_url, "repo": repo, "number": number, "state": state,
        "merged": bool(merged), "head_repo": head_repo, "head_ref": head_ref,
        "head_sha": head_sha, "base_repo": base_repo, "base_ref": base_ref,
        "checkout": checkout, "branch": snapshot.branch_name,
        "observed_at": int(time.time()), "requests": budget.used,
    }

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

    if budget.used >= MAX_REQUESTS:
        return _unresolved(INSPECTION_LIMIT, "confirming read exceeds request budget",
            "The authenticated owner can supply the exact completion_contract to narrow inspection.",
            snapshot, principal, pr_url, evidence)

    # Re-read after the checkout inspection: everything above was collected over
    # time, and a head that moved while we looked is a different pull request
    # than the one we are about to authorise.
    try:
        fresh = budget.call(api, f"repos/{repo}/pulls/{number}")
        fresh_fields = _fields(fresh)
        f_state, f_merged, f_head_ref, f_head_sha, *_ = fresh_fields
    except (TransportError, KeyError, TypeError, ValueError, AttributeError) as exc:
        return _unresolved(
            UNAVAILABLE, f"the confirming re-read failed ({type(exc).__name__})",
            "Retry; the dispatcher does this on a bounded schedule.",
            snapshot, principal, pr_url, evidence)
    if fresh_fields[4:] != (head_repo, base_ref, base_repo) or (f_head_sha, f_head_ref, bool(f_merged), str(f_state).lower()) != (
            head_sha, head_ref, False, "open"):
        return _unresolved(
            UNAVAILABLE, "the pull request changed while it was being observed",
            "Retry; the dispatcher does this on a bounded schedule.",
            snapshot, principal, pr_url, evidence)

    evidence["requests"] = budget.used
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
        result = (api(endpoint, timeout=min(remaining, REQUEST_TIMEOUT_SECONDS))
                  if api is _gh_api else api(endpoint))
        if time.time() >= self.deadline:
            raise TransportError("deadline exhausted")
        return result


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
            if (not _tuple_unchanged(conn, snapshot)
                    or not checkout_matches(conn, snapshot.task_id, observation.evidence)):
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
            current.candidates, current.provenance, current.checkout) == (
        snapshot.board_identity, snapshot.run_id, snapshot.occurrence_event_id,
        snapshot.comment_digest, snapshot.lifecycle, snapshot.assignee,
        snapshot.workspace_kind, snapshot.workspace_path, snapshot.branch_name,
        snapshot.candidates, snapshot.provenance, snapshot.checkout)


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
        next_at = now + (RENEWED_OBSERVATION_SECONDS if exhausted(attempts)
                         else RETRY_BACKOFF_SECONDS[index])
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

    Changed evidence is due immediately. Unchanged evidence follows short
    backoff, then scheduled renewed authentication/observation after exhaustion.
    """
    now = int(time.time()) if now is None else now
    if snapshot.principal:
        row = conn.execute(
            "SELECT attempts, next_at FROM task_pr_reconcile_state WHERE task_id=? AND evidence_key=?",
            (snapshot.task_id, snapshot.evidence_key())).fetchone()
    else:
        # Authentication is itself budgeted: first wait on unchanged local input,
        # then resolve the account and bind the new attempt to that principal.
        row = conn.execute(
            "SELECT attempts, next_at FROM task_pr_reconcile_state WHERE task_id=? "
            "AND evidence_key LIKE ? ORDER BY last_at DESC, next_at DESC LIMIT 1",
            (snapshot.task_id, snapshot.evidence_key(include_principal=False) + ":%")).fetchone()
    return row is None or now >= int(row["next_at"])
