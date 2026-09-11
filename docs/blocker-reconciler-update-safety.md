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
native default home, its inactive named profiles, or the supplied custom root/profile
fleet enables the reconciler. Native and custom roots are both checked even when
HERMES_HOME selects a custom root. It
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
Keep service ownership, worker fences and natural-drain requirements intact.
The startup command must be revised to the constrained form below; arbitrary
existing launchers are no longer accepted. The local extension README was inspected read-only; its
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
Startup additionally requires hashes for `hermes_cli/main.py` and
`hermes_cli/__init__.py`. The entry module is located in a fresh process without
importing its live-config startup code during preflight.
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
"$guard_python" -I "$ops_dir/guard.py" \
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

Install the external guard as the service's launch boundary, with **one** explicit
home per service. `--launch` accepts only `gateway`, `serve`, or `dashboard`.
It does not accept executables, interpreters, module names, profile overrides or
extra arguments. The same absolute `--python` path is used for both validation
and execution (its venv symlink spelling is preserved). The guard pins imports
and the working directory to the resolved `--candidate`, launches only
`hermes_cli.main`, and supplies an explicit profile selection derived from
`--home`. Sticky active profiles and inherited HERMES_HOME cannot redirect it.
Non-canonical named homes that would normalize to another home are refused.

Revised gateway command (not executed):

```sh
exec "$guard_python" -I "$ops_dir/guard.py" \
  --candidate "$candidate" --python "$candidate_python" \
  --home "$profile_home" --approval "$ops_dir/approval.json" \
  --launch gateway
```

For the dashboard service use this bound command, with the reviewed port/host:

```sh
exec "$guard_python" -I "$ops_dir/guard.py" \
  --candidate "$candidate" --python "$candidate_python" \
  --home "$profile_home" --approval "$ops_dir/approval.json" \
  --port 9119 --bind-host 127.0.0.1 --launch dashboard
```

Use `--launch serve` for the headless backend. Dashboard/serve include `--isolated`
to prevent a named-home launch from forwarding to the machine-level server;
dashboard also uses `--no-open`. This is an explicit per-home service launch,
not a replacement for the unified interactive launcher. Extra runtime options
are intentionally unsupported in this bounded integration. A timestamp wrapper
may remain **outside** this command for log formatting, but must not choose a
second interpreter/launcher; replace its old child argv with the exact bound
command. No timestamp wrapper or service definition was changed in this lane.

Always invoke the external guard with `-I`, as shown: otherwise inherited Python
settings could interfere before the guard itself imports. The probe also uses `-I`; startup uses `-E -s -B -m hermes_cli.main`
from the checked candidate directory, with a fresh bytecode path. This keeps an
actual module launch while ignoring inherited Python settings and user-site code. The launched environment removes
PYTHONPATH/PYTHONHOME, sets HERMES_HOME to the checked home and preserves other
service variables, including delegated-child fences. No unrelated autonomy or
configuration is disabled. If the reconciler is explicitly false or absent,
startup still validates entry-module origin and binding, but does not require
reconciler approval/support. Readiness is persisted only after all validations;
invalid legacy launchers and missing enabled runtime support write a durable
refusal and do not execute the application. `ready` is preflight status, not
proof that the application subsequently started or the watcher ran.

`--launch` must be last. Multiple homes are supported for deployment preflight
without `--launch`, but rejected for a single startup. Config mutation and update
commands cannot be launched through this interface. A successful start check
cannot make an in-place source update safe. An external installed guard still
refuses at next startup if a native source update removes enabled support; the
maintained updater's early refusal prevents that source swap in the first place.

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
and removal/bypass of the wrapper are outside its coverage. Legacy arbitrary
launchers now refuse; no separate launch candidate/interpreter/home is accepted. Immutability and trusted approval custody prevent changes between
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


### Review remediation (after `63a31c3eb2`)

Both P1s from `review-result.txt` were reproduced with synthetic candidates and
isolated real profile enumeration. No activation or approval manifest was made.
Use the known complete interpreter and four workers for every command:

```sh
export HERMES_PYTHON=/Users/kyan/.hermes/upstream/hermes-agent/.worktrees/t_b5ca1e7f/.venv/bin/python
scripts/run_tests.sh tests/hermes_cli/test_extension_health.py -j4 --file-retries 0 -q -k test_update_checks_all_profiles_before_mutation
scripts/run_tests.sh tests/hermes_cli/test_extension_health.py -j4 --file-retries 0 -q -k test_external_preflight_checks_fresh_candidate_and_persists_result
scripts/run_tests.sh tests/hermes_cli/test_extension_health.py tests/hermes_cli/test_update_receipt.py -j4 --file-retries 0 -q
```

Native/custom enumeration: RED 6 failed/6 passed; GREEN 12 passed. Startup binding:
RED 7 failed/7 passed; GREEN 14 passed. Disabled-startup contract: RED 1 failed;
then GREEN in the full run. Additional profile/host redirect checks: RED 2 failed/
1 passed; then GREEN in the full run. Synthetic startup records interpreter,
module path, explicit home, profile argv and the real delegated-child environment
marker; stale launchers never execute. Inherited Python shadow/home settings are
injected into an isolated guard invocation. All config/markers are disposable.

The direct module-launch contract also ran RED (1 failed) before replacing the
bootstrap with a bound `-m hermes_cli.main` invocation. Final remedial regression:
**53 passed**, with `-j4`; Ruff and `git diff --check` passed.
