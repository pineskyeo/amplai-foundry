# Feature Specification: Hermes Slack Work Orchestration

## Goal

Slack 사용자가 Hermes와 대화해 AMPLAI 작업을 요청한다. AMPLAI는 요청을 검증된 `Work`로
기록하고 Claude Code 또는 Codex에 구현을 맡긴다. 결과와 승인 대기는 같은 Slack 업무 흐름에서
확인한다.

완료 상태는 자연어 응답이 아니다. `Project Store` 상태, 실행 evidence, Slack projection이 같은
`request_id`와 `work_id`를 가리키는 상태다.

## Terms

`Hermes` — Slack 대화와 작업 조율을 담당하는 교체 가능한 Client Partner다.

`Implementation Agent` — 승인된 repository `Work`를 구현하고 검증하는 Claude Code 또는 Codex다.

`Activation Action` — `DRAFT` 작업을 실행 가능 상태로 바꾸는 일회성 human decision이다.

`Runner Profile` — 한 `Work`가 사용할 host와 최소 권한을 고정한 host-local 실행 설정이다.

## User Outcomes

### UO-1 Submit And Track Work

사용자는 Hermes에게 상태 조회, 설계 또는 구현을 자연어로 요청한다. Hermes는 구조화된 요청을
AMPLAI에 제출하고 `request_id`를 돌려준다.

AMPLAI는 프로젝트와 target app을 확정하지 못하면 실행하지 않는다. 사용자는 hold 이유와 필요한
선택을 Slack에서 확인한다.

### UO-2 Approve Execution In Slack

변경 작업은 AMPLAI Slack App이 표시한 Activation Card에서 승인한다. Hermes의 자연어 응답이나
tool call은 승인이 아니다.

승인 click은 actor binding, project, revision, digest, channel, expiry와 one-time token을 다시
검증한다. 검증이 끝난 작업만 `WAITING|READY`로 전환한다.

### UO-3 Delegate To Claude Code Or Codex

각 `Work`는 `controller=design|work`와 `runner_profile=claude-code|codex`를 가진다. Supervisor는
해당 host의 public entry point만 호출한다.

한 `Work`는 한 번에 runner 하나만 사용한다. 같은 target app의 동시 실행 수는 app capacity를
넘지 않는다.

### UO-4 Observe Completion And Recovery

사용자는 Slack에서 `DRAFT`, `READY`, `RUNNING`, `HUMAN_REQUIRED`, `FAILED`, `DONE` 상태와 최신
evidence 요약을 조회한다. 권위 있는 값은 Project Store에서 읽는다.

worker crash, process restart 또는 Slack 재전송이 발생해도 동일 요청을 중복 실행하지 않는다.
복구 불가능한 상태는 조용히 성공으로 바꾸지 않고 operator action을 표시한다.

## Architecture Boundary

```text
Slack user
    |
    v
Hermes Slack App / Gateway             AMPLAI Slack App
conversation + request shaping         signed approval + status projection
    |                                          ^
    v                                          |
Hermes AMPLAI Plugin                           |
narrow tools, no shell/git/file                |
    | loopback bearer + idempotency            |
    v                                          |
AMPLAI Control Plane API -----------------------+
request/auth/idempotency/outbox
    |
    v
Project Store
CR / Work / Decision / Evidence / Event
    |
    v
Local Supervisor
managed worktree + lease + runner profile
    |
    +------------------+
    |                  |
Claude Code          Codex
`/design|/work`      `$design|$work`
```

Hermes는 Slack credential과 대화 session을 소유한다. AMPLAI는 project resolution, authority,
Work state, activation, evidence와 audit를 소유한다. Implementation Agent는 허용된 worktree 안의
구현과 검증만 소유한다.

## Trust Model

### Hermes Service Authority

Hermes plugin은 project-scoped service token을 사용한다. v1 permission은 다음으로 제한한다.

- `orchestration.request.submit`
- `orchestration.request.read`
- `orchestration.work.read`

Hermes token은 `Work` activation, human decision, Git publish, deployment와 canonical knowledge
write를 허용하지 않는다. Hermes가 전달한 Slack user ID는 routing/audit hint이며 authority가
아니다.

### Human Authority

Human authority는 AMPLAI Slack App이 받은 signed interaction에서만 만든다. AMPLAI는 기존
External Actor binding과 `AuthorityService`를 사용한다.

`activation.manage` permission과 one-time Activation Token이 모두 있어야 실행을 승인한다.
Hermes service token은 이 permission을 가질 수 없다.

### Agent Authority

Claude Code와 Codex는 `AMPLAI_WORK_ID`, lease token과 scoped worktree를 받는다. agent는 다른
`Work`, approval ledger, remote publish 또는 production operation 권한을 얻지 않는다.

## Slack Topology

두 Slack App을 사용한다.

| App | Connection | Responsibility | Secret Owner |
|---|---|---|---|
| Hermes Slack App | Socket Mode | 대화, request 접수, progress 질의 | Hermes profile |
| AMPLAI Slack App | signed HTTPS interaction + Web API | Activation Card, Proposal Card, authoritative status | AMPLAI runtime |

동일 Slack workspace와 allowlisted channel을 사용할 수 있다. token과 event subscription은 공유하지
않는다. 두 bot의 표시 이름과 message purpose를 구분한다.

## Request Contract

`OrchestrationRequest` v1은 다음 필드를 가진다.

```yaml
schema_version: 1
request_id: REQ-...
idempotency_key: slack:<workspace>:<message-or-thread>:<digest>
controller: status | design | work
goal: string
project_hint: string | null
target_app_hint: string | null
runner_hint: claude-code | codex | null
artifact_refs: [ART-...]
reply_route:
  provider: slack
  workspace_id: T...
  channel_id: C... | D...
  thread_id: string | null
submitted_at: RFC3339 UTC
```

`runner_hint`와 모든 `*_hint`는 요청값이다. AMPLAI가 active app registry, permission, compatibility와
capacity를 확인해 확정한다.

같은 `idempotency_key`와 같은 digest는 기존 결과를 반환한다. 같은 key와 다른 digest는
`IDEMPOTENCY_CONFLICT`다.

## Work Contract Extension

Project Store `Work`에 다음 additive field를 추가한다.

```yaml
controller: design | work
runner_profile: claude-code | codex
base_ref: immutable git commit
request_ref: REQ-...
```

기존 Work는 migration 시 `controller=work`, app binding의 현재 runner를 `runner_profile`로
해석한다. 기존 JSON을 rewrite하지 않는다.

Local app binding은 singular `runner`와 새 `runner_profiles`를 함께 읽는다. 새 환경은
`runner_profiles`와 `default_runner_profile`을 사용한다.

## State Flow

```text
REQUESTED
  -> RESOLUTION_HOLD
  -> DRAFT Work
  -> ACTIVATION_PENDING
  -> WAITING | READY
  -> CLAIMED
  -> RUNNING
  -> DONE | HUMAN_REQUIRED | BLOCKED | FAILED | CANCELLED
```

`status` 요청은 Work를 만들지 않는다. `design|work` 요청은 기본으로 `DRAFT`를 만든다.

Activation Action은 `DRAFT`만 승인한다. dependency가 남으면 `WAITING`, 모두 끝났으면 `READY`다.

## Managed Workspace

Supervisor는 사용자 main checkout에서 autonomous worker를 실행하지 않는다. 각 attempt는 host-local
managed worktree와 고정 `base_ref`를 사용한다.

retry와 session resume는 같은 worktree를 재사용한다. 다른 runner로 변경하면 기존 continuation을
재사용하지 않고 새 attempt와 explicit Decision을 만든다.

Supervisor는 commit, push, merge, release 또는 deploy를 자동 실행하지 않는다. worker 결과 branch와
evidence는 human publish gate 전까지 local 상태다.

## Memory Boundary

Hermes memory는 말투, 알림 선호와 현재 대화 context만 저장한다. Project architecture, decision,
policy와 완료 evidence는 Project Store 또는 governed Vault가 소유한다.

사용자가 명시적으로 기록을 요청한 경우에만 Hermes가 knowledge intake request를 제출한다. 결과는
Source와 Proposal까지이며 canonical Vault 자동 apply는 금지한다.

## Failure Contract

| Failure | Required Result |
|---|---|
| Hermes/Slack unavailable | Project Store와 running Work 유지, 재연결 후 조회 가능 |
| AMPLAI API unavailable | Hermes가 접수 실패를 표시, 성공 request ID 생성 금지 |
| Unknown project/app | `RESOLUTION_HOLD`, worker 미실행 |
| Unauthorized user | Activation 거부, Work state 불변, audit event 기록 |
| Duplicate Slack delivery | 기존 request/action result 반환 |
| Worker crash | lease expiry 후 bounded retry, attempt 증가 |
| Retry budget exhausted | `FAILED`, 자동 재시작 금지 |
| Result notification failure | Work state 유지, outbox retry, `DONE` 취소 금지 |
| Dirty/reused worktree conflict | claim 전 fail-closed, operator recovery 필요 |
| Secret in output | evidence reject 또는 redact, security event 기록 |

## Security Requirements

- Hermes Slack profile에서 `terminal`, `file`, `code_execution`, `delegation`, raw MCP toolset을 끈다.
- Hermes AMPLAI plugin은 fixed-schema tool만 제공한다.
- Control Plane API는 loopback bind가 기본이다.
- non-loopback bind는 bearer token, TLS proxy와 production approval이 필요하다.
- Slack allowlisted user/channel과 AMPLAI Actor binding을 별도로 확인한다.
- raw prompt, API token, Slack token, signing secret와 lease token을 event/evidence에 기록하지 않는다.
- `--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`와 unattended
  `auto_start=true` 조합을 거부한다.

## Non-Goals

- Hermes가 Git, shell 또는 canonical knowledge를 직접 수정하는 기능
- Hermes가 human approval을 대신하는 기능
- 하나의 `Work`를 Claude Code와 Codex가 동시에 수정하는 기능
- 자동 merge, push, deploy 또는 release
- Telegram adapter 완료를 Slack+Hermes provider activation의 선행조건으로 두는 것
- Hermes memory 전체를 AMPLAI knowledge로 자동 수집하는 기능
- multi-host distributed lease와 remote merge orchestration

## Acceptance Criteria

### AC-001 Authorized Request

allowlisted Slack user가 Hermes에 `design|work`를 요청하면 하나의 `request_id`와 하나의 `DRAFT Work`
graph가 생성된다. request, CR, Work와 reply route가 같은 correlation ID를 가진다.

### AC-002 Unauthorized And Ambiguous Request

unauthorized user, unknown project 또는 ambiguous target app은 `READY|RUNNING`을 만들지 않는다.
검증 가능한 hold/deny code를 반환한다.

### AC-003 Hermes Least Privilege

Slack Hermes profile에는 AMPLAI plugin tool과 safe conversational tool만 보인다. terminal, file edit,
delegation과 agent-launch tool schema가 보이지 않는다.

### AC-004 Activation Authority

Hermes tool call만으로 `DRAFT`를 activate할 수 없다. AMPLAI Slack App의 유효한 human click만
expected revision/digest와 token을 검사한 뒤 상태를 바꾼다.

### AC-005 Idempotency

request submit, activation click, Project Store bridge와 Slack projection의 duplicate를 주입해도
Work 하나와 action result 하나만 존재한다.

### AC-006 Runner Selection

`runner_profile=claude-code`는 `/design|/work`, `runner_profile=codex`는 `$design|$work` prompt를
생성한다. 저장된 Work 의미는 host와 무관하다.

### AC-007 Workspace Isolation

worker는 고정 `base_ref`의 managed worktree에서만 실행한다. 사용자 main checkout과 다른 active
Work의 파일은 바뀌지 않는다.

### AC-008 Claude Code Fresh And Resume

실제 Claude Code account로 fresh Work와 같은 Work resume를 각각 한 번 완료한다. session ID,
terminal state와 verifier evidence를 회수한다.

### AC-009 Codex Fresh And Resume

실제 Codex account로 fresh Work와 같은 Work resume를 각각 한 번 완료한다. thread ID,
terminal state와 verifier evidence를 회수한다.

### AC-010 Crash Recovery

worker kill, supervisor restart, lease expiry와 retry budget exhaustion을 주입한다. 같은 attempt의
중복 실행이 없고 terminal state가 계약과 일치한다.

### AC-011 Result Projection

`RUNNING`, `HUMAN_REQUIRED`, `FAILED`, `DONE` 상태가 authoritative Slack status message에
projection된다. message는 Work ID와 evidence summary를 포함하고 secret은 포함하지 않는다.

### AC-012 Provider-Scoped Activation

Slack+Hermes activation은 Telegram 상태와 독립적이다. `auto_start=false`가 기본이며 provider,
project, feature 단위 evidence가 모두 있을 때만 켠다.

### AC-013 Knowledge Safety

Hermes 대화는 canonical Vault에 자동 기록되지 않는다. 명시적 기록 요청도 Source와 Proposal에서
멈춘다.

### AC-014 Rollback

Hermes plugin과 `auto_start`를 끄면 기존 CLI, Slack Proposal Card, Claude Code와 Codex 직접
workflow가 그대로 동작한다. schema migration은 기존 Work를 읽는다.

### AC-015 Full Gate

pytest, Ruff, mypy, knowledge lint, docs freshness, verifier `v2`, Hermes plugin contract test,
Slack sandbox E2E, real Slack E2E와 두 native host E2E가 모두 evidence를 남긴다.

## Design Status

기술 설계와 executable acceptance는 완료 상태다. 사용자는 2026-09-02에 D-059~D-064와
`architecture_change`, `public_contract` 구현을 승인했다. 실제 Slack App 설치, credential 발급,
service enable과 `auto_start=true`는 구현과 분리된 `production_operation` evidence gate로 남는다.
