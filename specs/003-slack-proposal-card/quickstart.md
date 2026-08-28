# Quickstart: Slack Proposal Cards

## Prerequisites

- Python 3.11+
- repository dev dependencies installed
- existing governance schema migrated through version `32`
- active Slack Actor binding and `PROPOSAL_DECIDE` permission
- Slack app with `chat:write` and private-channel history scope when the test channel is private

No new web server or production endpoint is created by this feature.

## Automated Tests

Run focused tests first.

```bash
python -m pytest \
  tests/test_slack_cards.py \
  tests/test_review_cards.py \
  tests/test_slack_projection.py \
  tests/test_slack_ack_boundary.py \
  tests/test_slack_http.py
```

Run the full offline gate.

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy
amplai-foundry verify
```

`amplai-foundry verify` includes Vault knowledge lint and remains network-free.

## Real Workspace Result Card

Load the operator-owned Slack environment file in the current shell.

```bash
source .amplai/tmp/slack-e2e.env
python -m pytest tests/test_slack_http.py -m slack_e2e -k result_card -rs
```

The test sends a production-rendered Result Card, reads its marker, retries delivery, and proves one
remote message.

## Real Workspace Review Card

```bash
source .amplai/tmp/slack-e2e.env
python -m pytest tests/test_slack_http.py -m slack_e2e -k review_card -rs
```

The test sends a production-rendered Review Card, reads its marker, and verifies the three action
contracts. It does not print button values or raw credentials.

## Signed Local Interaction

The automated integration constructs the exact Slack Block Action payload, signs the raw bytes with
the configured test signing secret, runs `BoundedIngressAck`, then runs `IngressDecisionWorker`.

This proves parser, durable ingress, authority re-check, ActionToken, decision, and Result Card enqueue
without adding an externally reachable server. A human click requires an existing externally reachable
Slack Request URL and remains deployment wiring outside this feature.

## Expected Result

- Result Card has no actions.
- Review Card has exactly three confirmed actions.
- One valid action creates one Proposal transition.
- Retry/restart creates no duplicate remote Card.
- SQLite, exception, log, and pytest output contain no raw ActionToken credential.
