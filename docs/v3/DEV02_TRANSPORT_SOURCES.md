# DEV-02 transport verification notes

Checked 2026-09-16. Primary documentation below informed protocol handling; the approved V3 design files are unchanged. Documentation availability is not deployment qualification.

- OpenCode Server: https://opencode.ai/docs/server/ — authentication, `/global/health`, explicit sessions, asynchronous prompt receipt, status/messages, abort. Implemented version and exact-turn correlation; HTTP 204 and abort acknowledgement do not certify a stopped process. Tests use MockTransport, not a live account; `scripts/opencode_qualify.py` measures a real 1.17.13 server (Work 017). Measured there: the server orders a session's messages by ID (`msg_` + 12 hex of `ms * 0x1000` + 14 chars), so a client `messageID` must be in that ascending form or the server keeps generating replies; larger JSON bodies arrive gzip-encoded.
- Claude Code programmatic use: https://code.claude.com/docs/en/headless — print/JSONL output, explicit model and session resumption. Actual supported flags/binary must be checked for the pinned deployment version; configured version alone is not an observed binary receipt.
- Codex noninteractive mode: https://developers.openai.com/codex/noninteractive (redirected to https://learn.chatgpt.com/docs/non-interactive-mode) — machine-readable execution and exact session resume. Environment/private session binding and positive sandbox termination are AMPLAI controls, not assumptions about provider success.
- Docker run: https://docs.docker.com/reference/cli/docker/container/run/ — non-root, capability, mount, resource and network options. The code constructs a restricted pinned recipe. No container engine was present for live containment/egress testing in this stage.

No model-specific `AstraDriver` or undocumented future endpoint was added. Optional Responses and managed/app-server boundaries remain explicitly qualified/disabled by deployment, not enabled by marketing names or a documentation link.
