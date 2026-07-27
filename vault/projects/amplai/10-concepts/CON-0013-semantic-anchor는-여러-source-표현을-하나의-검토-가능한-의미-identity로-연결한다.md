---
schema_version: 1
id: CON-0013
namespace: org/default/project/amplai
project: amplai
kind: concept
status: candidate
title: Semantic Anchor는 여러 Source 표현을 하나의 검토 가능한 의미 identity로 연결한다
summary: Semantic Anchor는 동일하거나 변화한 요구의 Source, normalized proposition, canonical target과 comparison history를 연결하는 provisional runtime concept다.
created_at: 2026-07-28
updated_at: 2026-07-28
source_refs: [SRC-20260728-E0C88A4D]
relations:
  - {type: related_to, target: CON-0008}
  - {type: related_to, target: QUE-0009}
  - {type: depends_on, target: QUE-0005}
revision: 1
tags: [semantic-identity, anchor, intake, candidate]
---
# Semantic Anchor는 여러 Source 표현을 하나의 검토 가능한 의미 identity로 연결한다

## Definition

Semantic Anchor는 서로 다른 Source 표현이 동일한지, 기존 의미를 보완하는지, 충돌하거나 대체하는지를 비교하기 위한 안정된 의미 identity 후보다. Project scope, 현재 normalized proposition, 연결된 Source, canonical target, comparison history와 review state를 참조할 수 있다.

Semantic Anchor는 그 자체로 canonical truth가 아니며 unapproved Candidate를 authoritative knowledge로 승격하지 않는다. `SemanticAnchor`, `RequirementThread` 또는 다른 이름 중 무엇을 사용할지와 ID, persistence, lifecycle은 아직 결정되지 않았다.
