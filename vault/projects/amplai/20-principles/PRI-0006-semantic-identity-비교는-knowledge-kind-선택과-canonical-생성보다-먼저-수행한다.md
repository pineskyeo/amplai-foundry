---
schema_version: 1
id: PRI-0006
namespace: org/default/project/amplai
project: amplai
kind: principle
status: candidate
title: Semantic identity 비교는 knowledge kind 선택과 canonical 생성보다 먼저 수행한다
summary: Candidate의 suggested kind보다 project-scoped 의미 비교를 먼저 수행하고 기존 semantic target이 있으면 별도 canonical note 생성을 우선하지 않는다.
created_at: 2026-07-28
updated_at: 2026-07-28
source_refs: [SRC-20260728-E0C88A4D]
relations:
  - {type: supports, target: CON-0008}
  - {type: supports, target: PRI-0003}
  - {type: related_to, target: QUE-0009}
revision: 1
tags: [principle, semantic-identity, comparison, dedup]
---
# Semantic identity 비교는 knowledge kind 선택과 canonical 생성보다 먼저 수행한다

## Principle

Source에서 원자 Candidate를 추출한 뒤 project scope와 proposition을 기존 knowledge 및 검토 중 candidate와 비교한다. `suggested_kind`는 retrieval 범위를 제한하거나 기존 semantic target을 우회해 새 note를 만드는 근거가 될 수 없다.

기존 의미 target이 확인되면 그 target의 kind, lifecycle과 변경 계약을 따른다. 같은 wording이라도 Question과 Decision처럼 epistemic state가 다르면 자동 병합하지 않는다. 완전히 새로운 의미로 충분히 판정된 경우에만 새로운 kind와 canonical target을 제안한다.
