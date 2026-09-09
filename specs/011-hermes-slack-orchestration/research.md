# Research: Hermes Slack Work Orchestration

## Repository Evidence

| Evidence | Finding |
|---|---|
| `ARC-0003` | Hermes는 Work Manager 후보, Claude Code와 Codex는 Implementation Agent 후보다 |
| `ARC-0007` | Hermes와 messenger는 channel client이며 authority는 AMPLAI가 소유한다 |
| `CON-0010` | Hermes는 candidate 상태다. shell/Git/canonical write를 직접 수행하지 않는다 |
| `MGC-014` | Hermes capability adapter, shell capability와 permission construction 금지 |
| `MGC-015` | activation은 Project-Provider-Feature 단위, global enable 금지 |
| `D-055` | Claude Code와 Codex는 동일 Work protocol의 first-class host다 |
| `D-056` | Platform 0.4 Control Plane은 bearer auth, idempotency, event/outbox 기반이다 |
| `LOCAL_SUPERVISOR.md` | Supervisor는 scheduling만 하며 merge/push/deploy를 하지 않는다 |

## External Evidence

Hermes 공식 repository 문서는 Slack Socket Mode, allowlisted user/channel, per-platform toolset과
standalone plugin을 제공한다.

- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/slack.md>
- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/plugins/index.md>
- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md>

Hermes plugin tool handler에는 trusted Slack message provenance가 보장되지 않는다. authorization
field를 model-visible argument로 받으면 안 된다.

- <https://github.com/NousResearch/hermes-agent/issues/69882>

Claude Code는 non-interactive `-p`, structured output, named/session resume와 explicit permission
mode를 제공한다.

- <https://code.claude.com/docs/en/cli-usage>

Codex는 `codex exec`, JSONL, explicit sandbox/approval과 session resume를 제공한다.

- <https://learn.chatgpt.com/docs/non-interactive-mode>
- <https://learn.chatgpt.com/docs/codex-sdk>
- <https://learn.chatgpt.com/docs/agent-approvals-security>

## Facts

- Hermes는 Slack gateway와 plugin/toolset 기능을 제공한다.
- Hermes Slack profile은 기본 tool surface가 넓다.
- current AMPLAI Project Store는 Claude Code와 Codex adapter를 모두 가진다.
- current local app binding은 app당 runner 하나만 가진다.
- current Supervisor는 configured checkout에서 직접 worker를 실행한다.
- current MGC-012 Package 5는 human ledger gate에서 blocked 상태다.

## Inferences

- Hermes가 직접 Claude/Codex를 spawn하면 Project Store lease와 permission gate를 우회한다.
- Hermes와 AMPLAI가 같은 Slack App event를 동시에 소유하면 credential과 ack owner가 모호해진다.
- actual worker selection에는 Work-level runner field와 local runner profile이 필요하다.
- production automation에는 사용자 checkout과 분리된 managed workspace가 필요하다.

## Resolved Unknowns

| Unknown | Resolution |
|---|---|
| Hermes user ID를 authority로 쓸 수 있는가 | 불가. Hermes service는 DRAFT submit/read만 허용한다 |
| Slack App 하나를 공유하는가 | 공유하지 않는다. conversation과 authority app을 분리한다 |
| Telegram 완료를 기다리는가 | Slack+Hermes provider activation에는 불필요하다 |
| Hermes memory를 knowledge로 자동 승격하는가 | 금지. explicit request도 Source/Proposal에서 멈춘다 |
| Claude/Codex를 동시에 한 Work에 쓰는가 | 금지. Work별 runner 하나를 고정한다 |

## Remaining Human Gates

unknown은 없다. architecture, public contract와 production rollout 승인이 남아 있다.
