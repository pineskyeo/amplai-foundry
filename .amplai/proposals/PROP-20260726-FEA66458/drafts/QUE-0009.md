---
schema_version: 1
id: QUE-0009
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Candidate의 의미 중복과 충돌은 어떻게 판정하는가
summary: candidate statement와 기존 knowledge의 의미적 duplicate 또는 conflict를 deterministic rule과 semantic review로 어떻게 조합할지 결정이 필요하다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: depends_on, target: CON-0008}
  - {type: related_to, target: ARC-0004}
revision: 1
tags: [question, dedup, conflict, candidate]
---
# Candidate의 의미 중복과 충돌은 어떻게 판정하는가

## Question

동일 의미를 다른 표현으로 제안한 candidate와 기존 canonical knowledge를 어떻게 판정하는가? exact string, canonical alias, ontology term, embedding similarity, LLM semantic judge와 human review 중 하나만으로는 충분하지 않을 수 있다.

## Decision Needed

각 신호의 적용 순서, confidence/risk threshold, conflict escalation과 human review 기준을 정해야 한다.
