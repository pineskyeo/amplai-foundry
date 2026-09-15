# Implementation Plan: Portable Document Lifecycle

**Branch**: existing main; logical feature marker 012-portable-document-lifecycle
**Date**: 2026-09-11
**Spec**: [spec.md](spec.md)
**Native Work**: CR-SYNAPSE-APP-SPLIT-W003 — RUNNING under E021. S20/S19 and all14affected canonical revalidations pass. Current full S08 passes2494tests with4existing live Slack deselections, unchanged155source/46governing inputs, seal119 and selftest12. Current25source reviews resolve; actual Context delivers3owning guides+7instructions with exact hashes/MATCH. Two immutable R6 HTML bundles pass isolated offline/browser checks. Three fresh sequential R6reviews and post-review completion gates remain; prior evidence is preserved.

## Summary

Deliver a complete opt-in generic development baseline through the existing Foundry Kit,
then add exact-snapshot documentation lifecycle, safe deterministic HTML and recoverable
retirement checks. Preserve one work/design controller, current host trust, Project Store
authority and private Platform/Vault boundaries. Reuse the existing transaction installer,
V2 evaluator and generic Synapse document-review mechanism; do not copy private profiles.

The narrow D-055 index reconciliation is applied and Knowledge Readiness is READY.
Eight original task manifests plus three R1, two R2, two R3, three R4 and two R5 repair manifests preserve all23original requirements and46cases. User authority
is recorded in execution-authorization.json and native E021. S01 foundation execution
is recorded in s01-evidence.json and actual S02 installation in s02-evidence.json.
Original acceptance cases remain open until full integration and independent review pass.

## Technical Context

**Language/Version**: Python 3.6-compatible installed stdlib tools; Foundry verification
uses its existing Python 3.11 environment. Bash/Git remain required development tools.
**Primary Dependencies**: current Loop engine/installer/evaluator. JSON-first portable
metadata/task loading; optional existing PyYAML adapter for legacy task YAML.
**Storage**: owner Markdown/frontmatter or legacy sidecar; derived JSON records/evidence/
indexes and immutable HTML bundles. Existing Project Store remains separate and unchanged.
**Testing**: stdlib unittest, existing pytest/Ruff/mypy/schema/Vault/pack/seal V2 profile,
isolated filesystem/Store canaries and isolated offline browser checks.
**Target Platform**: portable local development on supported POSIX hosts; product/host
prerequisites are explicit profiles. No actual RHEL7 or Python 3.6 host is available.
**Project Type**: CLI/library development tooling and static document generation.
**Performance Goals**: full declared scope with explicit completeness; no silent 40-item
cutoff. Configured resource limits cause incomplete/error, never an empty-success result.
**Constraints**: no network/LLM rendering, new runtime service, auth model, private profile
export, unapproved cleanup, Store rebind or implicit Git/host activation.
**Scale/Scope**: 23 assigned Astra requirements, 15 feature requirements, eight contract
acceptances. One complete generic baseline plus repository adapters and local canaries.

## Constitution Check

| Principle | Design disposition |
|---|---|
| Knowledge safety | Original source/ADR history retained; one metadata owner; no Vault promotion. Stale skill-direction claim is reconciled only under existing approved D-055. |
| Small verifiable change | Eight vertical slices with independent acceptance and recovery boundaries; reuse current controller/installer instead of a parallel stack. |
| Evidence-based completion | Real canaries and full V2 checks; host grammar != Python 3.6 execution, local synthetic != native host or production. |
| Governed mutation | Native activation is separate; actual installs/retirement/publish require their own scope. Real cleanup remains report-only. |
| Independent review | Contract, recovery and regression reviewers run sequentially on one frozen target; blockers close the gate. |

No principle exception is requested. Existing D-046/D-053/D-055/D-056 remain authoritative.
The proposed local extension does not alter Platform schemas or current API contracts.
Post-design check: these boundaries remain unchanged. Actual approval/activation evidence
must be recorded before the implementation gate; a design artifact is not that evidence.

## Project Structure

### Design Artifacts

- spec.md, plan.md, work-contract.json: requirement and execution boundaries.
- research.md, baseline-inventory.md, repository-audit.md, closed-work-discovery.json:
  verified current behavior and engineering choices.
- data-model.md and contracts/portable-operations.md: state, ownership and operation contracts.
- quickstart.md: existing commands and explicitly planned canary validation.
- acceptance-map.json: 23 original requirements and 46 positive/negative cases allocated
  to planned slices. This is traceability only; no runnable task or execution PASS.
- task-manifests/ and generated tasks.md: added by taskify; not handwritten.
- execution-authorization.json: user continuation authority for local implementation;
  D002 remains the earlier four-UI Git-init decision.
- knowledge-readiness/context/environment/trace: execution prerequisites and evidence.

### Implementation Owners

- tools/amplai-loop-kit: canonical source, generic baseline/profile payload, complete seal,
  existing transaction installer, exact mirrors, package selftests and documentation.
- scripts/loopctl.py, loopv2.py, amplai_docs.py, eval.sh: existing controller adapters and
  portable document engine. No second scheduler/controller.
- scripts/amplai.py and amplai_runtime.py: presentation-only heartbeat/handoff repair.
- .ai-team runtime/policy/contracts/knowledge/verifier assets: portable inputs and Foundry
  adapters; Foundry canonical decisions/Vault contents are not copied into payload.
- taskify validator and Spec-Kit task generator/templates: JSON-first portable path,
  retained optional legacy YAML, no reduction in existing validation.
- tests/ai: behavioral, canary, mutation and recovery evidence.
- README/docs/affected skill guides: current developer instructions and source ownership.
  pyproject.toml may change only the narrow managed-tool lint/type boundaries required
  by D-056, not product dependencies or quality requirements.

The complete finite source family inventory is in baseline-inventory.md. New public
interfaces remain internal command capabilities under work/design, not new agent entry points.

## Data Flow And State

1. A sealed package and explicit baseline/profile selection produce a complete in-memory
   install plan. Baseline, markers, JSON merges, hooks and symlink mirrors compose once
   per path. Previous ownership and local changes participate in the same transaction.
2. Owner source/one metadata owner plus authorized dependency snapshots produce records.
   Derived hashes/reviews stay outside the bytes they attest. Unknown input remains incomplete.
3. Full change set and explicit dependencies select impact. Each review binds the current
   document/metadata/dependency/candidate/profile snapshot; batch is atomic, not approval.
4. Security/release/lifecycle/freshness filters run before relevance/title/snippet creation.
   Approved reviewed sources generate deterministic audience views and immutable bundle
   manifests. Publish/latest activation is separate from render and validate.
5. Section preservation and complete reference/retention evidence produce retirement
   proposals. Only exact approval can exercise recoverable quarantine in isolated tests.
   Normal Work finishes with report-only gardening, not deletion.

## Compatibility And Migration

Preserve the extension-only installer default and every existing three-way recovery test.
The new baseline option does not silently opt existing apps in. Removing the option on a
later update must preserve installation ownership history. Existing app rules, settings,
hooks and Store bindings are either unchanged or conflict explicitly.

Use new-schema JSON frontmatter for new documents and explicit sidecar ownership for
legacy sources. Do not rewrite legacy documents wholesale. Reject simultaneous new
metadata owners. Old status metadata is mapped without promoting ACTIVE to verified.
A preservation ledger records every significant migrated section; unresolved material is
retained. Existing timestamp acknowledgement cannot waive current review evidence.

Review-derived freshness is computed; source and evidence hashes have no circular dependency.
A separate exact release set controls current and historical guides. No implicit newest
commit or semver range interpretation is needed: declared release IDs are exact.

Repair default heartbeat output only; runtime renewal/authentication and supervisor calls
keep their contract. DONE JSON reads already work. Add own-result rendering and terminal
next-step guidance instead of relaxing READY-only claim or inventing a new execution store.

## Failure And Recovery

Before any install/write, reject missing/extra seal entries, unsafe paths/mirrors, conflicting
local modifications and unavailable required inputs. Typed actions retain inverse operations.
Recheck target state before mutation and final receipt. Failures preserve original or verified
recovery bytes; interrupted jobs resume from validated journals, not guessed success.

Document scan failures, unsupported metadata, unavailable external dependencies and limits
produce explicit incomplete/error. No absent input becomes zero-impact PASS. Snapshot drift
invalidates pending review publication and retains the prior report. Bundle outputs are
exclusive or exact verified reuse; partial bundles cannot be selected or activated.

Security filtering happens before output creation. Error diagnostics reveal only authorized
identifiers or generic reason codes. External builders receive separate allowlisted input,
never internal checkout metadata. No threat model assumes a label or hidden HTML is isolation.

Retirement checks bind exact approval/HEAD/content/references/retention/recovery identity.
Unverified dynamic/external references and backup files are HOLD. Application tests use
recoverable quarantine and audit/tombstone records, not permanent material-file deletion.
Real tracked cleanup and approval creation remain outside this execution scope.

## Slice Strategy

| Slice | Observable result | Key acceptance | Dependency |
|---|---|---|---|
| S01 | A generic baseline's installed controller, policies, twelve skills and JSON task toolchain run without Foundry-private prerequisites; current Foundry profile still passes. | KIT-001, REG-001 | D-055 knowledge preparation |
| S02 | Opt-in baseline + extension install/update/uninstall safely compose typed actions, complete seals and recovery; local files/trust/Store bindings survive. | KIT-001, KIT-002 | S01 |
| S03 | One metadata owner produces complete records and current/history selection with qualified canonical uniqueness and pre-output privacy filters. | DOC-002 | S01 |
| S04 | Full change/rename/delete/doc-only/tool/ontology impact and exact single/batch review drive existing docs gate without stale or incomplete PASS. | DOC-001, DOC-002 | S03 |
| S05 | Reviewed sources produce deterministic offline audience HTML and exact release-set bundles; output drift/private/XSS/current-selection tests pass. | DOC-003 | S03, S04 |
| S06 | Section preservation, scoped report-only gardening and exact approved fixture retirement/recovery protect unique history and user backups. | DOC-004 | S04 |
| S07 | DONE views show own results/read-only guidance; heartbeat output is redacted without changing runtime or dependency execution semantics. | WORK-001 | S01 |
| S08 | Canonical payload/root capabilities, fresh canaries, docs and full V2 evidence converge; all three sequential reviews and security close the local Work. | all, REG-001 | S02–S07 |

Each manifest must trace its assigned original positive and negative Astra acceptance.
No task is done because a file exists. Fixture-only application/native-host limitations are
visible in the acceptance ledger, not hidden as skipped PASS. Runtime code is not implemented
during this planning phase.

## Integration Refinement (S08)

S07 passes 35 canonical and 63 runtime/federation tests. S08 now closes the complete
original matrix. Initial real-repository discovery rejected UNCLASSIFIED_DOCUMENT;
that rejection was not a freshness PASS. Existing SourceTree supports exact Markdown-file roots, so
reviewed operating guides can adopt adjacent metadata without a second index or a root-wide
scan. Keep mandatory impact/reference rules and protected approval semantics unchanged.
Correct obsolete guide command examples; derive attributed review evidence with the existing
review store. Do not promote unrelated legacy or Vault content.

S08 also executes the queued preservation-mode, same-path revision race and complete
REQUEST/SPEC/PLAN/memory-family probes. Any reproduced defect is repaired in the same
owned runtime and canonical payload with RED/GREEN evidence. No new cleanup authority.
See s08-scope-refinement.json for exact paths and existing E021 authority. Internal guide
package evidence is not a fabricated product deployment release; W004 owns that activation.

Real discovery also found historical work records and installable Markdown templates
being required as current owner guides. Add explicit, policy-hashed document_source_routes:
historical sources retain exact baseline bytes and provenance without current approval;
source assets route to a classified current owner whose snapshot binds their complete
Markdown membership and bytes. Mandatory impact_rules cannot be rerouted, missing owners
and unknown sources still fail, and security is checked before emitting role records.
This is a document-input role distinction, not a second metadata authority or an exemption
for changed/unpreserved history. External executable symlinks are not followed by reference
discovery; an explicit required external dependency remains unavailable.

Real publication exposed a self-reference: an optional `specs/` folder hint included the
impact report's own temporary file. Non-required directory expansion retains the directory
identity and non-generated members while excluding existing candidate-exclusion roles for
Work evidence and runtime transactions. Explicit file references and required directories
still bind input content/membership, including ignored Work inputs. A normal guide can now
publish and retain its attributed review without weakening required dependency checks.

A directory tree describing the review-store location also exposed the store's own atomic
publication stage as an input. Directory expansion excludes only the configured review
result and its transaction, even for structural/required directory references. Each review
is independently validated as evidence; unrelated and ignored directory members remain
bound, and an explicit file reference to the store remains content-bound. Custom store
locations follow the same rule. This does not exclude arbitrary files in a required folder.

The existing evaluator writes last.json, history.jsonl and verifier-last.json below
.specify/eval. These three exact generated records are evidence, not product source;
later verification must not invalidate a guide solely by replacing its prior result.
Unknown files in the same directory remain candidate inputs, and an explicitly required
reference to an evidence file remains content-bound. No broad directory ignore is added.

## Review R1 Repair Refinement

R1 independent review returned three deduplicated blockers; see [review-r1.json](review-r1.json).
The exact R1 source archive and all earlier evidence remain historical, not current PASS.
This refinement restores existing KIT-001, DOC-001 and DOC-002 behavior under E021.
No requirement, governing Decision body, authentication or deployment authority changes.

| Slice | Bounded repair | Verification boundary |
|---|---|---|
| S09 | Preserve explicitly classified governing instructions and active indexed Decisions independently of release-specific guide selection. | Exact installed baseline required inputs; real index-shaped historical ledger; missing/forged Context records and policy/source drift rejected. |
| S10 | Apply reference security before common consumer relevance and snippet generation. | All seven consumers, current/history and actual CLI; encoded links, lower-security output and legitimate same-security historical references. |
| S11 | Reject oversized generated JSON before publication. | Exact serialized bytes checked against each reader budget; prior review store/impact projection remains readable after rejection and retry. |

Policy-declared governing inputs use exact relative paths, explicit instruction or
decision-ledger roles and security. They are control inputs, not newly approved release
guides. Required instructions cannot disappear through relevance limits. A ledger supplies
only active, applicable index entries or exact typed claim references; it cannot enter the
current guide/source list wholesale. Context provenance binds policy, index and source bytes;
validation compares the complete expected governing/source/claim sets, not only listed items.
Unavailable required authority blocks readiness with a non-disclosing error.

The common reference guard normalizes local link destinations before checking declared
security. A known higher-security target blocks disclosure before titles, body snippets or
ranking. Unclassified local targets are not treated as public. Explicit same-security
historical/control source classifications can authorize reference visibility without
promoting their content as current instructions. HTML retains its stricter output-bundle
membership rule. No restricted identifier is included in rejection messages.

Generated-record writes serialize once and check that exact byte count before creating a
pending file. The review store uses its fixed reader budget; impact projections use the
configured reader budget. Oversized replacement is incomplete, never a committed unreadable
success. Existing source-drift checks, atomic replacement and recoverable retirement remain.

S09, S10 and S11 execute sequentially because they share the canonical engine and payload.
Affected completed S01-S06 become needs_revalidation and retain their previous completion
evidence. S08 remains open and depends on all repairs plus fresh prior-slice revalidation.
New regressions first fail on the retained implementation, then pass on the repair. Full V2,
fresh guide evidence, security and all three independent lenses run again on a new R2 freeze.

## Review R2 Repair Refinement

R2 completed all three sequential independent lenses on one unchanged 303-file target.
Two P1 findings and one Blocking-P2 keep the implementation gate closed; see
[review-r2.json](review-r2.json). The R2 exact archive, tests and reviews remain historical.
This refinement implements existing security, dependency freshness and state-preservation
requirements under E021. No new product requirement, parser dependency or activation authority.

| Slice | Bounded repair | Verification boundary |
|---|---|---|
| S12 | Share a bounded full-document reference tokenizer and preserve exact decoded URI target identity. | Whitespace and multiline Markdown/HTML, nested/escaped destinations, reference definitions, all consumers/current/history, actual CLI/internal HTML, installed payload and exact dependency drift. |
| S13 | Preserve the validated atomic-record mode and reject concurrent permission/identity drift. | Restrictive umask, late chmod, same-bytes inode replacement, public review/impact operations, cleanup and retry; retain fixed byte budgets and private new-file defaults. |

Security discovery scans reference-bearing text before relevance/output, including literal
examples; dependency extraction retains its explicit inline-code, fence and structural-tree
rules. One tokenizer records destination syntax, offset and line number. It handles bounded
balanced/escaped destinations, angle paths, optional titles, multiline definitions and
quote-aware HTML attributes. Unsupported ambiguous reference syntax fails closed without
printing identifiers. This does not claim full CommonMark rendering or authorize external HTML.
Markdown escapes/entities and HTML attributes normalize according to their syntax exactly
once. URI fragments are separated before percent decoding; literal decoded `#`, semicolon
and whitespace remain filename characters. Prose/editor `:line` cleanup does not process
URI paths. All classification, dependency and rendering consumers bind the same target.

Atomic publication captures regular-file identity and permission metadata before staging,
sets the intended mode on the temporary descriptor independently of umask, and rechecks
metadata plus expected bytes immediately before publication. A concurrent restriction or
same-bytes replacement rejects publication and retains the current target; it is never
silently undone. This adds no live-server permissions or service-identity management.

S12/S13 run sequentially on the preserved R2 implementation. Each begins with RED evidence.
S01-S06 and S09-S11 retain prior completion history and require revalidation on the repair.
S07 presentation behavior is unchanged and remains covered by full integration regression.
S08 stays open for fresh canonical tests, Context, guide/review/HTML evidence, security,
convergence and all three independent R3 lenses. No original case or task is removed.

## Review R3 Repair Refinement

The three sequential R3 lenses finished against an unchanged 378-file target. Two
independent P1 findings and a confirmed main file-URI P1 keep the gate closed; see
[review-r3.json](review-r3.json). Root diagnostics also reproduce the same allocation
defect in typed inline-code references. Prior results and the frozen archive remain
historical. Replan the parser boundary instead of repeatedly adding isolated cases.

| Slice | Existing requirement repair | Acceptance boundary |
|---|---|---|
| S14 | Charge retained reference strings and scan work before allocation in both the shared lexer and typed extractor. | Bounded peak memory or SCOPE_INCOMPLETE, long single lines, code spans, source/payload, real selector and installed CLI; preserve required-dependency context and prior atomic guards. |
| S15 | Declare a finite shared HTML reference grammar and reject unsupported local-file schemes. | Direct URL attributes and namespace tags share exact identity/classification; compound or embedded-language references fail non-disclosingly before output; all consumers/history/CLI/HTML/dependency/installed paths and safe controls. |

S14 retains existing prefix/context semantics within an aggregate input-proportional
budget. It checks limits before copying prefixes, labels or expanded structural paths.
Token limits apply to typed output as well as lexer output. No silent truncation or
empty-success fallback is allowed. A limit failure is not a resolved review.

S15 supports a finite set of single-destination HTML URL attributes, including href,
src, poster, data, action, formaction, cite, longdesc, background and xlink:href.
Namespace tags use the same attribute parser. Do not infer multi-URL boundaries with
comma splitting: srcset/imagesrcset, URL lists, CSS/SVG functional references, srcdoc,
refresh and active embedded-language containers are explicitly unsupported and reject
before output. Unknown non-inert attributes also reject rather than acquiring implicit
reference permission. Ordinary inert markup/placeholder and supported same-security
links remain controls. The renderer still escapes raw markup; escaping alone is not
identifier access control. File URIs, including localhost and relative forms, reject
without reading or exposing an out-of-repository path. The non-local URI subset is
HTTP/HTTPS with explicit authority, protocol-relative URLs and mailto. Executable/data
schemes and unknown schemes reject rather than hiding an embedded reference language.
Inert data-/aria- attributes remain opaque author text, not an arbitrary prose DLP claim.
This is not a browser parser,
arbitrary prose DLP or a new external parser dependency.

S14 -> S15 run under main ownership. S12 -> S13 -> S09 -> S10 -> S11 and S01-S06
then revalidate with completion history preserved. S07 remains covered by full S08.
Current guide reviews, full 20-source Context, immutable R4 HTML, canonical S08 and
all three fresh sequential reviewers are mandatory. The four excluded projection
membership additions are refreshed after review; compare them normally, never waive
DERIVED_REPORT_DRIFT. No original acceptance, environment boundary or approval changes.

Actual repository integration found the impact-only discovery parses Work report examples
before applying its already configured Work-memory exclusion. Move that existing selection
before back-reference parsing through an explicit caller filter. Direct discovery remains
strict by default; current guides, unknown roles and normal reference validation do not gain
an exemption. Preserve the R3 reports byte-for-byte. This repairs ordering in the existing
document-role model, not a new semantic exclusion or relaxation of the finite grammar.
The same real pass encountered a retained Vault source with a long inline-code paragraph.
Back-reference candidate discovery does not need retained declaration prefixes: use a lean
bounded extraction mode and conservatively consider inline-code local candidates, including
relative extensionless names. Exact required-dependency classification and public source
validation keep their original full context and limits. No Vault file or scan root is removed.
Legacy Python fnmatch can reject malformed character ranges in ordinary inline notation.
Candidate discovery retains exact literal matches and treats such invalid globs as having
no wildcard matches. Exact required-dependency validation is unchanged; synthetic legacy
behavior and the available Python 3.9 interpreter are both checked, not claimed as RHEL7 proof.
The retained source also includes an opaque conversation URI with an authority. Candidate
discovery may classify unknown authority-bearing schemes as unavailable external pointers,
without fetching them or interpreting them as paths; file/data/executable schemes remain
rejected. This is not permission to select or publish that source. Exact dependency and
all current/history visibility consumers still reject unknown schemes. Preserve ordinary
local links alongside the opaque origin and verify the real source bytes remain unchanged.

## Review R4 Repair Refinement

R4의 세 순차 검토는 같은 540-file target을 검토했다.
`review-r4.json`의 P1 두 건과 Blocking-P2 한 건으로 completion gate는 닫힌다.
기존 2229-test PASS와 원본 검토 기록은 유지한다. 새 negative control의 PASS로 재사용하지 않는다.

| Slice | Existing Requirement Repair | Acceptance Boundary |
|---|---|---|
| S16 | Dependency declaration의 classification work를 유한하게 제한한다. | Header와 separator suffix를 분리해 반복 backtracking을 제거한다. Source/payload/installed command의 짧은 near-match, negation, multi-reference list와 aggregate work budget을 검증한다. |
| S17 | Typed local reference도 출력 전 security classification에 연결한다. | Required inline code, tree, directory/glob, 현재 존재하는 optional local target과 alias를 같은 bounded resolution으로 확인한다. Current/history/CLI/view의 private marker 차단과 same-security 대조군을 검증한다. |
| S18 | Borrowed baseline file의 POSIX mode를 재설치와 제거에서 보존한다. | Raw/composed file의 최초 설치, 동일 package 재설치, 갱신, conflict, 제거에서 bytes/mode/receipt를 확인한다. Installer-owned file의 명시적 package mode 갱신은 구분한다. |

S16은 기존 declaration 의미를 유지한다. Prefix를 복사하거나 classification하기 전에
해당 document의 유한 work budget에 반영한다. Header 탐색과 겹치지 않는 suffix 검사는
입력 길이에 비례한다. Count/file 한도를 늘리거나 timeout을 정상 결과로 바꾸지 않는다.

S17은 기존 literal/code Markdown-URI 검사를 교체하지 않는다. 그 검사와 typed local
reference의 합집합을 확인한다. URI의 한 번 decoding, 정확한 punctuation, required
dependency, directory/glob membership과 optional-reference review 의미를 유지한다.
현재 존재하는 typed local target은 ignore/known-absent 설정으로 security 검사를 건너뛰지 않는다.
Logical alias와 실제 repository target의 classification을 함께 확인한다.
외부 경로를 읽거나 unclassified target을 기본 허용하지 않는다.
현재 guide가 가리키는 실제 source의 classification이 없으면 기존 metadata model에서
그 source의 owner/security를 명시하고 다시 검토한다. 새 blanket exception은 추가하지 않는다.
Typed code/tree 표기를 새 hyperlink나 release 문서로 승격하지 않는다.
실제 navigable Markdown link의 기존 bundle-membership 검사는 유지한다.

S17 real-guide 대조군은 파일 입력이 아닌 기존 표기도 구분한다. 현재 guide와
`AGENTS.md`/installation 규약의 `/work`, `/design`, `/hooks`는 command 표기다.
Guide의 `/tmp`는 사용하지 말라고 설명하는 표준 임시 위치 이름이다. 이 네 개의
정확한 bare inline-code literal은 implicit file dependency로 분류하지 않는다.
Raw 표기가 정확히 같아야 하며 prefix/path subtree allowlist는 아니다. `Requires`로
명시한 값, Markdown URI, tree, 하위 경로, 다른 absolute/unknown path는 이 규칙을
사용하지 못한다. Guide 원문이나 보안 등급을 바꾸어 통과시키지 않는다.
추가 테스트는 실제 guide 검사에서 나온 이 false positive와 기존 차단 경계를 함께 검증한다.
실제 guide의 출력 전 reference 검사는 S17의 executable command다. S18 뒤 package가
고정된 시점의 current source review/release/HTML은 T008의 명시 acceptance로 유지한다.
이 phase 분리는 최종 gate를 줄이지 않으며 S17의 사전 검사를 최종 view 승인으로 보지 않는다.

S18은 기존 transaction을 재사용한다. Borrowed file의 검증된 local mode와 receipt를
유지한다. Local drift/conflict, force의 기존 권한 범위, rollback과 ownership 구분을
바꾸지 않는다. 실제 앱이나 host에 설치하지 않는다.

S16 -> S17 -> S18은 main이 순차 실행한다. S14/S15/S12/S13/S09/S10/S11 및 S01-S06은
`needs_revalidation`으로 전환하고 이전 completion evidence를 보존한다.
모든 기존 manifest command와 full S08을 새 source에서 실행한다.
S07은 full integration으로 확인한다. 원본 23 requirements/46 cases와 네 미검증 한계는 유지한다.
최신 Context, source guide review, immutable R5 HTML, browser 검증 후 세 독립 R5 lens를
다시 순차 실행한다. Report-only post-eval delta에는 plan뿐 아니라 analysis 갱신도 명시한다.
변경된 모든 output은 재검증하며 DERIVED_REPORT_DRIFT를 면제하지 않는다.

S14의 첫 R5 재검증에서 기존 positive review fixture의 미분류 source를 발견했다.
8개 실패는 같은 CROSS_BOUNDARY_REFERENCE를 재현했다. T008 convergence 범위에서
synthetic src/api.py와 해당 ignored-input fixture만 INTERNAL로 명시한다. 기존 freshness,
review evidence, mode 및 ignored-membership assertion은 유지한다. 미분류·RESTRICTED 대조군을
추가하고 product engine/payload는 바꾸지 않는다. 실패 원문은 tool truncation을 명시한
canonical excerpt와 완전한 focused reproduction으로 보존한다. 새 source pin으로 S14부터
13개 slice를 다시 실행한다. Current guide의 R5 설명과 release selector를 이 재검증 전에
고정하고, R4 archive/review/release/HTML은 유지한다. 최종 source review와 HTML은 여전히 T008이다.

R5 real Context 검사에서 release scope mapping 오류도 확인했다. R5 Kit candidate를
기존 Foundry product/governing guide 21개에 함께 부여해, 해당 제품의 미분류 원문과
namespace/command 표기까지 같은 release-wide selection에 들어갔다. 원래 승인한
private Foundry 독립성(FR-001)과 release별 선택(FR-010/015)에 맞게 R5 Kit membership은
PORTABLE-DEVELOPMENT 및 Kit README/CHANGELOG 3개로 한정한다. 나머지 21개의 이번 R5
enrollment만 정확한 이전 membership으로 되돌린다. 기존 원문·보안·authority·lifecycle,
archived bridge와 R4 artifact는 변경하지 않는다. Governing input은 원래 구현대로
release-independent이며 AGENTS/CLAUDE/runtime instruction과 active Decision을 유지한다.
Vault/Proposal/product source를 새로 INTERNAL로 분류하거나 guard/grammar를 바꾸지 않는다.

T008은 verify-kit-guide-selection.py로 실제 3-guide selection, required instruction 유지,
retained product reference의 차단을 검사한다. 모든 25개 mandatory source review는 여전히
필요하다. 이전 13 canonical revalidation의 151-file pin은 원본 그대로 보존하고, 현재 full
S08이 동일 product/package/test source에 21개 membership delta와 새 executable check를
추가해 검증한다. 이전 snapshot을 최신 source라고 부르거나 합계 test 수를 재사용하지 않는다.

## Review R5 Repair Refinement

S20 canonical acceptance passed with 295 tests, strict seal and real-guide reference
classification; the retained input pins are unchanged. S19's already-planned freshness
preparation names the finite paths explicitly: current release/offline-view identities
in documentation policy, exactly three owning guide sidecars, attributed canonical
review-store writes and the existing Kit checksum seal. Existing roots, source routes,
security labels and approval boundaries do not change. R6 HTML generation remains S08.

The three R5 lenses checked the same 664-file target. Their raw reports, exact archive
and main RED probes are retained by review-r5.json. The regression lens passed its
bounded checks; contract and recovery findings keep the completion gate closed.
No original requirement, security label, environment claim or E021 authority changes.

S20 repairs W003-R5-02 first. A disclosure consumer must distinguish unsafe resolution
from ordinary optional absence before choosing an alternative candidate. Root-relative
and document-relative candidates cannot hide an escaping alias by discarding it.
Bounded metadata checks cover leaf and parent aliases, dangling targets, cycles,
access failures and special files. Every candidate prefix consumes the existing finite
work budget; no outside content is read. Genuine absent examples, valid in-repository
aliases and absent children under valid aliases retain their established behavior.
Other dependency/impact consumers keep their existing missing-reference semantics.
Source, canonical payload, installed CLI, current/history and view-input tests share
the same denial boundary. Existing URI/tree/glob/classification checks remain.

S19 repairs W003-R5-01. Register exactly three owning Kit guides in the existing
repository knowledge map with unique active source IDs and normal priority. Preserve
all24previous entries. Current release/security/freshness filters still decide eligibility;
guides do not become mandatory governing instructions. The verifier checks the actual
context_records result and stored required_knowledge, exact source hashes for the three
guides and all seven instructions, and real context_validate. Selector-only success,
missing mappings/records and self-consistently forged Context must fail the new controls.
The other21product guide memberships and retained product-reference denial are unchanged.

The primary sequence is S20 -> S19 -> fourteen affected revalidations:
S17, S14, S15, S12, S13, S09, S10, S11, S01, S02, S03, S04, S05, S06.
Their old completion records remain; needs_revalidation does not erase evidence.
S16 classification and S18 installer code are unchanged; their existing regression tests
also run in the affected commands and full integration. S07 is checked by full S08.
Each new named harness is an explicit task deliverable. Task manifests remain the source
of generated tasks.md and preserve all original commands and case IDs.

After current source review and actual Context rebuild, run full S08 on one pinned
source. Refresh all25mandatory attributed source reviews, retain R5 artifacts and build
new immutable R6 developer/operator HTML with offline browser checks. Run all three
fresh independent R6 lenses sequentially. Changed post-eval reports are explicitly
identified and validated; derived-report drift is never waived. No RHEL7, Python3.6,
full native Work/resume, signing, fleet or actual deployment PASS is inferred.

## Verification

Existing canonical full command: .venv/bin/python .ai-team/verifiers/run.py --profile v2.
S01 includes an existing harness repair: the current Foundry task generator emits only
Phase sections, but eval.sh consumes Slice/Eval sections. The task index declares those
slices; reuse the existing generic Synapse renderer and preserve a failing-before check.
This is a generation-tool defect within S01, not permission to hand-edit generated tasks.
Every slice has a generated tasks.md Eval entry, targeted unittest/pytest tests and retained
input/output hashes. New harnesses are explicit slice deliverables, not invented existing
commands. Bug fixes require failing-before and passing-after evidence.

Required matrices include:

- Empty repo and existing local-edit canaries; missing dependency/seal extras; baseline-plus-
  marker same-path composition; exact symlinks; repeat/profile-omitted update; fault rollback.
- 41+ changes; staged/unstaged/new/rename/delete; doc-only metadata/contract; new build tools;
  ontology/binding pins; unavailable reads/parser/limits; current-review and stale-batch guards.
- Same topic across releases/security scopes; duplicate active canonical; all unknown/obsolete
  states across each portable consumer; title/snippet/index/error/private-link canaries.
- Determinism, output tamper, offline/no-script browser, keyboard/mobile/print, malicious raw
  source/title/code, explicit verified latest selection and audience-specific content.
- Unique section/constraint/incident preservation; retention/foreign references/backups;
  changed approval/HEAD/content; quarantine failure/recovery; real report-only garden.
- Isolated authoritative Work dependency lifecycle and DONE reads; own result display;
  credential-free default output with functional renewal and completion.

After verification: semantic convergence, current documentation, a frozen three-lens
sequential review, incremental report-only gardening, fresh security and native result.
No code changes during a review freeze. A new fix requires a new target and all three lenses.

## Execution And Evidence Limits

No Python 3.6 interpreter has been observed. Current native transport-only observations
are in s08-native-r6.json: both current-package installed host adapters returned the exact constant
response with tools and persistence restricted. This does not verify a complete native
Work/resume lifecycle or authorize fleet activation. Record syntax, local lifecycle
canaries and native transport separately; obtain applicable runtime evidence before
actual host/fleet activation. No auto_start, broad host command or trust bypass is used.

No RHEL7 test host exists. W004 owns target fingerprint, packaging/activation/rollback
and the separately approved actual-deployment evidence. Local Work completion never
means the complete Synapse split is production verified. The final HTML must distinguish
implemented local capability, missing target proof and unperformed publication/deployment.

## Complexity Tracking

No new platform, database, scheduler, RAG/MCP/ontology runtime or permanent cleanup service.
The new document module is a consumed extension of existing CLI/Loop gates. Existing
transaction and verifier engines remain the control points.
