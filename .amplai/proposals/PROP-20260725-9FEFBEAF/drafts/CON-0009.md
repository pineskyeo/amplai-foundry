---
schema_version: 1
id: CON-0009
namespace: org/default/project/amplai
project: amplai
kind: concept
status: candidate
title: AMPLAI는 프로젝트 중심 AI 운영체제다
summary: AMPLAI는 특정 Agent runtime이 아니라 프로젝트 지식, 정책과 업무 경계를 제공하는 AI 운영체제 후보다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: related_to, target: ARC-0001}
  - {type: related_to, target: ARC-0002}
  - {type: related_to, target: DEC-0003}
revision: 1
tags: [amplai, ai-os, project]
---
# AMPLAI는 프로젝트 중심 AI 운영체제다

## Definition

AMPLAI는 하나의 Agent runtime을 대체하는 범용 실행기가 아니다. 프로젝트가 공유하는 지식, 정책, 승인 경계와 capability를 runtime과 독립적으로 제공하는 프로젝트 중심 AI 운영체제 후보다.

Claude Code, Codex, Hermes 같은 runtime은 같은 Project Memory를 각자의 역할에 맞게 사용한다. 초기 구현은 기존 [[Memory Layer는 Domain Contract와 Repository Port를 소유한다]]와 [[Knowledge Representation Pipeline은 저장·검색·문맥 표현을 분리한다]] 계약을 유지한다.

