---
schema_version: 1
id: CON-0007
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Canonical Knowledge는 검토된 공식 지식이다
summary: Canonical Knowledge는 출처와 lifecycle을 갖고 팀이 공식 원본으로 승인한 장기 프로젝트 지식이다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: supports
    target: PRI-0002
  - type: related_to
    target: CON-0008
revision: 1
tags: [knowledge, canonical, governance]
---
# Canonical Knowledge는 검토된 공식 지식이다

## Definition

Canonical Knowledge는 source 추적, 명시적 lifecycle과 revision을 가진 장기 프로젝트 지식이다. 사람이 검토한 concept, principle, decision, question, architecture, experiment와 source가 여기에 속한다. 실행 중 생긴 임시 관찰은 바로 공식 지식이 되지 않는다.
