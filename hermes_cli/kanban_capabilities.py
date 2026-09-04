"""Kanban lifecycle capability manifest and pre-activation canary.

The failure this exists to stop
------------------------------
``hermes update`` resolves its branch to a hardcoded ``main`` unless
``--branch`` is passed. An install whose lifecycle capability lives on a
maintained branch therefore gets fast-forwarded onto an upstream tree that
never carried it — and the update reports success, because "the tree
imports" was the only post-swap check. The board then looks fine while its
control loop is simply gone: scheduled cards stop waking, machine failures
start rendering as human gates, and nothing anywhere says so.

Two guards, both required
-------------------------
1. **A maintained-branch strategy.** ``update.branch`` in ``config.yaml``
   (read by ``_resolve_update_branch``) so the updater pulls the lineage
   the install actually runs, instead of assuming upstream ``main``.
2. **A pre-activation canary.** Before a staged tree is activated, probe it
   in a subprocess for every capability the *running* install requires. A
   capability that disappeared fails the update loudly rather than silently.

Why the manifest lives on the running install
---------------------------------------------
The canary compares the staged tree's probe against the requirements of the
install being replaced. Reading the requirement list out of the staged tree
would make the check vacuous — a tree that dropped a capability would also
have dropped it from its own manifest and cheerfully certify itself.

Probes are behavioural: each one calls the real code path. None of them read
source text, so a rename or a refactor that preserves behaviour keeps
passing, and a stub that preserves the name but not the behaviour fails.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA_VERSION = 1

# How long the staged-tree probe may take before we treat it as failed.
CANARY_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Capability:
    """One lifecycle capability the board's health depends on."""

    name: str
    description: str
    probe: Callable[[], bool]


# ---------------------------------------------------------------------------
# Probes — each exercises the real code path, never the source text
# ---------------------------------------------------------------------------


def _probe_typed_block_projection() -> bool:
    """A machine hold and an affirmed gate must project differently."""
    from types import SimpleNamespace

    from hermes_cli import kanban_health as kh

    untyped = SimpleNamespace(id="t", block_kind=None, gate_evidence=None)
    if kh.classify_block(untyped).visible:
        return False
    affirmed = SimpleNamespace(
        id="t",
        block_kind="capability",
        gate_evidence=json.dumps(
            {
                "type": "human_decision",
                "action": "Sign it",
                "affirmed_by": "kevin",
                "affirmed_at": 1,
                "task_id": "t",
                "occurrence_event_id": 1,
            }
        ),
    )
    projection = kh.classify_block(affirmed)
    return bool(projection.visible and projection.action == "Sign it")


def _probe_typed_scheduled_hold() -> bool:
    """The typed-hold vocabulary and its writer must both exist."""
    from hermes_cli import kanban_health as kh

    if not {"dependency", "wake"} <= set(kh.VALID_HOLD_KINDS):
        return False
    if not kh.PARKED_HOLD_KINDS:
        return False
    return callable(getattr(kh, "set_hold", None))


def _probe_durable_wake_reconciler() -> bool:
    """Reconciliation plus a durable checkpoint the wake health depends on."""
    from hermes_cli import kanban_health as kh

    return (
        callable(getattr(kh, "reconcile_board", None))
        and callable(getattr(kh, "record_checkpoint", None))
        and callable(getattr(kh, "read_checkpoint", None))
        and callable(getattr(kh, "wake_subsystem_health", None))
    )


def _probe_legacy_hold_is_loud() -> bool:
    """A prose-only scheduled card must classify as unhealthy, not healthy."""
    from types import SimpleNamespace

    from hermes_cli import kanban_health as kh

    legacy = SimpleNamespace(
        id="t", status="scheduled", hold_kind=None, hold_wake_at=None
    )
    state = kh.classify_hold(
        legacy,
        now=0,
        wake_health={"enabled": True, "healthy": True, "reason_code": None},
        parents_done=True,
    )
    return (
        state.healthy is False
        and state.reason_code == kh.REASON_LEGACY_UNTYPED
        and state.needs_classification is True
    )


def _probe_forward_path_audit() -> bool:
    from hermes_cli import kanban_health as kh

    return callable(getattr(kh, "forward_paths", None)) and callable(
        getattr(kh, "forward_path", None)
    )


def _probe_board_health_report() -> bool:
    from hermes_cli import kanban_health as kh

    return (
        callable(getattr(kh, "board_health", None))
        and int(getattr(kh, "HEALTH_SCHEMA_VERSION", 0)) >= 1
    )


def _probe_ready_queue_reason_codes() -> bool:
    """Capacity waits and guards must be distinguishable from real stalls."""
    from hermes_cli import kanban_health as kh

    codes = {
        kh.READY_SPAWNABLE,
        kh.READY_CAPACITY_GLOBAL,
        kh.READY_CAPACITY_PER_PROFILE,
        kh.READY_INVALID_WORKSPACE,
        kh.READY_GUARD_ACTIVE_PR,
        kh.READY_CONTROL_PLANE_LANE,
        kh.READY_UNASSIGNED,
    }
    if len(codes) != 7:
        return False
    return callable(getattr(kh, "ready_queue_report", None)) and callable(
        getattr(kh, "dispatcher_stuck_alert", None)
    )


def _probe_dispatcher_stuck_is_gated() -> bool:
    """A queue with no spawnable card must never raise dispatcher-stuck."""
    from hermes_cli import kanban_health as kh

    capped = kh.ReadyQueueReport(
        board="b",
        entries=[kh.ReadyEntry("t1", "alice", kh.READY_CAPACITY_GLOBAL)],
        spawnable_ids=[],
        at_global_cap=True,
    )
    if kh.dispatcher_stuck_alert([capped], consecutive_idle_ticks=99, grace_ticks=3):
        return False
    live = kh.ReadyQueueReport(
        board="b",
        entries=[kh.ReadyEntry("t2", "alice", kh.READY_SPAWNABLE)],
        spawnable_ids=["t2"],
    )
    return bool(
        kh.dispatcher_stuck_alert([live], consecutive_idle_ticks=3, grace_ticks=3)
    )


def _probe_continuation_scope_inheritance() -> bool:
    """Child routing must be able to inherit scope from the parent row."""
    from tools import kanban_tools as kt

    return callable(getattr(kt, "_inherited_parent_scope", None))


def _probe_health_cli() -> bool:
    """The CLI must expose the board-health / scheduled-wake diagnostics."""
    import argparse

    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kcli.build_parser(sub)
    for argv in (
        ["kanban", "board-health", "--json"],
        ["kanban", "scheduled-wake", "--json"],
        ["kanban", "sentinel", "--json"],
    ):
        try:
            parser.parse_args(argv)
        except SystemExit:
            return False
    return True


def _probe_sentinel() -> bool:
    from hermes_cli import kanban_sentinel as ks

    return callable(getattr(ks, "run_sentinel", None))


REQUIRED_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "kanban.typed_block_projection",
        "Visible blocked is an affirmed, typed, atomic human gate; machine "
        "holds route to automation recovery.",
        _probe_typed_block_projection,
    ),
    Capability(
        "kanban.typed_scheduled_hold",
        "Scheduled cards carry a typed hold classification.",
        _probe_typed_scheduled_hold,
    ),
    Capability(
        "kanban.durable_wake_reconciler",
        "Idempotent reconciliation with a durable wake checkpoint.",
        _probe_durable_wake_reconciler,
    ),
    Capability(
        "kanban.legacy_hold_is_loud",
        "Legacy prose-only scheduled holds are visibly unhealthy and never "
        "silently healthy.",
        _probe_legacy_hold_is_loud,
    ),
    Capability(
        "kanban.forward_path_audit",
        "Every nonterminal card is audited for a machine-verifiable forward path.",
        _probe_forward_path_audit,
    ),
    Capability(
        "kanban.board_health_report",
        "First-class machine-readable board-health payload.",
        _probe_board_health_report,
    ),
    Capability(
        "kanban.ready_queue_reason_codes",
        "Nonspawnable ready states expose distinct reason codes.",
        _probe_ready_queue_reason_codes,
    ),
    Capability(
        "kanban.dispatcher_stuck_is_gated",
        "Dispatcher-stuck alerts only on eligible, below-cap work.",
        _probe_dispatcher_stuck_is_gated,
    ),
    Capability(
        "kanban.continuation_scope_inheritance",
        "Continuations inherit Project/principal/executor/tenant scope from "
        "the parent row.",
        _probe_continuation_scope_inheritance,
    ),
    Capability(
        "kanban.health_cli",
        "board-health / scheduled-wake / sentinel CLI diagnostics.",
        _probe_health_cli,
    ),
    Capability(
        "kanban.sentinel",
        "Deterministic no-agent sentinel available as a local CLI surface.",
        _probe_sentinel,
    ),
)

REQUIRED_CAPABILITY_NAMES: tuple[str, ...] = tuple(
    c.name for c in REQUIRED_CAPABILITIES
)


def capability_manifest() -> dict:
    """The serializable contract the canary checks a staged tree against."""
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "required": list(REQUIRED_CAPABILITY_NAMES),
        "descriptions": {c.name: c.description for c in REQUIRED_CAPABILITIES},
    }


def probe_capabilities() -> dict:
    """Run every probe on THIS tree. Returns ``{name: bool}``.

    A probe that raises counts as absent — a capability that cannot be
    exercised without blowing up is not a capability.
    """
    out: dict[str, bool] = {}
    for cap in REQUIRED_CAPABILITIES:
        try:
            out[cap.name] = bool(cap.probe())
        except Exception as exc:
            logger.debug("capability probe %s failed: %s", cap.name, exc)
            out[cap.name] = False
    return out


@dataclass
class CapabilityReport:
    ok: bool
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: str = ""
    probed: bool = True
    """False when the probe could not be executed at all (tree unimportable,
    subprocess blocked, timeout) — as opposed to executing and reporting a
    capability as absent.

    The report still fails closed either way (``ok`` is False, everything is
    listed missing), because "we could not tell" must never read as "fine".
    The two cases warrant different diagnostics, but both fail activation: a
    probe that ran and found a capability gone proves a dropped invariant; an
    unprobeable candidate has not supplied the evidence required to activate
    safely."""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "present": self.present,
            "missing": self.missing,
            "detail": self.detail,
            "probed": self.probed,
        }


def verify_capabilities(probe: Optional[dict] = None) -> CapabilityReport:
    """Check a probe result against the required manifest.

    Absent AND present-but-false both count as missing: a capability wired in
    but broken is the more dangerous of the two, because its name still
    appears everywhere that looks for it.
    """
    results = probe_capabilities() if probe is None else probe
    present, missing = [], []
    for name in REQUIRED_CAPABILITY_NAMES:
        if bool(results.get(name)):
            present.append(name)
        else:
            missing.append(name)
    return CapabilityReport(
        ok=not missing,
        present=present,
        missing=missing,
        detail=(
            "all required Kanban lifecycle capabilities present"
            if not missing
            else "missing required Kanban lifecycle capability: "
            + ", ".join(missing)
        ),
    )


# The probe reports WHERE it imported from, not just what it found. Without
# that, a staged root missing ``hermes_cli`` silently resolves the *installed*
# package instead and the canary certifies the wrong tree — the exact
# false-negative it exists to prevent.
_PROBE_SCRIPT = (
    "import json,sys\n"
    "try:\n"
    "    import hermes_cli.kanban_capabilities as m\n"
    "    print(json.dumps({\n"
    "        'module_file': getattr(m, '__file__', None),\n"
    "        'capabilities': m.probe_capabilities(),\n"
    "    }))\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'__error__': repr(exc)}))\n"
    "    sys.exit(3)\n"
)


def preactivation_canary(
    root: Path, *, timeout: int = CANARY_TIMEOUT_SECONDS
) -> CapabilityReport:
    """Probe a STAGED tree for every capability this install requires.

    Runs out-of-process against *root* so the answer describes the tree about
    to be activated, not the modules already imported here. Fails closed: an
    unimportable or crashing tree reports every capability missing, because
    "we could not tell" must never read as "fine".
    """
    root = Path(root)
    try:
        if not (root / "hermes_cli" / "kanban_capabilities.py").is_file():
            raise RuntimeError(
                f"{root} does not contain hermes_cli/kanban_capabilities.py"
            )
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_probe_env(root),
        )
        payload = json.loads((proc.stdout or "").strip() or "{}")
        if not isinstance(payload, dict) or "__error__" in payload:
            raise RuntimeError(
                payload.get("__error__", "probe produced no result")
                if isinstance(payload, dict)
                else "probe produced a non-object result"
            )
        # Provenance: the answer must describe the STAGED tree. If the
        # subprocess resolved an installed copy instead, we learned nothing
        # about what is about to be activated.
        module_file = payload.get("module_file")
        try:
            resolved_under_root = (
                module_file is not None
                and Path(module_file).resolve().is_relative_to(root.resolve())
            )
        except (OSError, ValueError):
            resolved_under_root = False
        if not resolved_under_root:
            raise RuntimeError(
                f"probe resolved hermes_cli from {module_file!r}, which is not "
                f"under the staged tree {root} — the result would describe the "
                f"wrong tree"
            )
        payload = payload.get("capabilities")
        if not isinstance(payload, dict):
            raise RuntimeError("probe returned no capability map")
    except Exception as exc:
        return CapabilityReport(
            ok=False,
            present=[],
            missing=list(REQUIRED_CAPABILITY_NAMES),
            probed=False,
            detail=(
                f"pre-activation capability canary could not probe {root}: {exc}. "
                "Missing (fail-closed): " + ", ".join(REQUIRED_CAPABILITY_NAMES)
            ),
        )
    return verify_capabilities(payload)


def _probe_env(root: Path) -> dict:
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{root}{os.pathsep}{existing}" if existing else str(root)
    )
    # Never let the probe touch the operator's real home.
    env.pop("HERMES_KANBAN_DB", None)
    return env
