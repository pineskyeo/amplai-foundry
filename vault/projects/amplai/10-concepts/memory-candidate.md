---
schema_version: 1
id: CON-0008
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Memory Candidate는 공식화 전의 지식 후보다
summary: Memory Candidate는 실행이나 source에서 발견됐지만 아직 Canonical Knowledge로 승인되지 않은 정보다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: related_to
    target: CON-0007
  - type: supports
    target: PRI-0003
revision: 1
tags: [memory, candidate, review]
---
# Memory Candidate는 공식화 전의 지식 후보다

## Definition

Memory Candidate는 Agent run, source ingestion 또는 사람의 관찰에서 발견한 지식 가능성이다. 아직 공식 주장으로 신뢰하지 않으며 중복 확인, provenance 연결, proposal과 review를 거쳐야 [[Canonical Knowledge는 검토된 공식 지식이다]]로 승격된다. 이번 Foundry는 후보 저장을 구현하지 않는다.
