---
schema_version: 1
id: ARC-0005
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: candidate
title: Knowledge Steward는 event-driven worker이고 Runtime은 상주 서비스다
summary: AMPLAI Runtime은 상주하고 Knowledge Steward는 source intake 또는 trigger에 따라 시작·종료되는 통제된 worker인 architecture 후보다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: related_to, target: ARC-0003}
  - {type: related_to, target: CON-0008}
  - {type: related_to, target: CON-0012}
revision: 1
tags: [architecture, knowledge-steward, runtime, worker]
---
# Knowledge Steward는 event-driven worker이고 Runtime은 상주 서비스다

## Boundary

AMPLAI Runtime은 API/Gateway, project and memory repository, ontology registry, search, proposal manager, policy/governor와 job queue를 상주 capability로 제공하는 architecture 후보다.

Knowledge Steward는 Source intake 또는 event trigger에 따라 실행되어 입력 해석, candidate extraction, relation suggestion, dedup/conflict 분석과 Proposal 작성을 수행한 뒤 종료하는 worker다. 자유롭게 사고하며 상주하는 autonomous LLM은 이 boundary에 포함하지 않는다.
