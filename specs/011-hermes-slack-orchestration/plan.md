# Implementation Plan: Hermes Slack Work Orchestration

## Design Result

기존 `MGC-014`, `MGC-015`, `MGC-016`을 다음 순서로 실행한다. `MGC-012` Package 5 closure는
Slack baseline dependency다.

```text
S00 Slack Package 5 closure
  -> S01 orchestration contracts
  -> S02 Control Plane request API + Project Store bridge
  -> S03 Hermes plugin + restricted Slack profile
  -> S04 activation card + provider gate
  -> S05 runner profiles + managed workspace
  -> S06 native host E2E
  -> S07 Slack production E2E + closure
```

Telegram `MGC-013`은 전체 Messenger workstream closure에는 남는다. Slack+Hermes provider
activation은 Telegram과 독립적으로 검증한다.

## Component Plan

### Control Plane

`src/amplai_foundry/control_plane/`에 orchestration request API를 추가한다.

```text
POST /v1/projects/{project_id}/orchestration-requests
GET  /v1/projects/{project_id}/orchestration-requests/{request_id}
GET  /v1/projects/{project_id}/work/{work_id}
GET  /v1/projects/{project_id}/work/{work_id}/events
POST /v1/projects/{project_id}/artifacts
```

모든 POST는 bearer permission, `Idempotency-Key`, body limit와 content digest를 검사한다.

Control Plane transaction은 request와 durable outbox event를 함께 기록한다. Project Store write는
별도 bridge가 처리한다. 두 store 사이의 distributed transaction은 만들지 않는다.

### Project Store Bridge

bridge는 `orchestration.requested` event를 lease한다. 같은 `request_id`의 CR/Work graph가 있으면
기존 ref를 receipt로 돌려준다.

bridge adapter는 `ProjectStore` port를 구현한다. `src/amplai_foundry` domain은 Loop Kit script를
import하지 않는다. host adapter edge만 `scripts/amplai_runtime.py`를 사용한다.

receipt를 쓰기 전 crash가 나면 event가 재전달된다. Project Store의 `request_ref` unique guard가
중복 Work를 막는다.

### Hermes Plugin

standalone Hermes plugin package를 repository integration asset으로 만든다. Hermes core fork는
만들지 않는다.

plugin tool은 다섯 개다.

| Tool | Permission | Mutation |
|---|---|---|
| `amplai_submit_request` | `orchestration.request.submit` | DRAFT request만 생성 |
| `amplai_get_request` | `orchestration.request.read` | 없음 |
| `amplai_get_work` | `orchestration.work.read` | 없음 |
| `amplai_list_pending_actions` | `orchestration.work.read` | 없음 |
| `amplai_request_knowledge_intake` | `orchestration.request.submit` | Source/Proposal request만 생성 |

plugin handler는 subprocess shell을 열지 않는다. loopback HTTPS client가 fixed endpoint와 JSON
schema만 사용한다.

Slack profile은 plugin toolset만 활성화하고 `terminal`, `file`, `code_execution`, `delegation`을
비활성화한다. Hermes가 tool provenance를 model-visible field에서 받지 못해도 activation authority는
AMPLAI Slack App에서 직접 검증하므로 안전 경계가 유지된다.

### Activation Card

`WorkActivationCardPayload`와 `ActivationAction`을 Proposal action과 별도 aggregate로 둔다.

```yaml
request_ref: REQ-...
work_refs: [CR-...-W...]
expected_revision: 1
expected_digest: sha256:...
action: approve | reject | request_changes
actor_ref: ACT-...
channel_ref: {...}
action_token: opaque
idempotency_key: slack-interaction-id
occurred_at: RFC3339 UTC
```

기존 hash-only token store, Actor binding, Slack raw signature verification과 bounded ack worker를
재사용한다. activation transaction은 Project Store action event와 연결되는 durable intent를 만든다.

`approve`는 dependency에 따라 `WAITING|READY`를 만든다. `reject|request_changes`는 worker를
실행하지 않는다.

### Runner Profiles

Work schema에 `controller`, `runner_profile`, `base_ref`, `request_ref`를 additive로 추가한다.

local app binding은 다음 의미를 지원한다.

```yaml
default_runner_profile: claude-code
runner_profiles:
  claude-code:
    type: claude-code
    command: claude
    args: [minimum reviewed args]
  codex:
    type: codex
    command: codex
    args: [--sandbox, workspace-write]
```

정확한 model 이름은 operator config가 소유한다. design artifact에 임의 model을 고정하지 않는다.

`controller=design`은 host의 design entry point를 사용한다. `controller=work`는 work entry point를
사용한다.

### Managed Workspace

claim 전에 다음을 검증한다.

- immutable `base_ref`
- allowed repository
- existing active lease 없음
- managed path ownership
- branch name collision 없음
- bypass permission 조합 없음

attempt workspace는 Project Store host-local 경로 아래 둔다. worker가 종료해도 evidence/publish
검토가 끝날 때까지 보존한다.

cleanup은 terminal Work와 published/discarded decision을 확인한 뒤 별도 operator command로 한다.

### Slack Status Projection

authoritative 상태는 AMPLAI Slack App이 보낸다. Hermes의 conversational summary는 status source가
아니다.

기존 outbox marker와 history readback을 사용해 duplicate message를 막는다. status payload에는
`request_id`, `work_id`, state, attempt, verifier summary, next human action만 넣는다.

## Slice Contracts

### S00 Slack Baseline Closure

Scope는 기존 `specs/003-slack-proposal-card/`다.

- human ledger gate 반영
- Package 5 재freeze
- round 22 three-lens review
- blocker 0 gate
- target branch publish review

Exit: `MGC-012` Slack baseline이 gate를 가진다.

### S01 Contracts And Decisions

- `OrchestrationRequest` schema
- `ActivationAction` schema
- Project Store Work additive fields
- runner profile local schema
- proposed Decisions approval
- schema compatibility tests

Exit: old Project Store fixture와 new fixture가 모두 validate된다.

### S02 Request API And Bridge

- Control Plane endpoints
- scoped API permissions
- durable outbox
- Project Store bridge
- idempotent CR/Work graph creation
- resolution hold
- artifact quarantine

Exit: duplicate and crash matrix가 Work를 중복 생성하지 않는다.

### S03 Hermes Integration

- standalone plugin
- fixed-schema tools
- restricted Slack profile template
- setup verifier
- token rotation runbook
- sandbox integration tests

Exit: Hermes Slack session에서 forbidden tool schema가 0개다.

### S04 Activation And Provider Gate

- Activation Card renderer
- signed Slack interaction adapter
- hash-only one-time token
- `activation.manage` authority
- provider/project/feature evidence gate
- `auto_start=false` default

Exit: Hermes service credential로 activation이 불가능하다.

### S05 Host Dispatch And Workspace

- multi-profile local binding
- controller-specific prompt
- managed worktree lifecycle
- runner lock and continuation
- permission/sandbox policy checks

Exit: Claude Code와 Codex command construction, redaction, resume가 deterministic test를 통과한다.

### S06 Native Host Verification

Claude Code와 Codex 각각 다음을 실행한다.

1. fresh no-op documentation fixture Work
2. paused/resumed fixture Work
3. verifier evidence 기록
4. terminal transition
5. prompt/token/secret absence 검사

`amplai-foundry`, `synapse`, `cortex`는 최소 한 번씩 target app이 된다. 두 runner는 각각 fresh와
resume evidence를 가진다.

### S07 Production Path Closure

- real Slack workspace request
- human activation click
- real worker execution
- failure injection
- result projection
- rollback rehearsal
- docs freshness
- three-lens review

Exit: contract, failure/recovery, regression lens blocker 0이다.

## Migration

1. new schema reader를 먼저 배포한다.
2. 기존 singular runner binding을 계속 읽는다.
3. new Work만 runner fields를 쓴다.
4. Hermes plugin은 feature flag off로 설치한다.
5. sandbox Slack workspace에서 provider gate를 연다.
6. production workspace는 별도 `production_operation` approval 뒤 연다.

rollback은 Hermes plugin disable, provider activation off, Supervisor `auto_start=false` 순서다.
기존 direct CLI와 Proposal Card path는 유지한다.

## Verification Matrix

| Layer | Verification |
|---|---|
| Schema | old/new fixtures, JSON Schema, migration read |
| Control Plane | auth, idempotency, body bound, store outage |
| Bridge | duplicate, crash-before-receipt, retry, conflict |
| Hermes | tool allowlist, no terminal, token scope, bad payload |
| Slack | signature, actor/channel binding, stale/expired/replay |
| Supervisor | lease, concurrency, dirty workspace, timeout, retry cap |
| Hosts | Claude fresh/resume, Codex fresh/resume |
| Fleet | foundry/synapse/cortex target Work |
| Security | prompt/token/secret redaction, config permissions |
| Operations | restart, rollback, notification retry |

## Required Commands

```bash
python3 scripts/loopctl.py doctor
python3 scripts/loopctl.py contract validate specs/011-hermes-slack-orchestration/work-contract.json
python3 scripts/loopctl.py readiness evaluate specs/011-hermes-slack-orchestration/knowledge-readiness.json
python3 scripts/loopctl.py context validate specs/011-hermes-slack-orchestration/context-pack.json
python3 scripts/loopctl.py docs validate --repo
.venv/bin/python .ai-team/verifiers/run.py --profile v2
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
```

real Slack와 native account E2E는 별도 operational evidence를 남긴다. 실행하지 않은 E2E를 PASS로
기록하지 않는다.

## Human Gates

- `architecture_change`: 두 Slack App, bridge/outbox, managed workspace
- `public_contract`: Work fields, API routes, Activation Action
- `production_operation`: Slack App 설치, token 발급, service enable, `auto_start=true`
- `git_publish`: commit, push, PR, merge

## First Work Slice

첫 구현 Slice는 `S00 Slack Baseline Closure`다. 현재 branch의 `MGC-012` Package 5를 닫기 전에는
새 Slack activation path를 열지 않는다.
