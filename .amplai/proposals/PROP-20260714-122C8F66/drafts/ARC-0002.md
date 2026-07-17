---
schema_version: 1
id: ARC-0002
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: active
title: Knowledge Representation Pipeline은 저장·검색·문맥 표현을 분리한다
summary: Markdown source, MemoryObject, search result와 LLM context는 다른 책임의 표현이며 변환 경계를 명시한다.
created_at: 2026-07-14
updated_at: 2026-07-14
source_refs: [SRC-20260714-122C8F66]
relations:
  - type: depends_on
    target: ARC-0001
  - type: implements
    target: CON-0007
  - type: related_to
    target: QUE-0001
revision: 1
tags: [architecture, memory, retrieval, context]
---
# Knowledge Representation Pipeline은 저장·검색·문맥 표현을 분리한다

## Boundary

Markdown은 사람이 작성하고 Git으로 관리하는 canonical source다. Markdown adapter는 이를 storage-independent `MemoryObject`로 변환한다. `MemoryObject`는 schema, lifecycle, provenance와 structured relation을 검증하는 domain contract이며 search index와 database adapter의 소유자가 아니다.

## Retrieval And Context

검색 단계는 scope와 lifecycle metadata로 후보를 줄인 뒤 lexical 또는 향후 vector ranking을 적용한다. 검색 결과는 score와 match 정보를 가진 `MemoryHit`으로 표현하고, Harness는 필요한 statement, rationale, applicability, exception과 evidence를 `ContextItem`으로 렌더링한다. 한 번의 model request에는 선택된 ContextItem을 `ContextBundle`로 조립한다.

## Constraint

표현 경계를 도입해도 canonical knowledge를 중복 저장하지 않는다. Vector index와 ContextBundle은 source of truth가 아니며 Markdown과 `MemoryObject`에서 재생성하거나 조립할 수 있어야 한다. 정확한 `MemoryHit`, `ContextItem`, `ContextBundle` schema는 LLM integration 전에 별도 proposal으로 확정한다.
