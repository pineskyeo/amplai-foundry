# Current Item — MGC-002

## Goal

Proposal definition과 Apply input을 content-addressed immutable object로 저장한다.

## Frozen Acceptance

- A1: 모든 definition/input object path가 SHA-256 digest로 결정됨
- A2: canonicalization version 1이 deterministic bytes를 생성함
- A3: 같은 digest와 같은 bytes 재저장은 idempotent함
- A4: 같은 digest에 다른 bytes를 쓰거나 기존 object를 overwrite할 수 없음
- A5: temporary write, file fsync, atomic rename, parent directory fsync, post-write rehash를 수행함
- A6: object read 시 digest를 재검증하고 불일치하면 fail-closed함
- A7: object write 실패와 process interruption이 partial canonical object를 노출하지 않음
- A8: qualified `ProposalRef`만 받고 local ID 단독 API를 제공하지 않음
- A9: active pointer, state transition, Token revoke, audit/outbox는 변경하지 않음
- A10: 기존 test와 `amplai-foundry verify` 통과
- A11: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Proposal definition manifest canonicalization
- Definition/input digest 계산과 검증
- Project-local immutable object path
- Atomic object write protocol
- Object read integrity verification
- Idempotent retry와 collision/corruption rejection test
- Write interruption과 partial-file recovery test

## Out Of Scope

- Active definition pointer와 CAS → `MGC-003`
- Proposal state transition → `MGC-003`
- ActionToken와 decision replay → `MGC-004`
- Audit/outbox transaction → `MGC-008`
- 기존 Proposal YAML migration → `MGC-011`

## Review Team

- Immutable contract reviewer subagent
- Filesystem failure/recovery reviewer subagent
- Regression/test reviewer subagent
