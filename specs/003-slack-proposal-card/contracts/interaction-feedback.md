# Contract: Interaction Feedback

## Acknowledgement

`BoundedIngressAck` keeps the existing response contract.

- Authenticated durable acceptance returns HTTP success within 3 seconds.
- The response means accepted for background processing.
- The response does not claim the Proposal decision completed.
- Rejected authentication or durable acceptance returns the existing fail-closed response.

## Safe Outcomes

A first successful decision produces no safe outcome. Its user-visible result is the separate
Result Card, which the Delivery section below owns. The worker returns nothing here so that one
decision is announced once.

The background worker maps every other internal result to a closed public vocabulary.

| Safe Outcome | Internal Source |
|---|---|
| `already_completed` | idempotent replay or consumed action |
| `expired` | expired token |
| `stale` | Proposal snapshot mismatch or revoked action |
| `denied` | actor, channel, permission, binding, or installation denial |
| `unavailable` | recovery hold without a more specific public result |

A retry produces no safe outcome. The command is not finished, so announcing a result would be
premature; the next attempt decides. Only a recovery hold is terminal enough to report.

Every listed outcome has a producer. An outcome value with no producer must not remain in the
enum — a later reader takes it for delivered behavior, or wires a producer and announces the same
event twice.

The user message contains no internal exception text, token ID, raw credential, request body, or policy
snapshot.

## Delivery

Successful decisions rely on the separate Result Card. Terminal failure outcomes may call a
`SlackInteractionFeedback` port after the governance transaction and ingress state transition.

The Slack adapter calls `chat.postEphemeral` with:

- command channel ID
- command external actor key
- fixed safe message selected by outcome enum

Feedback delivery is non-authoritative. Failure does not roll back a decision, re-open an ingress
command, or authorize another action. Worker result may carry a closed `feedback_error_code`.

## Provider Isolation

Only commands whose provider is Slack use the Slack feedback adapter. Other providers receive no new
network call and preserve current worker behavior.
