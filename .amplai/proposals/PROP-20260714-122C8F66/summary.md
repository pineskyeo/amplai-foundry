# Proposal Summary

## Source

`SRC-20260714-122C8F66`은 AMPLAI Memory의 표현 계층, lifecycle, retrieval, LLM context와 안전한 write flow를 정리한 사용자 제공 설계 문서다.

## Proposed Changes

- `ARC-0002`를 만들어 Markdown, `MemoryObject`, search result와 LLM context 표현의 책임을 분리한다.
- `QUE-0005`를 만들어 tenant/workspace/project/environment scope model을 미결 질문으로 기록한다.
- `MAP-0001`에 두 note를 연결한다.

## Non-Changes

- 기존 canonical knowledge를 supersede하거나 삭제하지 않는다.
- `MemoryObject`의 `scope`, `type`, `provenance` field를 추가하지 않는다.
- LLM API, vector DB, MCP server를 추가하지 않는다.

## Review Focus

- `MemoryHit`, `ContextItem`, `ContextBundle`을 다음 schema milestone의 공식 용어로 채택할지 결정한다.
- `kind`와 domain-specific type의 분리 방식을 결정한다.
- `namespace`를 확장하는 scope model의 migration 경로를 결정한다.
