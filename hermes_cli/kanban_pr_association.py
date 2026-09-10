"""Which pull requests are *this task's*, and which are only mentioned near it.

The respawn guard used to answer that question with a substring search over
comment bodies: any ``https://github.com/o/r/pull/N`` anywhere in a recent
comment parked the card for twenty-four hours. Comments are prose. A quoted
line from a sibling card, a fleet digest reposted onto the wrong task, a link
offered as an example — each one reads identically to "I opened this PR", and
each one stalls legitimate continuation for a day.

Prose is a **discovery hint**. Ownership has to come from somewhere that a
worker could not have written by accident:

``completion_contract``
    The task itself declares an exact PR URL. Publication binds it
    (``kanban_pr_acceptance_store.prepare_acceptance``), so it names the one PR
    this card completes through.
``task_runs.metadata.published_pr``
    A structured declaration attached to one run of this task.
``task_events(kind='pr_acceptance')``
    A recorded acceptance receipt, carrying the PR its evidence was collected
    against.

Those three are *structured provenance*: they are association on their own.

A fourth channel exists but cannot be settled from SQLite. A worker that opened
a PR and then crashed has written none of the above — it never reached
completion — yet its branch is pushed and its PR is open. What that card does
have is a **task-specific checkout**: its own worktree and branch. Whether the
PR at the end of that branch is really this task's is a question for GitHub, not
for the board, so this module only reports that the question exists
(``CANDIDATE``); :mod:`hermes_cli.kanban_pr_reconcile` answers it.

Author equality, timestamps, quote detection and "latest URL wins" are
deliberately absent. A comment's author is caller-supplied and a historical
run's comments are not attributable, so none of them can carry ownership.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from typing import Any, NamedTuple, Optional

# Same shape the guard and the acceptance collector already accept, kept local
# so this module does not import either of them.
PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)",
    re.IGNORECASE,
)

# Verdicts of :func:`classify`.
ASSOCIATED = "associated"   # structured provenance names a PR: this card owns it
HISTORY_UNRESOLVED = "history_unresolved"
CANDIDATE = "candidate"     # prose + a task-specific checkout: ask GitHub
UNASSOCIATED = "unassociated"  # prose only, or nothing at all: not this card's


class Association(NamedTuple):
    """One structured claim that ``pr_url`` belongs to a task.

    ``source``/``source_id`` are what a reconciliation receipt records as its
    provenance, so an operator reading the receipt can find the exact row the
    authority was derived from.
    """

    pr_url: str
    source: str
    source_id: str
    payload_digest: str


def _payload_digest(raw: str | bytes) -> str:
    return hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()


def normalize(raw: Optional[str]) -> Optional[str]:
    """The canonical form of the first PR URL in ``raw``, or None."""
    match = PR_URL_RE.search(raw or "")
    return match.group(0).lower().rstrip("/") if match else None


def repo_and_number(pr_url: str) -> Optional[tuple[str, int]]:
    match = PR_URL_RE.fullmatch(pr_url.rstrip("/"))
    return (match.group(1), int(match.group(2))) if match else None


def structured_associations(conn: sqlite3.Connection, task_id: str) -> list[Association]:
    """Every structured claim on this task, deterministically ordered.

    More than one *distinct* PR here is ambiguity, not a majority vote: see
    :func:`associated_pr`.
    """
    found: list[Association] = []

    row = conn.execute(
        "SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is not None:
        # A repo-only contract ("OWNER/REPO") declares where, not which; only an
        # exact PR contract is an association.
        url = normalize(row["completion_contract"])
        if url is not None:
            found.append(Association(url, "completion_contract", task_id, _payload_digest(row["completion_contract"])))

    for run in conn.execute(
        "SELECT id, metadata FROM task_runs WHERE task_id = ? AND metadata IS NOT NULL "
        "ORDER BY id ASC", (task_id,),
    ).fetchall():
        url = normalize(_json_field(run["metadata"], "published_pr"))
        if url is not None:
            found.append(Association(url, "published_pr", f"run:{int(run['id'])}", _payload_digest(run["metadata"])))

    for event in conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance' "
        "ORDER BY id ASC", (task_id,),
    ).fetchall():
        url = normalize(_json_field(event["payload"], "pr_url"))
        if url is not None:
            found.append(Association(url, "pr_acceptance", f"event:{int(event['id'])}",
                                     _payload_digest(json.dumps([event["run_id"], event["payload"]],
                                                                ensure_ascii=False, separators=(",", ":")))))

    return found


def _json_field(raw: Any, key: str) -> Optional[str]:
    """``raw`` parsed as a JSON object, ``key`` read out of it, or None.

    Malformed provenance is *not* association: it is unreadable, and reading a
    PR URL out of a broken blob by regex would be exactly the prose matching
    this module exists to stop.
    """
    if not isinstance(raw, (str, bytes)):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    field = value.get(key)
    return field if isinstance(field, str) else None


def prose_urls(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
    now: Optional[int] = None,
) -> set[str]:
    """PR URLs mentioned in comments inside the guard window. Hints only."""
    now = int(time.time()) if now is None else now
    cutoff = now - int(window_seconds)
    return {
        match.group(0).lower().rstrip("/")
        for row in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
            (task_id, cutoff),
        ).fetchall()
        for match in PR_URL_RE.finditer(row["body"] or "")
    }


def checkout_claim(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """Recorded checkout coordinates, independent of workspace kind.

    A path is an inspection target, not PR ownership. Git and remote evidence
    must establish its actual branch, head and repositories before admission.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path, branch_name, project_id "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return None
    branch = (row["branch_name"] or "").strip()
    path = (row["workspace_path"] or "").strip()
    if not branch and not path:
        return None
    return {"workspace_kind": row["workspace_kind"], "workspace_path": path or None,
            "branch_name": branch or None, "project_id": row["project_id"]}


def classify(
    conn: sqlite3.Connection, task_id: str, *, window_seconds: int,
    now: Optional[int] = None,
) -> str:
    """``ASSOCIATED`` / ``CANDIDATE`` / ``UNASSOCIATED`` for this task.

    ``ASSOCIATED`` does not depend on the comment window: structured provenance
    outlives prose, and a card whose contract names a PR is still that PR's card
    a week later — the old comment-only guard silently released those.
    """
    if structured_associations(conn, task_id):
        return ASSOCIATED
    interrupted = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "AND (outcome IN ('crashed', 'timed_out', 'gave_up', 'reclaimed', 'rate_limited') "
        "OR status IN ('crashed', 'timed_out', 'failed', 'released')) LIMIT 1",
        (task_id,)).fetchone()
    if interrupted is None or not historical_hints(conn, task_id):
        return UNASSOCIATED
    return CANDIDATE if checkout_claim(conn, task_id) is not None else HISTORY_UNRESOLVED


def historical_hints(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """History cannot become safe just because a discovery hint aged out."""
    return {match.group(0).lower().rstrip("/")
            for row in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (task_id,))
            for match in PR_URL_RE.finditer(row["body"] or "")}


def associated_pr(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The single PR this task's structured provenance names, or None.

    None for "no association" *and* for "more than one": a card claiming two
    pull requests is ambiguous, and ambiguity resolves to no automatic
    authority. Callers that must tell those apart read
    :func:`structured_associations`.
    """
    urls = {a.pr_url for a in structured_associations(conn, task_id)}
    return next(iter(urls)) if len(urls) == 1 else None
