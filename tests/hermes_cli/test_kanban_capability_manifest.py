"""Updates must not be able to silently drop Kanban lifecycle capability.

Root cause this pins: ``hermes update`` resolves its branch to a hardcoded
``main`` unless ``--branch`` is passed, so an install whose lifecycle
capability lives on a maintained branch is fast-forwarded onto an upstream
tree that never had it — and the update reports success. Two guards:

1. A supported, config-backed maintained-branch strategy, so the update
   pulls the lineage the install actually runs.
2. A pre-activation capability canary that probes the STAGED tree (not the
   running process) against the running install's required manifest and
   fails loudly when a required lifecycle capability disappeared.
"""

from __future__ import annotations

import json
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


def _staged_tree(root: Path, capabilities: dict) -> Path:
    pkg = root / "hermes_cli"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "kanban_capabilities.py").write_text(
        textwrap.dedent(
            f"""
            def probe_capabilities():
                return {capabilities!r}
            """
        ),
        encoding="utf-8",
    )
    return root


def test_canary_passes_when_the_staged_tree_keeps_every_capability(tmp_path):
    staged = _staged_tree(tmp_path / "staged", dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True))
    result = kc.preactivation_canary(staged)
    assert result.ok, result.detail
    assert result.missing == []


def test_canary_fails_when_the_staged_tree_dropped_a_capability(tmp_path):
    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    dropped = sorted(caps)[0]
    caps.pop(dropped)
    staged = _staged_tree(tmp_path / "staged", caps)
    result = kc.preactivation_canary(staged)
    assert result.ok is False
    assert dropped in result.missing
    assert dropped in result.detail


def test_canary_fails_closed_when_the_staged_tree_cannot_be_probed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = kc.preactivation_canary(empty)
    assert result.ok is False
    assert result.missing  # fail-closed: everything is reported missing
    # …but flagged as "could not tell", NOT as "capability was dropped".
    assert result.probed is False


def test_a_real_probe_is_distinguishable_from_an_unprobeable_tree(tmp_path):
    """The update driver responds differently to the two, so the report must
    keep them apart: a probe that ran and found a capability gone is proof of
    a dropped invariant; a probe that could not run is absence of evidence."""
    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    caps.pop(sorted(caps)[0])
    dropped = kc.preactivation_canary(_staged_tree(tmp_path / "dropped", caps))
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

    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    assert update_cmd._run_capability_canary(
        _staged_tree(tmp_path / "complete", caps), label="test"
    ) is True

    caps.pop(sorted(caps)[0])
    assert update_cmd._run_capability_canary(
        _staged_tree(tmp_path / "degraded", caps), label="test"
    ) is False
    assert "canary FAILED" in capsys.readouterr().out

    assert update_cmd._run_capability_canary(
        tmp_path / "does-not-exist", label="test"
    ) is False
    assert "could not probe" in capsys.readouterr().out


def _candidate_repo(root: Path, capabilities: dict) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _staged_tree(root, capabilities)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "candidate")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    return root


@pytest.mark.real_capability_preflight
def test_git_candidate_is_probed_without_moving_live_head(tmp_path):
    from hermes_cli import update_cmd

    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    repo = _candidate_repo(tmp_path / "repo", caps)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert update_cmd._preflight_git_capability_candidate(["git"], repo, "main")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.real_capability_preflight
def test_git_candidate_cannot_self_certify_a_removed_capability(tmp_path):
    from hermes_cli import update_cmd

    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    dropped = sorted(caps)[0]
    caps.pop(dropped)
    repo = _candidate_repo(tmp_path / "repo", caps)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert not update_cmd._preflight_git_capability_candidate(["git"], repo, "main")
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before


@pytest.mark.real_capability_preflight
def test_git_candidate_staging_failure_is_fail_closed_and_nonmutating(tmp_path):
    from hermes_cli import update_cmd

    caps = dict.fromkeys(kc.REQUIRED_CAPABILITY_NAMES, True)
    repo = _candidate_repo(tmp_path / "repo", caps)
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
