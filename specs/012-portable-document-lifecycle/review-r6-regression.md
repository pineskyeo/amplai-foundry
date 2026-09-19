# W003 R6 Independent Regression Review

Counts: P0=0; P1=0; Blocking-P2=0; Advisory=0. No demonstrated regression finding remains in this frozen W003 R6 scope. Full isolated pytest passed 2494 tests with four existing live-Slack deselections; five targeted mutation mechanisms were killed through intended assertions. This is one independent lens, not whole-goal, deployment, or final acceptance approval.

## Review Identity And Authority

Lens: REGRESSION. This reviewer received no R6 contract/recovery conclusions and did not delegate. Findings only; no source fix or shared-checkout write. Local synthetic test repositories, process-memory mutations, local offline browser fixtures exercised by the existing suite, and report files are the execution boundary. No live Store/Vault, native Claude/Codex operation, host configuration, remote publication, or deployment was performed.

Owning root: `/Users/pinesky/workspace/amplai-foundry`.
Disposable base-plus-snapshot root: `/tmp/w003-regression-r6.oT9y3N/copy`.
All report filenames below are relative to `/tmp/w003-regression-r6.oT9y3N/`.

- Base and target HEAD: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
- Scope: `61462cceca36c9f4554835729e0ea4a180db6f40e83e8c937358eb16c26a297d`.
- Envelope SHA256: `31d5050daedbaed4f382f694341ec0d139a39bc575cf11e705e7305734b28a94`.
- Archive: `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r6-source-snapshot.tar.gz`.
- Archive SHA256: `57c486bd01777fd188d19d46409094dede8b3e63599d543f7af3a08d6c918a6e`; mode `0600`.
- Membership: 155 source plus 621 governing/evidence files; archive contains 777 unique regular members including the envelope. Each listed path's SHA256, size and permission mode was compared to both the actual checkout and archive. The envelope digest was recomputed with Node's JSON.stringify after deleting scope_sha256, preserving property order. See `review.py`, `before.json`, and final audit.

## Inputs Inspected

Read repository AGENTS/README, required verifier role/verification skill/G9, 3lens-review and owning code-review skill; work-contract, spec, plan and its repair refinements, all task acceptance/command contracts, original 23-requirement/46-case acceptance map, readiness, discovery, actual Context records, environment, trace and R6 handoff. Relevant permissions/test-discipline/contract rules and active claim/Decision references were inspected. Source review covered the actual dirty diff and current implementation, including source/payload reference candidate resolution, classification/limits and selection-before-ranking; actual Context generation/validation and the delivery verifier; installer borrowing/receipt/recovery behavior; terminal Work presentation and heartbeat redaction. Associated source/payload/installed CLI, resource, document lifecycle/review/integration/HTML and installer regression assertions were read.

Current inspected-only execution evidence: S19/S20 RED, green and canonical evidence, `s08-predecessor-revalidation-r6.json` and its 14 retained canonical runs, complete `s08-canonical-eval-r6.log`/`s08-local-verification-r6.json`, r13 attributed source review, current Context, native transport-only and view evidence. Historical R5/R4 material remains history, not R6 approval. The original acceptance map still has zero final passed cases; the R6 pre-review handoff and T008 retain pending closure. Counts from overlapping canonical predecessor checks are not summed.

## Isolation And Setup Evidence

The copy was created from `git archive` of the exact base and the exact validated review archive. Initial disposable Git initialization lacked the real provenance commit, so its Context/guide check results were INVALID; those failed outputs are retained. Local read-only fetch of the base object followed by disposable `update-ref HEAD` and `read-tree` restored the original base/index relationship. `core.precomposeunicode=true` was set before valid actual Context reproduction. Shared Git was not changed. Exact commands:

```text
python3 /tmp/w003-regression-r6.oT9y3N/review.py setup
/Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B /tmp/w003-regression-r6.oT9y3N/review.py setup
git -C <copy> fetch --no-tags /Users/pinesky/workspace/amplai-foundry 6e005a5e3035ef05510c403a7cd691d524b733bd
git -C <copy> update-ref HEAD 6e005a5e3035ef05510c403a7cd691d524b733bd
git -C <copy> read-tree 6e005a5e3035ef05510c403a7cd691d524b733bd
git -C <copy> config core.precomposeunicode true
```

The first system Python 3.9 setup failed with `TypeError: extractall() got an unexpected keyword argument 'filter'` after successful integrity checks and before extraction. Setup was repeated with available Python 3.11.15. This was a reviewer setup issue, not product evidence.

`run.py` supplies explicit isolated `src`, `scripts`, `tests` paths, removes inherited PYTHON/AMPLAI/GIT_* variables, disables bytecode and pytest plugin autoload, and uses `-p no:cacheprovider`. The shared virtualenv supplies interpreter/dependencies only. `imports.log` asserts actual amplai_docs, loopv2, amplai_foundry, test helper and fixture engine files resolve inside the disposable root (including /private/tmp normalization). No shared editable source resolution was accepted. Mutation modules live outside the copy. No ignored `.amplai` runtime content was copied.

Every executed check has an exact argv, cwd, exit code, duration and unabridged output log indexed in `commands.jsonl`. Helpers `run.py`, `checks2.py`, `final_checks.py`, `diagnostics.py`, `mutations.py`, `context_mutation.py`, `mutation_plugin.py`, `review.py` and `evidence_audit.py` retain the procedure.

## Executed Checks

Independently executed results:

| Check | Result | Evidence |
|---|---|---|
| Full existing pytest suite | 2494 passed / 4 existing live-Slack deselected, exit 0, 608.31 seconds | full-pytest.log |
| Focused S19/S20, typed visibility, governance | 244 passed, exit 0 | focused-baseline.log |
| Mutation-free recheck of all affected cases | 23 passed / 199 deselected, exit 0 | post-mutation-green.log |
| Actual current Context | MATCH; all 14 records; exact hash a66d9e62b971543b077052dbeef92a9f0533eab7c72808fa3d42689e3ab1ab10 | context-with-base.log |
| Actual generated/stored guide delivery | Exactly 3 owning guides + 7 instructions, 10 unique paths, 14 generated and stored records, exact source hashes; retained product reference denied | guide-with-base.log |
| Ruff check / format | All checks passed; 171 files already formatted | ruff.log, ruff-format.log |
| Canonical mypy environment | No issues in 121 source files | mypy-canonical-environment.log |
| Vault lint | 0 errors, 0 warnings, 56 notes | vault-lint.log |
| Schema / project pack | Current schemas; OK amplai | schema.log, project-pack.log |
| Doctor | PASS, existing Foundry profile and exact skill mirrors | doctor.log |
| Trace | 144 events; errors [] | trace.log |
| Kit seal | ok=true; all stale/missing/mismatched/unlisted arrays empty | seal.log |
| Package selftest | ok=true, 12 named fixture checks, version 2.5.0 | selftest.log |
| R6 offline output integrity | Both bundles MATCH; 8 recorded output hashes/sizes match | docs-with-base.log, evidence-audit.json |

Initial mypy invocation with the reviewer's extra scripts PYTHONPATH made optional runtime modules analyzable and produced four import-untyped/unused-ignore diagnostics in unchanged cli.py:916/919. The canonical configuration with only isolated src on PYTHONPATH passed. Both results are retained; no annotation/config/source fix was made.

## Mutation Evidence

Five bounded mutation mechanisms, all killed. The four engine mutations execute altered exact source in the returned source/payload fixture module, in process memory only. The fifth injects a narrow mutation into the existing verifier subprocess, still running the real context consumers. No product file bytes were edited, no package was resealed, no failures were attributed to seal/import/environment errors.

| Mutation | Production boundary | Existing tests and observed intended failure |
|---|---|---|
| Skip check_reference_candidate call | scripts/amplai_docs.py:1849 and exact baseline payload | S20 dangling leaf/parent × both hints × source/payload: 8 failures, `DID NOT RAISE DocumentError` at test_document_typed_visibility.py:56 |
| Set reject_unsafe=False | scripts/amplai_docs.py:1838 and payload | S20 escaping leaf/parent plus safe alternate × source/payload: 4 failures at the same required denial assertion |
| Stop spending prefix budget | scripts/amplai_docs.py:3708 and payload | S20 shared-prefix-budget source/payload: 2 failures because SCOPE_INCOMPLETE was not raised |
| Disable restricted classification rejection | scripts/amplai_docs.py:1799 and payload | S17 restricted inline/tree/title × source/payload: 6 failures because required disclosure denial was not raised |
| Remove exact delivered-set assertion only | specs/012-portable-document-lifecycle/verify-kit-guide-selection.py:55 | All 3 missing-guide mapping cases fail at test_document_context_delivery.py:97: mutated verifier incorrectly exits 0/PASS with 5 records; expected exit 1/REJECTED. Normal Context MATCH alone therefore cannot satisfy this test. |

Logs: `mutation-alias-check.log`, `mutation-alias-fallback.log`, `mutation-prefix-budget.log`, `mutation-classification.log`, `mutation-context-delivery.log`. This is five mechanisms across nine source/payload/verifier placements, not 23 independent mutations. The 23 failures are parameterized assertion instances. All 23 passed again in an unmodified fresh process. Engine disk digests were asserted unchanged at injection; final all-file digest audit confirmed all 776 frozen paths unchanged. No persistent mutation needs restoration; process termination discards altered functions. This is bounded targeted evidence, not an exhaustive mutation score or concurrency proof.

## Freshness Reproduction Limits

The excluded doc-impact projection is intentionally absent from the archive. The first isolated docs validation therefore returned CURRENT_INPUT_OR_EVIDENCE_UNAVAILABLE, while both offline bundles validated MATCH. Generating only the isolated derived projection then produced exit 3/STALE; validation returned DOCUMENT_REVIEW_REQUIRED, not a falsely waived PASS. The isolated aggregate dependency snapshot is `95f69543ee607cb7b485bcb6c6723bd05febe80a09153bfa9df0b2937bfc41fb`.

Comparison with the read-only current owning-root projection shows exactly four changed document snapshot fields, all declared_reference_sha256: `.ai-team/README.md`, `.ai-team/knowledge/README.md`, `AGENTS.md`, `README.md`. This matches absent optional runtime-path dependency context in the disposable copy; original source/modes, metadata, rules, candidate fingerprint and all other snapshot fields match. Because attributed impact reviews bind the aggregate snapshot, that difference invalidates the aggregate review/disposition pass for all 25 documents in this isolated projection. No runtime material was imported and no freshness waiver was used.

The current owning-root projection was inspected, not reexecuted here: status RESOLVED, scan_complete=true, semantic snapshot `b44fc0bb30a512d1bed7d3e36db2c33d846af96541908809b99d8588ae29d926`, file SHA256 `7988417fba123c75470017633f9b593637099ea83fed8cd53fcbb1361641ff56`. Parent reports actual owning-root validation exit 0/errors[]/two MATCH views after that projection refresh; this remains attributed parent evidence. Independently executed exact three-guide delivery and two-view validation succeeded without the missing runtime material. See evidence-audit.json and docs-* logs. This environment distinction is not a new source defect.

## Evidence Not Claimed As Reexecuted

The original canonical S08 evaluator and all fourteen predecessor canonical command histories were inspected and hash-bound, not rerun as those exact evaluators by this lens. Independent component/full pytest commands are separately listed. Twelve retained R6 browser artifacts (JSON, PNG and PDF across two audiences) were verified for existence, SHA256 and size; that does not mean those exact owning-guide browser sessions were rerun. Eight HTML outputs match their recorded hashes/sizes and both offline views passed current validation. The full suite's isolated renderer/browser fixture result is separate from retained owning-guide browser evidence.

Native transport logs are inspected-only and do not prove full native Work/resume. Python 3.6 runtime, RHEL7, signing, fleet installation/activation, W004 RPM/nginx packaging and production deployment remain unverified or out of this W003 scope. Four live Slack tests remain deliberately excluded by the existing marker. No requirement is promoted to final PASS by this report alone.

## Final Result

Findings: none. P0=0; P1=0; Blocking-P2=0; Advisory=0.

Full suite output: `2494 passed, 4 deselected in 608.31s (0:10:08)`, process exit 0. The suite covers prior installer/transaction/preservation/HTML/Work presentation behavior as well as S19/S20. No test setup error, timeout, skip inflation or unrelated mutation failure is counted as success. One full execution and targeted controls do not establish universal order independence or cross-platform behavior.

Final `review.py after` compared the shared HEAD, scope/envelope/archive hashes, complete archive membership and all 776 frozen file hashes/sizes/modes again: mismatches []. Final `evidence_audit.py` compared all 776 isolated file bytes/sizes/modes to the same envelope: copy_drift []. The sealed package contains no bytecode files. These are clean frozen bytes after all process-memory mutations; the copy intentionally retains its original dirty scope and only the generated excluded doc-impact projection. A Git-clean checkout would not describe the reviewed target. No source mutation remains, and no shared checkout/evidence/Git file was edited by this reviewer.

The documented isolated all-document freshness limitation remains unverified locally; actual current guide delivery and both output bundles are independently verified. Native/oldest-runtime/target/fleet limits remain as stated. Final gate bookkeeping, report-only gardening and original acceptance closure remain the parent's responsibility.

VERDICT: PASS
