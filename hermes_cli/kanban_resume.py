"""Explicit, single-use authority to resume ONE task on ONE existing pull request.

The dispatcher refuses to respawn a task once a recent comment carries a GitHub
PR URL (``check_respawn_guard`` -> ``"active_pr"``). That guard is load-bearing:
without it a crashed worker's successor opens a second PR for the same work, and
the duplicate-PR cluster is the single most expensive failure this repo has had.

But it has no exit. When a worker dies *mid-PR* — the branch is pushed, the PR is
open, the work is unfinished — the card is parked permanently, and the only ways
out are to weaken the guard for everyone or to hand-run the work outside the
board. This module is the third option: an operator states, once, "resume THIS
task, continuing THAT pull request", and the dispatcher honours it exactly once.

What the receipt is, precisely
------------------------------

A receipt names a **board, task, terminal run and pull request**, and carries a
digest of *all* the PR evidence that existed when it was issued. It bypasses
``active_pr`` and **nothing else**. Quota and auth blockers, the rate-limit
cooldown, the recent-success window, dependency gating, review routing, the
failure counter and every concurrency guard are untouched — a receipt is not a
force flag, and it cannot make an ineligible task eligible.

Why the digest, and not an author or a timestamp
------------------------------------------------

Comments record an author, a body and a time. Their ``commented`` events record
an author and a body *length*. None of that identifies a particular run's
comment: two workers commenting in the same second are indistinguishable, and an
author string is submitted data, not authority. So a receipt binds to comment
**ids** and to the exact URLs those comments contain. If a comment is added,
removed, or carries a different PR, the digest changes and the receipt stops
matching — which is the behaviour we want, because the operator authorised the
situation they looked at, not a later one.

Evidence the receipt does not cover is never exempted. A receipt for PR #7 does
not clear a second comment linking PR #9; the guard still fires, and it should —
that card now has two live PRs and a human should say which one continues.

Legacy ambiguity fails closed: a board whose comment rows predate ids, or whose
PR evidence cannot be enumerated, produces no digest and therefore no receipt.

Single use, consumed inside the claim
-------------------------------------

Validating a receipt and then claiming is two steps, and anything can happen
between them — a comment arrives, a worker starts, another dispatcher claims
first. So the receipt is re-validated and consumed **inside** the claim's write
transaction, before ``_reclaim_dangling_run`` touches anything: reclaiming first
would erase the very run state the validation reads. Consumption is a CAS, so
two dispatchers racing the same receipt produce one run, not two.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Iterable, Optional

# Same shape the dispatcher's guard matches, kept here so a receipt is written
# against exactly the evidence the guard reads.
PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

# A receipt is a statement about a situation the operator just looked at. Well
# past a dispatcher tick and a spawn, nowhere near "standing permission".
RECEIPT_TTL_SECONDS = 3600  # 1 hour

SCHEMA_SQL = """
-- One-shot authority to bypass the ``active_pr`` respawn guard for a single
-- task, continuing a single named pull request. Never a general force flag; see
-- hermes_cli/kanban_resume.py for the full contract.
CREATE TABLE IF NOT EXISTS task_resume_receipts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        TEXT NOT NULL,
    run_id         INTEGER NOT NULL,
    pr_url         TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    issued_by      TEXT NOT NULL,
    issued_at      INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    consumed_at    INTEGER,
    consumed_run_id INTEGER,
    revoked_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_resume_receipts_task
    ON task_resume_receipts(task_id, consumed_at, revoked_at);
"""


class ResumeAuthorityError(Exception):
    """The receipt could not be issued. The card is left exactly as it was."""


def normalize_pr_url(raw: str) -> str:
    """Lowercase the origin, keep the path: PR identity is case-insensitive in
    host and owner/repo, and a trailing slash or fragment is not a different PR."""
    match = PR_URL_RE.search(raw or "")
    if match is None:
        raise ResumeAuthorityError(f"not a GitHub pull request URL: {raw!r}")
    return match.group(0).lower().rstrip("/")


def pr_evidence(conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
                now: Optional[int] = None) -> list[tuple[int, tuple[str, ...]]]:
    """Every PR-bearing comment the guard can see, as ``(comment_id, urls)``.

    Ordered and de-duplicated so the digest is a function of the evidence rather
    than of row order. A comment carrying two different PRs contributes both:
    "one URL matched" is not "this comment is about the authorised PR".
    """
    now = int(time.time()) if now is None else now
    rows = conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ? AND created_at >= ? "
        "ORDER BY id ASC",
        (task_id, now - window_seconds),
    ).fetchall()
    evidence: list[tuple[int, tuple[str, ...]]] = []
    for row in rows:
        urls = tuple(sorted({
            match.group(0).lower().rstrip("/")
            for match in PR_URL_RE.finditer(row["body"] or "")
        }))
        if not urls:
            continue
        comment_id = row["id"]
        if comment_id is None:
            # Pre-id evidence cannot be bound to, and an unbindable receipt is
            # exactly the ambiguous provenance this module refuses to invent.
            raise ResumeAuthorityError(
                "this board has PR evidence that cannot be identified by comment id; "
                "resume authority is refused rather than guessed")
        evidence.append((int(comment_id), urls))
    return evidence


def evidence_digest(evidence: Iterable[tuple[int, tuple[str, ...]]]) -> str:
    """A stable digest of the whole evidence set.

    Any change — a comment added, a comment removed, a different PR linked —
    changes this, and a receipt whose digest no longer matches is not honoured.
    """
    canonical = json.dumps(
        [[comment_id, list(urls)] for comment_id, urls in evidence],
        separators=(",", ":"), sort_keys=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def authorized_pr_urls(evidence: Iterable[tuple[int, tuple[str, ...]]]) -> set[str]:
    """Every distinct PR named anywhere in the evidence."""
    return {url for _, urls in evidence for url in urls}


def exempt_reason(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
    now: Optional[int] = None,
) -> Optional[int]:
    """The id of a live receipt that authorises today's evidence, or ``None``.

    Read-only, and deliberately strict. Returning an id here does not consume
    anything: the claim re-runs this inside its own transaction and consumes the
    receipt there, because between this read and that claim a comment can land.
    """
    now = int(time.time()) if now is None else now
    try:
        evidence = pr_evidence(conn, task_id, window_seconds=window_seconds, now=now)
    except ResumeAuthorityError:
        return None
    if not evidence:
        return None
    digest = evidence_digest(evidence)
    urls = authorized_pr_urls(evidence)
    if len(urls) != 1:
        # More than one live PR on the card. A receipt names one lineage, and
        # exempting it would leave the other unexamined.
        return None
    (only_url,) = tuple(urls)

    row = conn.execute(
        """
        SELECT id FROM task_resume_receipts
         WHERE task_id = ? AND pr_url = ? AND evidence_digest = ?
           AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
         ORDER BY id DESC LIMIT 1
        """,
        (task_id, only_url, digest, now),
    ).fetchone()
    return None if row is None else int(row["id"])


def consume(
    conn: sqlite3.Connection, receipt_id: int, task_id: str, *,
    window_seconds: int, now: Optional[int] = None,
) -> bool:
    """Re-validate and atomically spend ``receipt_id``. Caller holds the write txn.

    Called from inside ``claim_task``'s transaction and BEFORE any run is
    reclaimed. Revalidation here is the whole point: the guard check that
    selected this receipt ran outside a transaction, and the evidence may have
    moved since.

    The UPDATE is the CAS. Two dispatchers reaching this line with the same
    receipt both re-validate successfully; exactly one of them changes a row.
    """
    now = int(time.time()) if now is None else now
    if exempt_reason(conn, task_id, window_seconds=window_seconds, now=now) != int(receipt_id):
        return False
    cur = conn.execute(
        "UPDATE task_resume_receipts SET consumed_at = ? "
        " WHERE id = ? AND task_id = ? AND consumed_at IS NULL AND revoked_at IS NULL "
        "   AND expires_at > ?",
        (now, int(receipt_id), task_id, now),
    )
    return cur.rowcount == 1


def attach_run(conn: sqlite3.Connection, receipt_id: int, run_id: int) -> None:
    """Record which run the spent receipt actually opened (audit, not authority)."""
    conn.execute(
        "UPDATE task_resume_receipts SET consumed_run_id = ? WHERE id = ?",
        (int(run_id), int(receipt_id)),
    )


# --- issuing -----------------------------------------------------------------

def _live_writer_reason(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Why this task still has a writer, or ``None`` when it plainly does not.

    "No active writer" is checked over the task row AND the run rows, because
    they can disagree: a crashed worker leaves an unfinished run behind while the
    task has already been released. Either one is a reason to refuse — resuming a
    card someone is still working is how two workers end up on one PR.
    """
    task = conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if task is None:
        return "task does not exist on this board"
    if task["status"] == "running":
        return "the task is running"
    if task["claim_lock"] is not None:
        expires = task["claim_expires"]
        if expires is None or int(expires) > now:
            return f"the task is claimed by {task['claim_lock']}"
    if task["current_run_id"] is not None:
        return f"run {task['current_run_id']} is still open on the task"
    open_run = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if open_run is not None:
        return f"run {open_run['id']} has not ended"
    return None


def issue(
    conn: sqlite3.Connection, task_id: str, *, run_id: int, pr_url: str,
    issued_by: str, window_seconds: int, now: Optional[int] = None,
    ttl_seconds: int = RECEIPT_TTL_SECONDS,
) -> int:
    """Record one-shot authority to resume ``task_id`` on ``pr_url``; return its id.

    ``issued_by`` is the *authenticated operator context* of the surface that
    called this — never an author string carried on a request body, and never
    read back out as authority. It is an audit field.

    ``conn`` is the board. There is no board parameter because there is no
    cross-board receipt: a receipt lives in the same database as the task it
    authorises, so a receipt issued on one board cannot be spent on another.

    Every check here is re-run at consumption time. Doing them here as well is
    what makes a refusal legible to the operator at the moment they ask, instead
    of a card that silently keeps not spawning.
    """
    now = int(time.time()) if now is None else now
    normalized = normalize_pr_url(pr_url)

    blocked = _live_writer_reason(conn, task_id, now)
    if blocked is not None:
        raise ResumeAuthorityError(f"cannot authorise a resume: {blocked}")

    run = conn.execute(
        "SELECT id, ended_at FROM task_runs WHERE id = ? AND task_id = ?",
        (int(run_id), task_id),
    ).fetchone()
    if run is None:
        # Includes the wrong-task and wrong-board cases: a run id from another
        # card does not name a run of this one, and a run id from another board
        # is not in this database at all.
        raise ResumeAuthorityError(
            f"run {run_id} does not belong to task {task_id} on this board")
    if run["ended_at"] is None:
        raise ResumeAuthorityError(
            f"run {run_id} has not ended; resume authority is for a finished attempt")

    evidence = pr_evidence(conn, task_id, window_seconds=window_seconds, now=now)
    if not evidence:
        raise ResumeAuthorityError(
            f"task {task_id} has no recent pull-request evidence to resume; "
            "the respawn guard this authorises is not what is holding it")
    urls = authorized_pr_urls(evidence)
    if normalized not in urls:
        raise ResumeAuthorityError(
            f"{normalized} is not among this task's recent pull requests: "
            f"{', '.join(sorted(urls))}")
    if len(urls) > 1:
        raise ResumeAuthorityError(
            "this task references more than one pull request "
            f"({', '.join(sorted(urls))}); a receipt exempts one lineage and "
            "would leave the others unexamined")

    cur = conn.execute(
        "INSERT INTO task_resume_receipts "
        "(task_id, run_id, pr_url, evidence_digest, issued_by, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, int(run_id), normalized, evidence_digest(evidence),
         issued_by, now, now + int(ttl_seconds)),
    )
    return int(cur.lastrowid)
