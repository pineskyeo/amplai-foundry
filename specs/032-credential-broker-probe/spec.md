# Work 032 — Can A Credential Broker Keep Driver Credentials Out Of The Agent's Reach?

**Created**: 2026-09-30 · **Type**: research probe (no product change) · **Status**: proposed, not started

## Why

D-091 records that the driver's provider credential lives where the agent's tools run. The design's
answer to this class of risk is a credential broker: the credential is not in agent text, context or
env, and a broker performs the exact operation or issues a short scoped token
(`design-reference/design/10_AUTHORITY_SECURITY.md:44`, `29_THREAT_MODEL.md:15`). That sentence covers
production credentials. It does not say the provider credential of a CLI driver can be brokered.
Whether it can is unknown, so this Work answers it with measurements before anything is built.

## Measured Baseline

`baseline-exposure.json`: the container uid reads the Claude token from env and from
`/proc/1/environ` and `/proc/7/environ`. The agent-tool path is not measured; the in-container model
refused the marker-only probe and the refusal was not circumvented.

## Questions (each ends in pass / fail / inconclusive with evidence)

| # | Question | How it is measured |
|---|---|---|
| Q1 | Does the Claude CLI run when the egress proxy adds the auth header and the container holds no token? | Run the pinned CLI in the qualified container with no `CLAUDE_CODE_OAUTH_TOKEN`; the proxy injects the header for the provider host only. Record whether a turn completes. |
| Q2 | Does the Codex CLI run on a ChatGPT-account login without `auth.json` in the container? | Same, with no `auth.json`. Record whether the CLI can start a session without a local credential file. |
| Q3 | Can the proxy hold the credential and refresh it (ChatGPT refresh tokens rotate, `codex.py:46`)? | Only if Q2 passes. Refresh through the proxy side, not the container. |
| Q4 | Can a tool in the container still reach a credential? | Marker-only side probe as the container uid (no model) after Q1/Q2, same markers as the baseline. |

## Non-goals

- No change to a driver, the sandbox or the proxy in the product paths. A probe uses its own container
  and its own proxy instance.
- The agent never copies or reads the operator's credential. The operator supplies scoped copies as
  today (`docs/v3/USING_AMPLAI_WORK.ko.md`).
- No claim of isolation from a measurement that did not run. A path that cannot be measured is reported
  as not measured.

## Exit

- All of Q1–Q4 pass for a driver: a new Decision supersedes D-091 for that driver and a Work builds the
  broker.
- Any fails: D-091 stays for that driver; the open question in D-091 (keep the risk, or move that driver
  to an isolated VM) goes to the operator.

## Depends On

D-091 (accepted). Uses `deployment/local-egress.json` and `scripts/container_qualify.py` as read-only
references.
