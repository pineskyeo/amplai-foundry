# AMPLAI Roadmap

## Approved Roadmap

현재 상세 roadmap proposal은 다음 문서다.

- [AMPLAI Master Roadmap Proposal v2](roadmaps/AMPLAI_MASTER_ROADMAP_PROPOSAL_v2.md)
- [AMPLAI Master Roadmap Proposal v1](roadmaps/history/AMPLAI_MASTER_ROADMAP_PROPOSAL_v1.md)
- [Machine-readable Roadmap](../plans/amplai-master-roadmap.yaml)

`v2`는 2026-07-26 사용자 승인으로 공식 roadmap 기준이 됐다. 현재 실행 상태의
authoritative source는 machine-readable roadmap이며, 각 변경은 versioned
`RoadmapChangeProposal`과 승인/정책 gate를 거친다.

## Current Baseline

첨부된 `v2`는 2026-07-25 시점의 분석 snapshot을 포함한다. 현재 저장소의 구현 상태는 다음과 같다.

| Capability | Current Status |
|---|---|
| Phase 0 Golden Contract / Clean-clone Gate | 완료 |
| Qualified `(namespace, local_id)` Identity | 완료 |
| Fail-closed Project Resolution | 완료 |
| Governed Knowledge Intake + Minimal Evaluation | 완료 |
| Semantic Golden Set / Conflict Hold | 완료 |
| Roadmap Proposal / Review / Apply / Safe Tracker Policy | 완료 |
| Portable Full Project Pack / Deterministic Archive | 완료 |
| Canonical Corpus | 53 notes |
| Test Baseline | `amplai-foundry verify` 전체 gate 통과가 기준 |

이 표는 implementation delta만 기록한다. Roadmap Phase의 완료 판정은 machine-readable roadmap의 `definition_of_done`과 별도 Gate를 따른다.

## Current Focus

Phase 0과 Phase 1A~1C는 `0.2.0`에서 완료됐다.

- 같은 local ID를 여러 Project Pack이 안전하게 소유한다.
- 프로젝트/분류 불확실성은 canonical 변경 대신 영구 evaluation hold로 남는다.
- Source → Candidate → Compare → Proposal → Review/Policy → Apply가 재현 가능하다.
- Project Pack은 clone 경로와 무관하며 runtime을 제외한 deterministic archive로 이동한다.

`0.4.0`은 Phase 2 이전에 **Control Plane bounded context**를 더했다
(`src/amplai_foundry/control_plane/`, `D-056`). 기존 Governance Store와 Knowledge Vault를
다시 쓰지 않고 project-scoped token, idempotent write, durable job, event/outbox,
replayable projection을 분리된 경계로 갖는다. Phase 2의 domain module 설계는 이 경계를
전제로 한다.

현재 focus는 `Phase 2 — Ontology Kernel`이다. Phase 1 계약을 깨지 않고 domain
module, typed ontology object, constraint validation과 migration 경계를 설계하는 것이
다음 작업이다.

## Product Direction

사용자는 의미, 목적과 evidence를 제공한다. AMPLAI는 project scope, 저장 구조, knowledge type, relation, Proposal flow와 validation을 책임진다.

```text
Minimal Project Identity
→ Knowledge Intake & Steward
→ Full Project Pack
→ Ontology Kernel
→ DC Test Vertical Slice
→ Context Runtime
→ External Agent Harness
→ Development/Management Agents
→ Evaluation Observatory
→ Meta-loop
→ Central/Hybrid Platform
```
