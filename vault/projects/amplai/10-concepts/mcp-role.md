---
schema_version: 1
id: CON-0005
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: AMPLAI에서 MCP는 기능과 지식을 노출하는 Adapter다
summary: MCP는 Memory Layer 자체가 아니라 외부 Agent가 AMPLAI capability에 접근하는 표준 경계다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001, SRC-20260711-002]
relations:
  - type: supports
    target: CON-0006
  - type: related_to
    target: QUE-0002
revision: 1
tags: [mcp, adapter, integration]
---
# AMPLAI에서 MCP는 기능과 지식을 노출하는 Adapter다

## Definition

MCP는 외부 Agent가 AMPLAI의 도구와 지식 조회 capability를 일관된 protocol로 호출하게 하는 adapter 경계다. MCP가 Canonical Knowledge를 소유하지 않으며 Memory Domain을 대체하지도 않는다. 초기 Foundry 범위에는 MCP server 구현을 포함하지 않는다.
