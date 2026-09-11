# Blocked lifecycle validation

Base: `084eeaf67d47916253824433591b9476cee35701`.
Implementation confined to the isolated `t_b5ca1e7f` worktree.
No live board operations, external messages, services, deployment, product effects,
or upstream PR were performed by this implementation run.

## Vertical RED/GREEN record

Every behavior below was tested failing before its implementation was changed.
Python runs used `scripts/run_tests.sh`, `--file-retries 0`, clean environment,
per-file subprocesses, and disposable fixture databases. The supplied interpreter
has SQLite 3.53.1 but lacked pytest; a worktree-local `.venv` installed pytest
8.4.2, pytest-asyncio 0.26.0, and pytest-timeout 2.4.0, with the supplied runtime's
site-packages available for application dependencies. Initial dependency discovery
and sandbox DNS failures were setup failures, not RED evidence.

| Slice | RED | GREEN |
| --- | --- | --- |
| `test_kanban_block_kinds.py`: repeated blocks remain sticky and unclaimable | 4 failures: second block produced triage for needs_input, capability, transient, None; 2 existing tests passed | 6 passed; with `test_kanban_blocked_sticky.py`, 8 passed |
| `test_kanban_notifier.py -k block_loop`: escalation wording and delivery | 1 failure: notification said TRIAGE; origin wake test passed | Full notifier file: 12 passed |
| `test_kanban_block_loop_repair.py`: atomic domain repair and unsafe-target refusal | 18 failures: supported repair module absent | 18 passed, including audit rollback, unchanged task fields/runs/history, repeat no-op, old-reader sticky query, no promotion/claim |
| Same file, `-k operator_cli`: parser through real command dispatch on temp DB | 1 failure: repair-block-loop was not a parser choice | Full file: 19 passed |
| `test_kanban_transfer.py -k unresolvable`: unavailable imported workspace | 1 failure: task was triage | Full transfer file: 19 passed; parked task remains blocked after recomputation and cannot be claimed |
| Desktop `completion-notify.test.ts`: escalation title matches blocked handoff | 1 failure, 27 passed: title said routed to triage | 28 passed |
| `test_kanban_cli.py -k block_cli_reports`: repeated-block output | 1 failure: escalation omitted human-decision explanation | Full CLI file: 5 passed |

Dependency waiting and parent-completion auto-promotion were already correct on
the base and stayed green. Recurrence semantics remain same-kind, including
untyped blocks; the change does not introduce reason-text equality matching.

## Final regression command

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_block_kinds.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py \
  tests/hermes_cli/test_kanban_block_loop_repair.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_transfer.py \
  tests/hermes_cli/test_kanban_promote.py \
  tests/hermes_cli/test_kanban_review_lifecycle.py \
  tests/hermes_cli/test_kanban_review_lifecycle_complete.py \
  tests/hermes_cli/test_kanban_resume_authority.py \
  tests/hermes_cli/test_kanban_pr_acceptance.py \
  tests/hermes_cli/test_kanban_pr_continuation.py \
  tests/gateway/test_kanban_notifier.py \
  tests/tools/test_kanban_tools.py \
  --file-retries 0 -j 4
```

Result: **251 passed, 0 failed, 2 skipped**, 13 files, 33.2 seconds.
The two `test_kanban_pr_acceptance.py` tests are Linux-marked and skipped on macOS;
no host identity was faked. The 61 PR-continuation tests and 52 resume-authority
tests passed. Existing PR-authority implementation files were not changed.

Desktop result: **28 passed** using Vitest 4.1.10 and the existing upstream
installed packages, with an isolated config pointing at this worktree's
`apps/desktop`, `environment: 'node'`, and the single completion-notify test
file. The standard UI config could not load the installed package set because
`@rolldown/plugin-babel` was missing. These pure notification tests mock the host
and OS notification doors and require no renderer, service, or browser.
Cache and temporary config lived under `/tmp/t_b5ca1e7f-*`; no package manifests
or lockfiles were changed. `git diff --check` passed.

## Operational limits

- No automatic migration. Legacy triage cards remain eligible for intake
  automation until the operator uses `repair-block-loop`.
- The repair writes an ordinary `blocked` audit event so old sticky readers
  recognize the repaired state. Future standalone escalation events require
  updated readers; older writers can still produce triage escalations. Deploy
  lifecycle writers and dispatchers consistently. No deployment is part of this change.
- Repair refuses execution ownership, open runs, other lifecycle phases,
  and stale or inconsistent escalation evidence. It does not force recovery of
  those cases or reset recurrence/failure/PR evidence.
- Explicit operator unblocking remains supported. Automations should not
  blindly unblock human-needed work.

## Changed files

- `apps/desktop/src/plugins/kanban/completion-notify.test.ts`
- `apps/desktop/src/plugins/kanban/completion-notify.ts`
- `apps/desktop/src/plugins/kanban/i18n.ts`
- `docs/kanban-blocked-lifecycle-validation.md`
- `gateway/kanban_watchers_notifier.py`
- `hermes_cli/kanban.py`
- `hermes_cli/kanban_block_repair.py`
- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban_parser.py`
- `hermes_cli/kanban_transfer.py`
- `locales/af.yaml`
- `locales/ar.yaml`
- `locales/de.yaml`
- `locales/en.yaml`
- `locales/es.yaml`
- `locales/fr.yaml`
- `locales/ga.yaml`
- `locales/hu.yaml`
- `locales/it.yaml`
- `locales/ja.yaml`
- `locales/ko.yaml`
- `locales/pt.yaml`
- `locales/ru.yaml`
- `locales/tr.yaml`
- `locales/uk.yaml`
- `locales/zh-hant.yaml`
- `locales/zh.yaml`
- `tests/gateway/test_kanban_notifier.py`
- `tests/hermes_cli/test_kanban_block_kinds.py`
- `tests/hermes_cli/test_kanban_block_loop_repair.py`
- `tests/hermes_cli/test_kanban_cli.py`
- `tests/hermes_cli/test_kanban_transfer.py`
- `tools/kanban_tools_schemas.py`
- `website/docs/user-guide/features/kanban.md`
- `website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/user-guide/features/kanban.md`

## P2 remediation: validate evidence on exact retries

Remediation base: `b5a3276f35b6f36984f2a2f99c6a1b64d4aeaa55`.
The earlier 251-Python/28-desktop results above describe the initial implementation,
not a rerun on this remediation. This follow-up changes only the repair domain
module, its tests, the retry wording in the user guide, and this record.

Vertical TDD, using the same isolated wrapper and disposable fixture databases:

1. Added strict original-escalation payload tests, then ran
   `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_loop_repair.py -k initial_repair --file-retries 0`.
   **RED: 5 failed, 13 passed, 19 deselected.** Missing `kind` for an untyped task
   and object/list/integer/boolean reasons were incorrectly accepted. Added minimal
   explicit-key/type validation. **GREEN: full repair file, 37 passed.**
2. Added tampered task/audit/reference and exact-retry tests, then ran
   `scripts/run_tests.sh tests/hermes_cli/test_kanban_block_loop_repair.py -k 'retry or atomic_idempotent' --file-retries 0`.
   **RED: 25 failed, 6 passed, 36 deselected.** Added shared original-evidence
   validation before either success path; bound the audit reference to the actual
   preceding same-task escalation; compared all preserved payload facts and trimmed
   operator identity/reason; rejected intervening lifecycle events.
   **GREEN: full repair file, 67 passed.**

The original escalation must explicitly contain `kind` (valid non-dependency
block kind or null), `reason` (string or null), and integer `recurrences`/`limit`
excluding booleans, with `limit >= 1`, `recurrences >= limit`, and agreement with
the task's blocker/counter. Retry audits must match the original facts and the
exact normalized repair request. Whitespace around actor/reason is ignored;
changing either value is refused. Commentary does not invalidate an otherwise
exact retry. The existing ordinary `blocked` audit event is retained for old readers.
The new refusal/no-op cases verify that the database remains byte-for-byte unchanged.

Focused regression command on the remediation:

```bash
scripts/run_tests.sh \
  tests/hermes_cli/test_kanban_block_loop_repair.py \
  tests/hermes_cli/test_kanban_block_kinds.py \
  tests/hermes_cli/test_kanban_blocked_sticky.py \
  tests/hermes_cli/test_kanban_cli.py \
  tests/hermes_cli/test_kanban_resume_authority.py \
  tests/gateway/test_kanban_notifier.py \
  --file-retries 0 -j 4
```

**144 passed, 0 failed, 0 skipped**, six files, 10.8 seconds.
`.venv/bin/ruff check hermes_cli/kanban_block_repair.py tests/hermes_cli/test_kanban_block_loop_repair.py`
and `git diff --check` also passed. No desktop code changed and no desktop tests
were rerun for this follow-up. Existing unrelated lint warnings were not cleaned up.
No live state, services, deployment, secrets, messaging, or ARBS effects were used.
