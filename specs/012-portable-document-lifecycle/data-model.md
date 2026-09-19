# Data Model

These are portable tooling records, not Platform/Vault entities or database migrations.
All digest fields use SHA256 over explicit canonical JSON or exact file bytes. Timestamps
describe observation; they are not evidence of freshness or approval.

## Package And Install Record

Package identity includes Kit version, complete sealed inventory, typed payload entries,
fragment/profile versions and required engine capabilities. Entries distinguish regular
files, exact relative skill mirrors and additive owned fragments.

Installation records retain previous base bytes/digests, selected baseline/profile,
per-path ownership and final installed digest, created paths, transaction journal and
recovery evidence. Profile omission during update cannot discard prior ownership.
One path has one composed action. Trust/settings/Store-binding fields not owned by the
transaction remain byte-preserved. An interrupted transaction is not installed success.

## Repository Profile

An explicit profile declares repository identity, permitted source/document roots,
required and optional skills, non-empty verifier commands, protected/dynamic entry paths,
dependency adapters and disclosure policy. Engine requirements cannot be removed by a
profile. A generic seed does not reference Foundry, Synapse, Cortex, Vault, private
provider identities, host credentials or a historical feature.

## Authored Document Metadata

Each source has one metadata owner: new `amplai_document` JSON frontmatter or a legacy
JSON sidecar. Both at once are an error. The source locator includes repository and
relative path; traversal, alias collisions and unvalidated symlinks are rejected.

Authored fields:

- Qualified `doc_id`, qualified `topic_id`, title, owner and kind.
- Authority: canonical/supporting/historical. Lifecycle: draft/active/deprecated/superseded/archived.
- Security: PUBLIC/EXTERNAL_APPROVED/INTERNAL/RESTRICTED; absent classification defaults INTERNAL.
- Exact applicable release IDs and audience/section selectors.
- Explicit dependency locators or configured contract/ontology/binding identifiers.
- Retention policy/hold/retain-until, replacement relation and example marker.

Unknown identity/state remains unprocessed. A superseded record requires a replacement
locator or an explicitly reviewed historical disposition. Old unmanaged frontmatter is
preserved as historical source content and is not another active metadata owner.

## Derived Document Record And Review

The generated record adds full source-byte digest, candidate revision/tree fingerprint,
resolved dependency records and effective freshness. Freshness is independent of authority
and lifecycle: canonical+stale is valid and excluded from current instructions.

A separate review binds document digest, metadata digest, dependency snapshot, candidate
revision, rule/profile version, outcome, reason, reviewer, method and evidence locators.
No source embeds its own full-file digest or the review digest that would change that file.
Legacy ACTIVE maps to active lifecycle with unknown freshness until exact evidence exists;
STALE remains stale; CANDIDATE maps to draft/unknown; deprecated/superseded remain historical.
An attributed reviewer string is not authenticated human authorization.

Current selection requires allowed security, exact release applicability, active lifecycle
and verified freshness. Canonical uniqueness key is `(topic_id, release_id, disclosure_scope)`.
The same topic may have separate valid versions. Explicit history selection does not bypass
security or permit a stale historical record to become current guidance.

## Change Set And Completeness

The change set records baseline/candidate identity, staged/unstaged/new/deleted paths,
rename source/destination, submodule/contract/ontology/binding pin changes and source hashes.
Read failures are errors, never empty diff. Documentation-only metadata/contract changes
and newly introduced tool paths participate in impact analysis.

Completeness records expected members, processed members, policy-excluded members with
reason/rule digest and unavailable/unprocessed members. The validator computes set equality
and rejects duplicates, missing members or arbitrary scan-limit exclusions. A declared cap
can stop work but cannot certify completeness. External dependencies require authorized
local snapshots; the resolver does not silently fetch another repository or provider.

Impact items retain why they were selected, exact dependencies and proposed disposition.
Review-required is not automatically stale; known contradictions can be stale. Batch review
is an atomic submission of individually justified records against one unchanged snapshot.

## Release Set And View Bundle

A verified release set declares UI/backend/contracts/ontology/Kit revisions and digests,
verification evidence, applicable document set, audience/security policy and source profile.
A dirty or newer source cannot replace that selection automatically.

A view bundle has deterministic input identity, output file inventory/hashes, source/review
references, effective classification and complete=true only after all required checks.
Output generation and publication are separate states. A latest pointer may name only a
validated selected bundle. W003 tests pointer validation in disposable fixtures; W004 owns
real assembly and activation. Private filenames, titles and snippets cannot enter an
external manifest or index merely because their HTML body is hidden.

## Preservation And Retirement

Preservation records map original revision/path/section and unique constraints/lessons to
retained source or destination doc/section, with action, omission reason and review evidence.
Coverage measures claims/sections, not a count of removed files. Unprocessed sections block
a completeness claim. Original ADR/incident text and provenance are retained.

A retirement plan binds exact targets, HEAD/content/reference-scan snapshot, retention,
replacement coverage, authority and a recoverable quarantine/journal. Approval binds that
plan and target digest, not a wildcard. Apply-time drift, unknown external consumer or hold
invalidates approval. Recoverable movement precedes any tombstone; partial failure preserves
the original or verified recovery copy. This Work performs application only in fixtures and
does not add a general Vault write endpoint or permanently delete repository material.

## Work Projection

Native Work/Decision/Evidence remain existing Project Store objects. A view is derived and
has no independent execution state. DONE views include their own result and read-only next
steps. READY-only claim and lease completion rules stay unchanged. Heartbeat output is a
redacted copy; stored/runtime credentials are not modified by presentation logic.
