---
schema_version: 1
id: PRI-0005
namespace: org/default/project/amplai
project: amplai
kind: principle
status: candidate
title: 프로젝트가 Memory를 소유하고 Runtime은 이를 사용한다
summary: 지식의 소유권과 lifecycle은 개별 Agent가 아니라 project scope에 두고 Runtime은 같은 지식을 역할별로 사용한다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: supports, target: CON-0007}
  - {type: supports, target: CON-0006}
  - {type: related_to, target: DEC-0001}
revision: 1
tags: [memory, ownership, project, runtime]
---
# 프로젝트가 Memory를 소유하고 Runtime은 이를 사용한다

## Principle

공유 지식의 소유권, provenance와 lifecycle은 Claude Code, Codex, Hermes 같은 개별 Runtime에 두지 않는다. Project가 Memory를 소유하고 각 Runtime은 동일한 canonical knowledge를 자신의 실행 목적에 맞게 조회하고 사용한다.

이 원칙은 물리 저장 형식과 다르다. 현재 공식 지식 원본은 [[Markdown을 현재 공식 지식 원본으로 사용한다]] 결정을 따르며, Project ownership은 그 지식의 scope와 governance를 뜻한다.

