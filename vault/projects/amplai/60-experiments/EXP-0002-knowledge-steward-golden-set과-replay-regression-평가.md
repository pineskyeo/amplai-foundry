---
schema_version: 1
id: EXP-0002
namespace: org/default/project/amplai
project: amplai
kind: experiment
status: candidate
title: Knowledge Steward Golden Set과 replay regression 평가
summary: representative source set으로 candidate extraction의 분류, evidence, unsupported inference와 duplicate behavior를 평가하는 실험 후보다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: supports, target: ARC-0004}
  - {type: supports, target: ARC-0005}
  - {type: supports, target: CON-0003}
revision: 1
tags: [experiment, knowledge-steward, evaluation, regression]
---
# Knowledge Steward Golden Set과 replay regression 평가

## Hypothesis

명백한 concept와 decision, 질문 문서, 중복·충돌 문서, 과도한 추론을 유도하는 문서, mixed-project 문서, roadmap과 incident report를 Golden Set으로 유지하면 Knowledge Steward 변경의 안전성을 반복 측정할 수 있다.

## Method

모델, prompt, ontology 또는 policy를 바꿀 때 Golden Set을 replay한다. classification accuracy, evidence locator accuracy, unsupported inference, duplicate creation rate와 approval requirement를 비교하고, forbidden canonical auto-apply가 발생하지 않는지 확인한다.
