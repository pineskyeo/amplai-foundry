---
schema_version: 1
id: CON-0002
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Loop는 목표 달성까지 반복하는 제어 흐름이다
summary: Loop는 관찰, 판단, 행동, 검증을 종료 조건까지 반복하는 실행 단위다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: depends_on
    target: CON-0001
  - type: related_to
    target: CON-0003
revision: 1
tags: [agent, loop, control]
---
# Loop는 목표 달성까지 반복하는 제어 흐름이다

## Definition

Loop는 현재 상태를 관찰하고 다음 행동을 판단한 뒤, 도구나 model을 실행하고 결과를 검증하는 순환이다. 명시적 종료 조건과 실패 한계를 가지며, 한 번의 실행을 개선하는 [[Meta-loop는 Loop 자체를 개선하는 반복이다]]와 구분한다.
