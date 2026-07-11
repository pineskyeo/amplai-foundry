---
schema_version: 1
id: QUE-NNNN
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Question 제목
summary: 아직 답하지 못한 단일 질문과 결정 필요성을 요약한다.
created_at: YYYY-MM-DD
updated_at: YYYY-MM-DD
source_refs: [SRC-YYYYMMDD-NNN]
relations:
  - type: depends_on
    target: CON-NNNN
revision: 1
tags: [question]
---
# Question 제목

## Question

결정해야 할 한 가지 질문, 필요한 evidence와 다음 review 조건을 기록한다.
