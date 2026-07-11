---
schema_version: 1
id: QUE-0003
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Vector 검색 도입 기준은 무엇인가
summary: Keyword와 structured retrieval이 부족하다는 측정 결과가 있을 때 vector search의 도입 기준을 정한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001, SRC-20260711-002]
relations:
  - type: depends_on
    target: PRI-0004
  - type: related_to
    target: DEC-0002
revision: 1
tags: [question, vector-search, evaluation]
---
# Vector 검색 도입 기준은 무엇인가

## Question

Vector search는 초기 기본값이 아니다. SQLite FTS5와 structured field filter가 실패하는 representative query set, relevance metric, latency와 운영 비용을 먼저 정의해야 한다. 의미 검색의 측정된 개선이 복잡도를 정당화할 때 도입한다.
