---
schema_version: 1
id: ARC-0001
namespace: org/default/project/amplai
project: amplai
kind: architecture
status: active
title: Memory Layer는 Domain Contract와 Repository Port를 소유한다
summary: Memory Layer 상위 기능은 Memory Object와 repository interface에 의존하고 저장 adapter에는 의존하지 않는다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-002]
relations:
  - type: implements
    target: CON-0007
  - type: supports
    target: DEC-0004
revision: 1
tags: [architecture, memory, repository]
---
# Memory Layer는 Domain Contract와 Repository Port를 소유한다

## Boundary

Memory Layer는 storage-independent Memory Object, lifecycle invariant와 read repository port를 소유한다. Markdown, SQLite, PostgreSQL과 vector index는 adapter 또는 파생 search component다. 상위 application 기능은 filesystem path나 YAML 구조에 직접 의존하지 않는다.
