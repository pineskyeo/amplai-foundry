---
schema_version: 1
id: DEC-0005
namespace: org/default/project/amplai
project: amplai
kind: decision
status: candidate
title: UNCERTAIN semantic comparison은 Canonical CREATE나 UPDATE를 승인하지 않는다
summary: 의미 관계가 불확실한 Candidate는 durable comparison hold와 human review 대상으로 남고 canonical mutation을 승인하지 않는 v1 policy 후보다.
created_at: 2026-07-28
updated_at: 2026-07-28
source_refs: [SRC-20260728-E0C88A4D]
relations:
  - {type: implements, target: PRI-0003}
  - {type: related_to, target: QUE-0009}
  - {type: related_to, target: QUE-0010}
  - {type: related_to, target: EXP-0002}
revision: 1
tags: [decision, semantic-identity, uncertain, hold]
---
# UNCERTAIN semantic comparison은 Canonical CREATE나 UPDATE를 승인하지 않는다

## Decision Candidate

Semantic comparison이 `NEW`, duplicate, refinement 또는 conflict 관계를 충분히 판정하지 못하면 `UNCERTAIN`으로 보류한다. `UNCERTAIN` 결과는 Canonical `CREATE`, `UPDATE` 또는 자동 apply를 승인하지 않는다.

현재 Proposal operation에 `HOLD`가 없으므로 v1은 Proposal 밖의 durable `ComparisonResult`와 review queue에 보존하는 방향을 검토한다. Storage, release condition, reviewer와 threshold는 [[Candidate의 의미 identity, 중복과 충돌은 어떻게 판정하는가]] 및 Golden Set 평가 전까지 확정하지 않는다.
