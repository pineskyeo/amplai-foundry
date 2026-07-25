---
schema_version: 1
id: MAP-0002
namespace: org/default/project/amplai
project: amplai
kind: map
status: active
title: AMPLAI Platform Map
summary: AMPLAI의 integration mode, 역할 경계, delivery architecture와 service boundary 질문을 연결하는 MOC다.
created_at: 2026-07-11
updated_at: 2026-07-25
source_refs: [SRC-20260711-FC31D25D, SRC-20260725-9FEFBEAF]
relations:
  - {type: related_to, target: CON-0005}
  - {type: related_to, target: CON-0006}
  - {type: related_to, target: CON-0009}
  - {type: related_to, target: CON-0010}
  - {type: related_to, target: CON-0011}
  - {type: related_to, target: CON-0012}
  - {type: related_to, target: PRI-0005}
  - {type: related_to, target: DEC-0003}
  - {type: related_to, target: DEC-0004}
  - {type: related_to, target: ARC-0001}
  - {type: related_to, target: ARC-0003}
  - {type: related_to, target: QUE-0002}
  - {type: related_to, target: QUE-0006}
  - {type: related_to, target: QUE-0007}
revision: 2
tags: [map, platform, roles, moc]
---
# AMPLAI Platform Map

## Integration

- [[AMPLAI에서 MCP는 기능과 지식을 노출하는 Adapter다]]
- [[External Agent Mode는 외부 실행기가 AMPLAI 기능을 호출하는 방식이다]]
- [[초기 AMPLAI는 External Agent Mode부터 시작한다]]

## Roles

- [[AMPLAI는 프로젝트 중심 AI 운영체제다]]
- [[프로젝트가 Memory를 소유하고 Runtime은 이를 사용한다]]
- [[Hermes는 Project Memory를 사용하는 Work Manager다]]
- [[Implementation Agent는 위임받은 변경을 구현하고 검증한다]]
- [[Governor는 승인과 정책 경계를 집행한다]]
- [[AMPLAI 역할 경계는 Knowledge Layer·Governor·Work Manager·Implementation Agent를 분리한다]]

## Architecture

- [[초기 배포는 Modular Monolith로 시작한다]]
- [[Memory Layer는 Domain Contract와 Repository Port를 소유한다]]
- [[MCP Gateway를 독립 서비스로 분리하는 시점은 언제인가]]
- [[운영 Memory와 Canonical Knowledge의 저장 경계는 어디인가]]
- [[Project Memory의 Source of Truth 범위는 어디까지인가]]
