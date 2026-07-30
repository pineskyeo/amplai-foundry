# Current Item — MGC-011

## Goal

Legacy v2 Proposal artifact와 audit evidence를 변경하지 않고 deterministic snapshot으로
고정한 뒤, qualified v3 Proposal definition·state·Audit·Outbox로 import한다. Approval
evidence가 없는 approved/applied Proposal은 synthetic approval evidence와
`legacy_approval_review_required` hold를 생성하며 human review 전 ApplyGrant를 금지한다.

## Frozen Acceptance

- A1: import는 operator가 legacy mutation freeze를 명시적으로 증명한 경우에만 시작하며 source root, Project identity와 canonical path를 검증함
- A2: snapshot은 모든 imported file의 relative path, byte length, SHA-256과 deterministic aggregate digest를 보존하고 symlink, non-regular file, path escape와 scan 중 mutation을 거부함
- A3: import 전 Project Pack snapshot과 Governance DB backup identity·digest를 durable migration root에 기록하고 backup 검증 실패 시 import하지 않음
- A4: dry-run은 legacy Proposal을 strict schema로 읽고 qualified ProjectRef·ProposalRef, source status/revision, target status와 evidence disposition을 deterministic plan으로 만들며 authoritative DB/object store/legacy files를 수정하지 않음
- A5: import는 legacy bytes와 mapping policy를 결합한 canonical v3 definition/input object를 생성하고 exact digest를 immutable object store에서 재검증함
- A6: qualified Proposal row, definition revision, source revision과 lifecycle mapping이 하나의 migration transaction에서 생성되며 다른 Project의 같은 local Proposal ID와 충돌하지 않음
- A7: legacy approval Audit가 존재하면 actor/time/action/idempotency evidence를 검증해 import하고, 없으면 `migration.synthetic_approval` event와 reason/source artifact digest를 생성함
- A8: synthetic approval 대상은 `legacy_approval_review_required`로 import되고 human re-review 전 ActionToken decision과 ApplyGrant 발급이 모두 거부됨
- A9: imported Audit는 append-only hash chain에 연결되고 imported idempotency identity는 replay와 payload conflict를 구분함
- A10: imported authoritative state에 대응하는 ordered Outbox projection을 생성하고 source revision·destination sequence가 기존 stream과 단조 증가함
- A11: 동일 snapshot/plan 재실행은 같은 완료 결과를 반환하고 다른 bytes 또는 mapping으로 같은 migration identity를 재사용하면 conflict로 실패함
- A12: import transaction 또는 late Audit·Outbox 실패는 Proposal row, object activation, migration counters와 activation state를 부분 commit하지 않음
- A13: verification은 source/import count, per-file hash, aggregate snapshot/plan digest, Proposal state/revision, Audit/idempotency와 projection roots를 양방향 검증함
- A14: rollback은 activation 전 완료된 migration만 exact imported root 집합으로 되돌리고 legacy artifact와 기존 v3 row를 변경하지 않음
- A15: activation 후 rollback은 거부되며 correction은 forward-recovery migration으로만 수행함
- A16: dry-run, import interruption, duplicate replay, hash/count mismatch, cross-project same local ID, synthetic approval hold, rollback 전후와 activation 후 rollback 거부 integration test가 통과함
- A17: 전체 regression과 `amplai-foundry verify`가 통과하고 Contract·Evidence·Ops subagent review의 P0/P1/Blocking-P2가 0건임

## Slices

1. Deterministic legacy snapshot and dry-run plan
2. Atomic qualified definition and Proposal state import
3. Audit/idempotency import, synthetic approval hold, and ordered projection
4. Verification, pre-activation rollback, activation boundary, and recovery tests

## Slice Status

- Slice 1: `PASS` at `9b195d3`
- Slice 2: `PASS` at `91dab70`
- Slice 3: `PASS` at `53d2223`
- Slice 4 Package 4.1 — A13 verification: `PASS` at `ef6ef06`
- Slice 4 Package 4.2a — lifecycle activation and Outbox gate: `PASS` at `bebb28e`
- Slice 4 Package 4.2b1 — exact-root rollback plan and attested destination provenance:
  `PASS` at `c24724c`
- Full tests: `564/564 PASS`
- Full verification: `7/7 PASS`
- Review gate: Contract·Evidence·Ops `P0=0`, `P1=0`, `Blocking-P2=0`
- Retained advisory: `OPS-S2-06` — interrupted immutable object cleanup/accounting,
  owner `MGC-011 Slice 4`
- Retained advisory: activation fingerprint 형식, mixed multi-Proposal classification,
  direct Apply/Publish gate와 predecessor trigger 복구 evidence를 Package 4.2 closure에서 보강함
- Retained advisory: attestation failure injection, indirect dependent registry와 immutable
  object accounting을 Package 4.2 closure에서 보강함
- Next: Slice 4 Package 4.2b2 — A14 atomic exact-root rollback executor

## Out Of Scope

- legacy files의 rewrite 또는 삭제
- migration 중 canonical Git/Vault publish
- synthetic approval의 자동 human 승인
- Slack·Telegram Provider adapter
- activation UI와 production kill switch

## Review Team

- Legacy mapping, qualified identity, authority and lifecycle contract reviewer
- Snapshot, hash/count, migration evidence and regression reviewer
- Transaction interruption, replay, rollback, activation and operational safety reviewer
