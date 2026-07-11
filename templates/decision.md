---
schema_version: 1
id: DEC-NNNN
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: Decision 제목
summary: 선택한 방향과 가장 중요한 결과를 요약한다.
created_at: YYYY-MM-DD
updated_at: YYYY-MM-DD
source_refs: [SRC-YYYYMMDD-NNN]
relations:
  - type: implements
    target: PRI-NNNN
revision: 1
tags: [decision]
---
# Decision 제목

## 결정

선택한 방향을 명확히 기록한다.

## 이유

근거와 trade-off를 기록한다.

## 재검토 조건

결정을 다시 열 객관적 조건을 기록한다.
