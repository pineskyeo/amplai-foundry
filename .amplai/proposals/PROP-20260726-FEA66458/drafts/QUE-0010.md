---
schema_version: 1
id: QUE-0010
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Knowledge Intake의 초기 자동 승인 경계는 어디까지인가
summary: intake 과정에서 자동 처리할 runtime/candidate 작업과 explicit approval이 필요한 canonical 의미 변경의 경계를 결정해야 한다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: related_to, target: CON-0012}
  - {type: depends_on, target: PRI-0003}
revision: 1
tags: [question, approval, intake, governor]
---
# Knowledge Intake의 초기 자동 승인 경계는 어디까지인가

## Question

Immutable Source 저장, hash 계산, source duplicate detection, derived index와 lint/test 같은 작업 중 무엇을 자동 처리할 수 있는가? Question 또는 Map의 candidate 처리와 canonical apply를 구분해야 한다.

## Decision Needed

Concept, principle, architecture, decision, ontology와 policy 변경을 포함해 어떤 canonical 의미 변경이 explicit approval을 요구하는지와, 평가 결과에 따라 자동화 범위를 확장하는 조건을 정해야 한다.
