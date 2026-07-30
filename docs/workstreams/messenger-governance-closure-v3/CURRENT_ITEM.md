# Current Item — MGC-004

## Goal

Decision 전용 hash-only `ActionToken`과 성공 결과 우선 replay를 구현한다. 승인된 decision은
Proposal state, Token 소비와 durable command result를 하나의 SQLite transaction으로 commit한다.

## Frozen Acceptance

- A1: `approve`, `reject`, `request_changes`마다 별도 ActionToken을 발급함
- A2: encoded raw Token은 64 bytes 이하이며 발급 시 한 번만 반환하고 DB에 hash만 저장함
- A3: Token은 qualified Proposal, active definition digest, content/state revision, decision epoch, action, human Actor와 Channel에 고정됨
- A4: Decision은 `reviewed` Proposal과 일치하는 issued·미만료 Token에만 허용됨
- A5: 성공 decision이 Proposal state/state revision, Token `consumed`, durable command result를 한 SQLite transaction으로 commit함
- A6: replay lookup을 live Token·Proposal stale 검증보다 먼저 수행하고 동일 성공 command replay가 최초 결과를 반환함
- A7: 같은 idempotency key에 다른 fingerprint가 오면 `IDEMPOTENCY_CONFLICT`이며 어떤 상태도 변경하지 않음
- A8: invalid/expired/consumed/Actor·Channel·Action mismatch, stale Proposal과 invalid transition이 Proposal과 Token을 변경하지 않음
- A9: definition revision으로 decision epoch가 바뀌면 이전 epoch의 issued Token을 같은 transaction에서 revoke함
- A10: public decision entrypoint가 repository `transition_state`를 단독 호출하지 않고 connection-bound orchestration만 사용함
- A11: raw Token이 DB, log, audit/outbox용 durable payload에 존재하지 않음을 test로 검증함
- A12: existing test와 `amplai-foundry verify` 통과
- A13: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- ActionToken schema, hash와 issuance
- Token lifecycle `issued → consumed | expired | revoked`
- Decision command fingerprint와 durable result
- Replay precedence와 idempotency conflict
- Atomic decision transaction
- Definition revision 시 이전 epoch Token revoke
- Failure, rollback, concurrency와 raw-secret absence test

## Out Of Scope

- Provider signature와 durable ingress/ack → `MGC-005`
- Actor binding과 server-created AuthorityContext → `MGC-006`
- Production CLI/Intake mutation 경로 폐쇄 → `MGC-007`
- Audit/outbox append와 projection → `MGC-008`
- ApprovedSnapshot과 ApplyGrant/Job → `MGC-009`

## Review Team

- Token secrecy and credential contract reviewer subagent
- Decision transaction and replay reviewer subagent
- Failure/concurrency evidence reviewer subagent
