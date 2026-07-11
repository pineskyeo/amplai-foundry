---
schema_version: 1
id: CON-0900
namespace: org/default/project/test
project: test
kind: concept
status: active
title: 정상 fixture concept contract
summary: 정상 fixture에서 schema와 provenance를 만족하는 concept다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-900]
relations:
  - type: derived_from
    target: SRC-20260711-900
revision: 1
tags: [concept, fixture]
---
# 정상 fixture concept contract

이 concept note는 필수 field, provenance, relation과 충분한 본문을 가진다. Map에서 참조되므로 고립 경고 없이 전체 정상 vault가 lint를 통과하는 기준을 제공한다.
