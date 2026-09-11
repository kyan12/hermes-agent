"""Local extension safety boundaries; all profiles and diagnostics are disposable."""
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("config,refused", [
    ("kanban:\n  blocker_reconciler:\n    enabled: true\n", True),
    ("kanban: [broken", True),
    ("kanban:\n  blocker_reconciler:\n    enabled: 'false'\n", True),
    ("kanban:\n  blocker_reconciler:\n    enabled: false\n", False),
])
def test_update_checks_all_profiles_before_mutation(tmp_path, monkeypatch, config, refused):
    from hermes_cli import update_cmd, main

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    active = tmp_path / "active"
    active.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(active))
    sibling = active / "profiles" / "work"
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


@pytest.mark.parametrize("damage", ["none", "removed", "stub", "malformed", "disabled", "startup", "startup-refused"])
def test_external_preflight_checks_fresh_candidate_and_persists_result(tmp_path, monkeypatch, damage):
    import hashlib
    import json
    import shutil
    import subprocess
    import sys
    from hermes_cli import extension_health

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: true\n")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    sources = {
        "hermes_cli/__init__.py": "",
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
    if damage == "disabled":
        (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: false\n")
        approval.unlink()
    launch = []
    marker = tmp_path / "started"
    if damage.startswith("startup"):
        launch = ["--launch", sys.executable, "-c",
                  "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)]
    result = subprocess.run([sys.executable, str(guard), "--candidate", str(candidate),
                             "--python", sys.executable, "--home", str(home),
                             "--approval", str(approval), *launch], capture_output=True, text=True)
    assert result.returncode == (0 if damage in {"none", "disabled", "startup"} else 2)
    receipt = json.loads((home / "logs/blocker-reconciler-health.json").read_text())
    assert receipt["status"] == ("ready" if result.returncode == 0 else "refused")
    assert "private-test-value" not in result.stderr + json.dumps(receipt)
    assert not (home / "kanban.db").exists()

    assert marker.exists() == (damage == "startup")
