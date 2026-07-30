# Current Item — MGC-001

## Goal

Mutable governance state의 기반이 되는 SQLite Store를 추가한다.

## Frozen Acceptance

- A1: Versioned SQLite schema와 migration entrypoint 존재
- A2: Local-filesystem-only runtime guard 존재
- A3: Required PRAGMA 검증 존재
- A4: 짧은 transaction helper와 rollback test 존재
- A5: Startup integrity/schema check 존재
- A6: Application behavior와 public API 미변경
- A7: 기존 test와 `amplai-foundry verify` 통과
- A8: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Governance DB connection factory
- Initial schema
- Schema version table
- Migration runner
- Transaction boundary helper
- Runtime profile validation
- Unit/integration test

## Out Of Scope

- Proposal YAML migration → `MGC-011`
- ActionToken v3 → `MGC-004`
- Provider ingress → `MGC-005`
- Outbox dispatcher → `MGC-008`
- Apply Worker → `MGC-010`

## Review Team

- Contract reviewer subagent
- Failure/recovery reviewer subagent
- Regression/test reviewer subagent
