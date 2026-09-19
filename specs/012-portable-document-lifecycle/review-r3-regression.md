# W003 R3 — Independent Regression Review

Verdict: PASS for the frozen implementation. Counts: P0 0, P1 0, Blocking-P2 0, Advisory 1. This is the third sequential lens; no current R3 peer conclusions were read. It does not close the remaining Work completion steps.

## Frozen scope and isolation

- Base/target HEAD: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
- Semantic scope: `883bc9e39e7a83739121f3a40c327af99bd3d85399483c100a83db32c6bb9a5c`.
- Scope-file SHA256: `56f56a1c242dae26bd1ab67515be37ca5c87d0bd28e0dd2acae1d358b8e484bb`.
- Document snapshot: `92fd8514805893ae39bc8629cdad53af2b3797678232506dbf53150f505e8349`.
- Retained archive: `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r3-source-snapshot.tar.gz`; 1,015,271 bytes; mode 0600; SHA256 `3577e29210b89058a34b5d1754677695b259f02635641688c91b4390495937e8`.

`verify-scope.cjs` checked every SHA256, byte count and octal mode: 147 source + 231 governing rows, before and after in both shared and isolated trees. Every check exited 0 with zero differences. The exact 378-row observations are retained in `scope-before.json`, `scope-after.json`, `scope-isolated-before.json`, and `scope-isolated-after.json`. Each individual mutation was restored and separately passed the same 378-row check (`scope-restored-uri.json`, `scope-restored-mode.json`, `scope-restored-identity.json`).

The isolated tree was a local, non-hardlinked clone at `checkout/`, overlaid with the frozen archive. `imports.json`/`imports.log` prove `amplai_foundry`, `amplai_docs`, `loopv2`, and `loopctl` resolve inside that tree, despite using the shared Python 3.11 virtual-environment executable. Subprocesses received explicit isolated `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1`, and a disposable `TMPDIR`; pytest's cache provider was disabled. No shared source, governing artifact, projection, or trace was edited. No live Store/Slack/native transport/network, shared Git staging/commit/push, or product-repository operations were performed. Existing tests initialize Git and synthetic Stores only in disposable fixtures.

Setup diagnostic: the first isolated verifier invocation exited 1 because the archive intentionally omits its self-referential `review-scope-r3.json`. The verifier was corrected to read the fixed authoritative shared scope file while checking the isolated rows; no evidence was accepted from that failed invocation.

## Evidence and checks

Read the governing contract/spec/plan and S08/R1/R2 refinements; acceptance/invariants/scope of all 13 raw task manifests and generated tasks; readiness/discovery/authorization/context/environment; acceptance ledger/trace; active D-046/D-053/D-055/D-056 sources; current S12/S13, revalidation, S08, guide r6, native transport and HTML evidence. Reviewed bounded actual diffs and relevant source sections, including installer ownership/seals, old controller integration, reference consumers, freshness, atomic publication, and terminal/heartbeat presentation.

All exact command argv, working directories, environment overrides, exit codes, durations, and log SHA256 values are retained in the named `.json` records beside the corresponding `.log` files. `run.cjs` is the command recorder; `verify-scope.cjs <stage> [checkout]` reproduces fingerprint checks.

| Run record | Exit | Observed result |
|---|---:|---|
| `selected-baseline.json` | 0 | 78 selected regression cases passed; 73.875 s. Real CLI and installed package, mandatory governing/forged Context, exact URI/renderer, stale reviews/incomplete scope, atomic failure cleanup, old installer preservation/rollback, heartbeat/terminal controls. |
| `mutation-uri.json` | 1 | 3 failed / 9 passed: exact URI identity and real inline renderer detect the mutation. |
| `mutation-mode.json` | 1 | 3 failed: real review/impact mode preservation and private new-record default detect omission. |
| `mutation-identity.json` | 1 | 4 failed / 2 passed: real review/impact reject concurrent chmod/same-bytes replacement; content-drift controls remain effective. |
| `restored-controls.json` | 0 | All 21 mutation controls passed / 133 deselected; 32.95 s pytest time. |
| `current-context.json` | 0 | Current shared Context MATCH. |
| `current-trace.json` | 0 | All 55 events valid. |
| `current-docs.json` | 1 | Only `DERIVED_REPORT_DRIFT`; semantic snapshot unchanged and both offline views MATCH. See A1. |
| `projection-difference.json` | 0 | Read-only membership comparison proves the exact A1 difference. |

The mutation cycle was sequential, solely in `checkout/scripts/amplai_docs.py`:

1. Line 1744: append `.split("#", 1)[0]` to `unquote(parsed.path, errors="strict")`. Killed at `tests/ai/test_document_references.py:72` and `:196`; mutation hash `8cd1ff80f06cc4e5faf441207d7df32a1a68859c01213e706b97d2b06c0dde37`.
2. Line 2116: replace `os.fchmod(handle.fileno(), mode)` with `pass`. Killed by public review/impact assertions at `tests/ai/test_document_review.py:118` and private-default control at `:206`; mutation hash `babbc3cbd8613b1a66aa6cc5917745f32e78f4bda1a7658479e2549ac90a24e8`.
3. Lines 2073–2082: replace `record_state`'s tuple with `(info.st_size,)`. Killed by both public operations at `tests/ai/test_document_review.py:145`; mutation hash `e0872c185305acaf19349b6a63d8463a5e921fda2d74aed87a922d48500427c4`.

After restoration, source and canonical payload both equal `dd8b1ac42632136db71da10646267a71187650949ed1b8664ccb4b4196e362d9`. Mutation failures are expected sensitivity evidence, not implementation defects. The fresh-before-selection assertions and actual CLI/installed tests prevent accepting an empty stale selection as proof of a working disclosure guard. Public atomic tests execute the real writer, with injected filesystem transitions at the publication boundary.

## Findings

A1 [Advisory] — Excluded impact projection needs final synchronization.

- Location: `specs/012-portable-document-lifecycle/doc-impact.json:747`; enforcement: `scripts/loopv2.py:1495`.
- Mechanism: the saved completeness membership has 378 paths; current membership has 382. Exactly `review-scope-r3.json`, `s08-convergence-r3.json`, `s08-security-scan-r3.json`, and `s08-view-evidence-r3.json` were added under this feature. Current classification adds the same four to `completeness.excluded`; there are no removals or semantic changes.
- Impact: the required current docs-validation command exits 1 until the derived projection is synchronized. Its fail-closed behavior is working. This is an explicitly excluded projection, not drift in any frozen source/governing row; the exact document snapshot remains `92fd8514…e8349`, current impact is RESOLVED/complete, and both HTML inputs match.
- Smallest remediation: after all lenses, regenerate only the named `doc-impact.json` projection and rerun docs validation under the existing post-review policy. No source repair or new review round is indicated by this observation. Do not report final Work completion before that validation is current.
- Reproduction: replay `current-docs.json` and `projection-difference.json`; the latter calls `docs_impact(..., write=False)`. No projection was refreshed by this review.

## Limits

The canonical 2,049 passed / 4 live Slack deselected / 389.26 s S08 result and 12 package selftests are retained evidence, not rerun here. The 78-case run and restored 21-case run overlap and are not a combined unique-test total. Mutation coverage is three deliberate guards, not an exhaustive mutation score. Browser/offline evidence and native transport were reviewed, not independently re-executed. Native evidence proves transport/continuation-ID observation only, not full Work/resume. Python 3.6 execution, RHEL7, ACL/SELinux/xattr qualification, W004 deployment, fleet activation, and production remain unverified. Post-review projection synchronization, report-only gardening, final security/native result/handoff remain outside this lens.

VERDICT: PASS
