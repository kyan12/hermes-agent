"""Updates must not be able to silently drop Kanban lifecycle capability.

Root cause this pins: ``hermes update`` resolves its branch to a hardcoded
``main`` unless ``--branch`` is passed, so an install whose lifecycle
capability lives on a maintained branch is fast-forwarded onto an upstream
tree that never had it — and the update reports success. Two guards:

1. A supported, config-backed maintained-branch strategy, so the update
   pulls the lineage the install actually runs.
2. A pre-activation capability canary that probes the STAGED tree (not the
   running process) against the running install's required manifest — and
   with the running install's own behavioural probes — failing loudly when a
   required lifecycle capability disappeared.

The candidate supplies the code under test. It must never supply the test:
a tree shipping no-op probes, or a manifest that quietly forgot a
capability, has to fail rather than certify itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_capabilities as kc


# ---------------------------------------------------------------------------
# The manifest itself
# ---------------------------------------------------------------------------


def test_required_capabilities_are_named_and_probeable():
    manifest = kc.capability_manifest()
    assert manifest["schema_version"] >= 1
    assert manifest["required"], "manifest declares no required capability"
    for name in manifest["required"]:
        assert isinstance(name, str) and name


def test_live_tree_satisfies_every_required_capability():
    report = kc.verify_capabilities()
    assert report.ok, f"missing on the live tree: {report.missing}"
    assert report.missing == []


def test_verify_fails_when_a_required_capability_is_absent():
    complete = kc.probe_capabilities()
    dropped = sorted(complete)[0]
    degraded = {k: v for k, v in complete.items() if k != dropped}
    report = kc.verify_capabilities(probe=degraded)
    assert report.ok is False
    assert dropped in report.missing


def test_verify_fails_when_a_capability_is_present_but_broken():
    probe = dict(kc.probe_capabilities())
    name = sorted(probe)[0]
    probe[name] = False
    report = kc.verify_capabilities(probe=probe)
    assert report.ok is False
    assert name in report.missing


# ---------------------------------------------------------------------------
# Pre-activation canary against a staged tree
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[2]

# A candidate that keeps every symbol of the sentinel callable and drops the
# only thing the sentinel is for: reporting a board it cannot recover. This is
# the exact shape a symbol-presence probe waves through.
_NO_OP_SENTINEL = textwrap.dedent(
    '''\
    """Candidate sentinel: same symbols, no behaviour."""
    from dataclasses import dataclass, field
    from typing import Optional

    SENTINEL_VERSION = 1
    EXPECTED_CONTROL_LOOP_VERSION = 1
    EXPECTED_HEALTH_SCHEMA_VERSION = 1
    ALERT_DEDUPE_SECONDS = 6 * 60 * 60
    CHECKPOINT_SENTINEL = "sentinel"
    REASON_CONTROLLER_ACTIVE = "controller_active"
    REASON_CONTROLLER_DOWN = "controller_down"
    REASON_BOARD_UNREADABLE = "board_unreadable"


    def detect_drift():
        return None


    @dataclass
    class SentinelReport:
        ok: bool = True
        version: int = SENTINEL_VERSION
        generated_at: int = 0
        drift: Optional[dict] = None
        boards: list = field(default_factory=list)
        repairs: list = field(default_factory=list)
        would_repair: list = field(default_factory=list)
        unresolved: list = field(default_factory=list)
        kevin_action: Optional[dict] = None
        alert_emitted: bool = False
        suppressed_until: Optional[int] = None

        def to_dict(self):
            return dict(self.__dict__)


    def run_sentinel(*, now=None, apply=True):
        return SentinelReport(generated_at=int(now or 0))
    '''
)

_SKIP_MIRROR = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".worktrees", "node_modules", ".venv", "venv", ".tox",
}


def _mirror_install(root: Path, patch: dict | None = None) -> Path:
    """A staged tree that really is this install, with optional file swaps.

    Files are symlinked so the mirror is cheap; any directory containing a
    patched file is materialised for real so the swap is visible. The result
    is a candidate whose behaviour the canary can genuinely exercise, which is
    the only kind of candidate a behavioural probe can say anything about.
    """
    patch = patch or {}
    # Always materialise the package the canary probes, so the staged tree is
    # a real file tree rather than a single symlink to this checkout.
    real_dirs = {"hermes_cli"}
    for rel in patch:
        parent = Path(rel).parent
        while str(parent) not in (".", ""):
            real_dirs.add(str(parent))
            parent = parent.parent

    def _mirror(rel: str) -> None:
        src = REPO_ROOT / rel if rel else REPO_ROOT
        dst = root / rel if rel else root
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            if child.name in _SKIP_MIRROR:
                continue
            child_rel = f"{rel}/{child.name}" if rel else child.name
            target = root / child_rel
            if child_rel in patch:
                target.write_text(patch[child_rel], encoding="utf-8")
            elif child_rel in real_dirs and child.is_dir():
                _mirror(child_rel)
            else:
                os.symlink(child, target)

    _mirror("")
    return root


def _candidate_with_replacement(tmp_path: Path, rel: str, old: str, new: str) -> Path:
    """Mirror this install with one deliberate production-seam regression."""
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert source.count(old) == 1, f"regression fixture is stale for {rel}"
    return _mirror_install(
        tmp_path / "staged",
        patch={rel: source.replace(old, new)},
    )


def _self_certifying_manifest() -> str:
    """A candidate manifest that declares every capability present."""
    return textwrap.dedent(
        f"""
        REQUIRED_CAPABILITY_NAMES = {tuple(kc.REQUIRED_CAPABILITY_NAMES)!r}
        MANIFEST_SCHEMA_VERSION = 99


        def capability_manifest():
            return {{
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "required": list(REQUIRED_CAPABILITY_NAMES),
                "descriptions": {{}},
            }}


        def probe_capabilities():
            # "Everything is fine." The running install must not believe it.
            return dict.fromkeys(REQUIRED_CAPABILITY_NAMES, True)
        """
    )


def test_canary_passes_when_the_staged_tree_keeps_every_capability(tmp_path):
    staged = _mirror_install(tmp_path / "staged")
    result = kc.preactivation_canary(staged)
    assert result.ok, result.detail
    assert result.missing == []


def test_candidate_cannot_self_certify_with_its_own_probes(tmp_path):
    """The hole this closes: the staged tree used to supply BOTH the
    implementation and the probe that judged it, so a candidate carrying no-op
    lifecycle code and an all-green ``probe_capabilities`` passed."""
    staged = _mirror_install(
        tmp_path / "staged",
        patch={
            "hermes_cli/kanban_sentinel.py": _NO_OP_SENTINEL,
            "hermes_cli/kanban_capabilities.py": _self_certifying_manifest(),
        },
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert result.probed is True
    assert result.missing == ["kanban.sentinel"], result.detail
    assert "kanban.sentinel" in result.detail


def test_canary_fails_a_candidate_that_keeps_symbols_but_no_ops_behaviour(tmp_path):
    """``callable(run_sentinel)`` is not a capability. Only the behaviour is."""
    staged = _mirror_install(
        tmp_path / "staged",
        patch={"hermes_cli/kanban_sentinel.py": _NO_OP_SENTINEL},
    )
    # The candidate's symbols are all still there and importable …
    assert (staged / "hermes_cli" / "kanban_sentinel.py").is_file()
    result = kc.preactivation_canary(staged)
    # … and the canary fails anyway, because the behaviour is gone.
    assert result.ok is False
    assert "kanban.sentinel" in result.missing


def test_canary_fails_when_dispatch_drops_native_reconciliation(tmp_path):
    staged = _candidate_with_replacement(
        tmp_path,
        "hermes_cli/kanban_db.py",
        "    if not dry_run:\n        try:\n            from hermes_cli import kanban_health as _kh\n\n            _health = _kh.reconcile_board(conn)\n",
        "    if False and not dry_run:\n        try:\n            from hermes_cli import kanban_health as _kh\n\n            _health = _kh.reconcile_board(conn)\n",
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert "kanban.durable_wake_reconciler" in result.missing


def test_canary_fails_when_dispatch_and_ready_report_drift(tmp_path):
    staged = _candidate_with_replacement(
        tmp_path,
        "hermes_cli/kanban_health.py",
        '        assignee = task.assignee or (fallback_assignee if lane == "ready" else None)\n',
        "        assignee = task.assignee or fallback_assignee\n",
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert "kanban.ready_queue_reason_codes" in result.missing


def test_canary_uses_running_probe_to_reject_zero_cap_drift(tmp_path):
    """The candidate cannot redefine max_spawn=0 as unlimited and self-certify."""
    staged = _candidate_with_replacement(
        tmp_path,
        "hermes_cli/kanban_db.py",
        "    max_spawn = normalize_max_spawn(max_spawn)\n",
        "    max_spawn = None if max_spawn == 0 else normalize_max_spawn(max_spawn)\n",
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert "kanban.ready_queue_reason_codes" in result.missing


def test_canary_fails_when_continuation_authority_is_reduced_to_ambient_scope(tmp_path):
    staged = _candidate_with_replacement(
        tmp_path,
        "tools/kanban_tools.py",
        '    self_tid = os.environ.get("HERMES_KANBAN_TASK")\n',
        "    self_tid = None  # regression: trust ambient scope, not the task row\n",
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert "kanban.continuation_scope_inheritance" in result.missing


def test_canary_fails_when_user_surface_block_projection_is_bypassed(tmp_path):
    staged = _candidate_with_replacement(
        tmp_path,
        "hermes_cli/kanban_health.py",
        "    projected = dict(payload)\n    status = getattr(task, \"status\", None)\n",
        "    return dict(payload)  # regression: expose durable blocked blindly\n    status = getattr(task, \"status\", None)\n",
    )
    result = kc.preactivation_canary(staged)
    assert result.ok is False, result.detail
    assert "kanban.typed_block_projection" in result.missing


def test_canary_fails_closed_when_the_staged_tree_cannot_be_probed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = kc.preactivation_canary(empty)
    assert result.ok is False
    assert result.missing  # fail-closed: everything is reported missing
    # …but flagged as "could not tell", NOT as "capability was dropped".
    assert result.probed is False


def test_a_stub_tree_is_unprobeable_rather_than_certified(tmp_path):
    """A minimal tree that only defines ``probe_capabilities`` has no
    implementation to exercise. It must read as absence of evidence, never as
    a pass."""
    stub = tmp_path / "stub" / "hermes_cli"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("", encoding="utf-8")
    (stub / "kanban_capabilities.py").write_text(
        _self_certifying_manifest(), encoding="utf-8"
    )
    result = kc.preactivation_canary(tmp_path / "stub")
    assert result.ok is False
    assert result.probed is False
    assert result.missing == list(kc.REQUIRED_CAPABILITY_NAMES)


def test_a_real_probe_is_distinguishable_from_an_unprobeable_tree(tmp_path):
    """The update driver responds differently to the two, so the report must
    keep them apart: a probe that ran and found a capability gone is proof of
    a dropped invariant; a probe that could not run is absence of evidence."""
    dropped = kc.preactivation_canary(
        _mirror_install(
            tmp_path / "dropped",
            patch={"hermes_cli/kanban_sentinel.py": _NO_OP_SENTINEL},
        )
    )
    unprobeable = kc.preactivation_canary(tmp_path / "missing-tree")

    assert dropped.ok is False and dropped.probed is True
    assert unprobeable.ok is False and unprobeable.probed is False


def test_update_canary_fails_the_update_only_on_a_real_dropped_capability(
    tmp_path, monkeypatch, capsys
):
    """An unimportable tree must not brick an otherwise-good update — the
    import validator already owns that case, and on the git path it
    deliberately only warns (stale bytecode is indistinguishable)."""
    from hermes_cli import update_cmd

    assert update_cmd._run_capability_canary(
        _mirror_install(tmp_path / "complete"), label="test"
    ) is True

    assert update_cmd._run_capability_canary(
        _mirror_install(
            tmp_path / "degraded",
            patch={"hermes_cli/kanban_sentinel.py": _NO_OP_SENTINEL},
        ),
        label="test",
    ) is False
    assert "canary FAILED" in capsys.readouterr().out

    assert update_cmd._run_capability_canary(
        tmp_path / "does-not-exist", label="test"
    ) is False
    assert "could not probe" in capsys.readouterr().out


def _candidate_repo(root: Path, patch: dict | None = None) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _mirror_install(root, patch=patch)
    _git(root, "add", "-A", "-f")
    _git(root, "commit", "-qm", "candidate")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    return root


@pytest.mark.real_capability_preflight
def test_git_candidate_is_probed_without_moving_live_head(tmp_path):
    from hermes_cli import update_cmd

    repo = _candidate_repo(tmp_path / "repo")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert update_cmd._preflight_git_capability_candidate(["git"], repo, "main")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.real_capability_preflight
def test_git_candidate_cannot_self_certify_a_removed_capability(tmp_path):
    from hermes_cli import update_cmd

    repo = _candidate_repo(
        tmp_path / "repo",
        patch={
            "hermes_cli/kanban_sentinel.py": _NO_OP_SENTINEL,
            "hermes_cli/kanban_capabilities.py": _self_certifying_manifest(),
        },
    )
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert not update_cmd._preflight_git_capability_candidate(["git"], repo, "main")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.real_capability_preflight
def test_git_candidate_staging_failure_is_fail_closed_and_nonmutating(tmp_path):
    from hermes_cli import update_cmd

    repo = _candidate_repo(tmp_path / "repo")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert not update_cmd._preflight_git_capability_candidate(
        ["git"], repo, "missing-branch"
    )
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


# ---------------------------------------------------------------------------
# Maintained-branch strategy + synthetic upstream advance rehearsal
# ---------------------------------------------------------------------------


def _git(repo: Path, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def synthetic_fleet(tmp_path):
    """An upstream repo plus an install that carries a lifecycle commit."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "up@example.com")
    _git(upstream, "config", "user.name", "Upstream")
    (upstream / "core.txt").write_text("v1\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "upstream v1")

    install = tmp_path / "install"
    subprocess.run(
        ["git", "clone", "-q", str(upstream), str(install)], check=True,
        capture_output=True, text=True,
    )
    _git(install, "config", "user.email", "me@example.com")
    _git(install, "config", "user.name", "Me")
    _git(install, "checkout", "-q", "-b", "hermes-maintained")
    (install / "lifecycle.txt").write_text("control-loop\n", encoding="utf-8")
    _git(install, "add", "-A")
    _git(install, "commit", "-qm", "lifecycle capability")

    # Upstream advances (the "Hermes update" the install is about to take).
    (upstream / "core.txt").write_text("v2\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "upstream v2")
    return upstream, install


def _write_update_branch(branch):
    """Persist ``update.branch`` through the real config writer."""
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    cfg.setdefault("update", {})["branch"] = branch
    save_config(cfg)


def test_update_branch_defaults_to_main_without_configuration():
    from hermes_cli import main as cli_main

    args = type("A", (), {"branch": None})()
    assert cli_main._resolve_update_branch(args) == "main"


def test_configured_maintained_branch_is_what_update_pulls():
    from hermes_cli import main as cli_main

    _write_update_branch("hermes-maintained")
    args = type("A", (), {"branch": None})()
    assert cli_main._resolve_update_branch(args) == "hermes-maintained"


def test_explicit_branch_flag_still_overrides_the_configured_branch():
    from hermes_cli import main as cli_main

    _write_update_branch("hermes-maintained")
    args = type("A", (), {"branch": "hotfix"})()
    assert cli_main._resolve_update_branch(args) == "hotfix"


def test_blank_configured_branch_falls_back_to_main():
    from hermes_cli import main as cli_main

    _write_update_branch("   ")
    args = type("A", (), {"branch": None})()
    assert cli_main._resolve_update_branch(args) == "main"


def test_synthetic_upstream_advance_on_the_maintained_branch_keeps_capability(
    synthetic_fleet,
):
    """Rehearsal: merging the advanced upstream into the maintained branch
    takes the new upstream code AND keeps the lifecycle capability."""
    upstream, install = synthetic_fleet
    _git(install, "fetch", "-q", "origin")
    _git(install, "merge", "-q", "--no-edit", "origin/main")

    assert (install / "lifecycle.txt").exists()
    assert (install / "core.txt").read_text(encoding="utf-8") == "v2\n"


@pytest.mark.real_capability_preflight
def test_update_capability_preflight_probes_post_merge_maintained_tree(
    synthetic_fleet, monkeypatch
):
    """The in-place updater must certify HEAD + upstream, not bare upstream.

    Kevin's maintained lifecycle commit is intentionally local to the custom
    production branch.  Probing ``origin/main`` alone therefore reports the
    capability missing and makes ``updates.parked_branch_strategy =
    update_in_place`` impossible to use.  The candidate must be the exact
    post-merge tree the updater is about to activate.
    """
    from hermes_cli import update_cmd

    _upstream, install = synthetic_fleet
    _git(install, "fetch", "-q", "origin")
    monkeypatch.setattr(
        update_cmd, "_installed_capability_manifest_present", lambda _root: True
    )

    observed = {}

    def _probe(candidate, *, label):
        candidate = Path(candidate)
        observed["label"] = label
        observed["lifecycle"] = (candidate / "lifecycle.txt").exists()
        observed["core"] = (candidate / "core.txt").read_text(encoding="utf-8")
        return observed["lifecycle"] and observed["core"] == "v2\n"

    monkeypatch.setattr(update_cmd, "_run_capability_canary", _probe)

    assert update_cmd._preflight_git_capability_candidate(
        ["git"], install, "main", merge_in_place=True
    )
    assert observed == {
        "label": "HEAD merged with origin/main",
        "lifecycle": True,
        "core": "v2\n",
    }


def test_synthetic_upstream_advance_onto_main_drops_capability(synthetic_fleet):
    """The failure mode being guarded: taking the update on upstream ``main``
    silently removes the lifecycle capability and still looks successful."""
    upstream, install = synthetic_fleet
    _git(install, "fetch", "-q", "origin")
    _git(install, "checkout", "-q", "main")
    _git(install, "merge", "-q", "--ff-only", "origin/main")

    assert not (install / "lifecycle.txt").exists()
    assert (install / "core.txt").read_text(encoding="utf-8") == "v2\n"


def test_capability_manifest_round_trips_as_json():
    payload = json.dumps(kc.capability_manifest())
    restored = json.loads(payload)
    assert set(restored["required"]) == set(kc.REQUIRED_CAPABILITY_NAMES)
