# Work 019 Real Runs (2026-09-28)

Local product (`amplai ops local-serve`, config `~/.amplai/local/local.json`): Codex disabled by
the operator, Claude CLI 2.1.278 (`claude-sonnet-5`) enabled, apps `amplai-foundry` and
`amplai-demo-app` (github.com/pineskyeo/amplai-demo-app, private), integration
`foundry-version-report`. Every goal was approved by the operator with `amplai approve`.

| Slice | Goal | Driver (system-selected) | Result |
|---|---|---|---|
| E | `goal-d64f18a7191b496f9875ca9186a1bf55` — identity format tests | `claude-cli`, policy rank 2; `codex-cli` excluded (disabled) | published, draft PR pineskyeo/amplai-foundry#15 |
| F | `goal-b68817737fa74acf803708c09151d05b` — design: driver-reported cost | `claude-cli` | published, `[AMPLAI design]` draft PR pineskyeo/amplai-foundry#16 |
| D | `goal-2b823b3387ce4b79a7f6487804bb2bcf` — platform field across two apps | `claude-cli` | revision 2, published, linked draft PRs pineskyeo/amplai-foundry#17 and pineskyeo/amplai-demo-app#1 |
| A | `amplai ops pr-sync` after the three PRs opened | — | `{"recorded": []}`: all PRs still OPEN, no outcome change to record |

## D: Real Steer And Replan

1. Node `amplai-foundry`, run `run-9f092e95e6b64d8cb13fd96e184c8d9e`: `amplai steer` with
   "Keep the change minimal: …". The process stopped at a boundary with a checkpoint, the same
   native session resumed with the message, and the plan records the steering as applied
   (2026-09-28T02:35:04Z). The attempt passed AC-1..AC-3 (200 s).
2. Node `amplai-demo-app`, run `run-56b6d6c78175454aa1714b87b7fd0ad0`: `amplai replan` with
   "When platform is empty, summarize() must omit the ' on <platform>' suffix entirely; …".
   The run was cancelled at a confirmed boundary, the planner drafted contract revision 2 with
   the new AC-5, and nothing ran until the operator approved it. Revision 2 then passed
   AC-4 and AC-5 (26.9 s) and published both PRs.

Found by the run: while a steered (resumed) turn runs, a further `steer`/`replan` is refused
(`REPLAN_NOT_RUNNING`): the resumed turn has no quiesce path, so it is not offered. The replan
was sent during the next node's attempt instead.

## The First D Attempt (Superseded)

`goal-f282a941011448b087d649526b314347` failed with `RESUMED_BOUNDARY` after a successful
pause and exact-session resume. Cause, measured in the app container: after ~30 s of a
foreground tool call Claude Code 2.1.278 emits `tool_progress`, which the normalizer
quarantined as `UNKNOWN_PROVIDER_EVENT`; the collector stopped the container and the agent's
`pytest … | tail -40` saw exit 137. Memory was ruled out (pytest peak 344 MiB under the 2 g
limit; no OOM kill in the VM kernel log at that time). Fix: `tool_progress` accepted
(`src/amplai_foundry/agent_drivers/protocol.py`), and the Claude qualification gained a
long-command gate (`scripts/container_qualify.py`, pass in 50.1 s with `tool_progress` in the
stream; `specs/019-v3-completion/driver-qualification-amplai-foundry.json`). The old E, F and D
goals were cancelled and resubmitted with the same text.
