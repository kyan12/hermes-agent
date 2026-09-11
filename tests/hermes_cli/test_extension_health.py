"""Local extension safety boundaries; all profiles and diagnostics are disposable."""
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("location", ["custom-sibling", "native-root", "native-sibling"])
@pytest.mark.parametrize("config,refused", [
    ("kanban:\n  blocker_reconciler:\n    enabled: true\n", True),
    ("kanban: [broken", True),
    ("kanban:\n  blocker_reconciler:\n    enabled: 'false'\n", True),
    ("kanban:\n  blocker_reconciler:\n    enabled: false\n", False),
])
def test_update_checks_all_profiles_before_mutation(tmp_path, monkeypatch, config, refused, location):
    from hermes_cli import update_cmd, main

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    active = tmp_path / "active"
    active.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(active))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    from hermes_constants import _get_platform_default_hermes_home

    native = _get_platform_default_hermes_home()
    sibling = {
        "custom-sibling": active / "profiles" / "work",
        "native-root": native,
        "native-sibling": native / "profiles" / "inactive",
    }[location]
    sibling.mkdir(parents=True)
    (sibling / "config.yaml").write_text(config)
    monkeypatch.setattr(update_cmd, "_resolve_update_options", lambda *a: SimpleNamespace(
        gw_input_fn=None, assume_yes=True))
    monkeypatch.setattr(update_cmd, "_begin_update_receipt_and_plan", lambda *a: None)
    monkeypatch.setattr(update_cmd, "_record_update_step", lambda *a: None)

    def mutation(*args):
        raise RuntimeError("mutation reached")

    monkeypatch.setattr(main, "_run_pre_update_backup", mutation)
    if refused:
        with pytest.raises(SystemExit) as exc:
            update_cmd._cmd_update_impl(SimpleNamespace(), False)
        assert exc.value.code == 2
        receipt = sibling / "logs" / "blocker-reconciler-health.json"
        assert '"status": "refused"' in receipt.read_text()
        assert config not in receipt.read_text()
    else:
        with pytest.raises(RuntimeError, match="mutation reached"):
            update_cmd._cmd_update_impl(SimpleNamespace(), False)


@pytest.mark.parametrize("damage", ["none", "removed", "stub", "malformed", "disabled", "startup", "startup-refused", "startup-env", "startup-serve",
    "startup-wrong-candidate", "startup-wrong-python", "startup-wrong-home", "startup-multi-home",
    "startup-entry-removed", "startup-disabled", "startup-named", "startup-profile-default", "startup-host-override"])
def test_external_preflight_checks_fresh_candidate_and_persists_result(tmp_path, monkeypatch, damage):
    import hashlib
    import json
    import shutil
    import subprocess
    import sys
    from hermes_cli import extension_health

    home = tmp_path / "profile"
    if damage in {"startup-named", "startup-profile-default"}:
        home = tmp_path / "custom" / "profiles" / ("work" if damage == "startup-named" else "default")
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: true\n")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    marker = tmp_path / "started"
    sources = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/main.py": (
            "import json, os, sys\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'home': os.environ.get('HERMES_HOME'), "
            "'fence': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'), 'module': __file__, "
            "'python': sys.executable, 'argv': sys.argv, 'original_argv': sys.orig_argv, "
            "'pythonpath': os.environ.get('PYTHONPATH'), 'pythonhome': os.environ.get('PYTHONHOME')}))\n"
        ),
        "gateway/__init__.py": "",
        "hermes_cli/kanban_db.py": "def blocker_reconciler_enabled(): return True\n",
        "hermes_cli/reconciler.py": "def reconcile(): pass\n",
        "gateway/kanban_watchers.py": "class GatewayKanbanWatchersMixin:\n    def reconcile(self): pass\n",
    }
    for name, content in sources.items():
        path = candidate / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    approval = tmp_path / "approval.json"
    hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in sources.items()}
    approval.write_text(json.dumps({"files": hashes, "symbols": [
        "hermes_cli.kanban_db:blocker_reconciler_enabled",
        "hermes_cli.reconciler:reconcile",
        "gateway.kanban_watchers:GatewayKanbanWatchersMixin.reconcile",
    ]}))
    # The installed guard remains available even if the candidate drops its source copy.
    guard = tmp_path / "installed-guard.py"
    shutil.copyfile(extension_health.__file__, guard)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", SimpleNamespace(
        blocker_reconciler_enabled=lambda: True))
    if damage in {"removed", "startup-refused"}:
        (candidate / "hermes_cli/kanban_db.py").unlink()
    if damage == "stub":
        path = candidate / "hermes_cli/kanban_db.py"
        path.write_text("blocker_reconciler_enabled = True\n")
        hashes["hermes_cli/kanban_db.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
        data = json.loads(approval.read_text())
        data["files"] = hashes
        approval.write_text(json.dumps(data))
    if damage == "malformed":
        (home / "config.yaml").write_text("kanban: [private-test-value")
    if damage in {"disabled", "startup-disabled"}:
        (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: false\n")
        approval.unlink()
        if damage == "startup-disabled":
            (candidate / "hermes_cli/kanban_db.py").unlink()
    launch = []
    extra_homes = ["--bind-host=--profile=other"] if damage == "startup-host-override" else []
    if damage.startswith("startup"):
        launch = ["--launch", "serve" if damage == "startup-serve" else "gateway"]
    if damage in {"startup-wrong-candidate", "startup-wrong-python", "startup-wrong-home"}:
        # Legacy arbitrary launchers must refuse, even if they are executable.
        stale = tmp_path / "stale-launch.py"
        stale.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        launch = ["--launch", sys.executable, str(stale)]
        if damage == "startup-wrong-python":
            alias = tmp_path / "other-python"
            alias.symlink_to(sys.executable)
            launch[1] = str(alias)
        if damage == "startup-wrong-home":
            launch += ["--profile", "other"]
    if damage == "startup-entry-removed":
        (candidate / "hermes_cli/main.py").unlink()
    if damage == "startup-multi-home":
        other = tmp_path / "other-home"
        other.mkdir()
        extra_homes = ["--home", str(other)]
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "2")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "wrong-inherited-home"))
    if damage == "startup-env":
        shadow = tmp_path / "shadow"
        shutil.copytree(candidate, shadow)
        (shadow / "hermes_cli/main.py").write_text("raise RuntimeError('wrong source launched')\n")
        monkeypatch.setenv("PYTHONPATH", str(shadow))
        monkeypatch.setenv("PYTHONHOME", str(tmp_path / "invalid-python-home"))
    result = subprocess.run([sys.executable, "-I", str(guard), "--candidate", str(candidate),
                             "--python", sys.executable, "--home", str(home),
                             "--approval", str(approval), *extra_homes, *launch], capture_output=True, text=True)
    assert result.returncode == (0 if damage in {"none", "disabled", "startup", "startup-env", "startup-serve", "startup-disabled", "startup-named"} else 2)
    receipt = json.loads((home / "logs/blocker-reconciler-health.json").read_text())
    assert receipt["status"] == ("ready" if result.returncode == 0 else "refused")
    assert "private-test-value" not in result.stderr + json.dumps(receipt)
    assert not (home / "kanban.db").exists()

    started = damage in {"startup", "startup-env", "startup-serve", "startup-disabled", "startup-named"}
    assert marker.exists() == started
    if started:
        identity = json.loads(marker.read_text())
        assert identity["home"] == str(home.resolve())
        assert identity["module"] == str(candidate / "hermes_cli/main.py")
        assert identity["python"] == sys.executable
        module_index = identity["original_argv"].index("-m")
        assert identity["original_argv"][module_index + 1] == "hermes_cli.main"
        assert identity["fence"] == "2"
        assert identity["pythonpath"] is None and identity["pythonhome"] is None
        assert identity["argv"][1:3] == ["--profile", "work" if damage == "startup-named" else "default"]
        if damage == "startup-serve":
            assert "--isolated" in identity["argv"]
