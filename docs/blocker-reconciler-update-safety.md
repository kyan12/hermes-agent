# Blocker reconciler deployment safety handoff

Status: implementation only, **not installed or activated**. Base is maintained
`e701ac7914aecc61fefecfc4ade5eb446d33ec30`, descended from `084eeaf67d`.
This lane does not restore the reconciler itself or change its lifecycle rules.
No approval for a restored candidate is supplied: the combined restoration must
first pass its behavioral tests and review. Existing pinned runtime remains
`/Users/kyan/.hermes/deployments/kanban-repair-e701ac7914aecc61fefecfc4ade5eb446d33ec30`.

## Why an external boundary

The [official updating guide](https://hermes-agent.nousresearch.com/docs/getting-started/updating/)
describes source/dependency updates and gateway restart. The local maintained
implementation is authoritative for ordering: `hermes_cli/update_cmd.py`
`_cmd_update_impl` opens the plan/receipt, snapshots, applies source/dependencies,
then restarts; `hermes_cli/main.py::cmd_update` finalizes failure receipts.
There is no general local-extension carry-forward hook. Syntax validation and a
versioned directory do not prove preservation of custom runtime behavior.

This integration refuses mutating `hermes update` with exit 2 whenever any
canonical fleet profile or the active custom home enables the reconciler. It
also refuses malformed config and non-boolean enabled values. The guard runs
before backup, gateway pause, source replacement, or restart, independent of
force options. Read-only `--plan` and `--check` retain their existing behavior.
The updater command boundary still finalizes its normal receipt. Each affected
profile also gets `logs/blocker-reconciler-health.json`, atomically replaced,
with timestamp and a fixed reason. No raw YAML, import exception, subprocess
output, or credentials are recorded. A failed diagnostic write remains a refusal
and is printed to stderr.

The external copy of `hermes_cli/extension_health.py` survives source updates.
It checks every explicitly supplied home and, when enabled, requires reviewed
SHA-256 file hashes plus fresh imports from the candidate interpreter. Required
symbols cover the config helper, a watcher method, and an implementation callable.
The probe uses a disposable enabled-config home, no inherited credentials, a
fresh bytecode-cache location, and a 30-second timeout. It rejects imports of the
specified modules from a different checkout. It does not call the reconciler or
open a live board. Explicit false or absent settings pass without approval;
malformed settings never become false through coercion.

## Owner-only installation and candidate approval (not executed)

An independent authorized deployment owner must perform these steps after the
restoration lane is integrated. Do not run them from a delegated gateway child.
Keep the existing launchers, service environment, worker fences and natural-drain
requirements intact. The local extension README was inspected read-only; its
standalone extension directory exists, but no reconciler implementation or
`hermes-local-extension-ops` skill was recovered there.

Use an external guard interpreter with PyYAML already installed, independent of
the checkout being updated. Do not downgrade or rebuild the active dependency
environment. Replace the explicit placeholders below with reviewed absolute paths.

```sh
reviewed_source=/absolute/path/to/combined-reviewed-worktree
ops_dir=/absolute/path/outside/all-updated-checkouts/blocker-reconciler-safety
mkdir -p "$ops_dir"
install -m 0644 "$reviewed_source/hermes_cli/extension_health.py" "$ops_dir/guard.py"
```

Stage the combined maintained source and matching dependencies/assets in a new
immutable deployment. Preserve `e701ac7914` and its `084eeaf67d` ancestry, all
maintained authority/worker repairs, and the installed overlay `ab0875c`.
Reapply this updater integration on every maintained update. Never update a
live deployment in place. Do not generate approval merely because staging succeeded.

After functional review, create `reviewed-files.txt` with one candidate-relative
path per line: include `hermes_cli/kanban_db.py`, `gateway/kanban_watchers.py`, the
restored engine, their runtime dependency/wiring closure, and the updater guard.
Create `reviewed-symbols.txt` with one actual `module:attribute` per line:

- `hermes_cli.kanban_db:blocker_reconciler_enabled`
- `gateway.kanban_watchers:GatewayKanbanWatchersMixin.<actual_reconciler_watcher_method>`
- `hermes_cli.<actual_implementation_module>:<actual_reconcile_callable>`

Use actual names from the restoration, not the angle-bracket placeholders.
The file list and symbols are a trusted review attestation, not discovery or
proof that arbitrary callable stubs implement reconciliation. Retain the combined
commit, test receipts, dependency lock identity and reviewer decision alongside it.
Generate hashes **only after that review**, with the candidate frozen:

```sh
guard_python=/absolute/path/to/independent/python
candidate=/absolute/path/to/immutable-reviewed-deployment
"$guard_python" - "$candidate" "$ops_dir" <<'PY'
import hashlib, json, pathlib, sys
root, ops = map(pathlib.Path, sys.argv[1:])
files = (ops / 'reviewed-files.txt').read_text().splitlines()
symbols = (ops / 'reviewed-symbols.txt').read_text().splitlines()
approval = {'files': {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                      for p in files}, 'symbols': symbols}
(ops / 'approval.json').write_text(json.dumps(approval, indent=2) + '\n')
PY
```

Before changing **any** runtime pointer/service definition, preflight the frozen
candidate using its own interpreter and every affected home (including profiles
on separate custom roots). Archive the approval and diagnostics with the decision:

```sh
candidate_python=/absolute/path/to/candidate/venv/bin/python
profile_home=/absolute/path/to/default-home
second_home=/absolute/path/to/another-profile-home
"$guard_python" "$ops_dir/guard.py" \
  --candidate "$candidate" --python "$candidate_python" \
  --home "$profile_home" --home "$second_home" \
  --approval "$ops_dir/approval.json"
```

Exit 2 means **leave pointers and services untouched**. Exit 0 means this preflight
passed, not permission to activate or evidence of a healthy running dispatcher.
The independent owner still must review the exact launch argv, drain naturally,
preserve service backups, activate once, and verify actual runtime identity and
reconciler behavior under the parent acceptance plan.

## Startup integration

Wrap each gateway/dashboard's existing launch command with the external guard.
The wrapper must remain outside the source tree and point at the same immutable
candidate and profile(s) as its launch command. For example, its final invocation
has this exact structure (fill in the existing command and arguments verbatim):

```sh
exec "$guard_python" "$ops_dir/guard.py" \
  --candidate "$candidate" --python "$candidate_python" \
  --home "$profile_home" --approval "$ops_dir/approval.json" \
  --launch /absolute/path/to/existing-launch-command existing-arg-1 existing-arg-2
```

`--launch` must be last. It replaces the guard process only after all homes pass,
preserving the service environment and delegated-child fences. The guard does not
alter HERMES_HOME or choose a profile for the launched process. Keep the launcher's
existing profile environment. On refusal, stderr and the persistent diagnostic
explain the failure, and no launch occurs. Do not use `--launch` for an update,
config mutation, or service-control command. A successful start check cannot
make an in-place source update safe.

## Rollback and limits

Before activation, rollback is simply leaving the existing pinned runtime and
service definitions untouched. After any authorized activation, restore the
freshly captured service/launcher definitions and previous immutable runtime
pointer through the same independent owner and drain procedure. Restore that
runtime's matching reviewed approval too; an approval from the new candidate must
not be reused. If the prior runtime lacks the enabled reconciler, the guard will
refuse it: do not bypass the guard to make rollback appear healthy. Obtain a
reviewed supported rollback build or a separately authorized explicit config
disable through the restored supported config CLI. This implementation never
silently disables anything. Do not restore the live DB to roll back code.

This boundary is not active until installed in every relevant launch/deployment
path. Direct git pulls, alternative updaters, omitted custom profile roots,
misbound launch commands and removal/bypass of the wrapper are outside its
coverage. Immutability and trusted approval custody prevent changes between
preflight and launch; this is not a hostile-code sandbox or a filesystem lock.
Imports and hashes establish reviewed support presence, not successful background
scheduling. The combined restored engine, real watcher wiring, all-host assets,
and live activation remain unvalidated in this lane. No live config, DB, service,
secrets, board lifecycle, dependency pins or other lane's files were changed.

## RED/GREEN evidence

All runs used disposable runner homes and the existing test interpreter, without
installing dependencies:

```sh
HERMES_PYTHON=/Users/kyan/.hermes/upstream/hermes-agent/.worktrees/t_b5ca1e7f/.venv/bin/python \
  scripts/run_tests.sh tests/hermes_cli/test_extension_health.py --file-retries 0 -q
```

Updater slice: RED 3 failed/1 passed (mutation reached), GREEN 4 passed. The profile
fixture was corrected to use the canonical custom-root profile location.
External candidate slice: RED 5 failed/4 passed (no external entry point/receipt),
GREEN 9 passed. Startup slice: RED 2 failed/9 passed (launch option unavailable),
GREEN 11 passed. The two parameterized contracts exercise real config parsing,
profile enumeration, candidate imports and subprocesses; updater side effects
stop at a sentinel before the first backup. They do not claim a live-engine E2E.
Final regression command adds `tests/hermes_cli/test_update_receipt.py`: 34 passed.
