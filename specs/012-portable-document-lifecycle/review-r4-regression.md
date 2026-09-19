# Independent Regression Review — R4

Work: CR-SYNAPSE-APP-SPLIT-W003 only. Reviewer: /root/w003_regression_r4.
Repository: /Users/pinesky/workspace/amplai-foundry.
Review mode: bounded local review; no other reviewers' reports or conclusions consulted.

## Verdict and findings

PASS for this regression lens on the frozen local candidate. No reproducible new regression was found in the inspected changes and executed checks.

| Severity | Count |
| --- | ---: |
| P0 | 0 |
| P1 | 0 |
| Blocking-P2 | 0 |
| Advisory | 0 |

There are no findings requiring a file/line repair. This verdict does not close the original acceptance matrix, the other review lenses, W004, or any unavailable environment check.

## Frozen target and isolation

- Scope: `specs/012-portable-document-lifecycle/review-scope-r4.json`.
- Scope digest: `83d37d8d661b7360be05308af5077cf64d542d2e5a452e1b46d56fbf37118e39`.
- Scope-file SHA256: `a9a85f5c5ea745b07320b34e9d7cdad8e66db1ee6bb25674995a11772ad5a01e`.
- HEAD/main: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
- Archive: `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r4-source-snapshot.tar.gz`; SHA256 `ad056026489939800efb0df7da7db73f3bc26075d270d774178b2737079496bb`.

Before, interim, and after checks verified all 149 source files and 391 governing files by bytes, mode, and SHA256. Every check matched. Archive content independently matched all 540 declared files; its additional 540 entries were AppleDouble companions. Archive bytes were never overwritten. HEAD, branch, Git index, and the observed shared Python caches were unchanged.

All tests and mutations ran in `/tmp/w003-r4-regression.VrWVfK/repo`, copied with `rsync -a` excluding `.git`, `.venv`, live `.amplai`, `.ai-team/local`, interpreter/tool caches, and `node_modules`. No shared-checkout test writes, Git staging/commit/push, fleet installation, Store rebinding, Vault/Platform mutation, host activation, or actual application repository changes were performed.

The borrowed interpreter was `/Users/pinesky/workspace/amplai-foundry/.venv/bin/python`, Python 3.11.15. Explicit PYTHONPATH was:

```text
/tmp/w003-r4-regression.VrWVfK/repo/src:/tmp/w003-r4-regression.VrWVfK/repo/tests:/tmp/w003-r4-regression.VrWVfK/repo/scripts:/tmp/w003-r4-regression.VrWVfK
```

Tests used `-B`, `PYTHONDONTWRITEBYTECODE=1`, disabled automatic third-party pytest plugin discovery and the pytest cache provider, and used explicit disposable fixture directories. The review plugin asserted collected-test and fixture-module origins. Recorded origins for canonical `amplai_docs.py`, payload `amplai_docs.py`, `loopv2.py`, and `amplai_foundry` were all in the disposable copy. Canonical and payload engine SHA256 both matched `b6db24a3cea54277c7371c0d3a31c6dd936007860b333b97e2a01a6a999b97f6`. `/private/tmp` is macOS's resolved form of `/tmp`.

## Governing and implementation review

Read AGENTS.md, `.ai-team/README.md`, the local code-review skill, permission policy, test/contract rules, work contract, spec, plan, generated tasks, all task manifests' relevant acceptance/invariant/command fields, readiness, selected discovery fields, Context Pack, environment, execution authority, checkpoint, trace, and current verification/view/native evidence. Read the applicable D-046/D-053/D-055/D-056 decisions and the portable operations contract. Inspected the actual installer/controller/runtime diffs and the new document engine/test boundaries.

Regression focus covered extension-only compatibility and opt-in baseline ownership, reinstall/uninstall and fault recovery, immutable package inventories, existing hooks/settings, current/history selection and exact URI/dependency identity, physical line/code/fence exclusion, bounded allocation, atomic review/impact replacement, restrictive permissions and retry, terminal Work reads, heartbeat redaction, fixture imports, compiler flags, and test order.

The original references were read from the retained read-only `synapse-app-split` checkout. Their SHA256 values match the acceptance map: requirements `5063beb80c88266378c844f1c091d16fae7cea9438f496e6a8caf34cac4bd224`, acceptance `ee49350982eeaefa11e0369cef3fee152f4e1c647f5557be5e32045d8a1cdce1`. Verified 84 original requirements/168 cases overall, and the exact 23 requirements/46 cases assigned to W003, with no altered IDs, case kinds, environments, or requirement links. Original procedures were inspected; no case was newly marked PASS by this review.

Current 149 source records exactly equal the S08 retry evaluation snapshot. The two post-evaluation governing differences are `plan.md` and `analysis.json`. The latter's exact evaluation-time bytes are retained in `analysis-before-r4-completion.json` (SHA256 `87b24a6d37058d63262701bfcbcda1e60658c9d65f74634c4a0dfd461f425f42`). Its current changes reconcile completed evidence, context and plan/acceptance hashes; requirements, task DAG and execution thresholds remain unchanged. This is report metadata, not executable source drift. Current Context bytes equal `s08-context-r4-final.json`.

## Executed verification

Exact subprocess argv, cwd, exit code, duration, PYTHONPATH, log path and log digest are retained in the named `*-command.json` files alongside raw logs.

| Check | Actual result | Retained record |
| --- | --- | --- |
| Isolated `python -B -m pytest -q -p review_isolation -p no:cacheprovider --basetemp <disposable path> tests/ai` | Exit 0; 710 passed across 15 files; 282.325 seconds | `targeted-rerun-command.json`, `targeted-rerun.log`, `origins-targeted.json` |
| Shared read-only `python -B scripts/loopctl.py docs validate specs/012-portable-document-lifecycle` | Exit 0; RESOLVED; exact document snapshot | `docs-command.json`, `docs.log` |
| Shared read-only `python -B scripts/loopctl.py context validate specs/012-portable-document-lifecycle/context-pack.json` | Exit 0; MATCH; 20 sources/11 decisions | `context-command.json`, `context.log` |
| Shared read-only `python -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl` | Exit 0; valid, no errors | `trace-command.json`, `trace.log` |
| Shared read-only contract validation and readiness evaluation without `--write` | Both exit 0; CONTRACT PASS and READY | `contract-command.json`, `readiness-command.json` |
| Shared and restored-copy `python -B tools/amplai-loop-kit/seal.py --verify` | Both exit 0; exact inventory; restored copy has zero package bytecode files | `seal-command.json`, `copy-seal.log`, `final-audit.json` |
| Original mapping/current evaluation-source audit | Exit 0; exact 84/168, 23/46 and 149 source records | `evidence-check.json`, `check_evidence.py` |
| Frozen source/archive/index/cache checks | Before/interim/after exit 0; zero drift | `pin-before.json`, `pin-interim.json`, `pin-after.json` |

The semantic document snapshot is `371a912969c350319dd59fd471418492ffaf02e08c30636025b3e6c0ddd6a3c0`; Context content hash is `d2402f8bb8e63ffea19bdaad17e2235619cfc65aeb87c981bce8b001441086d0`.

The retained full S08 canonical evidence reports 2229 passed, four existing live Slack cases deselected, eleven V2 checks, strict seal, and twelve package selftests. Its exact log SHA256 is `65c47f74a5309d8cdb0c9d84b9a831903dba1e6bdaa54a395b9423c878d8ec69`. I inspected and source-bound this evidence; I did not rerun full S08, Ruff, mypy, Vault/schema verification, browser checks, or native hosts. Those results are not represented as reviewer executions.

## Representative mutations and restoration

1. In the disposable `tests/ai/test_document_reference_resources.py`, replaced the exact-byte `compile(..., dont_inherit=True)` loader with `spec.loader.exec_module`. Ran only `test_reference_fixture_import_never_writes_package_bytecode`. Exit 1, one failed: line 48 detected the newly written `__pycache__`. Raw evidence: `cache-loader-mutant.log` (SHA256 `4c605cbb0d8604dda5b33222a74c0a04a24fc7bc9bdb7ff4403d8787bd096417`). The guard explicitly enabled normal bytecode behavior inside its temporary fixture, so the test's failure was not masked by the outer `-B` setting.
2. Restored the loader; in disposable canonical `scripts/amplai_docs.py`, replaced the physical CR/LF split with `text.splitlines()`. Ran only `test_nonphysical_separator_cannot_hide_dependency_drift`. Exit 1: all eight canonical mutation cases failed the exact snapshot-change assertion at test line 158, while all eight unchanged payload controls passed. Raw evidence: `physical-line-mutant.log` (SHA256 `c29780a00c1002c1b3e7fd82e10a1bb966e7a2b5c77a8fd9f638de45dfd09baf`). This also discriminated the intended canonical source from the independent payload input.
3. Restored the canonical source. The cache, nonphysical-separator and required-context tests passed in both forward and reverse group order: 19 passed/exit 0 per run (`restored-order-a-command.json`, `restored-order-b-command.json`). Restored-copy audit verified all 540 frozen records, no differences, and strict seal PASS. No shared source was mutated.

Reviewer harness diagnostics are retained honestly: the first test attempt exited 3 during collection, before test execution, because my path assertion compared resolved `/private/tmp` to unresolved `/tmp`; canonicalizing that assertion fixed the harness. Its log remains `targeted.log`. Two early evidence-audit attempts exited 1 because my initial check assumed only `plan.md` could differ after evaluation. Inspection established and verified the separately retained report-only `analysis.json` refresh described above. None of these attempts is claimed as product PASS, a product failure, or a missing check.

## Limits

The four original acceptance limitations remain explicitly unverified: AC-039-P full native Work/resume lifecycle, AC-050-P actual tracked retirement beyond isolated fixtures, AC-038-P real fleet activation, and AC-054-P actual deployment BOM/target activation. Native R4 evidence is transport-only. Actual Python 3.6, RHEL7, signing/production deployment, live Slack and authenticated-browser behavior were not executed or certified. This review provides bounded local regression evidence, not exhaustive proof or deployment authority.

VERDICT: PASS
