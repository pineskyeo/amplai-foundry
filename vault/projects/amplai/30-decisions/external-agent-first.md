---
schema_version: 1
id: DEC-0003
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: 초기 AMPLAI는 External Agent Mode부터 시작한다
summary: 초기 검증에서는 외부 Agent가 실행을 소유하고 AMPLAI는 지식과 capability 경계를 제공한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF, SRC-20260711-FC31D25D]
relations:
  - type: implements
    target: CON-0006
  - type: depends_on
    target: CON-0005
revision: 1
tags: [decision, agent, delivery]
---
# 초기 AMPLAI는 External Agent Mode부터 시작한다

## 결정

초기 AMPLAI는 자체 범용 Agent runtime보다 External Agent Mode를 먼저 지원한다.

## 이유

기존 Agent 실행기를 활용하면 Memory와 domain capability의 실제 가치를 작은 범위에서 검증할 수 있다. MCP adapter는 이후 roadmap에서 다루며 이번 Foundry에는 구현하지 않는다.
