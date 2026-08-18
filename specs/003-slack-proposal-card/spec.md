# Feature Specification: MGC-012 Package 5 — Slack Proposal Cards

**Feature Branch**: `003-slack-proposal-card`

**Created**: 2026-08-12

**Status**: Approved for planning

**Input**: Approved grilling decisions for a structured Slack Proposal review Card and a separate decision-result Card.

## Context

The Slack reference E2E proved message delivery, marker read-back, ordering, and human-delete
recovery, but it used a test-only one-line message. The actual Proposal decision payload has no
approved presentation contract, so the test did not prove what a reviewer sees or how the existing
governed decision buttons are delivered safely. This feature closes that presentation and interaction
gap without making Slack authoritative for Proposal state.

## Clarifications

### Session 2026-08-14

- Q: A transient store failure during Card delivery currently stops the whole destination permanently. Does this feature make that hold releasable? → A: No. Transient failures are reclassified to consume the ordinary retry budget, but releasing an operator hold stays out of scope for this feature (recorded as a known limitation in Assumptions).
- Q: When background processing of a reviewer action exhausts its retry budget, what does the reviewer receive? → A: A terminal safe outcome. The worker's final attempt ends as a recovery hold, so the existing safe-feedback path announces `unavailable`.
  - **Narrowed after this session, not overruled** (`D-039`, `D-041`, `D-042`). The answer above holds for the case it was asked about — the last attempt fails and nothing was decided. Two later measurements found sub-cases where announcing `unavailable` would contradict a message the reviewer already has: an earlier attempt may have committed the decision and sent its result Card, and the store failure that exhausted the budget may also block reading whether that happened. Both end as a recovery hold as answered here, but stay silent. FR-026 carries the full rule.
- Q: When the retry budget is exhausted without any worker observing the outcome (repeated worker death), what does the reviewer receive? → A: Nothing. The true outcome is unknown and a decision may already have produced a result Card, so announcing a failure could contradict it. The command stays visible for operator recovery instead.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Read a Decision Result Card (Priority: P1)

After a Proposal decision completes, channel members receive a structured result Card that clearly
states the Proposal identity, final decision, and relevant revisions. It is recognizably different
from a test message and contains no action controls.

**Why this priority**: This is the smallest independently useful slice and fixes the production-shaped
presentation gap already exposed by the Package 4 E2E.

**Independent Test**: Complete each supported Proposal decision and verify that one structured,
accessible result Card appears with the correct identity, status, and revisions and no buttons.

**Acceptance Scenarios**:

1. **Given** a Proposal decision is approved, **When** its projection is delivered, **Then** the channel receives one result Card showing the Proposal ID, project, approved status, content revision, and state revision.
2. **Given** a Proposal is rejected or changes are requested, **When** its projection is delivered, **Then** the Card uses the matching status wording and contains no approval controls.
3. **Given** a user receives a notification or uses assistive technology, **When** the Card is announced without its visual layout, **Then** a concise fallback sentence still identifies the Proposal and outcome.

---

### User Story 2 - Decide From a Review Card (Priority: P2)

When a Proposal enters review, one designated reviewer receives a structured Card in the bound Slack
channel. The Card summarizes the change set and offers Approve, Request changes, and Reject actions.
Only the designated reviewer can apply one of those governed actions.

**Why this priority**: This turns the already implemented Slack action receiver into a usable review
surface while preserving actor, channel, Proposal snapshot, and one-time decision boundaries.

**Independent Test**: Submit a Proposal for review, confirm the review Card and its 24-hour validity,
then use each action in an isolated scenario and verify exactly one matching Proposal transition.

**Acceptance Scenarios**:

1. **Given** a Proposal enters reviewed state with a designated reviewer and Slack channel, **When** the review Card is delivered, **Then** it shows the Proposal ID, project, revision, operation counts, up to three operation titles, designated reviewer, expiration, and exactly three decision actions.
2. **Given** the designated reviewer selects Approve and confirms, **When** the action is processed, **Then** the Proposal becomes approved once and a separate approved result Card is delivered.
3. **Given** the designated reviewer selects Request changes or Reject and confirms, **When** the action is processed, **Then** the Proposal enters the matching state once and a separate matching result Card is delivered.
4. **Given** another channel member selects an action, **When** the action is authenticated, **Then** no Proposal state changes and the actor receives a safe denial.

---

### User Story 3 - Recover Without Duplicate or Leaked Cards (Priority: P3)

A reviewer sees at most one usable review Card for one Proposal review snapshot even when delivery or
processing is retried, interrupted, or restarted. One-time decision credentials never become durable
application data or diagnostic output.

**Why this priority**: An interactive Card contains authority-bearing values. Delivery convenience
cannot weaken governed mutation, duplicate visible decisions, or leak credentials.

**Independent Test**: Exercise interruption boundaries before send, after remote acceptance, before
local receipt persistence, during retry, and after a decision; verify one usable Card, one decision,
and zero durable or diagnostic copies of raw credentials.

**Acceptance Scenarios**:

1. **Given** delivery stops before remote acceptance, **When** recovery runs, **Then** the abandoned action set is revoked and one replacement Card with a fresh action set may be delivered.
2. **Given** remote acceptance occurred but local completion did not, **When** recovery runs, **Then** the existing Card is identified and no duplicate Card is posted.
3. **Given** an action is expired, stale, consumed, revoked, replayed, or bound to another actor or channel, **When** it is processed, **Then** no new decision occurs and only a safe outcome is exposed.
4. **Given** any failure or diagnostic path, **When** stores, logs, exceptions, and test reports are inspected, **Then** no raw decision credential appears.

### Edge Cases

- A Proposal has more than three operations: show the first three titles in stable order and a remaining-count summary.
- An operation title or identifier exceeds the presentation limit: truncate deterministically without changing the underlying Proposal data.
- The Proposal definition changes after Card creation: all previous actions become stale and cannot decide the newer snapshot.
- The designated reviewer loses permission or is disabled before clicking: deny the action without changing Proposal state.
- Two valid action deliveries race: exactly one decision wins; the other observes the durable result.
- A Card remains visible after its 24-hour action window: it stays historical, but every action is rejected as expired.
- Result delivery is retried after a successful decision: the ordered projection recovers the existing receipt rather than creating a duplicate.
- Slack is unavailable: local Proposal state and credential safety remain correct, and delivery follows the bounded retry or hold policy.
- The local store is briefly busy while a Card is being prepared: delivery retries within its budget instead of stopping the destination on the first failure. Storage corruption is not brief and stops delivery immediately.
- Background processing of a reviewer action runs out of retries: the reviewer is told the action is unavailable, and the command remains listed for operator recovery.
- Processing runs out of retries without any attempt completing, because the worker keeps dying: no outcome is announced, because a decision may already have produced a result Card. Operator recovery is the only path.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST present every supported Proposal decision as a structured result Card rather than an unformatted test message.
- **FR-002**: A result Card MUST show Proposal ID, project, decision status, content revision, and state revision.
- **FR-003**: Approved, rejected, and changes-requested results MUST have distinct, unambiguous wording.
- **FR-004**: Every Card MUST include a concise fallback sentence that communicates its purpose without relying on visual layout.
- **FR-005**: A Proposal entering reviewed state MUST be eligible for one review Card bound to one Slack channel and one designated reviewer.
- **FR-006**: A review Card MUST show Proposal ID, project, content revision, operation totals by type, the first three operation titles, the remaining operation count, reviewer identity, and action expiration.
- **FR-007**: A review Card MUST expose exactly Approve, Request changes, and Reject actions, with a confirmation step before submission.
- **FR-008**: Review actions MUST remain usable for 24 hours from issuance and MUST be rejected after expiration.
- **FR-009**: Each review action MUST be bound to exactly one Proposal snapshot, action, reviewer, and channel.
- **FR-010**: Only the designated reviewer with current decision permission MUST be able to complete a Card action.
- **FR-011**: A valid Card action MUST use the existing governed decision path and MUST NOT bypass one-time action authorization.
- **FR-012**: Slack receipt acknowledgement MUST complete within the provider interaction deadline without claiming that the background decision has already succeeded.
- **FR-013**: A completed decision MUST produce a separate result Card; the first version MUST NOT edit or delete the original review Card.
- **FR-014**: A review Card remaining after decision, expiration, revision, or revocation MUST be historical only; its actions MUST NOT mutate state.
- **FR-015**: Duplicate or concurrent action delivery MUST produce at most one Proposal transition and one logical result.
- **FR-016**: Review Card delivery MUST recover an existing remotely accepted Card before attempting a replacement.
- **FR-017**: If delivery is known not to have occurred and the original raw actions are unavailable, the old action set MUST be revoked before a replacement set is issued.
- **FR-018**: Raw one-time decision credentials MUST NOT be stored in durable application state, logs, exceptions, metrics, or test evidence.
- **FR-019**: Card content MUST exclude full evidence text, draft content, local file paths, signing secrets, bot tokens, and raw decision credentials.
- **FR-020**: Long or numerous operation summaries MUST be bounded and deterministically shortened while preserving stable order and the correct total count.
- **FR-021**: Result and review delivery MUST preserve existing destination ordering, reconciliation, retry, hold, and provider-isolation behavior.
- **FR-022**: Slack interaction failure MUST NOT stop unrelated provider destinations or authorize a direct canonical knowledge mutation.
- **FR-023**: The real-workspace validation MUST send a Card produced from the same presentation contract used by the application, not a test-only substitute.
- **FR-024**: The feature MUST provide safe reviewer feedback for denied, expired, stale, already-completed, and otherwise-unavailable actions without exposing internal exception text. The set of safe outcomes named here MUST match the set the system can actually produce. An accepted action MUST NOT produce safe feedback; its user-visible result is the separate result Card required by FR-013, so that one decision is announced once.
- **FR-025**: The first version MUST support a single designated reviewer and MUST NOT imply multi-reviewer, quorum, or first-authorized-user semantics.
- **FR-026**: Background processing that exhausts its retry budget MUST reach a terminal state rather than remaining silent indefinitely. When the final attempt is observed, its outcome is known to have failed, and no earlier attempt committed a decision, the action MUST produce the unavailable safe outcome. When the final attempt is observed and failed but an earlier attempt already committed the decision, the system MUST NOT announce any outcome — that decision's result Card has already been delivered and a failure announcement would contradict it — and MUST keep the command listed for operator recovery under a distinct terminal error code. The same silent ending MUST apply when the system cannot determine whether a decision was committed, because the failure that exhausted the budget also prevents that determination. When the outcome is unknown because no attempt completed, the system MUST NOT announce any outcome and MUST keep the command listed for operator recovery.
- **FR-027**: A transient delivery-store failure MUST consume the ordinary retry budget rather than immediately stopping its destination, and MUST be distinguished from storage corruption, which remains terminal. This requirement governs **which failures are classified as retryable**, and to that extent it takes precedence over FR-021's requirement to preserve existing retry behavior. FR-021 continues to govern the mechanisms themselves: ordering, reconciliation, the retry budget, holds, and provider isolation MUST still be used rather than bypassed.

### Key Entities

- **Review Card**: A visible summary of one reviewed Proposal snapshot, its designated reviewer, bounded operation preview, expiration, and three governed actions.
- **Result Card**: A visible, action-free summary of one completed Proposal decision and its revisions.
- **Reviewer Binding**: The single actor and Slack channel allowed to use a review Card action.
- **Action Set**: Three one-time, action-specific credentials for one reviewer and Proposal snapshot, represented durably only by non-secret identity and verification data.
- **Card Delivery Lifecycle**: The durable facts needed to distinguish intent, remote acceptance, completion, replacement eligibility, and unrecoverable ambiguity without retaining raw actions.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the test workspace, 100% of approved, rejected, and changes-requested scenarios display one correctly labeled result Card containing all five required identity and revision fields.
- **SC-002**: A designated reviewer can complete any of the three decisions using the review Card in no more than two explicit interactions: select and confirm.
- **SC-003**: Across the defined interruption, retry, and restart scenarios, each Proposal review snapshot leaves at most one usable review Card and each action produces at most one state transition.
- **SC-004**: Unauthorized, expired, stale, revoked, consumed, and replayed actions produce zero Proposal state changes in all acceptance scenarios.
- **SC-005**: Automated inspection of durable state, logs, exceptions, metrics, and test evidence finds zero raw one-time decision credentials.
- **SC-006**: Cards with 1, 3, 4, and maximum-supported operation counts preserve correct totals, stable preview order, and bounded readable output.
- **SC-007**: A real-workspace result Card and review Card both match the approved presentation contract, retain their machine-readable identity, and can be reconciled without a duplicate post.
- **SC-008**: Existing non-Slack provider delivery scenarios remain unchanged after the feature is enabled.

## Assumptions

- One Slack workspace, one destination channel, and one designated reviewer are sufficient for the first version.
- The designated reviewer already has a verified Slack-to-Actor binding and Proposal decision permission.
- The action window is 24 hours because review is asynchronous; snapshot, actor, channel, action, permission, and one-time checks remain mandatory throughout that window.
- Operation titles and aggregate counts are safe to show in the approved test workspace; full reasons, evidence, draft bodies, and local paths remain outside the Card.
- The first version uses separate top-level result Cards. Editing the original Card, thread-only delivery, modal reason collection, multi-reviewer approval, quorum, web UI, and production rollout are excluded.
- Existing Slack request authentication, bounded acknowledgement, background decision processing, ordered delivery, and marker reconciliation remain authoritative and are extended rather than replaced.
- Releasing an operator hold is outside this feature. Once a destination is held, resuming it needs a governed release path that does not exist yet, so a failure that outlives the retry budget still stops that destination until that separate work lands.
- A real interactive click requires an externally reachable Slack interaction endpoint, but adding a new central server or deployment topology is outside this feature. Local signed-payload integration remains required even if external endpoint wiring is not available.
