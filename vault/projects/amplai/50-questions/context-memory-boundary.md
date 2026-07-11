---
schema_version: 1
id: QUE-0001
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Context와 Memory의 정확한 module 경계는 어디인가
summary: Retrieval, context assembly와 memory policy의 책임을 어느 module이 소유할지 결정이 필요하다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001, SRC-20260711-002]
relations:
  - type: depends_on
    target: CON-0004
  - type: related_to
    target: ARC-0001
revision: 1
tags: [question, context, memory]
---
# Context와 Memory의 정확한 module 경계는 어디인가

## Question

Memory repository의 조회 책임과 실행별 context assembly 책임 사이 경계를 확정해야 한다. Retrieval ranking, token budget, policy filtering과 citation packaging 중 어느 기능이 Memory Layer에 속하고 어느 기능이 Agent Harness에 속하는지 다음 설계 study에서 결정한다.
