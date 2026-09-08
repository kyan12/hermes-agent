"""Preserve installed Kanban lifecycle contracts across staged updates."""
from pathlib import Path
import shutil
import subprocess

def _run_capability_canary(root, *, label: str) -> bool:
    """Pre-activation Kanban lifecycle capability canary (see
    :mod:`hermes_cli.kanban_capabilities`).

    Probes the freshly-updated tree out-of-process for every lifecycle
    capability the install requires. This catches the failure the import
    check cannot: a tree that imports perfectly but no longer contains the
    board control loop, because the update pulled a lineage that never had
    it. Without this, such an update reports success and the board silently
    stops waking scheduled work.

    Returns True only when every required capability survived. Probe failure
    is absence of safety evidence and therefore fails activation closed.
    """
    try:
        from hermes_cli.kanban_capabilities import preactivation_canary

        report = preactivation_canary(root)
    except Exception as exc:
        print(f"  ✗ capability canary could not run ({label}): {exc}")
        return False
    if not report.probed:
        # An unprobeable candidate has not demonstrated that required
        # lifecycle invariants survive activation. Fail closed.
        print(f"  ✗ capability canary could not probe the tree ({label}).")
        return False
    try:
        from hermes_cli.update_receipt import record_step

        record_step(
            "capability_canary",
            report.ok,
            detail=(
                f"{len(report.present)} verified"
                if report.ok
                else f"missing: {', '.join(report.missing)}"
            ),
        )
    except Exception:
        pass
    if report.ok:
        print(
            f"  ✓ Kanban lifecycle capabilities verified "
            f"({len(report.present)} checked)"
        )
        return True
    print()
    print("  ✗ Pre-activation capability canary FAILED:")
    for name in report.missing:
        print(f"      missing: {name}")
    print()
    print("    The updated tree no longer provides Kanban lifecycle")
    print("    capability this install depends on. This is what a silent")
    print("    invariant drop looks like — most often the update pulled a")
    print("    branch that never carried it.")
    print("    Set the lineage you actually run:")
    print("      hermes config set update.branch <your-maintained-branch>")
    print("    then re-run `hermes update`.")
    return False


def _installed_capability_manifest_present(root) -> bool:
    """Return whether this install already opted into the lifecycle contract."""
    return (Path(root) / "hermes_cli" / "kanban_capabilities.py").is_file()


def _preflight_git_capability_candidate(
    git_cmd, root, branch: str, *, merge_in_place: bool = False
) -> bool:
    """Probe the exact candidate tree before mutating the live checkout.

    The verifier is imported from the still-running installation. Therefore a
    candidate cannot certify itself by deleting both a capability and its own
    manifest entry. Installs predating the manifest bootstrap on their first
    update; every later candidate is fail-closed.

    A maintained custom branch is different from a normal branch switch: its
    activation candidate is ``HEAD`` merged with ``origin/<branch>``. Probing
    the bare upstream ref would necessarily omit the local lifecycle commits
    that ``updates.parked_branch_strategy=update_in_place`` exists to preserve,
    making the supported strategy fail every update. Materialize that merge in
    the disposable worktree and certify the resulting files instead.
    """
    # Resolve facade bindings so the established updater test seam survives
    # upstream's decomposition into sibling modules.
    from hermes_cli.update_cmd import (
        _installed_capability_manifest_present, _run_capability_canary,
    )

    root = Path(root)
    if not _installed_capability_manifest_present(root):
        return True
    import tempfile

    candidate = Path(tempfile.mkdtemp(prefix="hermes-capability-candidate-"))
    added = False
    try:
        candidate_base = "HEAD" if merge_in_place else f"origin/{branch}"
        add = subprocess.run(
            list(git_cmd)
            + ["worktree", "add", "--detach", str(candidate), candidate_base],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if add.returncode != 0:
            print("  ✗ capability canary could not stage the fetched candidate.")
            if add.stderr.strip():
                print(f"    {add.stderr.strip().splitlines()[0]}")
            return False
        added = True
        label = f"origin/{branch}"
        if merge_in_place:
            merge = subprocess.run(
                list(git_cmd)
                + ["merge", "--no-commit", "--no-ff", f"origin/{branch}"],
                cwd=candidate,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if merge.returncode != 0:
                print(
                    "  ✗ capability canary could not materialize the "
                    f"maintained-branch merge with origin/{branch}."
                )
                if merge.stderr.strip():
                    print(f"    {merge.stderr.strip().splitlines()[0]}")
                return False
            label = f"HEAD merged with origin/{branch}"
        return _run_capability_canary(candidate, label=label)
    except Exception as exc:
        print(f"  ✗ capability canary could not stage the fetched candidate: {exc}")
        return False
    finally:
        if added:
            subprocess.run(
                list(git_cmd) + ["worktree", "remove", "--force", str(candidate)],
                cwd=root,
                capture_output=True,
                check=False,
            )
        shutil.rmtree(candidate, ignore_errors=True)
