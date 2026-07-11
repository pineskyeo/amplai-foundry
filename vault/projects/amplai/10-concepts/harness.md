---
schema_version: 1
id: CON-0001
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Harness는 Agent 실행을 통제하는 운영 껍질이다
summary: Harness는 Agent의 입력, 도구, 정책, 관찰, 종료 조건을 묶어 반복 가능한 실행을 만든다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: related_to
    target: CON-0002
  - type: supports
    target: PRI-0003
revision: 1
tags: [agent, harness, execution]
---
# Harness는 Agent 실행을 통제하는 운영 껍질이다

## Definition

Harness는 model 호출만 뜻하지 않는다. 입력 정규화, 사용할 도구와 권한, context 구성, loop 실행, 결과 관찰, 실패 처리와 종료 조건을 하나의 반복 가능한 실행 경계로 묶는다. [[Loop는 목표 달성까지 반복하는 제어 흐름이다]]와 함께 Agent 행동을 통제한다.
