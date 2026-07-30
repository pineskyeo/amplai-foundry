# Current Item — MGC-003

## Goal

Verified definition object를 가리키는 qualified active pointer와 Proposal state machine을
SQLite CAS로 구현한다.

## Frozen Acceptance

- A1: Active Proposal과 definition revision을 qualified `ProposalRef` composite key로 저장함
- A2: Initial activation은 존재하고 digest가 검증된 immutable definition만 가리킴
- A3: Definition activation은 expected active digest와 `state_revision` CAS를 원자적으로 검증함
- A4: Definition revision 성공 시 `content_revision`, `state_revision`, `decision_epoch`를 함께 증가시키고 status를 `draft`로 설정함
- A5: Proposal state machine의 허용 전이만 수행하고 terminal state 전이를 거부함
- A6: State-only 전이는 `state_revision`만 증가시키고 active definition과 `content_revision`을 유지함
- A7: CAS conflict와 invalid transition은 어떤 Proposal field도 변경하지 않음
- A8: Definition object 검증과 모든 filesystem I/O를 SQLite transaction 시작 전에 완료함
- A9: 모든 public query/mutation이 qualified `ProposalRef` 또는 `ProjectRef`를 요구함
- A10: ActionToken consume/revoke, audit/outbox와 Apply state side effect는 변경하지 않음
- A11: 기존 test와 `amplai-foundry verify` 통과
- A12: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Active Proposal SQLite schema와 repository
- Immutable definition revision record
- Expected digest와 `state_revision` CAS
- `content_revision`, `state_revision`, `decision_epoch`
- Proposal state machine과 terminal state enforcement
- Qualified get/list/mutation API
- CAS conflict와 rollback test
- Filesystem-I/O-outside-transaction test

## Out Of Scope

- ActionToken consume/revoke와 decision replay → `MGC-004`
- Provider ingress → `MGC-005`
- Actor authority integration → `MGC-006`
- Audit/outbox transaction → `MGC-008`
- Apply request와 Apply job → `MGC-009`
- 기존 Proposal YAML migration → `MGC-011`

## Review Team

- CAS/state contract reviewer subagent
- Transaction/failure reviewer subagent
- Regression/test reviewer subagent
