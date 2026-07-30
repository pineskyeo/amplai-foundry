# Current Item — MGC-009

## Goal

Approved Proposal snapshot에만 유효한 one-time `ApplyGrant`를 발급하고,
Apply request가 Proposal state·Grant·idempotency result·Audit·Outbox·`ApplyJob`을
하나의 SQLite transaction으로 생성하도록 한다.

## Frozen Acceptance

- A1: `ApprovedSnapshot`, `ApplyGrant`, Apply request result, `ApplyJob` schema와 required index·constraint·trigger가 새 migration과 schema verification에 고정됨
- A2: `ApplyGrant` 발급은 verified approved-card request 또는 accepted approved Decision result에서만 시작하며 `ProposalRef`만 받는 public issuance API가 없음
- A3: raw Grant는 caller에게 한 번만 반환하고 DB·Audit·Outbox·log에는 `grant_hash`만 저장됨
- A4: Grant가 qualified ProposalRef, approved snapshot digest, content/state revision, decision epoch, human Actor, Channel, TTL과 허용 action에 결합됨
- A5: Grant 발급과 Apply request 모두 server-created Authority와 `proposal.request_apply` permission을 요구함
- A6: Apply request는 replay result를 mutation validation보다 먼저 조회하고 같은 key·fingerprint에는 최초 Job 결과를 반환하며 다른 fingerprint에는 `IDEMPOTENCY_CONFLICT`를 반환함
- A7: Apply request transaction이 approved state·snapshot·Grant hash/Actor/Channel/TTL·active Job 부재를 검증하고 `approved → apply_requested`, Grant consume, queued Job, result, Audit·Outbox를 원자적으로 commit함
- A8: Apply request가 실패하면 Proposal, Grant, Job, result, Audit, aggregate sequence와 Outbox가 모두 rollback됨
- A9: 같은 Proposal snapshot에 대한 동시 Apply request에서 active ApplyJob은 정확히 하나만 생성되고 loser는 replay 또는 conflict로 종료됨
- A10: stale digest/revision/epoch, expired·consumed·wrong-Actor·wrong-Channel Grant는 상태 변경 없이 거부됨
- A11: Decision용 `ActionToken`은 Apply request credential로 사용할 수 없고 Apply용 `ApplyGrant`는 Decision credential로 사용할 수 없음
- A12: ApplyJob은 qualified ProposalRef, approved snapshot digest, expected base revision, state, attempts, lease owner/expiry, fencing token과 staging fields를 보유함
- A13: Job claim은 lease와 monotonic fencing token을 사용하고 stale heartbeat·finalize·staging update를 거부함
- A14: Worker는 canonical branch/ref 또는 canonical working tree를 변경할 authority를 갖지 않으며 MGC-009는 staging artifact와 publish request 입력까지만 생성함
- A15: token/grant secret scan, replay, rollback injection, concurrent request/claim, lease expiry와 stale fencing integration test가 통과함
- A16: 전체 regression과 `amplai-foundry verify`가 통과함
- A17: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Approved snapshot의 durable identity와 digest binding
- ApplyGrant issue·hash-only persistence·TTL·consume·replay
- Atomic Apply request와 active-job uniqueness
- ApplyJob queue, lease, heartbeat, retry-ready staging state
- Apply request와 Job lifecycle Audit·Outbox 연결
- Failure injection, concurrency와 fencing tests

## Out Of Scope

- canonical Git ref CAS와 publish intent/finalize → `MGC-010`
- legacy v2 Proposal import → `MGC-011`
- Slack·Telegram Apply button renderer → `MGC-012`, `MGC-013`
- Hermes Skill → `MGC-014`
- runtime activation UI와 kill switch → `MGC-015`

## Review Team

- Apply authority, Grant separation, and replay reviewer subagent
- ApplyJob transaction, concurrency, lease, and fencing reviewer subagent
- Migration, rollback, secret non-persistence, and acceptance-evidence reviewer subagent
