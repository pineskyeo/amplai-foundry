---
schema_version: 1
id: DEC-0904
namespace: org/default/project/test
project: test
kind: decision
status: superseded
title: 대체 대상이 빠진 과거 decision
summary: superseded_by 누락을 검증하기 위한 의도적으로 잘못된 decision이다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-904]
relations:
  - type: derived_from
    target: SRC-20260711-904
revision: 1
tags: [decision, fixture]
---
# 대체 대상이 빠진 과거 decision

## 결정

이 decision은 본문과 provenance는 유효하지만 status가 superseded인데 `superseded_by`가 없다. Linter는 lifecycle 전용 ERROR를 생성해야 한다.
