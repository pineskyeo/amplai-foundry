# W003 R3 — Independent Contract Lens

Target: `/Users/pinesky/workspace/amplai-foundry`, feature `specs/012-portable-document-lifecycle`, Work `CR-SYNAPSE-APP-SPLIT-W003`.

## Frozen inputs

Before and after: all 147 source + 231 governing files matched SHA256, bytes and octal modes; 378 unique regular files, zero mismatches. Scope-file SHA256: `56f56a1c242dae26bd1ab67515be37ca5c87d0bd28e0dd2acae1d358b8e484bb`. Scope semantic digest: `883bc9e39e7a83739121f3a40c327af99bd3d85399483c100a83db32c6bb9a5c` (Node JSON.stringify after removing scope_sha256).

The preserved archive `/tmp/synapse-split-resume.t7Jusk/w003-review-scope-r3-source-snapshot.tar.gz` matched SHA256 `3577e29210b89058a34b5d1754677695b259f02635641688c91b4390495937e8` before and after. All 378 tar entries matched the frozen contents, lengths and modes; no missing or additional members. Both verification commands exited 0. No shared source, projection or archive was changed.

## Findings

### R3-C1 — P1, blocking: HTML references outside href/src bypass disclosure filtering

Location: `scripts/amplai_docs.py:2858`; identical sealed baseline copy at `tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py:2858`. Existing coverage enumerates only href/src HTML forms at `tests/ai/test_document_references.py:133`.

Violated clauses: DOC-002/DOC-003, FR-015 (`spec.md:154`), and `contracts/portable-operations.md:54`–64: security must precede title/snippet/HTML output; unsupported reference syntax must fail before exposing its target. The S12 acceptance requires exact destinations to be bound or safely rejected.

Mechanism: the HTML lexer parses every attribute but emits a reference token only when its name is href or src. Reference-bearing srcset, poster, object data and SVG xlink:href silently yield zero tokens. Thus reference_visibility has nothing to classify, although the raw HTML is retained in snippets and escaped page text. Escaping prevents execution but does not remove restricted identifiers.

Concrete reproduction uses the existing actual engine and synthetic lifecycle fixture: a reviewed PUBLIC guide contains `<img srcset="PRIVATE_CANARY.md 1x" alt="PRIVATE_TITLE_CANARY">`; `docs/PRIVATE_CANARY.md` is explicitly RESTRICTED. The ordinary href control rejects CROSS_BOUNDARY_REFERENCE. All four omitted-attribute variants instead return count=1 and emit the private marker.

For srcset, all seven CURRENT_CONSUMERS reproduce this in current and explicit history modes. Actual copied canonical `loopctl docs inventory/query/index --release release-2 --security PUBLIC` each exits 0 and emits the marker, with and without --history. An INTERNAL release-backed HTML build with the same reference to a RESTRICTED document returns complete=true, writes the restricted filename into its page, and validates with valid=true / MATCH. No external publication or executable XSS is claimed.

Reproductions retained beside this report:

- `probe_reference_contract.py`: current engine imports are asserted; all-consumer, actual CLI and actual HTML observations; exit 0 is diagnostic completion, not contract PASS.
- `test_reference_contract.py`: independent expected-rejection assertions; **4 failed, 1 passed**, exit 1; passing case is href control. Failures are `DID NOT RAISE DocumentError` for srcset, poster, object data and xlink:href.

Smallest remediation: make the shared bounded scanner account for these reference-bearing HTML attributes, including multi-destination syntax, or reject unsupported forms non-disclosingly before any consumer output. Add source/installed, current/history, CLI and HTML negative controls; retain safe same-security controls and exact dependency freshness checks. Mirror/reseal the canonical payload. The existing href/src-only passing matrix does not certify this omitted boundary.

### R3-C2 — Advisory: excluded impact projection currently fails exact comparison

Location: `specs/012-portable-document-lifecycle/doc-impact.json:1`, comparison at `scripts/loopv2.py:1494`.

Exact command `python3 -B scripts/loopctl.py docs validate specs/012-portable-document-lifecycle` exits **1**, valid=false, verdict=STALE, errors=[DERIVED_REPORT_DRIFT]. Dependency snapshot remains `92fd8514805893ae39bc8629cdad53af2b3797678232506dbf53150f505e8349`; both configured offline bundles remain MATCH.

Read-only `loopv2.docs_impact(..., write=False)` comparison finds only completeness membership changes: previous expected=378, current expected=382, plus four corresponding excluded entries. Added paths are `review-scope-r3.json`, `s08-convergence-r3.json`, `s08-security-scan-r3.json`, and `s08-view-evidence-r3.json`, all under this feature. No semantic dependency change was observed. The projection is one of the four explicitly excluded derived files, so this observation is not frozen source drift. It cannot be reported as a current docs-validation PASS. Refresh only after the frozen reviews finish, then retain the actual validation result; no refresh was performed by this reviewer.

## Executed verification

Working directory for all commands below is the target repository. All fixtures and reviewer artifacts stayed below `/tmp/w003-r3-contract.3TJ1CF`; no live Store, host trust, service, model or network calls were used.

| Command | Result |
|---|---|
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B /tmp/w003-r3-contract.3TJ1CF/probe_reference_contract.py` | Exit 0; demonstrated R3-C1 disclosure, 14 common selections + 6 CLI operations, HTML complete/MATCH |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider --basetemp=/tmp/w003-r3-contract.3TJ1CF/negative-fixtures /tmp/w003-r3-contract.3TJ1CF/test_reference_contract.py` | Exit 1; 4 failed, 1 passed |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider --basetemp=/tmp/w003-r3-contract.3TJ1CF/pytest tests/ai/test_document_governance.py tests/ai/test_document_references.py tests/ai/test_document_lifecycle.py tests/ai/test_document_html.py` | Exit 0; existing four contract modules pass |
| `python3 -B scripts/loopctl.py context validate specs/012-portable-document-lifecycle/context-pack.json` | Exit 0; MATCH, all 20 current sources |
| `python3 -B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl` | Exit 0; valid, 55 events |
| `python3 -B tools/amplai-loop-kit/seal.py --verify` | Exit 0; no stale, missing, mismatched or unlisted entries |
| `node specs/012-portable-document-lifecycle/verify-traceability.mjs /Users/pinesky/workspace/synapse-app-split` | Exit 0; original hashes and 23 requirements/46 cases match; 0 final passed cases |

Read-only status/diff review covered the native view/redaction changes and the D-055 claim supersession. Native mutation/storage/claim behavior was not broadened by those inspected changes. The original claim statement and provenance remain retained; this is not a Vault approval. Reviewed material included applicable repository rules, code-review skill, contract/spec/plan/refinements, task acceptance and invariants, readiness/discovery/context/environment, original requirements, current evidence and the 55-event trace. No other current R3 lens conclusions were read.

The retained exact S08 log records 2049 passed / 4 live Slack cases deselected, full V2 and 12 package selftests. That full 389.26-second suite was inspected, not rerun. Existing PASS evidence remains valid for its assertions but does not cover R3-C1. Actual Python 3.6, RHEL7, full native Work/resume/lifecycle, production activation and W004 deployment remain unverified. S08 final review, post-review gardening/security, native result and handoff remain completion steps, not approved by this lens.

The probe also records a file-scheme variant; it is not included as a separate demonstrated blocker because its URI authority boundary needs separate triage.

VERDICT: CHANGES_REQUIRED
