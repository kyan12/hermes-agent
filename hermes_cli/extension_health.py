"""Fail-closed deployment boundary for the locally maintained blocker reconciler.

This file can be copied outside the checkout and run with a Python containing PyYAML.
It reads only raw config.yaml, never application config loaders, credentials, or board databases.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml


class HealthRefusal(Exception):
    """A fixed, non-secret diagnostic safe to persist."""


def reconciler_requested(home: Path) -> bool:
    try:
        path = home / "config.yaml"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        config = yaml.safe_load(raw)
        if config is None:
            config = {}
        for key in ("kanban", "blocker_reconciler"):
            if not isinstance(config, dict):
                raise HealthRefusal("invalid-config")
            config = config.get(key, {})
        if not isinstance(config, dict):
            raise HealthRefusal("invalid-config")
        enabled = config.get("enabled", False)
        if type(enabled) is not bool:
            raise HealthRefusal("invalid-config")
        return enabled
    except (OSError, UnicodeError, yaml.YAMLError):
        raise HealthRefusal("unreadable-config") from None


def diagnostic(home: Path, status: str, reason: str) -> None:
    """Atomic durable receipt; never include config values or exception text."""
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".reconciler-health-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"status": status, "reason": reason,
                       "observed_at": datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "blocker-reconciler-health.json")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def refuse_in_place_update() -> None:
    from hermes_constants import get_hermes_home, _get_platform_default_hermes_home
    from hermes_cli.profiles import _PROFILE_ID_RE
    from hermes_cli.update_receipt import _profile_homes

    homes = {Path(get_hermes_home()), *(Path(home) for _, home in _profile_homes())}
    # A custom HERMES_HOME changes _profile_homes()'s root, but the native
    # fleet can still share this checkout, including currently inactive profiles.
    native = _get_platform_default_hermes_home()
    homes.add(native)
    profiles = native / "profiles"
    if profiles.is_dir():
        homes.update(entry for entry in profiles.iterdir()
                     if entry.is_dir() and entry.name != "default" and _PROFILE_ID_RE.match(entry.name))
    refused = False
    for home in sorted(homes):
        try:
            if not reconciler_requested(home):
                continue
            raise HealthRefusal("enabled-extension-requires-staged-deployment")
        except HealthRefusal as exc:
            refused = True
            print(f"Blocker reconciler: {exc}. Update refused; use the external deployment preflight.",
                  file=sys.stderr)
            try:
                diagnostic(home, "refused", str(exc))
            except OSError:
                print("Blocker reconciler: diagnostic-write-failed; update remains refused.", file=sys.stderr)
    if refused:
        raise SystemExit(2)


# Run from the candidate interpreter, with an empty home and no inherited credentials.
# The external approval binds the wiring and implementation reviewed by the operator.
_PROBE = r'''
import importlib, importlib.util, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for symbol in sys.argv[2:]:
    module_name, attribute = symbol.split(":")
    if not attribute:
        # Locate startup without importing it: main imports live configuration.
        origin = pathlib.Path(importlib.util.find_spec(module_name).origin).resolve()
    else:
        module = importlib.import_module(module_name)
        origin = pathlib.Path(module.__file__).resolve()
    if origin != root.joinpath(*module_name.split(".")).with_suffix(".py"):
        raise RuntimeError("foreign module")
    if not attribute:
        continue
    value = module
    for part in attribute.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise RuntimeError("missing callable")
'''


def check_candidate(candidate: Path, python: str, approval: Path, *, startup: bool = False, enabled: bool = True) -> None:
    import hashlib
    import subprocess

    try:
        root = candidate.resolve(strict=True)
        symbols = []
        if enabled:
            data = json.loads(approval.read_text(encoding="utf-8"))
            files, symbols = data["files"], data["symbols"]
            if startup and not {"hermes_cli/main.py", "hermes_cli/__init__.py"} <= files.keys():
                raise ValueError()
            if not isinstance(files, dict) or not isinstance(symbols, list):
                raise ValueError()
            if "hermes_cli.kanban_db:blocker_reconciler_enabled" not in symbols:
                raise ValueError()
            if not any(s.startswith("gateway.kanban_watchers:GatewayKanbanWatchersMixin.") for s in symbols):
                raise ValueError()
            if not any(s.startswith("hermes_cli.") and not s.startswith("hermes_cli.kanban_db:")
                       for s in symbols):
                raise ValueError()
            for symbol in symbols:
                module, attribute = symbol.split(":")
                if not all(part.isidentifier() for part in (module + "." + attribute).split(".")):
                    raise ValueError()
                if module.replace(".", "/") + ".py" not in files:
                    raise ValueError()
            for relative, expected in files.items():
                path = (root / relative).resolve(strict=True)
                if Path(relative).is_absolute() or not path.is_relative_to(root):
                    raise ValueError()
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HealthRefusal("missing-or-mismatched-reviewed-approval") from None

    try:
        with tempfile.TemporaryDirectory(prefix="hermes-extension-probe-") as temporary:
            disposable = Path(temporary)
            (disposable / "config.yaml").write_text(
                "kanban:\n  blocker_reconciler:\n    enabled: true\n", encoding="utf-8")
            result = subprocess.run(
                [python, "-I", "-B", "-X", f"pycache_prefix={disposable / 'bytecode'}",
                 "-c", _PROBE, str(root), *symbols,
                 *(["hermes_cli.main:"] if startup else [])],
                cwd=disposable,
                env={"HOME": temporary, "USERPROFILE": temporary,
                     "HERMES_HOME": temporary, "PATH": os.defpath,
                     **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {})},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        if result.returncode:
            raise HealthRefusal("candidate-runtime-support-unavailable")
    except (OSError, subprocess.TimeoutExpired):
        raise HealthRefusal("candidate-runtime-probe-failed") from None


def bound_startup(args) -> tuple[list[str], dict[str, str]]:
    """Reject legacy launchers and profile overrides before recording readiness."""
    if len(args.home) != 1 or len(args.launch) != 1:
        raise HealthRefusal("startup-requires-one-home-and-fixed-target")
    commands = {
        "gateway": ["gateway", "run"],
        "serve": ["serve", "--isolated", "--host", args.bind_host, "--port", str(args.port)],
        "dashboard": ["dashboard", "--isolated", "--no-open", "--host", args.bind_host,
                      "--port", str(args.port)],
    }
    command = commands.get(args.launch[0])
    if command is None or not 0 <= args.port <= 65535 or args.bind_host.startswith("-"):
        raise HealthRefusal("unsupported-startup-target")
    if not Path(args.python).is_absolute() or not Path(args.python).is_file():
        raise HealthRefusal("startup-requires-absolute-interpreter")
    home = args.home[0].resolve()
    if home.parent.name == "profiles" and (
        home.name == "default" or home.name != home.name.strip().lower()
    ):
        raise HealthRefusal("startup-profile-would-be-redirected")
    # Preserve the venv path spelling: resolving a python symlink loses the venv.
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["HERMES_HOME"] = str(home)
    with tempfile.TemporaryDirectory(prefix="hermes-start-bytecode-") as cache:
        # -B prevents writes; the unique (now removed) prefix prevents stale reads.
        profile = home.name if home.parent.name == "profiles" else "default"
        argv = [args.python, "-E", "-s", "-B", "-X", f"pycache_prefix={cache}",
                "-m", "hermes_cli.main", "--profile", profile, *command]
    return argv, environment


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--python", required=True, help="Absolute candidate interpreter path")
    parser.add_argument("--home", type=Path, action="append", required=True,
                        help="Every affected profile home; repeat for fleet deployments")
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9119)
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--launch", nargs=argparse.REMAINDER,
                        help="Fixed startup target: gateway, serve, or dashboard (must be last)")
    args = parser.parse_args()
    failed = False
    startup = None
    reasons = {}
    for home in args.home:
        try:
            if not home.is_dir():
                raise HealthRefusal("profile-home-missing")
            if args.launch is not None:
                startup = bound_startup(args)
            requested = reconciler_requested(home)
            if requested or startup:
                check_candidate(args.candidate, args.python, args.approval,
                                startup=bool(startup), enabled=requested)
                reason = "reviewed-runtime-support-present" if requested else "extension-not-enabled"
            else:
                reason = "extension-not-enabled"
            reasons[home] = reason
        except HealthRefusal as exc:
            failed = True
            print(f"Blocker reconciler: {exc}; deployment/startup refused.", file=sys.stderr)
            try:
                diagnostic(home, "refused", str(exc))
            except OSError:
                print("Blocker reconciler: diagnostic-write-failed.", file=sys.stderr)
        except OSError:
            failed = True
            print("Blocker reconciler: diagnostic-write-failed; deployment/startup refused.", file=sys.stderr)
    if failed:
        return 2
    try:
        for home, reason in reasons.items():
            diagnostic(home, "ready", reason)
        if startup:
            # -m resolves the fixed module from this checked tree. -E/-s ignore
            # inherited Python settings and user site without excluding this cwd.
            os.chdir(args.candidate.resolve())
            os.execve(args.python, *startup)
    except OSError:
        for home in args.home:
            print("Blocker reconciler: startup-or-diagnostic-failed; refused.", file=sys.stderr)
            try:
                diagnostic(home, "refused", "startup-or-diagnostic-failed")
            except OSError:
                print("Blocker reconciler: diagnostic-write-failed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
