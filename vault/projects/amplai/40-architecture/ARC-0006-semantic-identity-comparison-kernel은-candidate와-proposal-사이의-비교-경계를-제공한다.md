---
schema_version: 1
id: ARC-0006
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: candidate
title: Semantic Identity & Comparison Kernel은 Candidate와 Proposal 사이의 비교 경계를 제공한다
summary: project-scoped Candidate retrieval, structured comparison, kind-aware policy와 durable ComparisonResult를 Proposal Builder 앞에 두는 component architecture 후보다.
created_at: 2026-07-28
updated_at: 2026-07-28
source_refs: [SRC-20260728-E0C88A4D]
relations:
  - {type: depends_on, target: ARC-0004}
  - {type: depends_on, target: CON-0008}
  - {type: implements, target: PRI-0006}
  - {type: related_to, target: QUE-0009}
  - {type: related_to, target: EXP-0002}
revision: 1
tags: [architecture, semantic-identity, comparison, kernel]
---
# Semantic Identity & Comparison Kernel은 Candidate와 Proposal 사이의 비교 경계를 제공한다

## Boundary

Semantic Identity & Comparison Kernel은 [[Governed Knowledge Compiler는 Source부터 Canonical Renderer까지의 통제 경계를 분리한다]] 안에서 Candidate Validator와 Proposal Builder 사이의 component boundary 후보다.

```text
KnowledgeCandidate
→ Candidate Validator
→ Project-scoped Retriever
→ Semantic Comparator
→ Comparison Policy
→ durable ComparisonResult 또는 review hold
→ Proposal Builder
```

Retriever는 suggested kind와 무관하게 관련 kind를 검색하되 active canonical knowledge와 unapproved candidate의 권위를 구분한다. Comparator는 content identity, normalized proposition/signature, lexical signals와 제한된 pairwise semantic evidence를 기록한다. Embedding을 도입하더라도 candidate retrieval 신호로만 사용하고 canonical merge나 identity 결정을 직접 수행하지 않는다.

Comparison Policy는 kind와 lifecycle을 고려한다. `REFINES`가 Decision을 직접 UPDATE하지 않으며 기존 supersedes 계약을 우회하지 않는다. 관계, confidence threshold, retrieval corpus와 ComparisonResult repository는 [[Candidate의 의미 identity, 중복과 충돌은 어떻게 판정하는가]]가 열린 동안 provisional이다.
