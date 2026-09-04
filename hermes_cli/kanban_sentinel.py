"""Deterministic, model-free Kanban sentinel.

The sentinel is a *verifier of last resort*, not a second controller. Its
whole value is that it fails in different ways than the thing it watches: no
provider, no agent loop, no LLM, no network — just SQLite reads and a small
number of arithmetic comparisons. If the native control loop
(:mod:`hermes_cli.kanban_health`) is healthy, the sentinel does nothing at
all.

Rules it holds itself to
------------------------
* **Inspect everything.** Every non-archived board, every sweep.
* **Fail loudly on drift.** It refuses to act on a controller whose version
  or reason-code vocabulary it was not written against. A verifier that
  silently keeps approving a subsystem it no longer understands is worse
  than no verifier.
* **Never race the controller.** While the controller's checkpoint is fresh,
  the sentinel is read-only. Two writers racing on the same holds is a
  defect, not redundancy.
* **Repair only what state already authorizes.** Exactly one repair class:
  resuming a *typed* hold the controller would itself have resumed. It is
  reversible (the card returns to its normal queue) and idempotent (the same
  guard the controller uses). It never resumes a legacy untyped hold, never
  touches a human gate, and never invents a classification.
* **One atomic action.** When the controller cannot recover, the sweep emits
  a single human action for the whole fleet — not one per card. A pager that
  fires forty times is a pager nobody reads.

Activation is deliberately NOT automatic: this module ships the capability
and ``hermes kanban sentinel`` runs it. Scheduling it is an operator
decision — see ``docs/kanban/health-control-loop.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_health as kh

logger = logging.getLogger(__name__)

SENTINEL_VERSION = 1

# The controller contract this sentinel was written against. A mismatch is a
# hard stop, not a warning.
EXPECTED_CONTROL_LOOP_VERSION = 1
EXPECTED_HEALTH_SCHEMA_VERSION = 1

# Reason codes the sentinel matches on. If the controller stops exporting
# one, the sentinel's logic has silently changed meaning — treat as drift.
REQUIRED_REASON_CODES = (
    "REASON_LEGACY_UNTYPED",
    "REASON_WAKE_DUE",
    "REASON_WAKE_MISSING",
    "REASON_WAKE_FAILED",
    "REASON_DEPENDENCY_SATISFIED",
    "REASON_INTENTIONAL_HOLD",
)

# Identical alerts inside this window are reported but not re-raised.
ALERT_DEDUPE_SECONDS = 6 * 60 * 60

REASON_CONTROLLER_ACTIVE = "controller_active"
REASON_CONTROLLER_DOWN = "controller_down"
REASON_BOARD_UNREADABLE = "board_unreadable"

CHECKPOINT_SENTINEL = "sentinel"


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drift:
    """A controller contract the sentinel does not recognise."""

    code: str
    detail: str

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


def detect_drift() -> Optional[Drift]:
    """Return a :class:`Drift` when the controller is not the one we verify."""
    actual = int(getattr(kh, "CONTROL_LOOP_VERSION", -1))
    if actual != EXPECTED_CONTROL_LOOP_VERSION:
        return Drift(
            "control_loop_version",
            f"controller reports CONTROL_LOOP_VERSION={actual}, sentinel "
            f"{SENTINEL_VERSION} verifies {EXPECTED_CONTROL_LOOP_VERSION}",
        )
    schema = int(getattr(kh, "HEALTH_SCHEMA_VERSION", -1))
    if schema != EXPECTED_HEALTH_SCHEMA_VERSION:
        return Drift(
            "health_schema_version",
            f"board-health schema is v{schema}, sentinel verifies "
            f"v{EXPECTED_HEALTH_SCHEMA_VERSION}",
        )
    absent = [name for name in REQUIRED_REASON_CODES if not hasattr(kh, name)]
    if absent:
        return Drift(
            "reason_code_vocabulary",
            "controller no longer exports: " + ", ".join(absent),
        )
    return None


# ---------------------------------------------------------------------------
# Alert dedupe state
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "sentinel-state.json"


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug("sentinel state write failed: %s", exc)


def _fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class SentinelReport:
    """One sweep."""

    ok: bool = True
    version: int = SENTINEL_VERSION
    generated_at: int = 0
    drift: Optional[dict] = None
    boards: list[dict] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    would_repair: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    kevin_action: Optional[dict] = None
    alert_emitted: bool = False
    suppressed_until: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "version": self.version,
            "generated_at": self.generated_at,
            "drift": self.drift,
            "boards": self.boards,
            "repairs": self.repairs,
            "would_repair": self.would_repair,
            "unresolved": self.unresolved,
            "kevin_action": self.kevin_action,
            "alert_emitted": self.alert_emitted,
            "suppressed_until": self.suppressed_until,
        }


def _atomic_action(*, title: str, action: str, evidence: dict, now: int) -> dict:
    """One human action for the whole sweep, in the typed-evidence shape."""
    return {
        "title": title,
        "action": action,
        "type": "human_decision",
        "affirmed_by": "hermes-kanban-sentinel",
        "affirmed_at": now,
        "evidence": evidence,
    }


def run_sentinel(
    *, now: Optional[int] = None, apply: bool = True
) -> SentinelReport:
    """Sweep every board once.

    ``apply=False`` is a full dry run: identical analysis, zero writes.
    """
    from hermes_cli import kanban_db as kb

    ts = int(now if now is not None else time.time())
    report = SentinelReport(generated_at=ts)

    drift = detect_drift()
    if drift is not None:
        # Stand down completely. We cannot prove our repairs still mean what
        # they meant when they were written.
        report.ok = False
        report.drift = drift.to_dict()
        report.kevin_action = _atomic_action(
            title="Kanban sentinel stood down: controller version drift",
            action=(
                "Re-verify hermes_cli/kanban_sentinel.py against the current "
                "kanban_health contract, then bump EXPECTED_CONTROL_LOOP_VERSION."
            ),
            evidence={"drift": drift.to_dict(), "task_ids": []},
            now=ts,
        )
        report.alert_emitted = _maybe_emit(report, ts, apply=apply)
        return report

    unresolved: list[dict] = []

    for slug in kh.all_board_slugs():
        board_row: dict = {"board": slug}
        conn = None
        try:
            conn = kb.connect(board=slug)
            checkpoint = kh.read_checkpoint(conn, kh.CHECKPOINT_RECONCILE)
            controller_live = bool(
                checkpoint
                and checkpoint.get("status") == "ok"
                and ts - int(checkpoint["updated_at"])
                <= kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS
            )
            board_row["controller_checkpoint"] = checkpoint
            board_row["reason_code"] = (
                REASON_CONTROLLER_ACTIVE if controller_live else REASON_CONTROLLER_DOWN
            )

            def _reconcile_if_still_down() -> None:
                nonlocal controller_live
                # Re-read after acquiring the dispatcher lock. A controller may
                # have started between the first observation and this write.
                current = kh.read_checkpoint(conn, kh.CHECKPOINT_RECONCILE)
                controller_live = bool(
                    current
                    and current.get("status") == "ok"
                    and ts - int(current["updated_at"])
                    <= kh.WAKE_CHECKPOINT_MAX_AGE_SECONDS
                )
                board_row["controller_checkpoint"] = current
                if controller_live:
                    board_row["reason_code"] = REASON_CONTROLLER_ACTIVE
                    return
                pass_report = kh.reconcile_board(
                    conn, now=ts, apply=apply, stamp_checkpoint=False
                )
                for entry in pass_report.resumed:
                    row = {**entry, "board": slug}
                    if apply:
                        report.repairs.append(row)
                    else:
                        report.would_repair.append(row)
                if apply and (pass_report.resumed or pass_report.diagnosed):
                    kh.record_checkpoint(
                        conn,
                        CHECKPOINT_SENTINEL,
                        status="ok",
                        now=ts,
                        detail=f"{len(pass_report.resumed)} repaired by sentinel",
                    )

            if not controller_live:
                if apply:
                    # Serialize with dispatch_once(), whose native reconciler
                    # owns this same board-scoped lock. A losing sentinel skips
                    # this sweep instead of becoming a second writer.
                    with kb._dispatch_tick_lock(kb.kanban_db_path(slug)) as held:
                        if held:
                            _reconcile_if_still_down()
                        else:
                            board_row["reason_code"] = REASON_CONTROLLER_ACTIVE
                else:
                    _reconcile_if_still_down()

            health = kh.board_health(conn, now=ts, board=slug)
            board_row["healthy"] = health["healthy"]
            board_row["counts"] = health["counts"]
            for row in health["no_forward_path"]:
                unresolved.append({**row, "board": slug})
            # Scope failures are independently actionable even when a card also
            # lacks a forward path. Preserve both reason codes; evidence below
            # deduplicates task ids so Kevin receives one atomic page.
            for row in health.get("scope", []):
                unresolved.append({**row, "board": slug})
        except Exception as exc:
            board_row["reason_code"] = REASON_BOARD_UNREADABLE
            board_row["error"] = str(exc)
            board_row["healthy"] = False
            report.ok = False
            unresolved.append(
                {"board": slug, "task_id": None, "reason_code": REASON_BOARD_UNREADABLE,
                 "detail": str(exc)}
            )
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        report.boards.append(board_row)

    report.unresolved = unresolved
    if unresolved:
        report.ok = False
        task_ids = sorted({u["task_id"] for u in unresolved if u.get("task_id")})
        codes = sorted({u.get("reason_code") for u in unresolved if u.get("reason_code")})
        report.kevin_action = _atomic_action(
            title="Kanban cards have no forward path the controller can restore",
            action=(
                "Classify or route the listed cards: run "
                "`hermes kanban board-health --all-boards` and give each one a "
                "typed hold, an owner, or a close."
            ),
            evidence={
                "task_ids": task_ids,
                "reason_codes": codes,
                "count": len(unresolved),
            },
            now=ts,
        )
        report.alert_emitted = _maybe_emit(report, ts, apply=apply)

    return report


def _maybe_emit(report: SentinelReport, now: int, *, apply: bool) -> bool:
    """Dedupe identical alerts. Returns True when this one is newly raised.

    A suppressed alert is still reported in the payload — the sweep tells the
    truth about board state every time. Suppression only governs whether this
    is a *new* page.
    """
    action = report.kevin_action
    if action is None:
        return False
    fingerprint = _fingerprint(
        {"title": action["title"], "evidence": action["evidence"]}
    )
    # A dry run must be observationally pure. ``kb.connect()`` initializes and
    # migrates a board, and the dedupe table below is itself durable state, so
    # even opening it would violate ``--dry-run``. Report that this action
    # would be newly emitted without claiming the dedupe interval.
    if not apply:
        return True

    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sentinel_alert_dedupe ("
                "fingerprint TEXT PRIMARY KEY, last_emitted_at INTEGER NOT NULL, "
                "title TEXT NOT NULL)"
            )
            previous = conn.execute(
                "SELECT last_emitted_at FROM sentinel_alert_dedupe WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            last = int(previous["last_emitted_at"]) if previous else 0
            if previous is not None and now - last < ALERT_DEDUPE_SECONDS:
                report.suppressed_until = last + ALERT_DEDUPE_SECONDS
                return False
            # The read and conditional upsert share BEGIN IMMEDIATE. Exactly
            # one concurrent sentinel can claim this alert interval.
            conn.execute(
                "INSERT INTO sentinel_alert_dedupe(fingerprint,last_emitted_at,title) "
                "VALUES(?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
                "last_emitted_at=excluded.last_emitted_at,title=excluded.title",
                (fingerprint, now, action["title"]),
            )
            conn.execute(
                "DELETE FROM sentinel_alert_dedupe WHERE fingerprint NOT IN ("
                "SELECT fingerprint FROM sentinel_alert_dedupe "
                "ORDER BY last_emitted_at DESC LIMIT 64)"
            )
            return True
    finally:
        conn.close()
