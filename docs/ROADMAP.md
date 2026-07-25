# AMPLAI Roadmap

## Canonical Proposal

현재 상세 roadmap proposal은 다음 문서다.

- [AMPLAI Master Roadmap Proposal v2](roadmaps/AMPLAI_MASTER_ROADMAP_PROPOSAL_v2.md)
- [AMPLAI Master Roadmap Proposal v1](roadmaps/history/AMPLAI_MASTER_ROADMAP_PROPOSAL_v1.md)
- [Machine-readable Roadmap](../plans/amplai-master-roadmap.yaml)

`v2`는 review proposal이다. 각 Phase의 Architecture와 Decision은 별도 Proposal과 승인 절차를 거쳐 확정한다.

## Current Baseline

첨부된 `v2`는 2026-07-25 시점의 분석 snapshot을 포함한다. 현재 저장소의 구현 상태는 다음과 같다.

| Capability | Current Status |
|---|---|
| Canonical Memory Foundation | 구현 |
| Immutable Source Ingestion | 구현 |
| Lexical Search | 구현 |
| Proposal Validate/Diff/Approve/Apply | 구현 |
| Source Immutability | 구현 |
| Optimistic Concurrency | 구현 |
| Proposal Project Boundary | 구현 |
| Untrusted Source Context Boundary | 구현 |
| Canonical Corpus | 39 notes |
| Test Baseline | 122 passed |

이 표는 implementation delta만 기록한다. Roadmap Phase의 완료 판정은 machine-readable roadmap의 `definition_of_done`과 별도 Gate를 따른다.

## Current Focus

현재 focus는 `Phase 1A — Minimal Project Identity`다.

- 완료: `ProjectId` 형식과 filesystem containment
- 완료: Proposal Source, evidence, target, draft project boundary
- 미완료: `(namespace, local_id)` qualified identity
- 미완료: ambiguous project resolution과 review hold
- 미완료: multi-project local ID coexistence

다음 단계는 `Phase 1B — Knowledge Intake & Steward`다. Source ingestion과 Proposal apply 기반은 이미 존재하지만 intent resolution, artifact classification, comparison, risk policy와 roadmap updater는 미구현이다.

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
