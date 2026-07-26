---
schema_version: 1
id: QUE-0008
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Knowledge Steward의 초기 모델 라우팅과 평가 정책은 무엇인가
summary: candidate extraction에 사용할 Claude, Codex, 사내 GLM 또는 hybrid routing을 어떤 품질·비용·보안 기준으로 선택할지 결정이 필요하다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: related_to, target: ARC-0005}
  - {type: related_to, target: EXP-0002}
revision: 1
tags: [question, knowledge-steward, model, routing]
---
# Knowledge Steward의 초기 모델 라우팅과 평가 정책은 무엇인가

## Question

Knowledge Steward의 첫 Extractor LLM은 Claude, Codex, 사내 GLM, 모델별 routing 또는 민감도 기반 hybrid routing 중 무엇이어야 하는가?

## Decision Needed

한국어 업무 문서 정확도, schema 준수율, evidence locator 정확도, 비용, 보안과 반복 실행 안정성을 Golden Set에서 비교할 기준과 selection policy를 정해야 한다.
