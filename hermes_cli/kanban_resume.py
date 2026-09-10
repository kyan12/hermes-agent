"""Single-use authority to resume ONE task on ONE existing pull request.

The dispatcher refuses to respawn a task that owns a pull request
(``check_respawn_guard`` -> ``"active_pr"``). Without that guard a crashed
worker's successor opens a second PR for the same work. With it, a worker that
died mid-PR parks its card forever.

A receipt is the narrow exit. It bypasses ``active_pr`` and nothing else. There
are two ways to earn one, and they are kept apart on purpose:

``attested``
    An authenticated operator confirmed an exact snapshot (below). This is the
    original path and it is unchanged.
``reconciled``
    The dispatcher's own principal verified against GitHub that the pull request
    is open (or merged) on this task's own branch and head — see
    :mod:`hermes_cli.kanban_pr_reconcile` and :func:`issue_reconciled`. Routine
    recovery must not require a human to click through an attestation, but a
    machine decision must never be *recorded* as one.

Either way the receipt names the same exact snapshot:

    board identity, task, latest terminal run BY ID, current occurrence, current
    lifecycle/assignee/workspace, the PR, and a digest of every comment's full
    content.

Provenance is never **reconstructed from prose**. ``task_comments`` has no run
id, so comment ownership by a historical run cannot be proven and is not
claimed. An operator states, now, that this PR is the continuation target for
this run and this occurrence, having seen this exact comment set; the reconciler
instead proves it from structured provenance or from the branch and head of the
task's own checkout. Neither reads ownership out of who a comment says wrote it.

The whole snapshot and the writer state are revalidated inside the claim
transaction, before any run is reclaimed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from typing import Any, Optional

PR_URL_RE = re.compile(r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)

RECEIPT_TTL_SECONDS = 3600

# Events that open or close an occurrence. Deliberately excludes ``commented``
# and ``respawn_guarded``: those are traffic, not a new attempt, and the comment
# digest already covers the former.
OCCURRENCE_EVENT_KINDS = (
    "claimed", "blocked", "unblocked", "scheduled", "status", "reclaimed",
    "gave_up", "completed", "promoted", "promoted_manual", "review_requested",
    "changes_requested", "reopened",
)

# The only lifecycle positions a resume may be authorised from: the card is
# waiting, not running. No transition is manufactured to get here.
RESUMABLE_STATUSES = ("ready", "todo")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_resume_receipts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    board_identity      TEXT NOT NULL,
    run_id              INTEGER NOT NULL,
    occurrence_event_id INTEGER NOT NULL,
    pr_url              TEXT NOT NULL,
    comment_digest      TEXT NOT NULL,
    lifecycle           TEXT NOT NULL,
    assignee            TEXT,
    workspace_kind      TEXT,
    workspace_path      TEXT,
    issued_by           TEXT NOT NULL,
    issued_at           INTEGER NOT NULL,
    expires_at          INTEGER NOT NULL,
    consumed_at         INTEGER,
    consumed_run_id     INTEGER,
    revoked_at          INTEGER,
    -- 'attested' (an operator confirmed this exact tuple) or 'reconciled' (the
    -- dispatcher's own principal verified the pull request against the task's
    -- checkout). Kept apart on purpose: automatic authority must never be
    -- readable as a human attestation in an audit.
    authority_kind      TEXT NOT NULL DEFAULT 'attested',
    -- Versioned JSON evidence for a reconciled receipt; NULL when attested.
    reconciliation      TEXT,
    -- 'resume_open_pr' or 'merged_closeout'. NULL = ordinary resume.
    continuation_mode   TEXT
);
CREATE INDEX IF NOT EXISTS idx_resume_receipts_task
    ON task_resume_receipts(task_id, consumed_at, revoked_at);
"""


# ``CREATE TABLE IF NOT EXISTS`` does nothing to a board that already has the
# table, so boards created before these columns existed need them added
# explicitly. ``kanban_db_connect._migrate_add_optional_columns`` reads this.
ADDITIVE_RECEIPT_COLUMNS = (
    ("authority_kind", "authority_kind TEXT NOT NULL DEFAULT 'attested'"),
    ("reconciliation", "reconciliation TEXT"),
    ("continuation_mode", "continuation_mode TEXT"),
)

ATTESTED = "attested"
RECONCILED = "reconciled"

# A reconciled receipt is only as good as the observation behind it, and remote
# state moves. Much shorter than the attested TTL: the dispatcher re-observes
# rather than leaning on a stale look.
RECONCILED_TTL_SECONDS = 300


class ResumeAuthorityError(Exception):
    """The receipt could not be issued. The card is left exactly as it was."""


def normalize_pr_url(raw: str) -> str:
    match = PR_URL_RE.search(raw or "")
    if match is None:
        raise ResumeAuthorityError(f"not a GitHub pull request URL: {raw!r}")
    return match.group(0).lower().rstrip("/")


# --- the snapshot a receipt binds -------------------------------------------

def board_identity(conn: sqlite3.Connection) -> str:
    """The resolved database this connection is actually attached to."""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row is not None and len(row) > 2 else None
    if not path:
        raise ResumeAuthorityError("this board has no resolvable database identity")
    return os.path.realpath(str(path))


def comments(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """EVERY comment on the task, full content, deterministic id order.

    Not just the PR-bearing ones: a comment saying "do not resume this" changes
    what the operator is agreeing to, and a digest that ignored it would let the
    receipt survive it.
    """
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? ORDER BY id ASC", (task_id,),
    ).fetchall()
    out = []
    for row in rows:
        if row["id"] is None:
            raise ResumeAuthorityError(
                "this board has comments without ids; they cannot be bound and "
                "resume authority is refused rather than guessed")
        out.append({
            "id": int(row["id"]), "task_id": row["task_id"], "author": row["author"],
            "body": row["body"] or "", "created_at": row["created_at"],
        })
    return out


def comment_digest(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(
        [[e["id"], e["task_id"], e["author"], e["body"], e["created_at"]] for e in entries],
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def pr_urls(entries: list[dict[str, Any]]) -> set[str]:
    return {m.group(0).lower().rstrip("/")
            for e in entries for m in PR_URL_RE.finditer(e["body"])}


def latest_terminal_run(conn: sqlite3.Connection, task_id: str):
    """The newest ended run BY ID. ``latest_run()`` orders by timestamp, which
    cannot break a tie between two runs that ended in the same second."""
    return conn.execute(
        "SELECT id, profile, outcome, ended_at, worker_pid FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def latest_occurrence_event_id(conn: sqlite3.Connection, task_id: str) -> int:
    placeholders = ", ".join("?" for _ in OCCURRENCE_EVENT_KINDS)
    row = conn.execute(
        f"SELECT COALESCE(MAX(id), 0) AS m FROM task_events "
        f"WHERE task_id = ? AND kind IN ({placeholders})",
        (task_id, *OCCURRENCE_EVENT_KINDS),
    ).fetchone()
    return int(row["m"])


def occurrence_events_since(conn: sqlite3.Connection, task_id: str, after_id: int) -> list[str]:
    placeholders = ", ".join("?" for _ in OCCURRENCE_EVENT_KINDS)
    return [r["kind"] for r in conn.execute(
        f"SELECT kind FROM task_events WHERE task_id = ? AND id > ? "
        f"AND kind IN ({placeholders}) ORDER BY id ASC",
        (task_id, int(after_id), *OCCURRENCE_EVENT_KINDS),
    ).fetchall()]


# The ONLY occurrence change a receipt survives: a card authorised while it was
# waiting on a parent gets promoted the ordinary way, once, and is then claimed.
# Anything else -- a block/unblock cycle, a review handoff, a second promotion --
# is a different occurrence and must not reuse the authority.
PERMITTED_TRANSITIONS = {
    ("todo", "ready"): ("promoted",),
    ("todo", "ready", "manual"): ("promoted_manual",),
}


def _transition_permitted(bound_lifecycle: str, lifecycle: str, kinds: list[str]) -> bool:
    if bound_lifecycle == lifecycle:
        return not kinds
    if (bound_lifecycle, lifecycle) != ("todo", "ready"):
        return False
    return kinds in (["promoted"], ["promoted_manual"])


def _pid_is_running(pid: Optional[int]) -> Optional[bool]:
    """True / False / None when it cannot be determined."""
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _task_writer_state(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    """Why this task may still have a writer, or ``None`` when it provably does not.

    Unknown is not clear: a pid we cannot classify refuses, because terminal
    bookkeeping does not prove a process stopped.
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
        # ANY lock, expired or not. The claim CAS requires `claim_lock IS NULL`,
        # so a stale lock is a claim that cannot succeed — and spending the
        # receipt on it would destroy single-use authority for nothing. The
        # reclaim passes own clearing it; this refuses until they have.
        return f"the task is claimed by {task['claim_lock']}"
    if task["current_run_id"] is not None:
        return f"run {task['current_run_id']} is still open on the task"
    open_run = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if open_run is not None:
        return f"run {open_run['id']} has not ended"

    pids = [("the task", task["worker_pid"])]
    pids += [(f"run {r['id']}", r["worker_pid"]) for r in conn.execute(
        "SELECT id, worker_pid FROM task_runs "
        "WHERE task_id = ? AND worker_pid IS NOT NULL", (task_id,)).fetchall()]
    for label, pid in pids:
        if pid is None:
            continue
        alive = _pid_is_running(pid)
        if alive is None:
            return f"{label} records pid {pid}, which could not be classified"
        if alive:
            return f"{label} records pid {pid}, which is still running"
    return None


def shared_checkout_writer(conn, task_id: str, now: int) -> Optional[str]:
    row = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or not row["workspace_path"]:
        return None
    checkout = os.path.realpath(row["workspace_path"])
    for other in conn.execute(
            "SELECT id, workspace_path FROM tasks WHERE id!=? AND workspace_path IS NOT NULL",
            (task_id,)).fetchall():
        if os.path.realpath(other["workspace_path"]) == checkout:
            reason = _task_writer_state(conn, other["id"], now)
            if reason:
                return f"shared checkout task {other['id']}: {reason}"
    return None


def writer_state(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
    return (_task_writer_state(conn, task_id, now)
            or shared_checkout_writer(conn, task_id, now))


def base_snapshot(conn: sqlite3.Connection, task_id: str, *, claimed_run_id: Optional[int] = None) -> dict[str, Any]:
    """Everything a receipt binds except the pull request itself.

    The PR comes from different places depending on who is asking: an operator
    confirms the one their card's comments name, while the dispatcher resumes
    the one the card's structured provenance (or its own branch) proves. The
    board, run, occurrence, comment digest, lifecycle and workspace binding are
    identical for both, and are the reason a receipt stops being authority the
    moment any of them moves.
    """
    task = conn.execute(
        "SELECT status, assignee, workspace_kind, workspace_path, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise ResumeAuthorityError(f"task {task_id} does not exist on this board")
    if claimed_run_id is not None:
        if task["status"] != "running" or task["current_run_id"] != claimed_run_id:
            raise ResumeAuthorityError("the intended claim is no longer current")
    elif task["status"] not in RESUMABLE_STATUSES:
        raise ResumeAuthorityError(
            f"task {task_id} is {task['status']}; a resume is authorised from "
            f"{' or '.join(RESUMABLE_STATUSES)}, and no transition is manufactured to get there")
    run = latest_terminal_run(conn, task_id)
    if run is None:
        raise ResumeAuthorityError(f"task {task_id} has no finished run to resume")
    entries = comments(conn, task_id)
    return {
        "board_identity": board_identity(conn),
        "task_id": task_id,
        "run_id": int(run["id"]),
        "run_profile": run["profile"],
        "run_outcome": run["outcome"],
        "occurrence_event_id": latest_occurrence_event_id(conn, task_id),
        "comment_digest": comment_digest(entries),
        "comments": entries,
        "lifecycle": task["status"],
        "assignee": task["assignee"],
        "workspace_kind": task["workspace_kind"],
        "workspace_path": task["workspace_path"],
    }


def post_claim_matches(conn, task_id: str, bound, *, run_id: int, claim_lock: str) -> bool:
    """Validate captured authority across exactly its own ready→running claim."""
    try:
        current = base_snapshot(conn, task_id, claimed_run_id=run_id)
        fields = set(_BOUND_FIELDS) - {"pr_url", "lifecycle", "occurrence_event_id"}
        if any(current[field] != bound[field] for field in fields):
            return False
        claim = conn.execute(
            "SELECT r.claim_lock, r.claim_expires, r.profile, r.status, r.ended_at, "
            "t.claim_lock AS task_lock, t.claim_expires AS task_expires "
            "FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=? AND r.task_id=?",
            (run_id, task_id)).fetchone()
        if (claim is None or claim["status"] != "running" or claim["ended_at"] is not None
                or not claim_lock or claim["claim_lock"] != claim_lock or claim["task_lock"] != claim_lock
                or claim["profile"] != bound["assignee"]
                or claim["claim_expires"] != claim["task_expires"]
                or claim["claim_expires"] <= int(time.time())):
            return False
        placeholders = ','.join('?' for _ in OCCURRENCE_EVENT_KINDS)
        events = conn.execute(
            f"SELECT kind, run_id, payload FROM task_events WHERE task_id=? AND id>? "
            f"AND kind IN ({placeholders}) ORDER BY id",
            (task_id, bound["occurrence_event_id"], *OCCURRENCE_EVENT_KINDS)).fetchall()
        if not events or events[-1]["kind"] != "claimed" or events[-1]["run_id"] != run_id:
            return False
        if not _transition_permitted(bound["lifecycle"], "ready", [e["kind"] for e in events[:-1]]):
            return False
        payload = json.loads(events[-1]["payload"])
        if (payload.get("lock"), payload.get("run_id"), payload.get("expires")) != (
                claim_lock, run_id, claim["claim_expires"]):
            return False
        from hermes_cli.kanban_db_dispatch import check_respawn_guard
        return check_respawn_guard(conn, task_id) in (None, "active_pr", "history_unresolved")
    except (ResumeAuthorityError, KeyError, IndexError, TypeError, ValueError):
        return False


def snapshot(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """The exact tuple an ATTESTED receipt binds, PR taken from the comments.

    Unchanged: the operator surface confirms what it can see, and what it can
    see is the card's comments.
    """
    current = base_snapshot(conn, task_id)
    urls = pr_urls(current["comments"])
    if not urls:
        raise ResumeAuthorityError(
            f"task {task_id} references no pull request; the guard this "
            "authorises is not what is holding it")
    if len(urls) > 1:
        raise ResumeAuthorityError(
            "this task references more than one pull request "
            f"({', '.join(sorted(urls))}); a receipt exempts one lineage and "
            "would leave the others unexamined")
    return {**current, "pr_url": next(iter(urls))}


def reconciled_snapshot(
    conn: sqlite3.Connection, task_id: str, *, pr_url: str,
) -> dict[str, Any]:
    """The tuple a RECONCILED receipt binds, PR supplied by the reconciler.

    The caller has already proved ownership of ``pr_url`` from structured
    provenance or from this task's own branch and head; prose is not consulted
    for identity here, only bound (via the comment digest) so that a comment
    arriving mid-flight still invalidates the authority.
    """
    return {**base_snapshot(conn, task_id), "pr_url": normalize_pr_url(pr_url)}


_BOUND_FIELDS = ("board_identity", "task_id", "run_id", "occurrence_event_id",
                 "pr_url", "comment_digest", "lifecycle", "assignee",
                 "workspace_kind", "workspace_path")


def _require_transition_permitted(
    conn: sqlite3.Connection, task_id: str, current: dict[str, Any], expected: dict[str, Any],
) -> None:
    """The card may have been promoted, once, and nothing else."""
    try:
        bound_occurrence = int(expected["occurrence_event_id"])
    except (KeyError, TypeError, ValueError):
        raise ResumeAuthorityError("the confirmation is missing occurrence_event_id") from None
    bound_lifecycle = expected.get("lifecycle")
    kinds = occurrence_events_since(conn, task_id, bound_occurrence)
    if bound_occurrence > current["occurrence_event_id"]:
        raise ResumeAuthorityError(
            "the confirmation names an occurrence later than the card's")
    if not _transition_permitted(bound_lifecycle, current["lifecycle"], kinds):
        raise ResumeAuthorityError(
            f"the card moved on: it was {bound_lifecycle!r}, it is now "
            f"{current['lifecycle']!r}"
            + (f" via {', '.join(kinds)}" if kinds else ""))


def preview(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """What the operator must look at, plus the tuple they must echo back."""
    current = snapshot(conn, task_id)
    blocked = writer_state(conn, task_id, int(time.time()))
    return {**current, "resumable": blocked is None, "blocked_reason": blocked,
            "expected": {k: current[k] for k in _BOUND_FIELDS}}


def _require_matches(current: dict[str, Any], expected: dict[str, Any]) -> None:
    for field in _BOUND_FIELDS:
        if field in ("lifecycle", "occurrence_event_id"):
            continue  # governed by _require_transition_permitted
        if field not in expected:
            raise ResumeAuthorityError(f"the confirmation is missing {field}")
        want, have = expected[field], current[field]
        if field in ("run_id", "occurrence_event_id"):
            try:
                want = int(want)
            except (TypeError, ValueError):
                raise ResumeAuthorityError(f"{field} must be an integer") from None
        if field == "pr_url":
            want = normalize_pr_url(str(want))
        if want != have:
            raise ResumeAuthorityError(
                f"the card changed while it was being reviewed: {field} is "
                f"{have!r}, the confirmation named {want!r}")


def issue(
    conn: sqlite3.Connection, task_id: str, *, expected: dict[str, Any],
    issued_by: str, attested: bool, now: Optional[int] = None,
    ttl_seconds: int = RECEIPT_TTL_SECONDS,
) -> int:
    """Record one-shot authority; return its id.

    ``issued_by`` is the verified session identity of the calling surface, never
    a value carried on the request body. ``attested`` is the operator's explicit
    statement that this PR continues this run for this occurrence, having read
    this comment set; without it there is no lineage and no receipt.
    """
    now = int(time.time()) if now is None else now
    if not attested:
        raise ResumeAuthorityError(
            "resume authority requires an explicit lineage attestation for this "
            "exact run, occurrence and comment set")
    current = snapshot(conn, task_id)
    _require_matches(current, expected)
    _require_transition_permitted(conn, task_id, current, expected)
    blocked = writer_state(conn, task_id, now)
    if blocked is not None:
        raise ResumeAuthorityError(f"cannot authorise a resume: {blocked}")
    cur = conn.execute(
        "INSERT INTO task_resume_receipts "
        "(task_id, board_identity, run_id, occurrence_event_id, pr_url, comment_digest,"
        " lifecycle, assignee, workspace_kind, workspace_path, issued_by, issued_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, current["board_identity"], current["run_id"],
         current["occurrence_event_id"], current["pr_url"], current["comment_digest"],
         current["lifecycle"], current["assignee"], current["workspace_kind"],
         current["workspace_path"], issued_by, now, now + int(ttl_seconds)),
    )
    return int(cur.lastrowid)


def issue_reconciled(
    conn: sqlite3.Connection, task_id: str, *, pr_url: str, principal: str,
    evidence: dict[str, Any], continuation_mode: str,
    now: Optional[int] = None, ttl_seconds: int = RECONCILED_TTL_SECONDS,
) -> int:
    """Record one-shot authority derived from a verified observation, not consent.

    Deliberately NOT ``issue(attested=True)``. Fabricating an operator
    attestation for a machine decision would make the audit log lie about who
    agreed to what, and would let a bug in the reconciler pass itself off as a
    human. The two authorities share every binding and every refusal — board,
    task, run, occurrence, comment digest, lifecycle, assignee, workspace,
    writer state — and differ only in where the pull request came from and in
    what the receipt says about itself.

    ``principal`` is the dispatcher's verified execution identity. It is never
    read off a request body or a task field.
    """
    now = int(time.time()) if now is None else now
    if not principal:
        raise ResumeAuthorityError("a reconciled receipt requires a verified principal")
    current = reconciled_snapshot(conn, task_id, pr_url=pr_url)
    blocked = writer_state(conn, task_id, now)
    if blocked is not None:
        raise ResumeAuthorityError(f"cannot authorise a resume: {blocked}")
    cur = conn.execute(
        "INSERT INTO task_resume_receipts "
        "(task_id, board_identity, run_id, occurrence_event_id, pr_url, comment_digest,"
        " lifecycle, assignee, workspace_kind, workspace_path, issued_by, issued_at,"
        " expires_at, authority_kind, reconciliation, continuation_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, current["board_identity"], current["run_id"],
         current["occurrence_event_id"], current["pr_url"], current["comment_digest"],
         current["lifecycle"], current["assignee"], current["workspace_kind"],
         current["workspace_path"], principal, now, now + int(ttl_seconds),
         RECONCILED, json.dumps(evidence, separators=(",", ":"), sort_keys=True),
         continuation_mode),
    )
    return int(cur.lastrowid)


def _live_receipt(conn: sqlite3.Connection, task_id: str, now: int):
    return conn.execute(
        "SELECT * FROM task_resume_receipts WHERE task_id = ? AND consumed_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (task_id, now),
    ).fetchone()


def _authority_kind(receipt) -> str:
    """``authority_kind`` if the board has been migrated, else ``attested``.

    Every receipt written before the column existed was an operator
    attestation, so that is the only safe reading of a missing value.
    """
    try:
        return receipt["authority_kind"] or ATTESTED
    except (KeyError, IndexError):
        return ATTESTED


def _receipt_matches_now(conn: sqlite3.Connection, task_id: str, receipt, now: int) -> bool:
    try:
        bound = {k: receipt[k] for k in _BOUND_FIELDS}
        if _authority_kind(receipt) == RECONCILED:
            # The PR was proved by provenance, not read off the comments, so the
            # comment set no longer has to name exactly one. It still has to be
            # unchanged (comment_digest) and the card must still own this PR --
            # a contract rewritten to another pull request revokes the authority
            # even though every comment stayed put.
            from hermes_cli import kanban_pr_reconcile as reconcile
            evidence = json.loads(receipt["reconciliation"])
            if not reconcile.checkout_matches(conn, task_id, evidence):
                return False
            current = reconciled_snapshot(conn, task_id, pr_url=receipt["pr_url"])
            if not _still_owns(conn, task_id, current["pr_url"]):
                return False
        else:
            current = snapshot(conn, task_id)
        _require_matches(current, bound)
        _require_transition_permitted(conn, task_id, current, bound)
    except (ResumeAuthorityError, KeyError, IndexError, ValueError, TypeError):
        return False
    return writer_state(conn, task_id, now) is None


def _still_owns(conn: sqlite3.Connection, task_id: str, pr_url: str) -> bool:
    """The card's structured provenance and prose still point at ``pr_url``.

    Local import: ``kanban_pr_association`` is a leaf, but keeping the import
    here documents that this module's authority rules do not depend on it for
    anything an operator does.
    """
    from hermes_cli import kanban_pr_association as _assoc

    urls = {a.pr_url for a in _assoc.structured_associations(conn, task_id)}
    if urls:
        return urls == {pr_url}
    return pr_url in pr_urls(comments(conn, task_id))


def exempt_reason(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: Optional[int] = None,
    now: Optional[int] = None,
) -> Optional[int]:
    """The id of a receipt that authorises the card as it is right now, or None.

    Read-only. Returning an id consumes nothing: the claim re-runs all of this
    inside its own transaction, because a comment, a run or a writer can arrive
    in between.
    """
    now = int(time.time()) if now is None else now
    receipt = _live_receipt(conn, task_id, now)
    if receipt is None:
        return None
    return int(receipt["id"]) if _receipt_matches_now(conn, task_id, receipt, now) else None


def consume(
    conn: sqlite3.Connection, receipt_id: int, task_id: str, *,
    window_seconds: Optional[int] = None, now: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Revalidate the whole tuple and the writer state, then spend the receipt.

    Caller holds the write transaction and has NOT reclaimed anything yet:
    ``_reclaim_dangling_run`` clears the pid this validation reads.

    Returns the bound lineage on success, ``None`` on refusal. The UPDATE is the
    CAS, so two dispatchers racing one receipt produce one run.
    """
    now = int(time.time()) if now is None else now
    receipt = conn.execute(
        "SELECT * FROM task_resume_receipts WHERE id = ? AND task_id = ? "
        "AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
        (int(receipt_id), task_id, now),
    ).fetchone()
    if receipt is None:
        return None
    if not _receipt_matches_now(conn, task_id, receipt, now):
        return None
    cur = conn.execute(
        "UPDATE task_resume_receipts SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ?",
        (now, int(receipt_id), now),
    )
    if cur.rowcount != 1:
        return None
    return {"receipt_id": int(receipt["id"]), "pr_url": receipt["pr_url"],
            "predecessor_run_id": int(receipt["run_id"]),
            "occurrence_event_id": int(receipt["occurrence_event_id"]),
            "issued_by": receipt["issued_by"],
            "authority_kind": _authority_kind(receipt),
            "continuation_mode": _optional(receipt, "continuation_mode")}


def attach_run(conn: sqlite3.Connection, receipt_id: int, run_id: int) -> None:
    conn.execute("UPDATE task_resume_receipts SET consumed_run_id = ? WHERE id = ?",
                 (int(run_id), int(receipt_id)))


def _optional(row, column: str):
    """``row[column]`` on a board that has the column, else None."""
    try:
        return row[column]
    except (KeyError, IndexError):
        return None


def lineage_for_run(conn: sqlite3.Connection, task_id: str, run_id: int):
    """The authority a run was opened under, for the worker's own instructions."""
    return conn.execute(
        "SELECT pr_url, run_id AS predecessor_run_id, issued_by, consumed_at, "
        "authority_kind, continuation_mode "
        "FROM task_resume_receipts WHERE task_id = ? AND consumed_run_id = ?",
        (task_id, int(run_id)),
    ).fetchone()
