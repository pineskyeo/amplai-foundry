---
schema_version: 1
id: CON-0006
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: External Agent Mode는 외부 실행기가 AMPLAI 기능을 호출하는 방식이다
summary: External Agent Mode에서는 Codex 같은 외부 Agent가 실행을 소유하고 AMPLAI는 지식과 도구 경계를 제공한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: depends_on
    target: CON-0005
  - type: related_to
    target: DEC-0003
revision: 1
tags: [agent, integration, mode]
---
# External Agent Mode는 외부 실행기가 AMPLAI 기능을 호출하는 방식이다

## Definition

External Agent Mode에서는 Codex나 다른 Agent runtime이 reasoning loop와 session을 소유한다. AMPLAI는 검토된 지식, domain tool과 policy 경계를 제공한다. 따라서 초기 제품이 자체 범용 Agent runtime을 먼저 만들지 않아도 실제 workflow를 검증할 수 있다.
