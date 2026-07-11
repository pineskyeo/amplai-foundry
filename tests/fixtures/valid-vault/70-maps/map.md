---
schema_version: 1
id: MAP-0900
namespace: org/default/project/test
project: test
kind: map
status: active
title: 정상 fixture knowledge map
summary: 정상 fixture의 atomic concept를 연결하는 작은 Map이다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-900]
relations:
  - type: related_to
    target: CON-0900
revision: 1
tags: [map, fixture]
---
# 정상 fixture knowledge map

이 Map은 [[정상 fixture concept contract]]를 사람이 탐색할 수 있게 연결한다. Structured relation도 같은 concept를 가리켜 deterministic orphan validation의 기준이 된다.
