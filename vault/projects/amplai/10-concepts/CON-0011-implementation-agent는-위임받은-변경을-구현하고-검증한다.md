---
schema_version: 1
id: CON-0011
namespace: org/default/project/amplai
project: amplai
kind: concept
status: candidate
title: Implementation Agent는 위임받은 변경을 구현하고 검증한다
summary: Implementation Agent는 명시된 작업 범위에서 코드 작성, 수정, 테스트와 PR 준비를 담당하는 실행 역할이다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: depends_on, target: CON-0006}
  - {type: related_to, target: CON-0010}
revision: 1
tags: [agent, implementation, development]
---
# Implementation Agent는 위임받은 변경을 구현하고 검증한다

## Definition

Implementation Agent는 승인된 작업 범위 안에서 코드 작성, 수정, 테스트, commit과 PR 준비를 수행하는 실행 역할이다. Claude Code와 Codex는 이 역할을 수행할 수 있는 Runtime 예시다.

이 개념은 특정 Runtime의 모든 역할을 구현에만 제한하지 않는다. 같은 Runtime도 별도 Harness와 정책 아래에서 curator, reviewer 또는 분석 역할을 수행할 수 있다.

