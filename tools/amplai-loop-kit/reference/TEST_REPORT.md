# Verification Report

> **이 문서는 2.2.0 시험의 기록이다.** 본문의 `synapse` 와 브랜치 이름은 그 시험이 실제로
> 어느 저장소에서 무엇을 상대로 돌았는지를 남긴 사실이므로 고치지 않는다. 2.3.0 부터 kit
> 정본은 `amplai-foundry` 이고 특정 앱을 기준 저장소로 두지 않는다 (`PROVENANCE.md`).

## Build reference

- Kit version: `2.2.0`
- Runtime protocol: `amplai.async-cross-app.v1`
- Target repository: `synapse`, branch `fix/amplai-loop-kit-2.2.0`
- Python used for verification: `3.9.6` (macOS system Python)
- Target compatibility implemented: Python `3.6+`, standard library only

## Test results

Run from the target repository root after installing the kit:

```text
tests/ai/test_amplai_async_runtime.py      16 PASS
tests/ai/test_amplai_kit_regressions.py    27 PASS
tests/ai/test_amplai_kit_installer.py      10 PASS
                                           53 PASS

tools/amplai-loop-kit/selftest.py          10 checks PASS
```

### Carried over from 2.1.0

- content hash tamper detection
- AUTO evidence requirement
- CHALLENGE independent reviewer requirement
- HUMAN approval evidence requirement
- DRAFT/WAITING/READY behaviour
- cross-change reference rejection
- dependency cycle rejection
- atomic app concurrency
- lease expiry recovery
- OPEN Question completion block
- HUMAN_REQUIRED isolation and reactivation guard
- generated handoff projection
- Claude command/session resume construction
- SessionStart context and SessionEnd checkpoint
- Cortex → Synapse → Cortex async round trip

### New in 2.2.0

Each 2.2.0 test names the 2.1.0 defect it prevents from returning.

- project lock: releasing a lock lost to a stale-reclaim leaves the new owner's
  lock intact, and the reclaim itself is exclusive
- child ids resolve against the change directory that exists, including the
  `CR-A` / `CR-A-Wing` prefix collision; a `change_id` shaped like a child id is
  rejected
- `work cancel` (with and without `--cascade`), `change cancel`,
  `work retarget`, `work reset-attempts`
- retry backoff: deferred claim, exponential growth, cap, expiry
- host-local lease boundary: foreign-host Work is not recovered, local Work is
- event chain stays sequential and append cost no longer grows with history
- `project reseal` repairs a hand-edited object, works when `policy.json`
  itself is unreadable, and refuses foreign files and path traversal
- policy drift reporting, machine-enforced vs prompt-only prohibitions,
  unattended permission-bypass warning
- supervisor no longer reconciles the store several times a second, and reports
  Work held back by backoff
- installer: fresh install, idempotent reinstall, install without
  `AUTONOMY_POLICY.md`, hook/permission preservation, hand-written JSON
  formatting preservation, local-modification conflict, forced backup,
  downgrade refusal, uninstall, Project Store rollback on failed registration

## Existing Synapse Loop V2 regression

Measured on this machine, not carried over from a previous report.

```text
clean origin/main            2 failed, 63 passed   (tests/ai/test_loop_runtime_v2.py)
this branch, full suite      2 failed, 148 passed  (tests/)
```

The two failures are identical before and after this change and were reproduced
on a clean `origin/main` worktree:

1. `LoopV2ContextHardeningTests::test_claim_evidence_paths_exist` — existing
   `cortex:...` cross-repository evidence locators are checked as local Synapse
   paths.
2. `RepositoryGardeningTests::test_knowledge_garden_scan_passes` — existing
   knowledge data has duplicate active claim ids
   (`claim-rootds-inspect-is-metadata-only`,
   `claim-wafer-identity-is-per-row-value`) plus the same stale Cortex locators.

No new existing-suite failure was introduced.

## Cross-app installability

`--dry-run` against the three target repositories, which 2.1.0 could not
install into because `.ai-team/AUTONOMY_POLICY.md` was a required path:

```text
synapse           29 actions, no conflicts
cortex            29 actions, no conflicts
amplai-foundry    29 actions, no conflicts
```

Only `synapse` was actually installed. `cortex` and `amplai-foundry` were
checked read-only.

## Environment limitation

The `claude` executable exists on this machine but no model invocation was
performed: doing so would have spent tokens and edited a repository outside the
reviewed scope. The command adapter, `-p` prompt construction, JSON result
parsing, session id persistence, `--resume` reconstruction, hook input/output
and the full async Work round trip are covered with a deterministic
`runner_type: command` worker instead.

Consequently the worker permission model is **unverified end to end**. Before
enabling `auto_start`, run one supervised Work manually and confirm the worker
can do what it needs with the runner args you configured.
