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
| `unavailable` | recovery hold without a more specific public result, **except** a hold carrying `INGRESS_DECISION_COMMITTED_UNRECONCILED`, which is answered with silence (see the exhausted-command endings below) |

A retry with attempts left produces no safe outcome. The command is not finished, so announcing a
result would be premature; the next attempt decides. Only a recovery hold is terminal enough to
report.

An exhausted command has no next attempt, so the premise above stops holding and silence would be
permanent. Three endings are possible and they are not the same. The split is made twice: first on
whether the worker observed the outcome of the final attempt, and then, within the observed case,
on whether an earlier attempt already committed a decision. Only the observed case with no
committed decision is announced.

- **The worker observed the last attempt, it failed, and no decision was committed.** The outcome
  is known, so the worker ends the command as a recovery hold and the mapping above reports
  `unavailable`. This adds a producer for an existing outcome; it does not add an outcome.
- **The worker observed the last attempt, it failed, but an earlier attempt had already committed
  the decision.** That decision stands and its result Card already went out. Reporting
  `unavailable` here would contradict a message the reviewer has in hand, and a delivered message
  cannot be recalled. The worker still ends the command as a recovery hold — the ledger needs
  reconciling — but carries the error code `INGRESS_DECISION_COMMITTED_UNRECONCILED`, which the
  outcome mapping answers with silence.

  **The same ending covers a fourth entry**: the worker could not read whether a decision was
  committed, because the store failure that exhausted the retry budget also failed that read. The
  result is the same and the reason is different — not *we know a decision landed* but *we cannot
  tell*. Silence is the recoverable choice; a wrong announcement is not (`D-042`).
- **The worker did not observe the outcome of the final attempt**, because it died while holding
  the claim and only lease expiry consumed the budget. Earlier attempts may well have completed and
  returned `RETRY`; what is missing is the last one. The outcome is unknown here: a decision may
  have committed before the worker stopped, in which case its result Card already went out, and
  announcing a failure would contradict it. The system announces nothing and the command stays
  listed for operator recovery.

  This is the condition the user gave (`spec.md:26`, "without any worker observing the outcome") and
  the condition the code already applies. `D-043` restored it after `T017` narrowed it to "no
  attempt ever completed", which left a reachable case in none of the three endings.

`INGRESS_DECISION_COMMITTED_UNRECONCILED` is an **error code, not a safe outcome.** It does not
belong in the safe outcome enum above and must not be added to it. The enum names what the reviewer
can be told; this code names why the reviewer is told nothing. Adding it would wire a producer for
an outcome that must never be announced — the exact failure the next paragraph warns about, run in
reverse.

`stranded()` lists all three endings. It is the operator's entry point for any of them.

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
