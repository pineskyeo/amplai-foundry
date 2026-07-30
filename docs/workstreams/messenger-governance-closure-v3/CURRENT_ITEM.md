# Current Item — MGC-008

## Goal

Accepted governance mutation과 audit·projection event를 같은 SQLite transaction에 기록하고,
destination별 strict ordering·retry·supersede·DLQ를 강제한다.

## Frozen Acceptance

- A1: Governance Audit·Outbox·destination cursor·delivery lease·DLQ schema가 migration과 schema verification에 고정됨
- A2: Accepted Decision transaction이 Proposal state·Token·idempotency result·aggregate sequence·Audit·Outbox를 원자적으로 commit함
- A3: Audit event는 aggregate별 strict sequence와 `previous_event_hash`·`event_hash` chain을 가지며 update/delete가 DB trigger로 거부됨
- A4: Startup reconciliation이 aggregate sequence gap·hash chain corruption·Audit/Outbox 불일치를 fail-closed함
- A5: Outbox event가 qualified aggregate, aggregate sequence, destination, destination sequence, payload digest와 immutable payload를 보유함
- A6: 같은 destination의 앞 event가 terminal 상태가 아니면 다음 event를 claim·deliver하지 못함
- A7: Dispatcher claim이 lease와 monotonic fencing token으로 직렬화되고 stale Dispatcher의 finalize가 거부됨
- A8: Retry는 bounded attempt와 backoff를 적용하며 한도 초과 또는 reconcile 불가 event를 DLQ와 operator hold로 이동함
- A9: 완전 대체 가능한 최신 pending event만 이전 event를 `superseded`로 전환하고 destination sequence continuity를 보존함
- A10: send 성공 후 local mark 전 crash를 remote reconcile로 복구하고 duplicate delivery 결과를 멱등 처리함
- A11: YAML projection이 `source_state_revision`과 `aggregate_sequence` CAS를 적용하고 reverse-order write를 거부함
- A12: Audit·Outbox insert 실패는 accepted Decision 전체를 rollback하며 raw ActionToken이 Audit·Outbox payload에 존재하지 않음
- A13: reverse order·competing Dispatcher·supersede·retry/DLQ·post-send crash·YAML CAS integration test가 통과함
- A14: 기존 Decision/Ingress replay와 전체 regression을 포함한 `amplai-foundry verify`가 통과함
- A15: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Aggregate sequence와 append-only hash-chained Audit
- Accepted Decision transaction의 Audit·Outbox atomic append
- Destination sequence·lease·fencing·retry·supersede·DLQ
- Generic Provider projection dispatcher contract
- YAML projection sequence CAS
- Startup integrity reconciliation과 failure injection

## Out Of Scope

- ApplyGrant와 Apply Job 구현 → `MGC-009`
- Fenced canonical publish → `MGC-010`
- Slack·Telegram Provider adapter → `MGC-012`, `MGC-013`
- Runtime activation UI와 kill switch → `MGC-015`

## Review Team

- Transaction and Audit integrity reviewer subagent
- Ordering, lease, retry, and crash-recovery reviewer subagent
- Projection and acceptance-evidence reviewer subagent
