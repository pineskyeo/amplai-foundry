# W003 R5 Independent Regression Review

Work: CR-SYNAPSE-APP-SPLIT-W003
Lens: REGRESSION, third sequential independent review. No other R5 lens conclusions were supplied.
Repository: /Users/pinesky/workspace/amplai-foundry
HEAD/base: 6e005a5e3035ef05510c403a7cd691d524b733bd
Scope: specs/012-portable-document-lifecycle/review-scope-r5.json
Scope SHA256: 8cf0f966f0828e83ebe38089d372c0b5b598bcb18127630a1943b338de4c76f2
Envelope SHA256: b55caffb94912d373af08e83003650193790b8ac6ceb6b81faa68be0100a4451
Archive: /tmp/synapse-split-resume.t7Jusk/w003-review-scope-r5-source-snapshot.tar.gz
Archive SHA256: 5abd5efec29f929a0f6d63c6a8b91d1d3d5be759287b286d5c3076a5b21d3f54

## Findings

P0: 0. P1: 0. Blocking-P2: 0. Advisory: 0.
No demonstrated regression blocker in this frozen scope.

## Basis

Read Foundry AGENTS.md, code-review and 3lens-review instructions, raw contract/spec/plan/generated tasks and relevant manifests, readiness/discovery/context/environment, analysis/trace/permissions, applicable test and contract rules, and relevant active D-046/D-053/D-055/D-056 knowledge. Examined actual runtime/installer/controller changes and their regression assertions, progressively around the affected functions.

The existing tests exercise actual source, canonical payload, installed CLI, all seven selection consumers, current/history selection, exact URI/dependency identity, finite reference budgets, borrowed raw/composed file modes, package mode updates, local conflicts and uninstall restoration. Positive same-security controls accompany denial cases. Test fixtures classify finite synthetic sources; they do not grant a blanket exception to unknown or restricted sources.

The full retained s08-canonical-eval-r5.log and s08-local-verification-r5.json agree: 2374 passed, four pre-existing live Slack exclusions, 11 V2 checks, five outer fast checks, seal, 12 selftest checks and exact Kit guide selection. This canonical suite was inspected, not repeated. Focused counts below are separate and are not added to 2374.

All 152 full-evaluation source pins still match. Of 32 at-run governing pins, exactly the declared plan.md and acceptance-map.json reporting revisions differ. Their recorded before/after hashes match the envelope. Current reporting documents were reviewed as frozen review inputs, not represented as the bytes evaluated during the full run.

R12 has 25 unique attributed source-review requests and 25 matching source-check pins. The review store has 25 current review records. The exact Kit candidate selects three owner guides and retains seven release-independent required instructions. Eight frozen HTML bundle files and 12 retained browser artifacts match their recorded hashes and sizes. Browser evidence was inspected and hash-checked, not rerun. W003 remains 23 requirements/46 cases with zero final PASS; the overall design's 84/168 is a separate denominator.

## Isolated Execution

One new experiment directory: /tmp/w003-r5-regression.cJS4Cl
Disposable checkout: /tmp/w003-r5-regression.cJS4Cl/repo

Reconstructed from the recorded base using a local no-hardlink clone, core.precomposeunicode=true and the verified archive overlay. Verified all 664 copied hash/byte/mode pins before testing. Imports for amplai_foundry, amplai_docs and selected test modules resolved under /private/tmp/w003-r5-regression.cJS4Cl/repo; /tmp normalization was accounted for. The shared virtual environment supplied the interpreter/dependencies only.

Test environment:
PYTHONDONTWRITEBYTECODE=1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
PYTHONPATH=/tmp/w003-r5-regression.cJS4Cl/repo/src:/tmp/w003-r5-regression.cJS4Cl/repo/tests:/tmp/w003-r5-regression.cJS4Cl/repo/scripts
Interpreter: /Users/pinesky/workspace/amplai-foundry/.venv/bin/python -B

Executed from the disposable checkout:

1. python -B -m pytest -q -p no:cacheprovider --basetemp=/tmp/w003-r5-regression.cJS4Cl/pytest-focused tests/ai/test_document_typed_visibility.py tests/ai/test_document_reference_resources.py tests/ai/test_portable_baseline.py -k 'typed or s16 or s18 or fixture_import'
   Exit 0; 150 selected cases passed. Collection verification: 150/278 selected, 128 outside this focused selection. Logs: focused.log, focused-collection.log.
2. python -B /tmp/w003-r5-regression.cJS4Cl/mutation_probe.py
   Exit 0; five of five in-memory mutants killed by existing regression assertions. Logs/probe: mutation.log, mutation_probe.py.
3. python -B specs/012-portable-document-lifecycle/verify-kit-guide-selection.py
   Exit 0; PASS_KIT_GUIDE_SELECTION, three guides, seven instructions, retained unclassified product references blocked. Log: guide-selection.log.
4. python3 -B tools/amplai-loop-kit/seal.py --verify
   Exit 0; ok=true and every drift/missing/extra list empty. Log: seal.log.
5. python3 -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl
   Exit 0; valid=true, events=136, errors=[].
6. python -B /tmp/w003-r5-regression.cJS4Cl/pin_check.py
   Exit 0; final pin audit PASS. Log/probe: final-pins.log, pin_check.py.

All relative log names above are under /tmp/w003-r5-regression.cJS4Cl.

Mutation results:
- Removing typed visibility iteration from scripts/amplai_docs.py:1764 and its payload counterpart is killed by the existing restricted inline-reference test: DID NOT RAISE DocumentError.
- Removing classification precharge from scripts/amplai_docs.py:3535 and its payload counterpart is killed before prefix processing by the existing budget assertion.
- Restoring the cache-writing fixture loader in tests/ai/test_document_reference_resources.py:28 is killed by the existing no-package-bytecode assertion, even with normal bytecode behavior explicitly enabled inside its disposable fixture.

Mutations existed only in process memory and ended with the probe. Its intentional cache file is confined to the synthetic mutant-cache-loader directory outside the disposable repo. Final source pins are unchanged and the disposable sealed package has zero __pycache__ directories.

## Limits And Final Recheck

No shared source, evidence, cache or Git writes; no source fixes, publication, network/native-host calls, real Store/Vault access or production mutation. No full-suite rerun or browser rerun. No claim of Python 3.6 runtime, RHEL7, full native Work/resume, signing, fleet activation, actual retirement or production deployment PASS. The focused in-memory mutations cover specific guards, not exhaustive mutation adequacy.

Final check after tests and mutations: shared 664/664 and isolated 664/664 hash/byte/mode pins match; envelope and semantic scope hashes match; archive SHA256 matches with exactly 665 unique expected members, no AppleDouble, matching contents/modes and envelope; HEAD unchanged. Source-at-run drift is empty and only the two declared post-evaluation governing deltas remain. The excluded derived projections receive no freshness waiver. Post-review projection reconciliation, gardening/security and native completion remain the main workflow's separate gates.

VERDICT: PASS
