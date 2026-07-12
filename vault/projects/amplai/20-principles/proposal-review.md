---
schema_version: 1
id: PRI-0003
namespace: org/default/project/amplai
project: amplai
kind: principle
status: active
title: 공식 지식 변경은 Proposal과 검토를 거친다
summary: Agent 또는 사람이 발견한 변경은 proposal로 제출하고 검토한 후 공식 지식에 반영한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF]
relations:
  - type: depends_on
    target: CON-0008
  - type: supports
    target: CON-0007
revision: 1
tags: [principle, proposal, review]
---
# 공식 지식 변경은 Proposal과 검토를 거친다

## Principle

Agent가 만든 요약이나 사람이 제안한 변경은 기존 공식 지식을 즉시 덮어쓰지 않는다. 변경 proposal은 근거, 영향 대상과 lifecycle 변화를 명시하고 사람의 review를 거친다. 새 결정이 이전 결정을 대체하면 `supersedes` 관계를 남긴다.
