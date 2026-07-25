---
schema_version: 1
id: SRC-20260725-9FEFBEAF
namespace: org/default/project/amplai
project: amplai
kind: source
status: active
title: AMPLAI Hermes Claude Code 역할 정리
summary: user-note에서 수집한 자유 형식 원문
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: []
relations: []
revision: 1
tags:
- user-note
source_metadata:
  source_type: user-note
  content_sha256: 9fefbeaf3bd232d3a03dfeb1ea3387b9c9e9a59939e14a5463875a9dfeb2fdd2
  normalized_sha256: 87ea67f622c80eca975b11e90e07656cc4efc4dbf1d3f7a5234cd33f72189800
  original_filename: amplai-hermes-claude-code-roles.md
  media_type: text/markdown
  ingested_at: '2026-07-25T09:21:44.638081+09:00'
  created_by: user
---
# AMPLAI Hermes Claude Code 역할 정리

## Original Content
AMPLAI · Hermes · Claude Code 역할 정리

핵심 결론

AMPLAI

• 프로젝트 중심 AI 운영체제(AI OS)
• 프로젝트의 기억과 지식을 관리
• Runtime와 독립적인 Knowledge Layer 제공

Hermes

• 프로젝트 기억을 사용하는 개인 비서 / PM / Work Manager
• 메신저, 일정, Jira, 검색, 업무 분배 담당
• 구현은 직접 하기보다 적합한 개발 에이전트에게 위임

Claude Code / Codex

• 구현 개발자
• 코드 작성, 수정, 테스트, PR 생성 담당

────────

권장 아키텍처

```text
                AMPLAI
         (Project AI Operating System)
                      │
        ┌─────────────┼─────────────┐
        │             │             │
 Knowledge Layer   Governor     Work Manager
        │
        ▼
 Project Memory
 ├─ Facts
 ├─ Skills
 ├─ Ontology
 ├─ Decisions
 ├─ Sessions
 ├─ Jira State
 └─ Documents
        │
        ├────────────┬────────────┐
        │            │            │
     Hermes     Claude Code    Codex
      PM/비서      구현개발자     구현개발자
```

────────

Hermes 역할

• 프로젝트 기억 검색
• 메신저 연동
• Jira 연동
• 일정 관리
• 업무 요약
• 작업 분배
• Claude/Codex 호출
• 결과 취합

예시:

사용자 > “이번 주 UI Split 진행상황 정리하고 구현까지 진행해.”

Hermes 1. Jira 조회 2. Git 조회 3. Project Memory 검색 4. 작업 계획 생성
5. Claude Code 호출

Claude Code - 구현 - 테스트 - Commit

Hermes - 결과 요약 - Jira 업데이트 - 메신저 보고

────────

Project Memory 구조 제안

```text
.amplai/
├── memory/
├── skills/
├── ontology/
├── decisions/
├── work/
├── sessions/
├── prompts/
├── reviews/
└── cache/
```

Git 관리

Git 포함 - memory - skills - ontology - decisions - prompts - reviews

Git 제외 - cache - sqlite.db - embeddings - logs

Session은 SQLite 대신 JSONL 저장을 권장.

────────

핵심 철학

기존: > Agent owns Memory

AMPLAI: > Project owns Memory

즉 Claude, Codex, Hermes 등 어떤 Runtime을 사용하더라도 동일한 프로젝트
기억을 공유한다.

────────

장기 방향

• AMPLAI = AI OS
• Hermes = PM / 개인비서 / Work Manager
• Claude Code = 구현 개발자
• Codex = 구현 개발자
• Governor = 승인 및 정책
• Project Memory = 유일한 Source of Truth 
