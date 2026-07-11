---
schema_version: 1
id: CON-0004
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Context는 실행 입력이고 Memory는 재사용 가능한 보존 정보다
summary: Context는 현재 실행에 주입된 정보이고 Memory는 실행 밖에서 보존되어 다시 선택될 수 있는 정보다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001, SRC-20260711-002]
relations:
  - type: related_to
    target: QUE-0001
  - type: depends_on
    target: CON-0007
revision: 1
tags: [context, memory, boundary]
---
# Context는 실행 입력이고 Memory는 재사용 가능한 보존 정보다

## Definition

Context는 한 실행에서 model과 도구가 볼 수 있도록 선택하고 조립한 입력이다. Memory는 실행 수명과 독립적으로 보존되고 이후 실행에서 검색과 정책을 통해 Context 후보가 된다. 모든 Context가 Memory는 아니며 모든 Memory가 매번 Context에 들어가지 않는다.
