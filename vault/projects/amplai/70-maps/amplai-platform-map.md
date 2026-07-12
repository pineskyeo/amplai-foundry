---
schema_version: 1
id: MAP-0002
namespace: org/default/project/amplai
project: amplai
kind: map
status: active
title: AMPLAI Platform Map
summary: AMPLAI의 integration mode, delivery architecture와 service boundary 질문을 연결하는 MOC다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-FC31D25D]
relations:
  - {type: related_to, target: CON-0005}
  - {type: related_to, target: CON-0006}
  - {type: related_to, target: DEC-0003}
  - {type: related_to, target: DEC-0004}
  - {type: related_to, target: ARC-0001}
  - {type: related_to, target: QUE-0002}
revision: 1
tags: [map, platform, moc]
---
# AMPLAI Platform Map

## Integration

- [[AMPLAI에서 MCP는 기능과 지식을 노출하는 Adapter다]]
- [[External Agent Mode는 외부 실행기가 AMPLAI 기능을 호출하는 방식이다]]
- [[초기 AMPLAI는 External Agent Mode부터 시작한다]]

## Architecture

- [[초기 배포는 Modular Monolith로 시작한다]]
- [[Memory Layer는 Domain Contract와 Repository Port를 소유한다]]
- [[MCP Gateway를 독립 서비스로 분리하는 시점은 언제인가]]
