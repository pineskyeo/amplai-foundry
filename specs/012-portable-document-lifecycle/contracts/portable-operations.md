# Portable Operations Contract

This is the local candidate CLI/internal tooling contract. Existing public development entry points
remain work/design. No HTTP API, Platform schema, canonical-Vault mutation or deployment API
is added. Assigned slices implement and test the CLI wiring; independent completion and
server qualification remain separate evidence gates.

## Install Baseline

Extend the existing installer with explicit `--bootstrap-baseline` and `--repo-profile`
selection. Without opt-in, retain current extension-only prerequisites and behavior.
Validate the complete package seal before target access or mutation. Construct baseline plus
marker/JSON/hook/mirror changes in memory and emit one final typed action per target path.

Missing/extra/tampered package entries, unsafe mirrors, local conflicts and version-equal
content drift reject before a completion receipt. Existing three-way merge, transaction
backup, recovery and uninstall remain the only mutation mechanism. Failed recovery exposes
a resumable journal and preserves backup; it never reports success or deletes user backups.
Do not initialize Git, bind a Store, activate a supervisor or change host trust implicitly.

## Inventory And Impact

Existing `loopctl docs impact` delegates to the portable document engine and repository
profile. Inputs include exact authorized roots, baseline/candidate, metadata owners,
declared dependencies and rule/profile version. Outputs include inventory, selected impact,
per-item reasons, snapshot and machine-computed completeness.

Unclassified new tooling and document-only changes cannot disappear through a default
non-semantic glob. Rename has both old and new locators; deletion retains its baseline
reference identity. Missing required/external snapshots are explicit failures. Policy
exceptions identify finite existing debt, owner and expiry, not all future unmatched files.

## Review And Validate

Add snapshot-bound `docs review` and atomic `docs review-batch` using the existing Synapse
protocol shape, without making reviewer labels human approval. Required fields are outcome,
reason, reviewer, method, current snapshot and evidence. `reviewed_unchanged` is valid when
the exact document and dependency meanings were examined; touching a date is not a review.

Single and batch paths share validation. Every batch entry must validate before one atomic
report replacement. Recheck inputs immediately before publication. A concurrent change
keeps previous evidence intact and returns SOURCE_DRIFT. `docs validate` recomputes current
scope and rejects stale, incomplete or mismatched evidence. Legacy acknowledge input may
be accepted for compatibility but cannot resolve freshness.

The serialized UTF-8 record must fit its reader before staging. Preserve existing POSIX
owner/group/mode independently of umask and reject metadata, identity or content drift
before publication. A new record uses mode 0600. Failure does not authorize history
pruning or a retry against unchecked inputs. Target ACL/SELinux/xattr support is not
certified by the local POSIX fixture evidence.

## Resolve Context And Build View

The internal resolver accepts principal/disclosure profile, exact release and optional
explicit history selection. Apply security and lifecycle/release/freshness checks before
relevance ranking, title/snippet extraction, HTML or index creation. Current consumers are
portable docs inventory/query/index/view and Loop source/claim selection. Existing Foundry
Platform/Vault APIs retain their separate governed contract and are not silently recertified.

Use the bounded full-document reference scanner for multiline and nested Markdown and
HTML attributes, including literal examples. Separate URI query/fragment syntax before
decoding once; decoded filename punctuation stays literal. Prose glob/editor-locator
normalization must not reinterpret a URI target. Unsupported or ambiguous reference
syntax fails before output without exposing the rejected target.

The finite HTML subset distinguishes single-destination attributes from inert text.
Single destinations include href/src/poster/data/action/formaction/cite/longdesc,
background/manifest/codebase/classid/usemap/itemid and namespace references such as
xlink:href. Namespace tag names use the same scanner. Compound destinations (including
srcset/imagesrcset and ping/archive lists), CSS/SVG functional references, srcdoc,
refresh, dynamic script/style/animation containers and base-URI modifiers are unsupported.
Unknown value-bearing attributes reject; explicit inert attributes and data-/aria-
metadata remain opaque author text. No full HTML/CSS/JavaScript parser or prose DLP is
claimed. Unsupported raw examples must be represented as escaped descriptive text.

Exact dependency and visibility validation reject local file, executable/data and unknown
URI schemes. Impact-only lean
discovery can retain unknown authority-bearing origins as unavailable external candidates,
never as proof, a fetched resource or a visibility approval; local references beside them
remain discoverable. File/data/javascript/vbscript still reject even in candidate mode.

Supported non-local URIs are HTTP/HTTPS with explicit authority, protocol-relative URLs and mailto; no
destination is fetched. Source/HTML syntax is decoded once before URI interpretation.
Reference count, scan work and retained strings have explicit input-proportional limits,
checked before prefix/label/path allocation; a limit is SCOPE_INCOMPLETE, never truncation.

`docs build` consumes an exact reviewed input set and writes a new deterministic bundle.
Existing identical output is verified for idempotent retry; differing output is rejected.
Markdown raw HTML is escaped; unsupported syntax or unsafe links cannot become executable
content. Basic navigation/content works offline without scripts, fonts or external requests.
Audience profiles select reviewed source sections; the renderer never rewrites facts.

External build requires an approved allowlisted source bundle at its input boundary. It
must not receive internal checkout metadata. No external build or publication is authorized
by a security label alone. Bundle validation binds all emitted files, including indexes,
metadata, hidden attributes and links. No embedded source maps or raw agent traces.

## Preserve, Plan Retirement And Apply

Preservation/retirement planning is read-only and returns section mapping, exact targets,
references, retention, required authority and recovery plan or HOLD. `garden incremental`
uses Work scope after review; full scan requires an explicit repository-gardening Work.
Normal `garden apply` stays report-only. No backup/worktree cleanup is implicit.

The internal approved-retirement operation is tested only against disposable fixture roots.
It accepts a bound plan and approval, rechecks HEAD/content/retention/reference identities,
uses safe relative target resolution, recoverable quarantine and a journal, and emits an
audit/tombstone only for the exact approved set. Failed or interrupted application cannot
erase unapproved paths; recovery revalidates journal and quarantined bytes. Real application
requires a separate scoped human action and is not exercised in this Work.

## Errors And Evidence

Preserve existing CLI exit conventions. Distinguish validation failure from unavailable
environment and human approval. Structured error codes cover MISSING_SOURCE,
UNSUPPORTED_PARSER, SCOPE_INCOMPLETE, EXTERNAL_REFERENCE_UNAVAILABLE, SOURCE_DRIFT,
CANONICAL_CONFLICT, APPROVAL_REQUIRED, APPROVAL_MISMATCH, RETENTION_HOLD, UNSAFE_PATH
and RENDER_FAILED. Error summaries never expose rejected private titles, paths or credentials.

All operation results bind input/rule/profile snapshots. Same input retries do not create
new documents or silently overwrite completed bundles. A valid JSON file or an exit-zero
subprocess without required evidence is not by itself a completed operation.

## Native Work Presentation

Keep context/show/handoff reads valid for terminal Work. Markdown includes the Work's own
result, linked evidence and status-specific instructions; it does not tell a DONE Work to
claim or update itself. Heartbeat CLI serializes a redacted copy of the response. Runtime
token use, renewal, storage, READY-only claim and dependency activation remain unchanged.
