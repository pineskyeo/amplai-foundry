---
schema_version: 1
id: ARC-0004
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: candidate
title: Governed Knowledge Compiler는 Source부터 Canonical Renderer까지의 통제 경계를 분리한다
summary: 자연어 Source를 structured candidate, validation, dedup/conflict, proposal/governor와 deterministic renderer를 거쳐 canonical asset으로 만드는 architecture 후보다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: depends_on, target: ARC-0002}
  - {type: implements, target: PRI-0002}
  - {type: implements, target: PRI-0003}
  - {type: related_to, target: CON-0008}
revision: 1
tags: [architecture, knowledge-compiler, governance, renderer]
---
# Governed Knowledge Compiler는 Source부터 Canonical Renderer까지의 통제 경계를 분리한다

## Boundary

Governed Knowledge Compiler는 immutable Source를 Extractor의 구조화된 candidate로 해석하고, deterministic validator, dedup/conflict engine, proposal store와 Governor를 거쳐 deterministic canonical renderer로 전달하는 architecture 후보다.

LLM은 candidate statement와 관계를 제안할 수 있지만 asset ID, 저장 경로, front matter와 rendering을 만들거나 canonical repository를 직접 수정하지 않는다. 동일 Source의 재분석은 Source hash, model·prompt·ontology·policy version, extraction result와 review decision을 남겨 replay와 비교가 가능해야 한다.
