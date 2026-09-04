"""Native Kanban health control loop.

This is the deterministic controller that keeps a board's lifecycle honest.
It answers three questions about every card, with no model in the loop:

1. **Is this card's ``blocked`` state a real human gate?** A visible gate
   means ONE current atomic human-only action backed by typed, affirmed
   evidence. An un-typed block (the shape a crashed worker leaves behind) or
   a typed-but-unaffirmed claim is a *machine* hold: it routes to automation
   recovery and never reaches the human column. An affirmed gate stays
   visible — suppressing it would be the opposite failure.

2. **Does this card have a machine-verifiable forward path?** Exactly one of:
   a live worker, an eligible ready slot, an accepted dependency chain, a
   typed scheduled hold with a healthy durable wake/checkpoint, or an
   affirmed human gate. Anything else is reported, not assumed fine.

3. **Can the controller move it?** :func:`reconcile_board` resumes due typed
   holds exactly once, parks unfinished dependencies and intentional
   external/physical/roadmap waits, and diagnoses legacy prose-only holds
   without ever bulk-resuming them.

Design notes
------------
* Every classifier is a pure function of (task row, clock, checkpoint), so
  the CLI, the dashboard API, the gateway telemetry and the sentinel all
  reach the same verdict from the same inputs.
* Reconciliation is idempotent. It is safe to run from the dispatcher tick,
  from ``hermes kanban board-health --reconcile``, and from the sentinel;
  concurrent runs converge rather than double-resume.
* The controller never invents authority. It resumes only what typed state
  already authorizes, and escalates everything else.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Bumped when the payload shape or the reason-code vocabulary changes in a
# way an out-of-tree verifier (the sentinel) must notice. The sentinel
# refuses to act on a version it was not written against — a silent
# vocabulary drift is exactly how a verifier starts approving the wrong
# thing.
CONTROL_LOOP_VERSION = 1
HEALTH_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Typed classifications for a ``scheduled`` card.
#
#   dependency  — waiting on parent cards. Resumes when they finish.
#   wake        — waiting on the clock. Resumes when ``hold_wake_at`` is due.
#   external    — waiting on a third party (a counterparty, a vendor, a court).
#   physical    — waiting on something in the physical world (hardware, mail).
#   roadmap     — deliberately deferred to a later phase of the plan.
#
# The last three are *intentional parks*: healthy, unmovable by automation,
# and correctly invisible to the human gate column. They are not failures.
VALID_HOLD_KINDS = frozenset(
    {"dependency", "wake", "external", "physical", "roadmap"}
)
PARKED_HOLD_KINDS = frozenset({"external", "physical", "roadmap"})
RESUMABLE_HOLD_KINDS = frozenset({"dependency", "wake"})

# Block kinds that can, with affirmed evidence, become a visible human gate.
# ``transient`` and ``dependency`` never can: the first is a machine retry
# signal, the second is routed to ``todo`` by ``block_task`` and gated on
# parents. ``None`` (legacy/un-typed) never can either.
HUMAN_GATE_BLOCK_KINDS = frozenset({"needs_input", "capability"})

# Evidence types a human gate or an intentional hold may cite. An evidence
# record with an unrecognised type is not evidence — it is prose in a JSON
# wrapper, and the whole point of this contract is to stop prose from
# reaching the human column.
VALID_EVIDENCE_TYPES = frozenset(
    {
        "human_decision",
        "credential_grant",
        "physical_action",
        "legal_approval",
        "external_party",
    }
)

# Terminal statuses: no forward path is required or expected.
TERMINAL_STATUSES = frozenset({"done", "archived"})

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

# Block projection
REASON_AFFIRMED_GATE = "affirmed_human_gate"
REASON_UNTYPED_BLOCK = "untyped_block"
REASON_UNAFFIRMED_GATE = "unaffirmed_gate"
REASON_TRANSIENT_BLOCK = "transient_block"

# Hold classification
REASON_INTENTIONAL_HOLD = "intentional_hold"
REASON_WAKE_ARMED = "wake_armed"
REASON_WAKE_DUE = "wake_due"
REASON_WAKE_MISSING = "wake_missing"
REASON_WAKE_DISABLED = "wake_disabled"
REASON_WAKE_FAILED = "wake_failed"
REASON_LEGACY_UNTYPED = "legacy_untyped"
REASON_DEPENDENCY_UNFINISHED = "dependency_unfinished"
REASON_DEPENDENCY_SATISFIED = "dependency_satisfied"
REASON_DEPENDENCY_BROKEN = "dependency_broken"
REASON_UNKNOWN_HOLD_KIND = "unknown_hold_kind"

# Forward path
REASON_LIVE_WORKER = "live_worker"
REASON_ELIGIBLE_READY = "eligible_ready"
REASON_UNASSIGNED = "unassigned"
REASON_NO_FORWARD_PATH = "no_forward_path"
REASON_AWAITING_TRIAGE = "awaiting_triage"

# Scope
REASON_UNSCOPED_ACTIVE_WORK = "unscoped_active_work"

# Forward-path kinds
PATH_LIVE_WORKER = "live_worker"
PATH_ELIGIBLE_READY = "eligible_ready"
PATH_DEPENDENCY_CHAIN = "dependency_chain"
PATH_SCHEDULED_HOLD = "scheduled_hold"
PATH_HUMAN_GATE = "human_gate"
PATH_AUTOMATION_RECOVERY = "automation_recovery"
PATH_TRIAGE = "triage"
PATH_NONE = "none"

# Ready-queue reason codes (dispatcher telemetry). Distinct codes exist so a
# capacity wait can never be reported as a credential failure.
READY_SPAWNABLE = "spawnable"
READY_UNASSIGNED = "unassigned"
READY_CONTROL_PLANE_LANE = "control_plane_lane"
READY_CAPACITY_GLOBAL = "capacity_global"
READY_CAPACITY_PER_PROFILE = "capacity_per_profile"
READY_INVALID_WORKSPACE = "invalid_workspace"
READY_GUARD_ACTIVE_PR = "guard_active_pr"
READY_GUARD_RECENT_SUCCESS = "guard_recent_success"
READY_GUARD_BLOCKER_AUTH = "guard_blocker_auth"
READY_GUARD_RATE_LIMIT = "guard_rate_limit_cooldown"

_GUARD_REASON_CODES = {
    "active_pr": READY_GUARD_ACTIVE_PR,
    "recent_success": READY_GUARD_RECENT_SUCCESS,
    "blocker_auth": READY_GUARD_BLOCKER_AUTH,
    "rate_limit_cooldown": READY_GUARD_RATE_LIMIT,
}

# Checkpoints
CHECKPOINT_RECONCILE = "reconcile"

# A wake is only credible while something is demonstrably running the
# reconciler. Two dispatcher tick intervals plus slack: past this the
# checkpoint proves nothing about the next hour.
WAKE_CHECKPOINT_MAX_AGE_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def reconcile_enabled() -> bool:
    """Whether the native reconciler is allowed to move cards.

    ``kanban.health_reconcile`` in ``config.yaml`` (default true). Turning it
    off does not make parked cards healthy — it makes every ``wake`` hold
    report ``wake_disabled``, because with the controller off nothing will
    ever wake them.
    """
    try:
        from hermes_cli.config import load_config

        return bool(
            (load_config() or {}).get("kanban", {}).get("health_reconcile", True)
        )
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """A parsed, validated typed-evidence record."""

    type: str
    action: str
    affirmed_by: str
    affirmed_at: int
    source: Optional[str] = None
    task_id: Optional[str] = None
    occurrence_event_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "action": self.action,
            "affirmed_by": self.affirmed_by,
            "affirmed_at": self.affirmed_at,
            "source": self.source,
            "task_id": self.task_id,
            "occurrence_event_id": self.occurrence_event_id,
        }


def parse_evidence(raw: Any) -> Optional[Evidence]:
    """Return an :class:`Evidence` when *raw* is an affirmed typed record.

    ``None`` for anything else — malformed JSON, an unrecognised type, a
    missing atomic action, or no affirming principal. Fail-closed by design:
    the failure mode we are preventing is unaffirmed prose being projected
    to a human as though someone had signed off on it.
    """
    if raw is None:
        return None
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None

    etype = str(data.get("type") or "").strip()
    if etype not in VALID_EVIDENCE_TYPES:
        return None
    action = str(data.get("action") or "").strip()
    if not action:
        return None
    affirmed_by = str(data.get("affirmed_by") or "").strip()
    if affirmed_by not in {"Kevin Yan", "kevin", "operator:kevin", "operator:cli", "operator:dashboard"}:
        return None
    try:
        affirmed_at = int(data.get("affirmed_at") or 0)
    except (TypeError, ValueError):
        return None
    if affirmed_at <= 0:
        return None
    source = data.get("source")
    task_id = data.get("task_id")
    occurrence_event_id = data.get("occurrence_event_id")
    if occurrence_event_id is not None:
        try:
            occurrence_event_id = int(occurrence_event_id)
        except (TypeError, ValueError):
            return None
        if occurrence_event_id <= 0:
            return None
    return Evidence(
        type=etype,
        action=action,
        affirmed_by=affirmed_by,
        affirmed_at=affirmed_at,
        source=str(source) if source else None,
        task_id=str(task_id) if task_id else None,
        occurrence_event_id=occurrence_event_id,
    )


def set_gate_evidence(conn, task_id: str, *, evidence: Any) -> bool:
    """Attach affirmed typed evidence to a card. Returns False if invalid.

    Refusing to persist unvalidated evidence is what keeps the projection
    honest: there is no path by which prose reaches ``gate_evidence``.
    """
    from hermes_cli import kanban_db as kb

    parsed = parse_evidence(evidence)
    if parsed is None:
        return False
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        occurrence = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if current is None or current["status"] != "blocked" or occurrence is None:
            return False
        bound = Evidence(
            type=parsed.type,
            action=parsed.action,
            affirmed_by=parsed.affirmed_by,
            affirmed_at=parsed.affirmed_at,
            source=parsed.source,
            task_id=task_id,
            occurrence_event_id=int(occurrence["id"]),
        )
        cur = conn.execute(
            "UPDATE tasks SET gate_evidence = ? WHERE id = ?",
            (json.dumps(bound.to_dict()), task_id),
        )
        if cur.rowcount != 1:
            return False
        kb._append_event(
            conn,
            task_id,
            "gate_evidence",
            {
                "type": bound.type,
                "affirmed_by": bound.affirmed_by,
                "occurrence_event_id": bound.occurrence_event_id,
            },
        )
    return True


def affirm_human_gate(
    conn,
    task_id: str,
    *,
    evidence: Any,
    kind: str = "needs_input",
    reason: Optional[str] = None,
    author: str = "operator",
    expected_run_id: Optional[int] = None,
) -> bool:
    """Atomically create one trusted, occurrence-bound human gate.

    Worker and machine calls use :func:`kanban_db.block_task`, which records an
    unaffirmed recovery request instead.  The status, run closure, audit event,
    evidence binding, and operator comment commit together so a crash cannot
    expose a prose-only blocker.
    """
    from hermes_cli import kanban_db as kb

    parsed = parse_evidence(evidence)
    if parsed is None or kind not in HUMAN_GATE_BLOCK_KINDS:
        return False
    now = int(time.time())
    if parsed.affirmed_at > now + 60 or now - parsed.affirmed_at > 15 * 60:
        return False

    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, block_kind, block_recurrences, current_run_id "
            "FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if row is None or row["status"] not in ("ready", "running", "triage"):
            return False
        if expected_run_id is not None and (
            row["status"] != "running"
            or row["current_run_id"] != int(expected_run_id)
        ):
            return False
        recurrences = (
            int(row["block_recurrences"] or 0) + 1
            if row["block_kind"] == kind
            else 1
        )
        recovery_event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? "
            "AND kind='automation_recovery_requested' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        source_status = None
        if recovery_event and recovery_event["payload"]:
            try:
                source_status = json.loads(recovery_event["payload"]).get(
                    "source_status"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                source_status = None
        params: list[Any] = [kind, recurrences, task_id]
        guard = ""
        if expected_run_id is not None:
            guard = " AND current_run_id=?"
            params.append(int(expected_run_id))
        cur = conn.execute(
            "UPDATE tasks SET status='blocked', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, hold_kind=NULL, "
            "hold_wake_at=NULL, gate_evidence=NULL, block_kind=?, "
            "block_recurrences=? WHERE id=? AND status IN "
            "('ready','running','triage')" + guard,
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        run_id = kb._end_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason
        )
        if run_id is None:
            # A machine request reaches ``triage`` only after its owned run was
            # already closed with the authoritative retry-status metadata.
            # Reuse that occurrence's ended run when the operator affirms it;
            # synthesizing a second run would make unblock_task forget whether
            # the card must return to ``review`` or ``ready``.
            latest = kb.latest_run(conn, task_id)
            run_id = latest.id if latest is not None else None
        if run_id is None and reason:
            run_id = kb._synthesize_ended_run(
                conn, task_id, outcome="blocked", summary=reason
            )
        event_id = kb._append_event(
            conn,
            task_id,
            "blocked",
            {
                "reason": reason,
                "kind": kind,
                "recurrences": recurrences,
                "affirmed": True,
                "source_status": source_status,
            },
            run_id=run_id,
        )
        bound = Evidence(
            type=parsed.type,
            action=parsed.action,
            affirmed_by=parsed.affirmed_by,
            affirmed_at=parsed.affirmed_at,
            source=parsed.source,
            task_id=task_id,
            occurrence_event_id=event_id,
        )
        conn.execute(
            "UPDATE tasks SET gate_evidence=? WHERE id=? AND status='blocked'",
            (json.dumps(bound.to_dict()), task_id),
        )
        kb._append_event(
            conn,
            task_id,
            "gate_evidence",
            {
                "type": bound.type,
                "affirmed_by": bound.affirmed_by,
                "occurrence_event_id": event_id,
            },
            run_id=run_id,
        )
        if reason:
            kb.add_comment(conn, task_id, author, f"BLOCKED: {reason}")
    return True


# ---------------------------------------------------------------------------
# Typed holds
# ---------------------------------------------------------------------------


def set_hold(
    conn,
    task_id: str,
    *,
    kind: str,
    wake_at: Optional[int] = None,
    evidence: Any = None,
    reason: Optional[str] = None,
    apply: bool = True,
    author: Optional[str] = None,
) -> bool:
    """Classify (and optionally park) a card as a typed scheduled hold.

    ``apply=True`` also transitions the card to ``scheduled`` via
    :func:`kanban_db.schedule_task`. ``apply=False`` only records the
    classification, which is how a legacy prose hold gets triaged in place
    without changing its status.
    """
    from hermes_cli import kanban_db as kb

    if kind not in VALID_HOLD_KINDS:
        raise ValueError(
            f"hold kind must be one of {sorted(VALID_HOLD_KINDS)}, got {kind!r}"
        )
    parsed = parse_evidence(evidence) if evidence is not None else None
    if evidence is not None and parsed is None:
        return False
    if wake_at is not None:
        wake_at = int(wake_at)

    task = kb.get_task(conn, task_id)
    if task is None:
        return False
    if parsed is not None:
        parsed = Evidence(
            type=parsed.type,
            action=parsed.action,
            affirmed_by=parsed.affirmed_by,
            affirmed_at=parsed.affirmed_at,
            source=parsed.source,
            task_id=task_id,
        )

    # Status, typed fields, closing run, and both audit events are one commit.
    # A crash or constraint failure cannot leave a scheduled-but-untyped row.
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if current is None:
            return False
        run_id = None
        if apply and current["status"] != "scheduled":
            cur = conn.execute(
                "UPDATE tasks SET status='scheduled', claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, gate_evidence=NULL "
                "WHERE id=? AND status IN ('todo','ready','running','blocked')",
                (task_id,),
            )
            if cur.rowcount != 1:
                return False
            run_id = kb._end_run(
                conn, task_id, outcome="scheduled", status="scheduled", summary=reason
            )
            if run_id is None and reason:
                run_id = kb._synthesize_ended_run(
                    conn, task_id, outcome="scheduled", summary=reason
                )
            kb._append_event(
                conn, task_id, "scheduled", {"reason": reason}, run_id=run_id
            )
        cur = conn.execute(
            "UPDATE tasks SET hold_kind = ?, hold_wake_at = ?, "
            "gate_evidence = ? WHERE id = ? AND status = ?",
            (
                kind,
                wake_at,
                json.dumps(parsed.to_dict()) if parsed else None,
                task_id,
                "scheduled" if apply else current["status"],
            ),
        )
        if cur.rowcount != 1:
            return False
        kb._append_event(
            conn,
            task_id,
            "hold_typed",
            {"kind": kind, "wake_at": wake_at, "reason": reason},
            run_id=run_id,
        )
        if reason and author:
            kb.add_comment(conn, task_id, author, f"SCHEDULED: {reason}")
    return True


def clear_hold(conn, task_id: str) -> None:
    """Drop the typed-hold classification once a card has resumed."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET hold_kind = NULL, hold_wake_at = NULL WHERE id = ?",
            (task_id,),
        )


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def record_checkpoint(
    conn,
    name: str,
    *,
    status: str = "ok",
    now: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    """Stamp a durable control-loop checkpoint."""
    from hermes_cli import kanban_db as kb

    ts = int(now if now is not None else time.time())
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO board_health_checkpoints (name, status, updated_at, detail) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET status = excluded.status, "
            "updated_at = excluded.updated_at, detail = excluded.detail",
            (name, status, ts, detail),
        )


def read_checkpoint(conn, name: str) -> Optional[dict]:
    try:
        row = conn.execute(
            "SELECT name, status, updated_at, detail "
            "FROM board_health_checkpoints WHERE name = ?",
            (name,),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return {
        "name": row["name"],
        "status": row["status"],
        "updated_at": int(row["updated_at"]),
        "detail": row["detail"],
    }


def wake_subsystem_health(conn, *, now: Optional[int] = None) -> dict:
    """Is anything actually able to wake a scheduled card right now?

    Returns ``{"enabled", "healthy", "reason_code", "checkpoint"}``. A
    ``reason_code`` of ``None`` means the wake path is sound.
    """
    ts = int(now if now is not None else time.time())
    enabled = reconcile_enabled()
    checkpoint = read_checkpoint(conn, CHECKPOINT_RECONCILE)
    if not enabled:
        return {
            "enabled": False,
            "healthy": False,
            "reason_code": REASON_WAKE_DISABLED,
            "checkpoint": checkpoint,
        }
    if checkpoint is None:
        return {
            "enabled": True,
            "healthy": False,
            "reason_code": REASON_WAKE_FAILED,
            "checkpoint": None,
        }
    if checkpoint["status"] != "ok":
        return {
            "enabled": True,
            "healthy": False,
            "reason_code": REASON_WAKE_FAILED,
            "checkpoint": checkpoint,
        }
    if ts - checkpoint["updated_at"] > WAKE_CHECKPOINT_MAX_AGE_SECONDS:
        return {
            "enabled": True,
            "healthy": False,
            "reason_code": REASON_WAKE_FAILED,
            "checkpoint": checkpoint,
        }
    return {
        "enabled": True,
        "healthy": True,
        "reason_code": None,
        "checkpoint": checkpoint,
    }


# ---------------------------------------------------------------------------
# Block projection (invariant A)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockProjection:
    """How a ``blocked`` card should be rendered."""

    visible: bool
    reason_code: str
    action: Optional[str] = None
    evidence: Optional[Evidence] = None


def classify_block(task) -> BlockProjection:
    """Decide whether a ``blocked`` card is a human gate or a machine hold.

    Visible requires all of: a human-capable block kind, and an affirmed
    typed evidence record naming ONE atomic action. Everything else routes
    to automation recovery, where a controller (not a person) owns it.
    """
    kind = getattr(task, "block_kind", None)
    evidence = parse_evidence(getattr(task, "gate_evidence", None))

    if kind is None:
        # The shape a crashed/timed-out worker leaves behind. Never a gate.
        return BlockProjection(False, REASON_UNTYPED_BLOCK)
    if kind == "transient":
        return BlockProjection(False, REASON_TRANSIENT_BLOCK)
    if kind not in HUMAN_GATE_BLOCK_KINDS:
        return BlockProjection(False, REASON_UNTYPED_BLOCK)
    if evidence is None:
        # A worker *claiming* it needs input is a claim, not evidence.
        return BlockProjection(False, REASON_UNAFFIRMED_GATE)
    if evidence.task_id != task.id or evidence.occurrence_event_id is None:
        return BlockProjection(False, REASON_UNAFFIRMED_GATE)
    return BlockProjection(
        True, REASON_AFFIRMED_GATE, action=evidence.action, evidence=evidence
    )


# ---------------------------------------------------------------------------
# Hold classification (invariants B + D)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldState:
    """Classification of one ``scheduled`` card."""

    task_id: str
    kind: Optional[str]
    wake_at: Optional[int]
    healthy: bool
    reason_code: str
    needs_classification: bool = False
    resumable: bool = False


def classify_hold(task, *, now: int, wake_health: dict, parents_done: bool) -> HoldState:
    """Classify a ``scheduled`` card against the clock and its dependencies."""
    kind = getattr(task, "hold_kind", None)
    wake_at = getattr(task, "hold_wake_at", None)

    if kind is None:
        # Legacy prose-only hold. Loud by construction: we cannot prove a
        # forward path, and guessing one is how a card silently never runs.
        return HoldState(
            task.id, None, wake_at, False, REASON_LEGACY_UNTYPED,
            needs_classification=True,
        )
    if kind not in VALID_HOLD_KINDS:
        return HoldState(
            task.id, kind, wake_at, False, REASON_UNKNOWN_HOLD_KIND,
            needs_classification=True,
        )
    if kind in PARKED_HOLD_KINDS:
        evidence = parse_evidence(getattr(task, "gate_evidence", None))
        if evidence is None or evidence.task_id not in (None, task.id):
            return HoldState(
                task.id, kind, wake_at, False, REASON_UNAFFIRMED_GATE,
                needs_classification=True,
            )
        return HoldState(task.id, kind, wake_at, True, REASON_INTENTIONAL_HOLD)

    if kind == "dependency":
        if parents_done:
            return HoldState(
                task.id, kind, wake_at, True, REASON_DEPENDENCY_SATISFIED,
                resumable=True,
            )
        return HoldState(task.id, kind, wake_at, True, REASON_DEPENDENCY_UNFINISHED)

    # kind == "wake"
    if wake_at is None:
        return HoldState(
            task.id, kind, None, False, REASON_WAKE_MISSING,
            needs_classification=True,
        )
    if not wake_health["healthy"]:
        # A perfectly-formed wake time is worthless if nothing is running the
        # reconciler. Report the subsystem's own reason, not the card's.
        return HoldState(
            task.id, kind, wake_at, False, wake_health["reason_code"]
        )
    if wake_at <= now:
        return HoldState(
            task.id, kind, wake_at, True, REASON_WAKE_DUE, resumable=parents_done
        )
    return HoldState(task.id, kind, wake_at, True, REASON_WAKE_ARMED)


def _parents_done(conn, task_id: str) -> bool:
    rows = conn.execute(
        "SELECT t.status FROM tasks t JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?",
        (task_id,),
    ).fetchall()
    return all(r["status"] in ("done", "archived") for r in rows)


# ---------------------------------------------------------------------------
# Forward paths (invariant B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForwardPath:
    """The one machine-verifiable way a card can still move."""

    task_id: str
    kind: str
    ok: bool
    reason_code: str
    detail: Optional[str] = None


def forward_path(
    conn, task, *, now: int, wake_health: dict
) -> ForwardPath:
    status = task.status

    if status == "running":
        from hermes_cli import kanban_db as kb
        live = bool(
            task.current_run_id
            and task.claim_lock
            and task.claim_expires
            and int(task.claim_expires) > now
            and task.worker_pid
            and kb._pid_alive(task.worker_pid)
        )
        return ForwardPath(
            task.id, PATH_LIVE_WORKER if live else PATH_NONE, live,
            REASON_LIVE_WORKER if live else REASON_NO_FORWARD_PATH,
            None if live else "running row has no authoritative live claim",
        )

    if status == "triage":
        return ForwardPath(task.id, PATH_TRIAGE, True, REASON_AWAITING_TRIAGE)

    if status in ("ready", "review"):
        if not task.assignee:
            return ForwardPath(
                task.id, PATH_NONE, False, REASON_UNASSIGNED,
                detail="no assignee — the dispatcher will never spawn it",
            )
        return ForwardPath(task.id, PATH_ELIGIBLE_READY, True, REASON_ELIGIBLE_READY)

    if status == "todo":
        rows = conn.execute(
            "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task.id,)
        ).fetchone()
        if rows is None:
            # ``todo`` with no parents is a card nothing will ever promote.
            return ForwardPath(
                task.id, PATH_NONE, False, REASON_NO_FORWARD_PATH,
                detail="todo with no parent dependency to gate on",
            )
        return ForwardPath(
            task.id, PATH_DEPENDENCY_CHAIN, True, REASON_DEPENDENCY_UNFINISHED
        )

    if status == "scheduled":
        hold = classify_hold(
            task, now=now, wake_health=wake_health,
            parents_done=_parents_done(conn, task.id),
        )
        return ForwardPath(
            task.id, PATH_SCHEDULED_HOLD, hold.healthy, hold.reason_code
        )

    if status == "blocked":
        projection = classify_block(task)
        if projection.visible:
            return ForwardPath(
                task.id, PATH_HUMAN_GATE, True, REASON_AFFIRMED_GATE,
                detail=projection.action,
            )
        return ForwardPath(
            task.id, PATH_AUTOMATION_RECOVERY, False, projection.reason_code,
            detail="machine hold awaiting automation recovery",
        )

    return ForwardPath(task.id, PATH_NONE, True, "terminal")


def forward_paths(conn, *, now: Optional[int] = None) -> list[ForwardPath]:
    """Forward paths for every nonterminal card on the board."""
    from hermes_cli import kanban_db as kb

    ts = int(now if now is not None else time.time())
    wake = wake_subsystem_health(conn, now=ts)
    out = []
    for task in kb.list_tasks(conn, include_body=False):
        if task.status in TERMINAL_STATUSES:
            continue
        out.append(forward_path(conn, task, now=ts, wake_health=wake))
    return out


# ---------------------------------------------------------------------------
# Reconciliation (invariant D)
# ---------------------------------------------------------------------------


@dataclass
class ReconcileReport:
    """What one reconciliation pass did, and what it refused to do."""

    resumed: list[dict] = field(default_factory=list)
    parked: list[dict] = field(default_factory=list)
    diagnosed: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    applied: bool = True

    def to_dict(self) -> dict:
        return {
            "resumed": self.resumed,
            "parked": self.parked,
            "diagnosed": self.diagnosed,
            "errors": self.errors,
            "applied": self.applied,
        }


def reconcile_board(
    conn,
    *,
    now: Optional[int] = None,
    apply: bool = True,
    stamp_checkpoint: bool = True,
) -> ReconcileReport:
    """Move every typed hold that state already authorizes moving. Idempotent.

    Resumes a hold exactly once: the resume clears ``hold_kind`` and flips the
    status out of ``scheduled``, so a second pass finds nothing to do. Never
    resumes a legacy prose-only hold, an intentional park, or a dependency
    whose parents are unfinished.

    ``stamp_checkpoint=False`` runs the same reconciliation without claiming
    the controller checkpoint. The sentinel uses it: when it steps in because
    the native controller is down, stamping the controller's own liveness
    signal would erase the very evidence that the controller is down.
    """
    from hermes_cli import kanban_db as kb

    ts = int(now if now is not None else time.time())
    report = ReconcileReport(applied=apply)

    # The reconciler IS the wake mechanism, so it does not ask whether some
    # *other* process stamped a recent checkpoint — inside this call the
    # answer is trivially "yes, this one". Asking would deadlock the
    # bootstrap: the first-ever pass would find no checkpoint, declare every
    # wake hold unwakeable, and never stamp the checkpoint that would have
    # fixed it. Observers (board_health, the sentinel) still use the real
    # staleness check; only the controller exempts itself.
    enabled = reconcile_enabled()
    wake = {
        "enabled": enabled,
        "healthy": enabled,
        "reason_code": None if enabled else REASON_WAKE_DISABLED,
        "checkpoint": read_checkpoint(conn, CHECKPOINT_RECONCILE),
    }

    try:
        scheduled = kb.list_tasks(conn, status="scheduled", include_body=False)
    except Exception as exc:  # pragma: no cover - defensive
        report.errors.append({"task_id": None, "error": str(exc)})
        if apply and stamp_checkpoint:
            record_checkpoint(
                conn, CHECKPOINT_RECONCILE, status="failed", now=ts, detail=str(exc)
            )
        return report

    for task in scheduled:
        try:
            hold = classify_hold(
                task, now=ts, wake_health=wake,
                parents_done=_parents_done(conn, task.id),
            )
            entry = {
                "task_id": task.id,
                "title": task.title,
                "hold_kind": hold.kind,
                "wake_at": hold.wake_at,
                "reason_code": hold.reason_code,
            }
            if not hold.healthy or hold.needs_classification:
                # Loud, never auto-resumed. A legacy card is not evidence that
                # the work may proceed.
                report.diagnosed.append(entry)
                continue
            if not enabled:
                # Operator switched the controller off: report what WOULD move,
                # move nothing.
                report.parked.append({**entry, "reason_code": REASON_WAKE_DISABLED})
                continue
            if not hold.resumable:
                report.parked.append(entry)
                continue
            if not apply:
                report.resumed.append({**entry, "applied": False})
                continue
        except Exception as exc:
            # Classification and dependency reads are part of this card's
            # reconciliation path. Contain them so the pass can stamp a failed
            # checkpoint instead of leaving an older successful checkpoint
            # looking fresh.
            report.errors.append({"task_id": task.id, "error": str(exc)})
            continue
        try:
            # Re-read and classify under the same BEGIN IMMEDIATE transaction
            # that performs the guarded transition and audit write.
            with kb.write_txn(conn):
                current = kb.get_task(conn, task.id)
                if current is None or current.status != "scheduled":
                    report.parked.append({**entry, "reason_code": "already_resumed"})
                    continue
                current_hold = classify_hold(
                    current,
                    now=ts,
                    wake_health=wake,
                    parents_done=_parents_done(conn, current.id),
                )
                if not current_hold.resumable:
                    report.parked.append(
                        {**entry, "reason_code": current_hold.reason_code}
                    )
                    continue
                new_status = kb._landing_status_after_parents(conn, current.id)
                cur = conn.execute(
                    "UPDATE tasks SET status=?, current_run_id=NULL, "
                    "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
                    "hold_kind=NULL, hold_wake_at=NULL, gate_evidence=NULL, "
                    "consecutive_failures=0, last_failure_error=NULL "
                    "WHERE id=? AND status='scheduled' AND hold_kind=? "
                    "AND (hold_wake_at IS ? OR hold_wake_at = ?)",
                    (
                        new_status, current.id, current_hold.kind,
                        current_hold.wake_at, current_hold.wake_at,
                    ),
                )
                if cur.rowcount != 1:
                    report.parked.append({**entry, "reason_code": "already_resumed"})
                    continue
                kb._append_event(
                    conn, current.id, "hold_resumed",
                    {"hold_kind": current_hold.kind, "reason": current_hold.reason_code},
                )
            report.resumed.append(entry)
        except Exception as exc:
            report.errors.append({"task_id": task.id, "error": str(exc)})

    if apply and enabled and stamp_checkpoint:
        record_checkpoint(
            conn,
            CHECKPOINT_RECONCILE,
            status="failed" if report.errors else "ok",
            now=ts,
            detail=(
                f"{len(report.resumed)} resumed, {len(report.parked)} parked, "
                f"{len(report.diagnosed)} diagnosed"
            ),
        )
    return report


# ---------------------------------------------------------------------------
# Scope diagnostics (invariant C)
# ---------------------------------------------------------------------------


def canonical_project_id(conn) -> Optional[str]:
    """The board's single canonical Project, or None when ambiguous.

    Returns a project id only when every project-linked card on the board
    agrees. With zero or several distinct projects there is no fact to
    report, so the unscoped-work diagnostic stays silent rather than
    guessing which project a card belongs to.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM tasks "
            "WHERE project_id IS NOT NULL AND project_id != ''"
        ).fetchall()
    except Exception:
        return None
    ids = {r["project_id"] for r in rows}
    if len(ids) == 1:
        return next(iter(ids))
    return None


def scope_diagnostics(conn) -> list[dict]:
    """Flag active cards that carry no Project on an unambiguously-scoped board."""
    from hermes_cli import kanban_db as kb

    canonical = canonical_project_id(conn)
    if canonical is None:
        return []
    out = []
    for task in kb.list_tasks(conn, include_body=False):
        if task.status in TERMINAL_STATUSES or task.status == "triage":
            continue
        if task.project_id:
            continue
        out.append(
            {
                "task_id": task.id,
                "title": task.title,
                "reason_code": REASON_UNSCOPED_ACTIVE_WORK,
                "canonical_project_id": canonical,
                "status": task.status,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Ready-queue telemetry (invariant E)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadyEntry:
    """One ready card and the specific reason it did or did not spawn."""

    task_id: str
    assignee: Optional[str]
    reason_code: str
    detail: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "assignee": self.assignee,
            "reason_code": self.reason_code,
            "detail": self.detail,
        }


@dataclass
class ReadyQueueReport:
    """Per-board ready-queue census with a reason code for every card."""

    board: Optional[str] = None
    entries: list[ReadyEntry] = field(default_factory=list)
    spawnable_ids: list[str] = field(default_factory=list)
    at_global_cap: bool = False

    def to_dict(self) -> dict:
        return {
            "board": self.board,
            "entries": [e.to_dict() for e in self.entries],
            "spawnable": self.spawnable_ids,
            "at_global_cap": self.at_global_cap,
        }


def workspace_precondition_error(task) -> Optional[str]:
    """Read-only check of whether this card's workspace can resolve.

    Mirrors ``kanban_db.resolve_workspace``'s refusals WITHOUT creating any
    directory, so telemetry can name an invalid workspace instead of waiting
    for the spawn to fail and be misreported as a credential problem.
    """
    from pathlib import Path

    kind = task.workspace_kind or "scratch"
    path = (task.workspace_path or "").strip() or None
    if kind == "dir":
        if not path:
            return "workspace_kind=dir but no workspace_path"
        if not Path(path).expanduser().is_absolute():
            return f"workspace_path {path!r} is not absolute"
        return None
    if kind == "scratch":
        if path and not Path(path).expanduser().is_absolute():
            return f"workspace_path {path!r} is not absolute"
        return None
    if kind == "worktree":
        if path:
            return None
        try:
            from hermes_cli import kanban_db as kb

            meta = kb.read_board_metadata()
            if not (meta.get("default_workdir") or "").strip():
                return "worktree workspace with no workspace_path and no board default_workdir"
        except Exception:
            return None
        return None
    return f"unknown workspace_kind {kind!r}"


def ready_queue_report(
    conn,
    *,
    board: Optional[str] = None,
    max_in_progress: Optional[int] = None,
    max_in_progress_per_profile: Optional[int] = None,
    include_other_boards: bool = False,
) -> ReadyQueueReport:
    """Census every unclaimed ready card with the reason it is or is not spawnable.

    This is the telemetry counterpart of the dispatcher's own skip decisions
    and it applies the same rules in the same order, so a card can never be
    "spawnable" here and skipped there. Claimed cards are excluded entirely:
    work already in flight is not waiting work.
    """
    from hermes_cli import kanban_db as kb

    report = ReadyQueueReport(board=board)

    running = kb.count_running_tasks(conn)
    if include_other_boards:
        try:
            running += kb.count_running_tasks_other_boards(board)
        except Exception:
            pass
    if (
        isinstance(max_in_progress, int)
        and max_in_progress > 0
        and running >= max_in_progress
    ):
        report.at_global_cap = True

    per_profile_cap = (
        max_in_progress_per_profile
        if isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
        else None
    )
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for row in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL GROUP BY assignee"
        ):
            per_profile_running[row["assignee"]] = int(row["n"])

    try:
        from hermes_cli.profiles import profile_exists
    except Exception:
        profile_exists = None  # type: ignore[assignment]

    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()

    for row in rows:
        task = kb.get_task(conn, row["id"])
        if task is None:
            continue
        assignee = task.assignee

        if not assignee:
            report.entries.append(
                ReadyEntry(task.id, None, READY_UNASSIGNED, "needs routing")
            )
            continue
        if profile_exists is not None and not profile_exists(assignee):
            # A control-plane lane is pulled by a terminal via claim_task.
            # Correctly idle, not a failure — and emphatically not a
            # credential problem.
            report.entries.append(
                ReadyEntry(
                    task.id, assignee, READY_CONTROL_PLANE_LANE,
                    "assignee is a control-plane lane, not a Hermes profile",
                )
            )
            continue

        ws_error = workspace_precondition_error(task)
        if ws_error:
            report.entries.append(
                ReadyEntry(task.id, assignee, READY_INVALID_WORKSPACE, ws_error)
            )
            continue

        if report.at_global_cap:
            report.entries.append(
                ReadyEntry(
                    task.id, assignee, READY_CAPACITY_GLOBAL,
                    f"{running} running at max_in_progress={max_in_progress}",
                )
            )
            continue
        if per_profile_cap is not None:
            current = per_profile_running.get(assignee, 0)
            if current >= per_profile_cap:
                report.entries.append(
                    ReadyEntry(
                        task.id, assignee, READY_CAPACITY_PER_PROFILE,
                        f"{assignee} at {current}/{per_profile_cap} in flight",
                    )
                )
                continue

        try:
            guard = kb.check_respawn_guard(conn, task.id)
        except Exception:
            guard = None
        if guard is not None:
            report.entries.append(
                ReadyEntry(
                    task.id, assignee,
                    _GUARD_REASON_CODES.get(guard, f"guard_{guard}"),
                    f"respawn guard: {guard}",
                )
            )
            continue

        report.entries.append(ReadyEntry(task.id, assignee, READY_SPAWNABLE))
        report.spawnable_ids.append(task.id)
        if per_profile_cap is not None:
            per_profile_running[assignee] = per_profile_running.get(assignee, 0) + 1

    return report


def ready_queue_summary(reports: Iterable[ReadyQueueReport]) -> dict:
    """Aggregate several boards' reports into counts by reason code."""
    spawnable = 0
    nonspawnable: dict[str, int] = {}
    for report in reports:
        for entry in report.entries:
            if entry.reason_code == READY_SPAWNABLE:
                spawnable += 1
            else:
                nonspawnable[entry.reason_code] = (
                    nonspawnable.get(entry.reason_code, 0) + 1
                )
    return {"spawnable": spawnable, "nonspawnable": nonspawnable}


@dataclass(frozen=True)
class DispatcherStuckAlert:
    """A dispatcher-stuck condition that is actually actionable."""

    reason_code: str
    task_ids: list[str]
    message: str


def dispatcher_stuck_alert(
    reports: Iterable[ReadyQueueReport],
    *,
    consecutive_idle_ticks: int,
    grace_ticks: int,
) -> Optional[DispatcherStuckAlert]:
    """Alert only for genuinely eligible, below-cap work past the grace window.

    The pre-control-loop probe answered "is the ready queue non-empty?" and
    turned every capacity wait, respawn guard and control-plane lane into a
    *credential* warning that never cleared. Only ``spawnable`` cards — the
    ones the dispatcher itself would have spawned this tick — can raise this.
    """
    if consecutive_idle_ticks < max(int(grace_ticks), 1):
        return None
    reports = list(reports)
    stuck: list[str] = []
    for report in reports:
        stuck.extend(report.spawnable_ids)
    if not stuck:
        return None
    summary = ready_queue_summary(reports)
    other = ", ".join(
        f"{code}={count}" for code, count in sorted(summary["nonspawnable"].items())
    )
    message = (
        f"kanban dispatcher stuck: {len(stuck)} eligible below-cap card(s) "
        f"waited {consecutive_idle_ticks} consecutive ticks with 0 spawns "
        f"(ids: {', '.join(stuck[:5])}{'…' if len(stuck) > 5 else ''}). "
        f"Check the assignee profile's venv/PATH/credentials."
    )
    if other:
        message += f" Other ready cards are correctly waiting: {other}."
    return DispatcherStuckAlert(READY_SPAWNABLE, stuck, message)


# ---------------------------------------------------------------------------
# Board health payload (CLI + API)
# ---------------------------------------------------------------------------


def board_health(
    conn,
    *,
    now: Optional[int] = None,
    board: Optional[str] = None,
    include_ready_queue: bool = False,
) -> dict:
    """One machine-readable verdict for a whole board.

    Shared by ``hermes kanban board-health``, the dashboard's
    ``/api/plugins/kanban/board-health`` endpoint and the sentinel, so all
    three agree by construction rather than by convention.
    """
    from hermes_cli import kanban_db as kb

    ts = int(now if now is not None else time.time())
    wake = wake_subsystem_health(conn, now=ts)

    kevin_blocked: list[dict] = []
    automation_recovery: list[dict] = []
    holds: list[dict] = []
    no_forward_path: list[dict] = []
    counts: dict[str, int] = {}

    tasks = kb.list_tasks(conn, include_body=False)
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
        if task.status in TERMINAL_STATUSES:
            continue

        if task.status == "blocked":
            projection = classify_block(task)
            row = {
                "task_id": task.id,
                "title": task.title,
                "assignee": task.assignee,
                "block_kind": task.block_kind,
                "reason_code": projection.reason_code,
            }
            if projection.visible and projection.evidence is not None:
                row["action"] = projection.action
                row["evidence_type"] = projection.evidence.type
                row["affirmed_by"] = projection.evidence.affirmed_by
                row["affirmed_at"] = projection.evidence.affirmed_at
                kevin_blocked.append(row)
            else:
                automation_recovery.append(row)

        if task.status == "triage":
            reason_code = (
                REASON_UNAFFIRMED_GATE
                if task.block_kind in HUMAN_GATE_BLOCK_KINDS
                else REASON_TRANSIENT_BLOCK
                if task.block_kind == "transient"
                else REASON_UNTYPED_BLOCK
            )
            automation_recovery.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "assignee": task.assignee,
                    "block_kind": task.block_kind,
                    "reason_code": reason_code,
                }
            )

        if task.status == "scheduled":
            hold = classify_hold(
                task, now=ts, wake_health=wake,
                parents_done=_parents_done(conn, task.id),
            )
            holds.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "hold_kind": hold.kind,
                    "wake_at": hold.wake_at,
                    "healthy": hold.healthy,
                    "reason_code": hold.reason_code,
                    "needs_classification": hold.needs_classification,
                }
            )

        path = forward_path(conn, task, now=ts, wake_health=wake)
        if not path.ok:
            no_forward_path.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "path_kind": path.kind,
                    "reason_code": path.reason_code,
                    "detail": path.detail,
                }
            )

    scope = scope_diagnostics(conn)

    payload = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "control_loop_version": CONTROL_LOOP_VERSION,
        "board": board or _current_board_safe(),
        "generated_at": ts,
        "counts": counts,
        "kevin_blocked": kevin_blocked,
        "automation_recovery": automation_recovery,
        "holds": holds,
        "no_forward_path": no_forward_path,
        "scope": scope,
        "wake": wake,
    }
    payload["healthy"] = not (no_forward_path or scope)
    if include_ready_queue:
        payload["ready_queue"] = ready_queue_report(
            conn,
            board=payload["board"],
            max_in_progress=_configured_max_in_progress(),
            max_in_progress_per_profile=_configured_per_profile_cap(),
        ).to_dict()
    return payload


def _current_board_safe() -> Optional[str]:
    try:
        from hermes_cli import kanban_db as kb

        return kb.get_current_board()
    except Exception:
        return None


def _configured_max_in_progress() -> Optional[int]:
    try:
        from hermes_cli import kanban_db as kb

        return kb.configured_max_in_progress()
    except Exception:
        return None


def _configured_per_profile_cap() -> Optional[int]:
    try:
        from hermes_cli.config import load_config

        value = (load_config() or {}).get("kanban", {}).get(
            "max_in_progress_per_profile"
        )
        return int(value) if value else None
    except Exception:
        return None


def all_board_slugs() -> list[str]:
    """Every non-archived board slug, ``default`` included."""
    from hermes_cli import kanban_db as kb

    return [
        b.get("slug") or kb.DEFAULT_BOARD
        for b in kb.list_boards(include_archived=False)
    ]
