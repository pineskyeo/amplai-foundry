# Design Research

Status: repository evidence and design decisions. D-055 index reconciliation is now
applied; user continuation authority is native E021. No product implementation, live
installation, publication or deployment is claimed by this research record.

## Baseline And Scope

The unchanged Foundry V2 baseline passed 11 checks: 1,625 tests passed and four live
Slack cases were deselected. The host was Darwin arm64/Python 3.11.15. This is neither
Python 3.6 nor native Claude/Codex/RHEL7 execution evidence.

The Astra archive is a design input, not executable authority. Its SHA256 is
`3c6640083e2113f92d069a787242a15f63bfba81759901a5ae45a3e417195b27`.
Design chapters 03, 05, 06, 07 and 14, document/profile schemas and the assigned 23
requirements were compared with this repository. Proposed interface names are mapped
to existing mechanisms; no additional platform or semantic runtime is introduced.

## R01 — Complete Opt-In Baseline

Decision: extend the canonical `tools/amplai-loop-kit` with an explicit generic
baseline profile, consumed by the existing transaction installer. Preserve extension-only
installation by default. Compose baseline bytes and async marker/JSON additions into
one final action per path before applying anything.

Evidence: `install.py:358` requires existing work/design/WORKFLOW; `add_action:383`
rejects duplicate paths; `plan:860`, `backup_and_apply:875`, `rollback_applied:922`
already implement planning and recovery. The current selftest creates stub baseline
files, so it cannot prove a fresh repository can run the development workflow.

The complete baseline includes controller/engine/evaluator/verifier, all required
policy/schema/index seeds, twelve generic development capabilities, task validator and
generator, their referenced assets, and the three required Spec-Kit shell helpers.
`decisions.index.json` is an actual context dependency even though doctor omits it.
The inventory and exact consumed paths are in `baseline-inventory.md`.

Rejected: a second installer or a second development controller; stub skill markers;
copying Foundry's entire `.ai-team`, Vault, work history, approval ledger or host config.

## R02 — Portable Profile, Not Private Defaults

Decision: generic seed policy and a repository-owned profile describe source roots,
documentation roots, additional protected/dynamic paths and actual verification commands.
Foundry retains its current full registry and six optional skills. A fresh repository
uses the twelve required development capabilities and a non-empty portable verifier.
Product-specific tests are declared by the repository; generic checks never certify them.

Evidence: `loopctl.py:431`/`:513` enforce the Foundry 18-skill set; the verifier registry
calls `.venv`, Vault and a specific historical feature. `loopv2.py:833` embeds Foundry
environment assumptions. These are adapters, not generic engine dependencies.

Rejected: weakening doctor globally, giving empty profiles PASS, copying Cortex
dynamic-entry paths or changing Claude/Codex trust/sandbox/approval defaults.

## R03 — Python And Manifest Compatibility

Decision: retain Python 3.6-compatible standard-library runtime spelling in installed
Kit tools. Make task manifest loading JSON-first while keeping optional installed
PyYAML support for legacy YAML. The portable seed uses JSON objects in `.yaml` files;
the existing generated-task marker, Slice and Eval contracts remain unchanged.
Convert the existing validator's annotations without removing any validation rules.

Evidence: the current validator requires future annotations and built-in generic type
annotations; generator and validator unconditionally require PyYAML. The rest of the
examined runtime passes the host AST's 3.6 grammar check. Actual Python 3.6 is absent.
The root supervisor has newer syntax than its portable payload and must not overwrite it.

Rejected: vendoring a new YAML framework, accepting malformed input as an empty task
set, upgrading installed tools silently to Foundry's Python 3.11 requirement.
Grammar checks and isolated current-host execution are separate evidence dimensions;
unavailable old-interpreter/native-host smoke remains UNVERIFIED, never PASS.

## R04 — Metadata And Evidence Have One Owner

Decision: new document source carries an `amplai_document` JSON frontmatter object.
JSON is a deliberately supported YAML subset and needs no parser dependency. Legacy
documents use one explicit JSON sidecar owner while preserving their original bytes.
Old unmanaged frontmatter is historical source content, not a second current metadata
record. A source with both new frontmatter and a sidecar fails with duplicate authority.
Unsupported frontmatter is reported as unprocessed; it is not silently guessed.

Authored metadata contains identity/topic/owner/kind/authority/lifecycle/security,
release applicability, dependency selectors, retention and replacement relationships.
Full source hashes, dependency snapshots, review evidence and effective freshness are
derived from source plus external review records. They are not inserted into the source
whose full hash they attest; this avoids a self-referential hash and review-edit cycle.
Generated records adapt the proposed Astra schema; this is a local extension, not a
change to Platform/Vault schemas or their write APIs.

Rejected: manually synchronized registry and frontmatter; dates as freshness; a hand-edited
HTML copy; a source embedding its own full-file hash. Existing status values retain a
migration map, but legacy ACTIVE alone cannot create verified freshness.

## R05 — Complete Change And Review Snapshots

Decision: reuse the proven generic Synapse impact/review mechanism, adapting only
repository policy and dependency resolvers. Include staged/unstaged/new/rename/delete,
document-only semantic changes, build tools, schema/templates and configured ontology
pins/bindings. New source patterns must be classified or recorded as unprocessed.
Full scope membership, code/document/metadata/contract/policy hashes and exact candidate
revision form the snapshot. Semantic review remains attributed evidence, not human approval.

Evidence: Foundry `docs_referencing:1222` limits discovery to 40; current acknowledgement
and touched-file logic does not bind a semantic review. Synapse's automatic policy also
misses its newly added build-tool guide, requiring a supplemental exact-hash review.
The portable design closes that discovery gap instead of silently inheriting it.

Rejected: another copied controller; automatic batch approval; dropping unreadable,
large or inaccessible required dependencies and then reporting zero impact.

## R06 — Selection Before Output

Decision: the same resolver serves portable inventory/index/query, documentation bundles
and Loop context. Filter security, exact release membership, lifecycle and effective
freshness before ranking, titles, snippets or output creation. Canonical uniqueness is
qualified by topic, release and disclosure scope. Explicit history views preserve the
same security checks. Unknown input cannot become current active knowledge.

The default is INTERNAL and current-only. A disclosure receipt must bind an exact
allowlisted input bundle for external output. External builders receive only that bundle,
not an internal checkout hidden by a UI filter. No external receipt or publication is
created by this Work. The Foundry Vault and Platform search APIs remain separate owners;
this Work does not change their storage, promotion or authentication contracts.

Evidence: `selected_sources:523` ranks before complete lifecycle filtering;
`active_claims:561` excludes only three states. Existing Platform/Vault governance is
explicitly outside the portable Kit under D-046/D-055.

## R07 — Deterministic Human View

R3 refinement: the HTML attribute index and SVG URL reference rules contain destinations
beyond href/src. A partial comma-splitting srcset/CSS parser is not an adequate classifier.
Use a finite single-URL/inert-attribute subset and explicitly reject compound or embedded
languages. Source: https://html.spec.whatwg.org/multipage/indices.html#attributes-3 and
https://www.w3.org/TR/SVG2/linking.html#URLReference. The srcset parsing algorithm and
CSS URL tokenization require context and escape handling, not naive delimiter splitting:
https://html.spec.whatwg.org/multipage/images.html#parsing-a-srcset-attribute and
https://www.w3.org/TR/css-syntax-3/#consume-url-token.

The exact local file probe proved file:/ and file://localhost/ name the restricted fixture
while the old selector emitted its identifier. Reject local-file and opaque executable/data
schemes; support ordinary HTTP/HTTPS authority URLs, protocol-relative URLs and mailto.
This is a finite tooling language, not full web-platform support. Bounded output retention
also needs preallocation accounting: loop iteration limits alone did not cover copied
prefixes in either lexical or typed reference extraction. See review-r3-main-diagnostic.json.

Decision: render the reviewed Markdown subset with escaped raw HTML and strict local
links into self-contained, script-optional HTML. No network fetch, model rewriting or
CDN assets. Reader/developer/operator/leader views select reviewed sections from one
source, not separately authored summaries. Source/profile/release hashes identify the
bundle; existing output is verified or rejected, never overwritten silently.

Latest is an explicit pointer to a verified release set, not the highest commit/date.
Validation and rendering do not publish or activate. Release manifests bind UI/backend/
contracts/ontology/Kit pins. W004 owns actual deployment assembly and activation.

## R08 — Preservation And Recoverable Retirement

Decision: section preservation records precede split/merge/archive proposals. Ordinary
gardening stays scoped and report-only. Backups, tracked source, decisions and incident
evidence are protected. A retirement operation requires exact plan/approval/content/HEAD/
reference/retention/recovery bindings and rechecks them immediately before application.

Exercise recoverable quarantine, journal, interrupted-operation recovery and tombstones
only in disposable fixtures. Do not permanently delete material files or run repository
cleanup. Unavailable external/dynamic references are HOLD, not proof of no consumers.
No new general canonical-Vault mutation API is created.

## R09 — Completed Work Is A Readable Projection

Decision: preserve the existing read and READY-only execution contracts. Make Markdown
handoff include the Work's own result and choose terminal-state instructions. Redact a
copy of heartbeat CLI output; retain runtime renewal, token authentication and stored lease.

An independent isolated fixture executed DRAFT→READY→CLAIMED→RUNNING→DONE. Runtime and
CLI JSON/Markdown/handoff/show all read DONE, without Store content or mtime changes.
Markdown omits its own result and always instructs updating the same Work. Default
heartbeat stdout includes a lease token; claim stdout already redacts its credential.
Nine existing related tests pass. See `closed-work-discovery.json` for attributed evidence.
Native E002's original failure cause remains unproven; do not label the read API broken.

## R10 — Existing Knowledge Conflict

`claims.jsonl:5` still says `.claude/skills` is canonical. Approved D-055 and current
AGENTS/CLAUDE say `.agents/skills`, with exact Claude symlink mirrors. D-055 also has
a committed approval record. This is stale index metadata, not an unresolved architecture
decision. Preserve its statement/provenance and mark that claim superseded by D-055 before
building the implementation Context Pack. Do not create a new canonical claim or rewrite
the old Decision. This preparation is recorded in knowledge-reconciliation.json and
Knowledge Readiness is now READY.

## R11 — R5 Existing-Contract Repairs

Decision: preserve unsafe candidate status at the disclosure boundary and verify actual
Context delivery, not only upstream selection. R5 review and independent main probes
demonstrate both defects; see review-r5.json and its retained RED logs.

The source/payload probes distinguish escaping, dangling and cyclic leaf/parent aliases
from valid aliases and ordinary absence. Resolve these through bounded metadata checks
without outside content reads or new security exceptions. Silent unsafe-candidate
discard and blanket rejection of all optional examples are both rejected alternatives.

The Kit's three guides exist in current document selection but not in the existing
knowledge map, so generated and stored Context omit them. Register the exact owning
sources and check both delivered sets plus current source hashes and real validation.
Do not create another selector, treat guides as instructions or import private product
sources. These findings need no new business/schema/host decision.

## Remaining Gates

Technical boundaries and executable scenarios are defined. Before implementation:
complete the plan/contracts/task manifests/analyze gate, record the narrow knowledge
index reconciliation, capture READY context/environment and activate the existing native
Work under the original local-development authority. Native host/old-Python/target
evidence limitations remain explicit and never become synthetic PASS.
