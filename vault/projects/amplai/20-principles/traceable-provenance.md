---
schema_version: 1
id: PRI-0002
namespace: org/default/project/amplai
project: amplai
kind: principle
status: active
title: 공식 지식은 출처를 추적할 수 있어야 한다
summary: 모든 active 공식 지식은 근거가 된 source record를 하나 이상 가리킨다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001]
relations:
  - type: supports
    target: CON-0007
  - type: related_to
    target: PRI-0003
revision: 1
tags: [principle, provenance, safety]
---
# 공식 지식은 출처를 추적할 수 있어야 한다

## Principle

Active 공식 지식은 최소 하나의 source record를 `source_refs`로 가리킨다. 사용자는 주장과 결정이 어디에서 왔는지 확인할 수 있어야 한다. Source record 자체는 provenance의 시작점이므로 다른 source reference를 의무로 요구하지 않는다.
