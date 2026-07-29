---
schema_version: 1
id: DEC-0007
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: 메신저 Proposal Action은 AMPLAI가 재검증하고 승인과 Apply를 분리한다
summary: 메신저 interaction은 authority가 아니며 AMPLAI가 current Proposal과 actor 권한을 재검증한 뒤 상태를 전이한다.
created_at: 2026-07-29
updated_at: 2026-07-29
source_refs: [SRC-20260729-B13BC5DA]
relations:
  - {type: implements, target: PRI-0003}
  - {type: depends_on, target: ARC-0007}
  - {type: related_to, target: CON-0012}
revision: 1
tags: [decision, messenger, proposal, approval, apply, authority]
---
# 메신저 Proposal Action은 AMPLAI가 재검증하고 승인과 Apply를 분리한다

## Decision

메신저 button click이나 Hermes의 응답은 승인 자체가 아니다. AMPLAI가 actor와 project authority, expected Proposal version·digest, 현재 상태, action token과 idempotency를 재검증한 뒤 승인·거절·수정 요청을 성립시킨다.

Proposal 승인은 제안 내용에 대한 동의다. 실제 canonical 또는 Git 변경은 별도 `apply` action과 검증을 거친다.

## Consequence

오래된 Proposal Card, 다른 Actor·Channel의 token, 중복 callback과 허용되지 않은 상태 전이는 canonical 상태를 바꾸지 않는다. 모든 결정 action은 누가, 언제, 어느 channel에서 어떤 Proposal version에 수행했는지 append-only audit로 재구성 가능해야 한다.
