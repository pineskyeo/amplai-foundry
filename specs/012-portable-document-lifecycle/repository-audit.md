# Repository Evidence Audit

Status: design evidence only. Native W003 remains DRAFT and unclaimed. This record
does not authorize a live install, Store rebind, Git publication or knowledge mutation.

## Source, Payload And Installed State

Baseline HEAD is `6e005a5e3035ef05510c403a7cd691d524b733bd`. The canonical package
is `tools/amplai-loop-kit`, version 2.4.0; Foundry Platform is version 0.4.0.

All 23 package-owned payload files match their manifest hashes. Installed root files
match 19 of 23 byte-for-byte. Hash values in the manifest include the `sha256:` prefix.
The four differences require individual disposition, not blind copying:

| Installed file | Observed difference | Required disposition |
|---|---|---|
| `scripts/amplai_runtime.py` | Formatting around activation arguments and conditions | Preserve behavior; select one reproducible package source |
| `scripts/amplai_supervisor.py` | Formatting/comments; root has parenthesized context-manager syntax, payload keeps Python 3.6-compatible spelling | Do not copy newer root syntax into the portable payload |
| `tests/ai/test_amplai_async_runtime.py` | Root adds request-ref idempotency, managed-worktree/controller and default-runner-profile cases | Preserve these regressions in source and portable test coverage |
| `tests/ai/test_amplai_kit_regressions.py` | Root adds payload-runtime tests and rejects unsafe runner profiles; old payload tests still expect registration followed by a warning | Reconcile tests against current runtime behavior; do not weaken the safety guard |

Evidence: complete `diff -u` comparisons of the four root/payload pairs and the
manifest inventory. A version label alone does not establish installed capability.

## Existing Mechanisms To Reuse

- `install.py:328` validates declared owned files and fragments. `validate_target`
  requires existing Loop V2 files, so this is an extension installer, not a complete
  empty-repository baseline. Three-way file/fragment merge, rollback and uninstall
  already exist and must remain the transaction mechanism.
- The installer does not consume a complete package checksum inventory in
  `validate_package`. Existing `seal.py --verify` supplies that separate capability.
  Installation must not accept undeclared extra files or a missing sealed dependency.
- `scripts/loopctl.py` and `scripts/loopv2.py` use the existing controller and standard
  library engine. Doctor currently expects 18 Foundry skills and local policy paths.
  A portable repository profile must separate generic engine requirements from the
  Foundry-owned additions without deleting them.
- Synapse W001/S04 has tested dependency-bound documentation impact/review and atomic
  batch submission. Reuse the generic mechanism, with explicit repository adapters;
  do not import Synapse ontology, equipment knowledge or private paths into the Kit.
- Foundry's verifier registry includes Platform/Vault-specific checks. It is not a
  valid default verifier for a blank repository.

## Demonstrated Gaps

- `scripts/loopv2.py:561` excludes only deprecated/superseded/rejected claims.
  Candidate, archived and unknown claims are not an explicit active-only selection.
- Documentation review still uses legacy acknowledgement/touched semantics and a
  bounded `docs_referencing` result. Complete scan state and exact reviewed content
  must govern the new lifecycle; an unreadable or truncated scan cannot imply no impact.
- `.ai-team/policy/gardening.json` includes user backup suffixes in SAFE_AUTO and
  includes Cortex-specific dynamic-entry paths. Portable policy must preserve user
  backups and receive repository-specific build/packaging evidence through a profile.
- `scripts/amplai.py:257` returns the heartbeat result directly. The runtime result
  contains the lease credential. Default progress output needs explicit redaction,
  retaining the functional renewal and opt-in credential handling contract.
- Root README semantic-runtime guidance conflicts with the existing D-046 boundary.
  Correct only the affected guidance; do not reintroduce the removed semantic stack.

## Evidence Limits And Remaining Discovery

The clean baseline V2 verifier passed all 11 checks: 1,625 tests passed and four were
deselected, on Python 3.11.15 / Darwin arm64. This does not prove Python 3.6 execution,
native Claude/Codex invocation, RHEL7 behavior or production readiness.

An independent isolated lifecycle reproduction now confirms completed-Work reads succeed
through runtime and CLI JSON/Markdown/handoff/show, without Store content or mtime changes.
Markdown omits the Work's own result and incorrectly gives the same update instruction
for DONE. Default heartbeat output exposes the lease credential while claim output redacts
it. See `closed-work-discovery.json`; native E002's original cause remains unproven.

The complete baseline inventory is recorded in `baseline-inventory.md`. The current
task validator's future annotations/built-in generic type spelling and unconditional
PyYAML reads need a Python 3.6-compatible JSON-first route. Host grammar checks are not
old-interpreter execution. The current source/policy docs also contain a stale canonical
skill-direction claim contradicted by approved D-055; `research.md` records its resolution.

Before READY: finish design artifacts and exact positive/negative acceptance mapping,
reconcile the stale knowledge index under existing D-055 and capture the implementation
Context Pack/environment. Host/old-Python smoke availability remains an explicit evidence
limitation. Discovery remains read-only apart from feature design artifacts and its marker.
