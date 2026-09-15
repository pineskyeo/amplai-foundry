W003 R5 FAILURE/RECOVERY review: one P1 remains. Counts: P0 0; P1 1; Blocking-P2 0; Advisory 0.

All relative paths below refer to `/Users/pinesky/workspace/amplai-foundry`.

Scope integrity

Before and after review, all 152 source files and 512 governing artifacts matched their recorded SHA-256, byte count and POSIX mode. HEAD remained `6e005a5e3035ef05510c403a7cd691d524b733bd`.

- Scope digest: `8cf0f966f0828e83ebe38089d372c0b5b598bcb18127630a1943b338de4c76f2`.
- Envelope SHA-256: `b55caffb94912d373af08e83003650193790b8ac6ceb6b81faa68be0100a4451`.
- Archive SHA-256: `5abd5efec29f929a0f6d63c6a8b91d1d3d5be759287b286d5c3076a5b21d3f54`; verified exactly 664 files plus the envelope, without metadata companions.

The current S08 execution snapshot matches all 152 source files. Its 32 governing pins differ only at the two declared post-evaluation documents: `plan.md` and `acceptance-map.json`. All 25 r12 attributed guide-source hashes remain current.

No shared source, package, evidence, cache or Git mutation occurred.

Finding R5-FR-001 [P1] — Unsafe optional alias becomes an absent example and bypasses disclosure checks

Locations: [scripts/amplai_docs.py:3718](/Users/pinesky/workspace/amplai-foundry/scripts/amplai_docs.py:3718), [scripts/amplai_docs.py:1833](/Users/pinesky/workspace/amplai-foundry/scripts/amplai_docs.py:1833), and [scripts/amplai_docs.py:1868](/Users/pinesky/workspace/amplai-foundry/scripts/amplai_docs.py:1868). The canonical baseline payload contains the same implementation.

`reference_candidates()` catches an unsafe resolution and silently discards that candidate. For an optional inline reference, another candidate—the document-relative spelling—can remain as a nonexistent path. `reference_visibility()` consequently finds nothing and allows the reference because it is optional. The existing logical alias’s security classification is never checked.

Reproduction uses only disposable synthetic files:

1. A reviewed PUBLIC guide contains `See ` followed by the inline-code path `assets/PRIVATE_CANARY.bin`.
2. Policy explicitly classifies `assets` as RESTRICTED and includes that optional path in `reference_scan.known_absent`.
3. The path exists as a symlink to a synthetic file outside the fixture repository.
4. Root-relative resolution rejects the escaping symlink, but the nonexistent `docs/assets/PRIVATE_CANARY.bin` fallback survives. Selection returns success with the restricted identifier in its snippet.

This violates DOC-002/FR-015’s pre-output disclosure boundary, FR-010’s historical security requirement, and S17’s explicit rule that existing optional targets cannot use known-absent hints to bypass classification (`plan.md:371`; T017-AC1).

Observed impact:

- Canonical source and payload each emitted the marker through all seven consumers, in current and historical modes: 28 successful disclosures.
- An otherwise equivalent symlink resolving inside the repository was denied in all 28 controls with `CROSS_BOUNDARY_REFERENCE`.
- A freshly installed, byte-identical engine’s inventory/query/index CLI returned exit 0 and emitted the marker in all six current/history checks.
- `prepare_view_input(..., security="PUBLIC")` also returned the marker in its source bundle.
- Changing the reference to an explicit requirement produced `MISSING_SOURCE` without the marker.

The demonstrated leak is the restricted identifier; these probes do not establish an outside-file content read. Silent success also removes the diagnostic needed to repair the unsafe alias.

Minimum remedy: preserve unsafe-resolution status separately from genuine absence. Reject an existing escaping/unresolvable alias before optional fallback, and enforce its logical classification before discarding it. Retain legitimate absent-example behavior. Add this combination to source, payload, installed CLI and view-input regressions.

Verification performed

All executions used a new disposable checkout at `/tmp/w003-r5-recovery.qESisa/checkout`, assembled from the recorded base and verified snapshot. Imports of the engine, fixture helpers and application package were asserted to resolve inside that checkout. Python used `-B`; pytest cache and plugin autoload were disabled.

- `run_focused.py tests/ai/test_document_reference_resources.py tests/ai/test_document_typed_visibility.py`: **207 passed**, exit 0, 17.54 seconds.
- `run_focused.py tests/ai/test_portable_baseline.py tests/ai/test_document_review.py tests/ai/test_document_preservation.py -k 's18 or hard_stop or substituted_parent or mid_transaction_failure or concurrent or atomic or publication_failure or process_interruption or recovery or failed_verifier or backup_never or exact_retry or invalid_batch'`: **55 passed, 120 deselected**, exit 0, 84.92 seconds.
- `probe_optional_alias.py` and `probe_optional_alias_delivery.py`: exit 0 as diagnostic scripts; they reproduce the failure above, not a security PASS.
- `verify_scope.py --extract` and `verify_scope.py --after`: exit 0, zero integrity mismatches.

The initial reviewer harness stopped before tests because `/tmp` resolves to `/private/tmp` on this host. Normalizing the temporary root resolved that harness assertion.

The passing focused checks support the repaired declaration budgets, borrowed-file permissions, transaction interruption/recovery, concurrent-writer rejection, atomic-record preservation and idempotent retry mechanisms. They do not cover the demonstrated unsafe-alias combination.

Preserved evidence

Under `/tmp/w003-r5-recovery.qESisa/`:

- `verify_scope.py`, `scope-before.log`, `scope-after.log`
- `run_focused.py`, `focused-reference-r2.log`, `focused-recovery.log`
- `probe_optional_alias.py`, `optional-alias.json`
- `probe_optional_alias_delivery.py`, `optional-alias-delivery.json`, and retained `optional-alias-delivery/` fixtures

The recorded 2,374-test canonical S08 and browser evidence were reviewed, not rerun. No network, native host lifecycle, live Store/Vault operation or deployment was exercised. Python 3.6, RHEL7 and full native Work/resume remain explicitly unverified.

VERDICT: CHANGES_REQUIRED
