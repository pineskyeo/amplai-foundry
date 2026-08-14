# Contract: Review Card Lifecycle

## Request

`ReviewCardService.request()` accepts:

- reviewed `ProposalRef`
- designated reviewer `DirectAuthorityRequest`
- `idempotency_key`
- `request_fingerprint`
- fixed 24-hour action TTL policy

It requires:

- active Proposal status `reviewed`
- current immutable definition digest and revisions
- active human Actor binding for the Slack external actor
- `PROPOSAL_DECIDE` permission in the Proposal project
- request channel equal to the reviewer-bound Slack channel
- valid operation `type` and `title` fields

It atomically records command, audit event, and one Slack outbox event.
The first accepted command designates the reviewer and Slack destination for that immutable snapshot;
a later request cannot create another command by changing reviewer or channel.

## Destination

`SlackProjectionDestination` performs these steps:

```text
validate semantic digest
  -> reconcile marker when retrying
     -> found: return existing receipt
     -> ambiguous: raise reconciliation error, no token change
     -> confirmed absent: continue
  -> prepare action set
     -> revoke previous issued generation
     -> issue three fresh 24-hour tokens
     -> persist token IDs and generation in one transaction
  -> render Review Card in memory
  -> post message with marker
     -> provider response channel differs: revoke action set and enter hold
  -> return receipt
```

## Crash Matrix

| Boundary | Durable State | Recovery |
|---|---|---|
| Before action-set transaction | Outbox leased only | Reclaim and prepare generation 1 |
| After token issue, before post | Issued IDs, raw lost | Reconcile absent, revoke, issue next generation |
| During post with unknown outcome | Issued IDs, receipt absent | Reconcile first; never replace on ambiguity |
| Process interruption during render or post | Issued IDs may exist, raw lost | Scrub secret-bearing frames; preserve the interruption class; reconcile before replacement |
| After Slack accepted, before local mark | Remote marker + issued IDs | Reconcile finds marker and returns receipt |
| After local delivered mark | Delivered receipt | No second claim |
| Terminal Slack rejection | Held/dead-letter event | Revoke only the generation prepared by that attempt |
| Revocation cleanup fails | Issued IDs + closed cleanup error | Retain fail-closed hold; no provider or credential text in diagnostics |
| Definition revision before click | Historical Card + stale tokens | Existing snapshot check denies decision |
| Another action wins | Consumed winner + revoked siblings | Other actions cannot mutate Proposal |

## Replacement Rules

1. Replacement requires confirmed remote absence.
2. Old issued generation is revoked before new generation is inserted.
3. Revocation and replacement issue use one transaction.
4. Generation increases by one.
5. At most one generation remains `issued`.
6. A delivered remote Card never receives a replacement solely because raw credentials are no longer
   available locally.
7. Every new generation expires exactly 24 hours after its own issuance, independent of request or
   outbox delay.
8. Cleanup always supplies the generation prepared by that delivery attempt. A stale attempt cannot
   revoke a newer replacement generation.

## Decision Rules

Existing ActionToken checks remain authoritative:

- token hash
- allowed action
- Proposal identity and immutable definition digest
- content, state, and decision epoch
- actor identity and current permission
- bound Slack workspace and channel
- expiry and token state
- idempotency and request fingerprint

`ChannelRef.message_id` is delivery provenance, not the authorization boundary. A click from the
new Review Card may therefore have a different message timestamp while retaining the same Slack
workspace and channel. Review command uniqueness and Slack destination ordering use the same
provider/workspace/channel scope. The destination sequence is channel-wide, but source-state revision
monotonicity is evaluated per Proposal identity so equal revisions from different Proposals can be
interleaved. Non-Slack providers retain their existing full-channel destination derivation.

On one successful decision, the selected token becomes `consumed`. Sibling tokens for the same action
set become `revoked` in the same decision transaction.

## Observability

Allowed diagnostic fields:

- event ID
- destination ref and sequence
- action-set generation
- token IDs
- safe error code
- fixed message and irreversibly derived label for unrecognized provider or adapter data
- remote receipt

Forbidden diagnostic fields:

- raw button value
- raw Slack request body
- raw ActionToken credential
- bot token or signing secret

Automated canary tests inspect model JSON, exception strings, SQLite text/blob columns, captured log,
pytest `--showlocals` output, and HTTP fake request diagnostics. Provider-controlled exception
messages and unrecognized error codes are never copied verbatim into Review Card diagnostics.

## Delivery Budget Binding

The HTTP transport and destination use the same `max_history_pages`. The HTTP transport and
dispatcher use the same `lease_seconds`. The destination and dispatcher use the same `max_attempts`.
Page mismatch rejects destination construction; lease or attempt mismatch rejects delivery before an
outbox event is claimed.
