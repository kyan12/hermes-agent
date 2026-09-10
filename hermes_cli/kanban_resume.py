"""Explicit, single-use authority to resume ONE task on ONE existing pull request.

The dispatcher refuses to respawn a task whose recent comments carry a GitHub PR
URL (``check_respawn_guard`` -> ``"active_pr"``). Without that guard a crashed
worker's successor opens a second PR for the same work. With it, a worker that
died mid-PR parks its card forever.

A receipt is the narrow exit. It bypasses ``active_pr`` and nothing else, and it
is authority only because an authenticated operator confirmed an exact snapshot:

    board identity, task, latest terminal run BY ID, current occurrence, current
    lifecycle/assignee/workspace, the PR, and a digest of every comment's full
    content.

Provenance is **attested, not reconstructed**. ``task_comments`` has no run id,
so comment ownership by a historical run cannot be proven and is not claimed.
The operator states, now, that this PR is the continuation target for this run
and this occurrence, having seen this exact comment set. Legacy evidence without
that explicit binding stays refused.

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
    revoked_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_resume_receipts_task
    ON task_resume_receipts(task_id, consumed_at, revoked_at);
"""


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


def writer_state(conn: sqlite3.Connection, task_id: str, now: int) -> Optional[str]:
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


def snapshot(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """The exact tuple a receipt binds. Raises when the card cannot be resumed."""
    task = conn.execute(
        "SELECT status, assignee, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise ResumeAuthorityError(f"task {task_id} does not exist on this board")
    if task["status"] not in RESUMABLE_STATUSES:
        raise ResumeAuthorityError(
            f"task {task_id} is {task['status']}; a resume is authorised from "
            f"{' or '.join(RESUMABLE_STATUSES)}, and no transition is manufactured to get there")
    run = latest_terminal_run(conn, task_id)
    if run is None:
        raise ResumeAuthorityError(f"task {task_id} has no finished run to resume")
    entries = comments(conn, task_id)
    urls = pr_urls(entries)
    if not urls:
        raise ResumeAuthorityError(
            f"task {task_id} references no pull request; the guard this "
            "authorises is not what is holding it")
    if len(urls) > 1:
        raise ResumeAuthorityError(
            "this task references more than one pull request "
            f"({', '.join(sorted(urls))}); a receipt exempts one lineage and "
            "would leave the others unexamined")
    return {
        "board_identity": board_identity(conn),
        "task_id": task_id,
        "run_id": int(run["id"]),
        "run_profile": run["profile"],
        "run_outcome": run["outcome"],
        "occurrence_event_id": latest_occurrence_event_id(conn, task_id),
        "pr_url": next(iter(urls)),
        "comment_digest": comment_digest(entries),
        "comments": entries,
        "lifecycle": task["status"],
        "assignee": task["assignee"],
        "workspace_kind": task["workspace_kind"],
        "workspace_path": task["workspace_path"],
    }


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


def _live_receipt(conn: sqlite3.Connection, task_id: str, now: int):
    return conn.execute(
        "SELECT * FROM task_resume_receipts WHERE task_id = ? AND consumed_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (task_id, now),
    ).fetchone()


def _receipt_matches_now(conn: sqlite3.Connection, task_id: str, receipt, now: int) -> bool:
    try:
        current = snapshot(conn, task_id)
        bound = {k: receipt[k] for k in _BOUND_FIELDS}
        _require_matches(current, bound)
        _require_transition_permitted(conn, task_id, current, bound)
    except (ResumeAuthorityError, KeyError, IndexError):
        return False
    return writer_state(conn, task_id, now) is None


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
            "issued_by": receipt["issued_by"]}


def attach_run(conn: sqlite3.Connection, receipt_id: int, run_id: int) -> None:
    conn.execute("UPDATE task_resume_receipts SET consumed_run_id = ? WHERE id = ?",
                 (int(run_id), int(receipt_id)))


def lineage_for_run(conn: sqlite3.Connection, task_id: str, run_id: int):
    """The authority a run was opened under, for the worker's own instructions."""
    return conn.execute(
        "SELECT pr_url, run_id AS predecessor_run_id, issued_by, consumed_at "
        "FROM task_resume_receipts WHERE task_id = ? AND consumed_run_id = ?",
        (task_id, int(run_id)),
    ).fetchone()
