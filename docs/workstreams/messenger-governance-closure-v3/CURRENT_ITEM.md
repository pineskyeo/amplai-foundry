# Current Item — MGC-010

## Status

PASS at `9f3215ff9549f89749bdd805eea5fd783f6b5cb1`.

- A1–A17 satisfied
- Full `amplai-foundry verify`: 7/7 PASS
- Subagent review: Contract PASS, Evidence PASS, Ops PASS
- Final blocking counts: P0 0, P1 0, Blocking-P2 0
- Advisory: Git ref observation currently holds the SQLite writer transaction; a narrower
  per-project serialization boundary and a permanent populated-v14 strict-dispatch fixture are
  follow-up hardening items.

## Goal

`publish_pending` ApplyJob의 immutable staging 결과를 durable `PublishIntent`로 준비하고,
Publish Coordinator만 canonical Git ref를 compare-and-swap한 뒤 authoritative state를
원자적으로 finalize하거나 명시적인 recovery hold로 전환하도록 한다.

## Frozen Acceptance

- A1: `PublishIntent`, Publish result, Project publish gate와 required index·constraint·immutable trigger가 새 migration과 schema verification에 고정됨
- A2: Publish Coordinator가 canonical Git ref와 canonical working tree의 유일한 writer이며 Worker·일반 Governance service에 canonical write API가 노출되지 않음
- A3: candidate commit은 isolated staging input에서 생성되고 approved snapshot, expected base revision, staged artifact bytes/digest, publish-input bytes/digest와 tree digest를 서버가 다시 검증함
- A4: prepare transaction이 current `publish_pending` Job과 fencing token, immutable roots, approved snapshot/base revision, candidate commit/tree, 현재 canonical ref와 Project active-publish 부재를 검증함
- A5: prepare가 qualified Project·Proposal·Job과 `expected_old_ref`, `candidate_commit`, tree/input digests를 결합한 immutable `prepared` intent를 생성하고 Project publish gate를 같은 transaction에서 잠금
- A6: `prepared` intent는 TTL 또는 Worker lease 만료로 reclaim·overwrite·cancel되지 않으며 오직 Publish/Recovery Coordinator의 명시적인 상태 전이로 해제됨
- A7: canonical publication은 durable `prepared` intent를 먼저 다시 읽고 candidate commit/tree digest를 검증한 뒤 exact `Git ref CAS(expected_old_ref, candidate_commit)`만 수행하며 force update를 사용하지 않음
- A8: Git CAS가 `expected_old_ref` 불일치로 실패하면 canonical ref를 변경하지 않고 durable `publish_conflict`와 Job `recovery_hold`를 기록함
- A9: CAS 성공 후 finalize transaction이 actual Git ref를 다시 읽고 Publish result, Job `succeeded`, Proposal `apply_requested → applied`, applied revision, Audit·Outbox와 publish gate release를 원자적으로 commit함
- A10: finalize 전 오류는 성공으로 추정하지 않으며 recovery가 actual ref를 읽어 `expected_old_ref`면 retry/cancel, `candidate_commit`이면 finalize, 다른 ref면 `publish_conflict`와 `recovery_hold`로 결정함
- A11: Git CAS 후 process hard-kill과 commit ambiguity에서도 recovery가 같은 candidate를 중복 publish하지 않고 최초 결과를 확정하거나 명시적인 hold를 생성함
- A12: competing Coordinator와 stale Worker/fencing token은 하나의 active prepared intent·하나의 canonical ref transition·하나의 terminal result만 만들 수 있음
- A13: non-retryable publish failure는 Proposal을 `apply_failed`로 전환하고, ambiguous outcome은 Proposal `apply_requested`와 Job `recovery_hold`를 유지함
- A14: prepare/finalize/recovery의 DB failure 또는 late Audit·Outbox failure가 authoritative state, result, aggregate sequence, destination cursor와 gate를 부분 commit하지 않음
- A15: prepared-kill, pre-CAS kill, post-CAS/pre-finalize kill, base-ref conflict, ambiguous-ref recovery, competing Coordinator와 stale Worker integration test가 통과함
- A16: 전체 regression과 `amplai-foundry verify`가 통과함
- A17: Subagent review P0/P1/Blocking-P2 0건

## In Scope

- Immutable PublishIntent·Publish result와 Project-scoped publish gate
- Isolated candidate commit/tree verification
- Publish Coordinator 전용 Git ref CAS
- Publish finalize transaction과 ordered Audit·Outbox
- Prepared-intent startup/runtime recovery
- Hard-kill, CAS conflict, ambiguity와 competing Coordinator tests

## Out Of Scope

- legacy v2 Proposal import → `MGC-011`
- Slack·Telegram Provider adapter → `MGC-012`, `MGC-013`
- Hermes Skill → `MGC-014`
- runtime activation UI와 kill switch → `MGC-015`
- Git 이외 canonical publish backend

## Review Team

- Publish intent, Git CAS, and sole-writer boundary reviewer subagent
- Crash recovery, ambiguity, concurrency, and fencing reviewer subagent
- Migration, rollback, Audit/Outbox, and acceptance-evidence reviewer subagent
