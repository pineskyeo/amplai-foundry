---
schema_version: 1
id: CON-0003
namespace: org/default/project/amplai
project: amplai
kind: concept
status: active
title: Meta-loop는 Loop 자체를 개선하는 반복이다
summary: Meta-loop는 여러 실행 결과를 평가해 Harness, 정책, 지식 또는 평가 기준을 개선한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF]
relations:
  - type: depends_on
    target: CON-0002
  - type: related_to
    target: CON-0008
revision: 1
tags: [agent, meta-loop, improvement]
---
# Meta-loop는 Loop 자체를 개선하는 반복이다

## Definition

Meta-loop는 개별 task의 다음 step을 고르는 Loop가 아니다. 여러 Loop의 결과와 실패 패턴을 검토해 prompt, tool policy, Harness, 평가 기준 또는 공식 지식 변경 proposal을 만드는 상위 개선 흐름이다. 자동 공식 지식 승인은 포함하지 않는다.
