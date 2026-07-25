---
schema_version: 1
id: QUE-0004
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Raw Source 보존 기간은 얼마인가
summary: 출처 검증, 개인정보, 비용과 삭제 의무를 함께 고려한 원문 보존 정책이 필요하다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF]
relations:
  - type: depends_on
    target: PRI-0002
  - type: related_to
    target: CON-0008
revision: 1
tags: [question, source, retention]
---
# Raw Source 보존 기간은 얼마인가

## Question

Canonical Knowledge의 provenance를 검증할 수 있도록 raw source가 필요하지만 영구 보존이 항상 안전하지는 않다. Source 유형별 법적 삭제 의무, 개인정보, 저장 비용과 재수집 가능성을 확인한 뒤 retention과 redaction policy를 정한다.
