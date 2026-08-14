# Data Model: Slack Proposal Cards

## Overview

Review intent와 ActionToken generation만 새 durable entity다. Visible Slack message는 기존
outbox event와 remote receipt가 추적한다.

## Review Projection Payload

Secret-free outbox payload다.

| Field | Type | Rule |
|---|---|---|
| `aggregate_ref` | `ProposalRef` | current reviewed Proposal |
| `active_definition_digest` | digest | immutable snapshot binding |
| `content_revision` | integer | `>= 1` |
| `state_revision` | integer | reviewed state revision |
| `decision_epoch` | integer | token snapshot binding |
| `reviewer_actor_id` | string | one human Actor |
| `reviewer_external_key` | string | safe Slack mention identity |
| `bound_channel_ref` | `ChannelRef` | Slack workspace + channel; request message ID is delivery provenance only |
| `expires_at` | aware datetime | request-time scheduling hint; not issued token expiry |
| `operation_count` | integer | full operation count |
| `operation_counts` | mapping | allowed operation type to count |
| `operation_titles` | tuple | first three safe titles, stable order |
| `remaining_operation_count` | integer | `operation_count - len(titles)` |

Invariant: `operation_count == sum(operation_counts.values())`.

Invariant: `remaining_operation_count == operation_count - len(operation_titles)`.

Invariant: payload contains no reason, evidence, draft body/path, raw credential, signing secret, or
bot token.

## Review Card Command

Table: `governance_review_card_commands`.

| Field | Type | Rule |
|---|---|---|
| `idempotency_key` | TEXT PK | caller retry identity |
| `request_fingerprint` | TEXT | 64-char lowercase SHA-256 |
| `project_namespace` | TEXT | Proposal identity |
| `project_id` | TEXT | Proposal identity |
| `proposal_id` | TEXT | Proposal identity |
| `active_definition_digest` | TEXT | current snapshot |
| `content_revision` | INTEGER | current snapshot |
| `state_revision` | INTEGER | reviewed revision |
| `decision_epoch` | INTEGER | current snapshot |
| `reviewer_actor_id` | TEXT | designated Actor |
| `reviewer_actor_type` | TEXT | `human` only |
| `reviewer_external_key` | TEXT | bound Slack user ID |
| `provider_installation_ref` | TEXT | bound provider installation; restores the authority request |
| `channel_json` | TEXT | canonical secret-free `ChannelRef` |
| `expires_at` | TEXT | immutable request-time scheduling hint |
| `payload_digest` | TEXT | canonical Review payload digest |
| `payload_json` | TEXT | secret-free Review payload |
| `requested_at` | TEXT | aware timestamp |

Unique snapshot constraint: Proposal identity + `active_definition_digest` + `content_revision` +
`state_revision` + `decision_epoch` has one command. The first command designates the reviewer and
Slack destination. Changing reviewer, provider/workspace/channel, or `message_id` does not create a
second Review Card for the same snapshot.

Replay rule: Same `idempotency_key` and fingerprint returns the existing command/event. Same key with
another fingerprint raises `IDEMPOTENCY_CONFLICT`.

## Review Action Set

Table: `governance_review_action_sets`.

| Field | Type | Rule |
|---|---|---|
| `event_id` | TEXT PK part 1 | Slack outbox event |
| `generation` | INTEGER PK part 2 | starts at 1, contiguous |
| `approve_token_id` | TEXT | ActionToken identity only |
| `request_changes_token_id` | TEXT | ActionToken identity only |
| `reject_token_id` | TEXT | ActionToken identity only |
| `state` | TEXT | `issued`, `revoked`, `consumed`, `expired` |
| `issued_at` | TEXT | matches token records |
| `expires_at` | TEXT | exactly `issued_at + 24 hours` for this generation |
| `resolved_at` | TEXT nullable | required outside `issued` |

Invariant: One event has at most one `issued` generation.

Invariant: Cleanup identifies `event_id + generation`; it never selects whichever generation happens
to be issued when a stale attempt finishes.

Invariant: Token IDs are distinct and point to ActionToken rows with matching Proposal snapshot,
reviewer, channel, action, issue time, generation expiry, and state.

Slack authorization scope is the `workspace_id` + `channel_id` pair. A request-side `message_id`, when
present in an existing `ChannelRef`, records delivery provenance; it is not required to equal the new
Review Card message timestamp returned by Slack.

Invariant: Raw credential is not a column and never appears in model serialization.

## Result Projection Payload

Existing `DecisionProjectionPayload` remains the durable entity. Slack presentation derives:

| Card Field | Source |
|---|---|
| Proposal ID | `aggregate_ref.proposal_id` |
| Project | `aggregate_ref.project_ref` |
| Status | `proposal_status` |
| Content revision | `content_revision` |
| State revision | `state_revision` |
| Decision epoch | `decision_epoch` subtext |

No schema change applies to decision results.

## State Transitions

### Review Command

```text
absent -> recorded -> outbox pending -> delivered
                              |-> retry_wait
                              |-> recovery_hold/dead_letter
```

The command row is immutable. Outbox owns delivery state.

### Action Set

```text
absent -> issued -> consumed
                  -> expired
                  -> revoked

issued(generation N) -> revoked -> issued(generation N+1)
```

`issued -> revoked -> replacement` occurs only after remote absence is confirmed.

## Integrity Checks

- Schema verifier compares exact columns, constraints, indexes, and triggers.
- Governance event integrity derives Review payload from command row and matches audit/outbox digests.
- Action-set verifier checks generation continuity and token bindings.
- Existing action token append/transition triggers remain authoritative.
