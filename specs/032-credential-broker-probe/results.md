# Work 032 Results — Credential Broker Probe (2026-09-30)

Harness: `scripts/broker_probe.py` with `broker_addon.py` in a mitmproxy container
(`mitmproxy/mitmproxy@sha256:e0deb0df…`, `--cap-drop=ALL`, `no-new-privileges`, uid 65534,
`mitmdump` run directly). The broker joins `amplai-internal` and the outside bridge like the egress
proxy, terminates TLS only for the allowlisted provider hosts and replaces a placeholder in
request headers with the real value it reads from the operator's own credential file, mounted
read-only (the Claude token file; the Codex scoped `auth.json`). The agent container is the new app
image (`amplai-worker-app-amplai-foundry@sha256:2ab06021…`) with the sandbox flags, the broker as
proxy and the broker CA as an extra trust root. Every output, the broker log and (Codex) the
container's `auth.json` after the run were searched for the real value in the probe process; none
contained it. Raw record: `broker-probe.json`. The product paths did not change.

## Answers

| # | Question | Claude CLI | Codex CLI |
|---|---|---|---|
| Q1 | Does the CLI run with only a placeholder in the container? | **pass**: `PONG`, 3.7 s, `/v1/messages` 200; `CLAUDE_CODE_OAUTH_TOKEN` in the container is the 87-character placeholder | — |
| Q2 | Does the CLI run on a ChatGPT login without the real tokens in the container? | — | **pass**: `PONG`, 8.8 s, `/backend-api/codex/responses` upgraded to WebSocket (101) through the broker. The container's `auth.json` keeps the JWT claims but no signature and a placeholder refresh token |
| Q3 | Can the broker own the token refresh? | not applicable (the OAuth token file is static) | **not measured**: no refresh happened (0 requests to `auth.openai.com`). When the access token nears expiry Codex will send the placeholder refresh token in the request body (추정: it then fails). A broker that owns refresh must rewrite the refresh request body and keep the returned tokens on its side |
| Q4 | Can a tool in the container still reach the credential? | **no**: the real value is never in the container (env holds the placeholder) | **no** for the probed run: the container holds only the signature-less claims |

## Findings

- The broker must handle WebSocket (Codex responses) and HTTP/1.1 and HTTP/2 negotiation; mitmproxy
  did both without configuration.
- Both CLIs contact hosts outside the provider allowlist (Claude: a Datadog intake; Codex: image
  CDN hosts `*.oaiusercontent.com` and `ab.chatgpt.com`). The broker refused them and the turns
  still completed.
- Claude: `/api/claude_cli/bootstrap` answered 403 through the broker; the turn was not affected.
  Why is unknown.
- Trust: Node (Claude) takes `NODE_EXTRA_CA_CERTS`; Codex took `SSL_CERT_FILE` pointing at the
  system bundle plus the broker CA.
- The Codex placeholder keeps the JWT header and payload (account id, plan, expiry, email). These are
  claims, not a credential, but they are personal data visible to the agent.

## Consequence (Work 032 exit rule)

- Claude: Q1 and Q4 pass. A Decision may supersede D-091 for Claude and a Work may build the broker
  into the product path.
- Codex: Q2 and Q4 pass for a run that needed no refresh; Q3 is open. The broker is not complete for
  Codex until refresh is handled on the broker side.
- OpenCode: not probed (its provider login and server password need their own design).
