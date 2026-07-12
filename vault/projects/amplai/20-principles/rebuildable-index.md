---
schema_version: 1
id: PRI-0004
namespace: org/default/project/amplai
project: amplai
kind: principle
status: active
title: 검색 인덱스는 공식 원본에서 재생성 가능해야 한다
summary: 검색 DB와 vector index는 Canonical Knowledge에서 다시 만들 수 있는 파생 데이터로 유지한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF]
relations:
  - type: supports
    target: DEC-0002
  - type: depends_on
    target: CON-0007
revision: 1
tags: [principle, search, index]
---
# 검색 인덱스는 공식 원본에서 재생성 가능해야 한다

## Principle

검색 DB, FTS table과 vector index는 Canonical Knowledge의 소유자가 아니다. 파생 index를 삭제해도 Markdown 또는 향후 canonical repository에서 같은 의미를 다시 만들 수 있어야 한다. Index schema 변경은 공식 지식 migration을 요구하지 않는다.
