# W003 R4 Independent Contract Review

Work: CR-SYNAPSE-APP-SPLIT-W003. Repository: /Users/pinesky/workspace/amplai-foundry.
Lens: CONTRACT. Read-only review; no other reviewer's current conclusions received.

## Frozen target and evidence

- Base/target HEAD: `6e005a5e3035ef05510c403a7cd691d524b733bd`.
- Scope digest: `83d37d8d661b7360be05308af5077cf64d542d2e5a452e1b46d56fbf37118e39`.
- Scope file SHA256: `a9a85f5c5ea745b07320b34e9d7cdad8e66db1ee6bb25674995a11772ad5a01e`.
- Archive SHA256: `ad056026489939800efb0df7da7db73f3bc26075d270d774178b2737079496bb`.
- Before and after review: all 149 source and 391 governing entries matched their content hashes, sizes and POSIX modes; HEAD matched; drift was empty. The archive's 540 declared members also matched their hashes. Python tarfile additionally exposes 540 macOS AppleDouble `._` companion entries; these are archive metadata, not additional reviewed product files.
- Original requirements/acceptance hashes matched the two authority pins in acceptance-map.json. The 23 assigned requirements and 46 original procedures remain present, with zero final PASS cases pending closure.

Reviewed applicable AGENTS.md, code-review skill, permissions and contract/test rules; raw work contract, spec, plan, operation/data contracts, manifest obligations, readiness/discovery/Context/environment, active D-046/D-053/D-055/D-056 decisions, evidence trace and current same-app checkpoint. Reviewed implementation and behavioral tests across baseline/installer, metadata/filtering, impact/review publication, offline views, preservation/retirement and native presentation; deeper inspection covered S14/S15 resource, URI, HTML and physical-line repairs. Current local verification, view evidence and convergence are evidence of the recorded executions, not independent approval.

## Finding

R001 [P1, blocking] `scripts/amplai_docs.py:1769` (same code in `tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py:1769`) — recognized local dependencies bypass reference disclosure checks when written as inline code or a directory tree.

`reference_visibility()` only consumes `document_reference_tokens()`, which emits Markdown/HTML link destinations. The typed extractor and `declared_reference_snapshot()` recognize additional local dependency syntax, but those destinations never reach the disclosure guard. For a PUBLIC reviewed guide, either `Requires ` followed by the code span `PRIVATE_CANARY.md`, or a `docs/` tree with `└── PRIVATE_CANARY.md`, resolves and hashes the existing RESTRICTED `docs/PRIVATE_CANARY.md` as a declared dependency. The source is nevertheless `freshness=verified`, and the PUBLIC selected result includes that restricted filename in its snippet.

Reproduction in a disposable copy using the existing repository/review/release fixtures:

1. Create the RESTRICTED target and a PUBLIC guide with one of the two recognized forms; record the normal exact-snapshot fixture review.
2. Confirm `declared_dependencies` contains `docs/PRIVATE_CANARY.md` and freshness is verified.
3. Call each of the seven current consumers in both current/history modes with PUBLIC security. All 14 combinations return the restricted identifier for each form.
4. Run the actual `loopctl.py docs query --release release-2 --security PUBLIC`: exit 0 and the identifier appears in stdout. Build an INTERNAL offline view (lower than RESTRICTED): `complete=true`, subsequent validation succeeds, and HTML contains the identifier.

The same behavior was reproduced through the canonical engine and baseline payload engine. The CLI uses the copied canonical script; this probe does not claim a separate installed-CLI reproduction. A direct PUBLIC HTML build correctly rejects `APPROVED_EXTERNAL_INPUT_REQUIRED`; that approval boundary was not bypassed.

Violated obligations: DOC-002/DOC-003; spec FR-015 and User Story 3 acceptance 2 (`spec.md:70`); original AC-053-N/AC-052-P disclosure and current-consumer protection. This is not arbitrary-prose DLP: both exact inputs are already machine-recognized, required local dependencies. An author can change an existing protected Markdown link into supported code/tree notation and expose the same restricted locator while preserving a valid review and output.

Minimal remediation: apply the existing target classification and disclosure checks to all supported, explicitly recognized local reference forms before ranking/snippet/HTML derivation. Preserve finite parsing/resource bounds, URI identity and required-dependency behavior; retain safe same-security controls. Add canonical/payload/CLI and lower-security view regressions for required inline-code and structural-tree references, including current/history selection, then mirror/reseal and review a new frozen target.

Reproduction source: `/tmp/w003-r4-contract.aBwYIx/probe_reference_visibility.py`.
SHA256: `2ec7c3d2106bc65125db916c203ad5ee0a92a4fad9f3c1a21f7543f93027aba6`.
Its four rows (two source engines x two forms) each observed: dependency bound=true, freshness=verified, 14/14 consumer-mode leaks, CLI exit=0 with identifier leak, INTERNAL HTML complete=true/valid=true with identifier leak. Probe exit 0 means these observations were collected; it is not an acceptance PASS.

## Actual checks and commands

Disposable copy: `/tmp/w003-r4-contract.aBwYIx/repo`, made with rsync excluding .git, .venv, live .amplai/.ai-team/local state and caches. Imported amplai_foundry, amplai_docs, loopv2 and both test helpers resolved under the disposable copy. The existing venv supplied only the interpreter/dependencies. All writes from tests/probes were in disposable fixtures.

With `PYTHONPATH=/tmp/w003-r4-contract.aBwYIx/repo/src:/tmp/w003-r4-contract.aBwYIx/repo/tests:/tmp/w003-r4-contract.aBwYIx/repo/scripts` and interpreter `/Users/pinesky/workspace/amplai-foundry/.venv/bin/python`:

- `-B -m pytest -q -p no:cacheprovider tests/ai/test_document_reference_resources.py tests/ai/test_document_html_references.py tests/ai/test_document_governance.py tests/ai/test_document_integration.py`: exit 0. Collection confirmed 80+88+22+33 = 223 cases.
- `-B /tmp/w003-r4-contract.aBwYIx/probe_reference_visibility.py`: exit 0; R001 observed as above.
- `-B scripts/loopctl.py trace verify specs/012-portable-document-lifecycle/evidence-trace.jsonl`: exit 0, valid=true, 88 events, no errors.
- `-B tools/amplai-loop-kit/seal.py --verify`: exit 0, ok=true, no stale/missing/unlisted/mismatched entries.
- Read-only Node/Python hash checks verified both scope pins, archive pin, 540 named archive contents and original authority pins.

Preparation diagnostics were not product failures: the first import-origin assertion compared `/tmp` with macOS's resolved `/private/tmp` and was corrected; the initial archive-member equality assertion found the AppleDouble companions, after which all 540 declared contents were verified. The initial direct PUBLIC HTML probe was denied by the expected approval gate; the final HTML check used the permitted INTERNAL builder.

## Limits and disposition

- Did not rerun full V2, the browser harness, shared-checkout validation, native hosts, RHEL7 or Python 3.6. The retained current full report records 2229 passing tests, four existing live-Slack deselections and 11 V2 checks. Those figures are observed prior evidence only.
- Current documentation evidence records RESOLVED/MATCH snapshot `371a912969c350319dd59fd471418492ffaf02e08c30636025b3e6c0ddd6a3c0`. No projection exclusion was treated as a freshness waiver; no impact refresh or shared-file edit was performed.
- T008 legitimately remains in progress for the three sequential reviews, final documentation membership, gardening/security and native result. Missing native full Work/resume, Python 3.6/RHEL7, fleet, signing and production proofs remain explicitly unverified. W004 deployment preparation is separate and unimplemented. None is counted as PASS here.
- No additional speculative findings. R001 is sufficient to keep the review gate closed. No Git, Store, host/fleet/deployment, Vault or shared-source mutation was performed by this review.

VERDICT: CHANGES_REQUIRED

FINDINGS:
- R001 [blocking] scripts/amplai_docs.py:1769 — P1: recognized required inline-code/tree references bypass target disclosure checks, exposing RESTRICTED locators in PUBLIC selection/CLI and validated INTERNAL HTML. Apply the existing guard to supported typed local references and add the demonstrated regression matrix.
