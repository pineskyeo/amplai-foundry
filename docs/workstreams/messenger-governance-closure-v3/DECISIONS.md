# Messenger Governance Closure V3 Decisions

## D-001 — State Authority

- Status: accepted
- Decision: SQLite가 active definition pointer와 mutable governance state의 authority다.
- Source: `AMP-SPEC-MGC-003#Authoritative-State`

## D-002 — Definition Storage

- Status: accepted
- Decision: Proposal definition과 Apply input은 immutable content-addressed object다.
- Source: `AMP-SPEC-MGC-003#Immutable-Definition-Contract`

## D-003 — Canonical Publish

- Status: accepted
- Decision: Worker는 staging만 수행하고 Publish Coordinator가 Git ref CAS를 소유한다.
- Source: `AMP-SPEC-MGC-003#Publish-Coordinator`

## D-004 — Review Gate

- Status: accepted
- Decision: 각 implementation item은 subagent code/contract/operations review를 통과한다.
- Source: 사용자 요청

## D-005 — Reachable Migration Immutability

- Status: accepted
- Decision: 이미 도달 가능한 schema migration body와 checksum은 변경하지 않고 보강은 새 additive migration으로 적용한다.
- Source: MGC-011 Slice 4 Package 4.2a compatibility review

## D-006 — Attested Rollback Provenance

- Status: accepted
- Decision: destination rollback provenance는 import Audit/Outbox 생성 전에 실제 이전 cursor와 command에 결합해 attest하며, 동시 attestation이 없는 predecessor evidence는 신뢰하지 않는다.
- Source: MGC-011 Slice 4 Package 4.2b1 operations review
