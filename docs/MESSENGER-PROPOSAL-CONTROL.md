# Messenger Proposal Control

## Goal

Hermes와 messenger는 요청 접수와 UI를 담당한다. AMPLAI는 qualified project identity, Actor authority, Proposal state, approval, apply와 audit를 소유한다.

이 계약은 Slack, Telegram, Web UI와 CLI가 같은 `ProposalAction` 의미를 사용하게 한다. Channel SDK는 Domain과 Application layer에 들어오지 않는다.

## Current Roadmap Boundary

Phase 0과 Phase 1A~1C는 완료 상태다. 이 구현은 완료 상태를 되돌리지 않고 기존 identity, intake와 Proposal contract를 hardening한다.

현재 machine-readable roadmap focus는 `phase-2-ontology-kernel`이다. Slack·Telegram adapter와 Hermes Skill은 external channel gate가 준비된 뒤 연결한다.

## Ownership

| Area | Owner |
|---|---|
| 자연어·파일 접수, Proposal Card render | Hermes / channel adapter |
| Project 확정 | AMPLAI `ProjectResolver` |
| Actor와 permission 확정 | AMPLAI authority boundary |
| Proposal version·digest와 state transition | AMPLAI Proposal action core |
| Canonical apply | AMPLAI apply service |
| 구현과 검증 | approved Worker |
| Audit | AMPLAI action ledger |

Hermes는 project hint를 제출할 수 있다. Hermes는 `AuthorityContext.permissions`를 만들거나 Proposal 상태를 직접 변경하지 않는다.

## Domain Contract

### ActorRef

`ActorRef`는 AMPLAI 내부 승인 주체다. `actor_type`은 `human`, `service`, `agent`다.

### ChannelRef

`ChannelRef`는 provider의 immutable ID와 message 위치를 기록한다. Display name은 identity나 authority 근거로 사용하지 않는다.

### AuthorityContext

`AuthorityContext`는 인증된 Actor, qualified `ProjectRef`, permission, request ID, channel과 인증 시각을 보존한다. Server가 External Actor binding과 policy를 확인한 뒤 생성한다.

### ProposalAction

```yaml
proposal_ref:
  project_ref:
    project_id: amplai
    namespace: org/default/project/amplai
  proposal_id: PROP-20260729-A1B2C3D4
expected_version: 1
expected_digest: sha256:...
action: approve
actor_ref:
  actor_id: ACT-USER-1
  actor_type: human
channel_ref: {defined by provider adapter}
action_token: opaque-token
idempotency_key: provider-interaction-id
occurred_at: 2026-07-29T17:00:00+09:00
```

`action`은 `approve`, `reject`, `request_changes`, `apply`다. Core v1은 첫 세 decision action을 처리한다.

## State Mapping

현재 Knowledge Proposal lifecycle을 유지한다.

```text
draft | reviewed
  → approved
  → rejected
  → changes_requested

approved
  → apply request gate
  → applied
```

`reviewed`는 messenger의 `awaiting_approval` 의미에 대응한다. `apply`는 decision action과 같은 transaction에서 실행하지 않는다.

## Concurrency And Replay

Proposal은 `revision`과 deterministic SHA-256 digest를 가진다. Action의 `expected_version`과 `expected_digest`가 current Proposal과 모두 일치해야 한다.

같은 `idempotency_key`와 같은 payload는 최초 결과를 반환한다. 같은 key의 다른 payload는 `IDEMPOTENCY_CONFLICT`다.

Action ledger는 idempotency result와 audit event를 하나의 immutable JSON artifact로 원자 기록한다. Ledger 기록이 실패하면 Proposal state를 이전 값으로 복원한다.

## Audit

각 accepted action은 다음을 기록한다.

- qualified `ProposalRef`
- Actor와 Channel
- before/after status
- expected version과 displayed digest
- request ID와 idempotency key
- occurred/processed time

Action token 원문은 audit artifact에 기록하지 않는다.

## Error Contract

| Code | Meaning |
|---|---|
| `PROPOSAL_NOT_FOUND` | qualified Proposal 불일치 또는 부재 |
| `PROPOSAL_STALE` | version 또는 digest 불일치 |
| `AUTHORITY_DENIED` | project 또는 permission 불일치 |
| `ACTION_ACTOR_MISMATCH` | action actor와 server authority 불일치 |
| `ACTION_CHANNEL_MISMATCH` | action channel과 인증 channel 불일치 |
| `INVALID_PROPOSAL_STATE` | decision action 불가 상태 |
| `IDEMPOTENCY_CONFLICT` | 같은 key의 다른 payload |
| `APPLY_REQUIRES_APPROVAL` | approved 이전 apply 요청 |
| `APPLY_ACTION_DEFERRED` | Worker/Token gate 전 apply 실행 차단 |
| `ACTION_BUSY` | 다른 action transaction 진행 중 |

Token verifier는 `ACTION_TOKEN_INVALID`, `ACTION_TOKEN_EXPIRED`, `ACTION_TOKEN_CONSUMED`, `ACTION_ACTOR_MISMATCH`, `ACTION_CHANNEL_MISMATCH`를 fail-closed로 반환한다.

## External Channel Gate

Slack·Telegram decision UI를 활성화하기 전에 다음을 완료한다.

- [x] External Actor binding repository
- [x] Server-created `AuthorityContext`
- [ ] Hash-only one-time Action Token store
- [ ] Token expiry와 atomic consume
- [ ] Slack signature와 timestamp 검증
- [ ] Telegram webhook secret와 chat policy 검증
- [ ] Proposal Card adapter contract
- [ ] apply request와 Worker isolation
- [ ] provider retry E2E

이 gate 전에는 core contract test와 read-only Proposal Card prototype만 허용한다.

## Verification

`tests/test_proposal_actions.py`는 approve, reject, request changes, stale action, actor/channel/permission mismatch, invalid token, replay와 deferred apply를 검증한다.

Committed JSON Schema는 다음이다.

- `schemas/authority-context.schema.json`
- `schemas/external-actor-binding.schema.json`
- `schemas/proposal-action.schema.json`
- `schemas/proposal-action-audit.schema.json`
