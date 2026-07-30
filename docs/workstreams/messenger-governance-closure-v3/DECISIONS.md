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

## D-007 — Versioned Rollback Evidence Compatibility

- Status: accepted
- Decision: reachable v28 rollback evidence는 `scope_version=1`로 그대로 보존한다.
  신규 synthetic hold provenance와 exact deletion은 additive v29의
  `scope_version=2`로만 생성한다. v1 synthetic hold는 삭제하지 않고 exact migration
  evidence로 검증하며 terminal rolled-back gate가 mutation을 차단한다.
- Source: MGC-011 Slice 4 Package 4.2b2 compatibility review

## D-008 — Forward Recovery Eligibility And Evidence

- Status: accepted
- Decision: activation 이후 `v3_definition_revision` recovery plan은 `draft` 또는
  `changes_requested` Proposal만 허용한다. applied/dependent root와 unresolved synthetic
  approval hold는 차단하며, activated migration의 immutable definition/import command/Audit/Outbox
  graph는 계속 reconciliation한다.
- Source: MGC-011 Slice 4 Package 4.2c1 Contract·Evidence·Ops review

## D-009 — Atomic Forward Recovery Execution

- Status: accepted
- Decision: activation 후 definition correction은 authenticated recovery plan과 exact
  activation/lifecycle root를 검증한 뒤 하나의 transaction에서 Proposal CAS, immutable
  definition revision, Audit, Outbox와 digest-bound recovery result를 함께 commit한다.
  Recovery evidence는 exact definition revision에 결합하며 startup reconciliation이
  command, item, result, Audit, Outbox와 historical definition provenance 변조를 차단한다.
- Source: MGC-011 Slice 4 Package 4.2c2 Contract·Evidence·Ops review

## D-010 — V2 Migration Closure

- Status: accepted
- Decision: MGC-011 A1–A17은 `c3d635f`에서 PASS다. Activation 후 legacy
  definition correction은 active transaction의 repository-local recovery scope와 durable
  command evidence를 함께 요구하며 public 또는 committed-incomplete command 우회를
  차단한다. MGC-012 Slack Reference Adapter로 진행한다.
- Source: MGC-011 final Contract·Evidence·Ops gate

## D-011 — Slack Authentication Boundary

- Status: accepted
- Decision: Slack message button ingress는 raw form body의 `v0` HMAC과 5-minute
  timestamp를 deserialize 전에 검증한다. Installation identity는 기존 Authority 계약과
  동일한 단일 `workspace_id:api_app_id` 형식이다. Parser는 strict UTF-8, duplicate-key
  rejection과 body/depth/node/string budget을 적용하며 unsupported surface는 fail-closed한다.
- Source: MGC-012 Package 1 Contract·Evidence·Ops review
