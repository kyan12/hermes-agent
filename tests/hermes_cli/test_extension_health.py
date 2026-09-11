"""Local extension safety boundaries; all profiles and diagnostics are disposable."""
from pathlib import Path
from types import SimpleNamespace

import pytest


def _candidate_python(candidate):
    import os
    import subprocess
    import venv

    environment = candidate / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                           check=True, capture_output=True, text=True)
    site = Path(probe.stdout.strip())
    (site / "candidate.pth").write_text(str(candidate) + "\n")
    return str(python), site / "candidate.pth"


def test_native_health_accepts_real_maintained_implementation(tmp_path):
    import hashlib
    import json
    import sys
    from hermes_cli import extension_health

    root = Path(__file__).resolve().parents[2]
    environment = tmp_path / "installed"
    environment.mkdir()
    python, installed_path = _candidate_python(environment)
    # Install the real source in the disposable interpreter; reuse only the
    # runner's dependency directories, not its editable source installation.
    dependencies = dict.fromkeys(path for path in sys.path if Path(path).name in {"site-packages", "dist-packages"})
    installed_path.write_text("\n".join([str(root), *dependencies]) + "\n")
    symbols = [
        "hermes_cli.kanban_db:blocker_reconciler_enabled",
        "hermes_cli.kanban_blocker_reconcile:enqueue_blocker_reconciliation",
        "gateway.kanban_watchers:GatewayKanbanWatchersMixin._kanban_notifier_watcher",
    ]
    paths = {symbol.split(":")[0].replace(".", "/") + ".py" for symbol in symbols}
    paths.update({"hermes_cli/main.py", "hermes_cli/__init__.py"})
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"symbols": symbols, "files": {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in paths
    }}))
    extension_health.check_candidate(root, python, approval, startup=True)


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
    "startup-entry-removed", "startup-disabled", "startup-named", "startup-profile-default", "startup-host-override", "startup-disabled-worker-absent"])
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
    candidate_python, installed_path = _candidate_python(candidate)
    marker = tmp_path / "started"
    sources = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/main.py": (
            "import json, os, sys\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'home': os.environ.get('HERMES_HOME'), "
            "'fence': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT'), 'module': __file__, "
            "'python': sys.executable, 'cwd': os.getcwd(), 'argv': sys.argv, 'original_argv': sys.orig_argv, "
            "'pythonpath': os.environ.get('PYTHONPATH'), 'pythonhome': os.environ.get('PYTHONHOME')}))\n"
        ),
        "hermes_cli/update_cmd.py": "def _cmd_update_impl(*args): pass\n",
        "hermes_cli/extension_health.py": "def refuse_in_place_update(): pass\n",
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
    if damage in {"disabled", "startup-disabled", "startup-disabled-worker-absent"}:
        (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: false\n")
        if damage == "disabled":
            approval.unlink()
        else:
            data = json.loads(approval.read_text())
            data["files"] = {p: h for p, h in data["files"].items() if p in {
                "hermes_cli/__init__.py", "hermes_cli/main.py", "hermes_cli/update_cmd.py",
                "hermes_cli/extension_health.py"}}
            data["symbols"] = []
            approval.write_text(json.dumps(data))
            (candidate / "hermes_cli/kanban_db.py").unlink()
            if damage == "startup-disabled-worker-absent":
                (candidate / "hermes_cli/update_cmd.py").unlink()
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
    if launch:
        cli = tmp_path / "bound-bin" / "hermes"
        cli.parent.mkdir()
        made = subprocess.run([sys.executable, "-I", str(guard), "--candidate", str(candidate),
                               "--python", candidate_python, "--home", str(home),
                               "--approval", str(approval), "--cli-shim", str(cli), "--write-cli-shim"],
                              capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
        extra_homes += ["--cli-shim", str(cli)]
    result = subprocess.run([sys.executable, "-I", str(guard), "--candidate", str(candidate),
                             "--python", candidate_python, "--home", str(home),
                             "--approval", str(approval), *extra_homes, *launch], cwd=tmp_path, capture_output=True, text=True)
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
        assert identity["python"] == candidate_python
        module_index = identity["original_argv"].index("-m")
        assert identity["original_argv"][module_index + 1] == "hermes_cli.main"
        assert identity["fence"] == "2"
        assert identity["pythonpath"] is None and identity["pythonhome"] is None
        assert identity["cwd"] == str(tmp_path)
        if damage == "startup-named":
            assert identity["argv"][1:3] == ["--profile", "work"]
        else:
            assert "--profile" not in identity["argv"]
        if "gateway" in identity["argv"]:
            import shlex
            from gateway.status import looks_like_gateway_command_line, _command_line_belongs_to_profile

            assert "--external-supervisor" in identity["argv"]
            command_line = shlex.join(identity["original_argv"])
            assert looks_like_gateway_command_line(command_line)
            assert _command_line_belongs_to_profile(command_line, home)
        if damage == "startup-serve":
            assert "--isolated" in identity["argv"]


# Only synthetic source in tmp_path is executable here; no real updater is invoked.
@pytest.mark.live_system_guard_bypass
@pytest.mark.macos_only
@pytest.mark.parametrize("scenario", ["worker", "cli", "lost-update-guard", "lost-engine-chat", "lost-engine-kanban", "wrong-editable"])
def test_bound_cli_shim_preserves_worker_provenance_and_update_boundary(tmp_path, monkeypatch, scenario):
    import hashlib
    import json
    import os
    import shutil
    import subprocess
    import sys
    from hermes_cli import extension_health
    from hermes_cli.kanban_db_dispatch import _resolve_hermes_argv

    candidate = tmp_path / "candidate"
    candidate.mkdir()
    candidate_python, installed_path = _candidate_python(candidate)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  blocker_reconciler:\n    enabled: true\n")
    marker = tmp_path / "identity.json"
    sources = {
        "hermes_cli/__init__.py": "",
        "hermes_cli/main.py": (
            "import os, sys, json\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).write_text(json.dumps({{'module': __file__, 'python': sys.executable, "
            "'argv': sys.argv, 'home': os.environ['HERMES_HOME'], 'bin': os.environ['HERMES_BIN'], "
            "'cwd': os.getcwd(), 'path': os.environ['PATH'], 'fence': os.environ.get('HERMES_DELEGATED_CHILD_CONTEXT')}))\n"
        ),
        "hermes_cli/update_cmd.py": "def _cmd_update_impl(*args): pass\n",
        "hermes_cli/extension_health.py": "def refuse_in_place_update(): pass\n",
        "hermes_cli/kanban_db.py": "def blocker_reconciler_enabled(): return True\n",
        "hermes_cli/reconciler.py": "def reconcile(): pass\n",
        "gateway/__init__.py": "",
        "gateway/kanban_watchers.py": "class GatewayKanbanWatchersMixin:\n    def reconcile(self): pass\n",
    }
    for name, content in sources.items():
        path = candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"files": {p: hashlib.sha256(s.encode()).hexdigest()
                                               for p, s in sources.items()}, "symbols": [
        "hermes_cli.kanban_db:blocker_reconciler_enabled", "hermes_cli.reconciler:reconcile",
        "gateway.kanban_watchers:GatewayKanbanWatchersMixin.reconcile",
    ]}))
    guard = tmp_path / "guard.py"
    shutil.copyfile(extension_health.__file__, guard)
    cli = tmp_path / "bin" / "hermes"
    cli.parent.mkdir()
    base = [sys.executable, "-I", str(guard), "--candidate", str(candidate),
            "--python", candidate_python, "--home", str(home), "--approval", str(approval),
            "--cli-shim", str(cli)]
    made = subprocess.run([*base, "--write-cli-shim"], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    stale = tmp_path / "stale" / "hermes"
    stale.parent.mkdir()
    unsupported = tmp_path / "unsupported-started"
    stale.write_text(f"#!/bin/sh\ntouch '{unsupported}'\n")
    stale.chmod(0o755)
    env = dict(os.environ, HERMES_BIN=str(stale), PATH=str(stale.parent) + os.pathsep + os.defpath,
               HERMES_HOME=str(home), HERMES_DELEGATED_CHILD_CONTEXT="1")
    worktree = tmp_path / "task-worktree"
    worktree.mkdir()
    if scenario == "worker":
        launched = subprocess.run([*base, "--launch", "gateway"], env=env, cwd=worktree, capture_output=True, text=True)
        assert launched.returncode == 0, launched.stderr
        parent = json.loads(marker.read_text())
        monkeypatch.setenv("HERMES_BIN", parent["bin"])
        monkeypatch.setenv("PATH", parent["path"])
        argv = _resolve_hermes_argv()  # Real native resolver, without board operations.
        assert argv == [str(cli)]
        worker_home = home / "profiles" / "work"
        worker_home.mkdir(parents=True)
        env.update(HERMES_HOME=str(worker_home))
        command = [*argv, "-p", "work", "--cli", "chat", "-q", "synthetic"]
    else:
        # CLI maintenance stays reachable when reconciler support is absent.
        (candidate / "hermes_cli/kanban_db.py").unlink()
        command = [str(cli), "config", "set", "kanban.blocker_reconciler.enabled", "false"]
        if scenario == "lost-update-guard":
            (candidate / "hermes_cli/update_cmd.py").unlink()
            command = [str(cli), "update"]
        elif scenario in {"lost-engine-chat", "lost-engine-kanban"}:
            command = [str(cli), "chat" if scenario.endswith("chat") else "kanban", "list"]
        elif scenario == "wrong-editable":
            wrong = tmp_path / "wrong-install"
            shutil.copytree(candidate / "hermes_cli", wrong / "hermes_cli")
            installed_path.write_text(str(wrong) + "\n")
    result = subprocess.run(command, env=env, cwd=worktree, capture_output=True, text=True)
    assert result.returncode == (2 if scenario in {"lost-update-guard", "lost-engine-chat", "lost-engine-kanban", "wrong-editable"} else 0), result.stderr
    assert not unsupported.exists()
    if result.returncode == 0:
        identity = json.loads(marker.read_text())
        assert identity["module"] == str(candidate / "hermes_cli/main.py")
        assert identity["python"] == candidate_python
        assert identity["home"] == env["HERMES_HOME"]
        assert identity["argv"][1:] == command[1:]
        assert identity["fence"] == "1"
        assert identity["cwd"] == str(worktree)
    else:
        assert not marker.exists()
        assert json.loads((home / "logs/blocker-reconciler-health.json").read_text())["status"] == "refused"
