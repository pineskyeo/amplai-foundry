---
schema_version: 1
id: QUE-0006
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: 운영 Memory와 Canonical Knowledge의 저장 경계는 어디인가
summary: Session, Jira state와 work state를 reviewed canonical knowledge와 어떤 저장·lifecycle 경계로 분리할지 결정이 필요하다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: depends_on, target: CON-0004}
  - {type: related_to, target: ARC-0001}
  - {type: related_to, target: CON-0008}
revision: 1
tags: [question, operational-memory, boundary]
---
# 운영 Memory와 Canonical Knowledge의 저장 경계는 어디인가

## Question

Hermes가 다루는 Session, Jira state, 일정과 work state 중 무엇을 장기 Project Memory로 보존해야 하는가? 검토된 canonical knowledge와 변경 빈도가 높은 operational state가 같은 lifecycle과 저장소를 사용하면 provenance와 현재성 기준이 흐려질 수 있다.

## Decision Needed

Operational state의 소유 system, retention, snapshot과 promotion-to-canonical 규칙을 정의해야 한다. Session JSONL 또는 SQLite 선택은 이 경계를 정한 뒤 별도 결정한다.

