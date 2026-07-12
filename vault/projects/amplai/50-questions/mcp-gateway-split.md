---
schema_version: 1
id: QUE-0002
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: MCP Gateway를 독립 서비스로 분리하는 시점은 언제인가
summary: 독립 scaling, security 또는 release 요구가 생길 때 Gateway 분리 기준을 정해야 한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF, SRC-20260711-FC31D25D]
relations:
  - type: depends_on
    target: CON-0005
  - type: related_to
    target: DEC-0004
revision: 1
tags: [question, mcp, service-boundary]
---
# MCP Gateway를 독립 서비스로 분리하는 시점은 언제인가

## Question

초기 Modular Monolith 안의 MCP adapter를 언제 독립 배포 단위로 분리할지 기준이 필요하다. 외부 traffic scale, 별도 credential boundary, 독립 release cadence 또는 장애 격리가 실제 요구로 확인되는 시점을 후보 조건으로 검증한다.
