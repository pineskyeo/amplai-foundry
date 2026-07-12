---
schema_version: 1
id: DEC-0002
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: 검색 DB는 파생 인덱스로 취급한다
summary: 검색 DB는 공식 지식 원본이 아니며 canonical repository에서 언제든 다시 생성한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF, SRC-20260711-FC31D25D]
relations:
  - type: implements
    target: PRI-0004
  - type: depends_on
    target: DEC-0001
revision: 1
tags: [decision, search, derived-data]
---
# 검색 DB는 파생 인덱스로 취급한다

## 결정

향후 SQLite FTS5나 vector search를 도입해도 검색 DB는 파생 index로 관리한다.

## 결과

공식 지식 수정 API는 canonical repository에만 적용한다. 검색 장애나 schema 변경이 발생하면 [[검색 인덱스는 공식 원본에서 재생성 가능해야 한다]]에 따라 index를 다시 만든다.
