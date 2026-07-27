---
schema_version: 1
id: DEC-0006
namespace: org/default/project/amplai
project: amplai
kind: decision
status: candidate
title: Phase 1B의 첫 신규 구현 slice는 Phase 1A gate 뒤 Semantic Comparison 계약을 고정한다
summary: Phase 1A project identity gate를 통과한 뒤 Phase 1B에서 Semantic Identity contract, comparison policy와 Golden Set을 먼저 검증하는 sequencing 후보다.
created_at: 2026-07-28
updated_at: 2026-07-28
source_refs: [SRC-20260728-E0C88A4D]
relations:
  - {type: depends_on, target: QUE-0005}
  - {type: related_to, target: ARC-0006}
  - {type: related_to, target: EXP-0002}
revision: 1
tags: [decision, roadmap, phase-1b, semantic-comparison]
---
# Phase 1B의 첫 신규 구현 slice는 Phase 1A gate 뒤 Semantic Comparison 계약을 고정한다

## Decision Candidate

Phase 1A의 qualified project identity, ambiguous resolution hold와 multi-project local ID gate가 완료된 뒤 Phase 1B의 첫 신규 구현 slice에서 Semantic Identity contract, deterministic comparison policy, durable `ComparisonResult`와 Golden Set을 검증하는 순서를 제안한다.

Immutable Source ingestion은 이미 구현된 baseline이므로 새 slice의 선행 조건으로 사용한다. 이 후보는 Phase 1A 완료, Phase 1B 착수 또는 `current_focus` 변경을 선언하지 않는다. `1B-1` numbering은 이전 deferred 제안과 명칭이 충돌하므로 roadmap review에서 별도로 정리한다.
