---
schema_version: 1
id: DEC-0001
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: Markdown을 현재 공식 지식 원본으로 사용한다
summary: 검토된 프로젝트 지식은 현재 Markdown과 Git으로 관리하며 Obsidian과 AI가 같은 파일을 읽는다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF, SRC-20260711-FC31D25D]
relations:
  - type: implements
    target: CON-0007
  - type: supports
    target: PRI-0002
revision: 1
tags: [decision, markdown, storage]
---
# Markdown을 현재 공식 지식 원본으로 사용한다

## 결정

검토된 AMPLAI 프로젝트 지식은 Markdown 파일과 Git으로 관리한다. Obsidian과 AI는 동일한 파일을 읽는다.

## 이유

사람이 직접 검토하고 변경 이력을 볼 수 있으며 별도 사람용 문서를 중복 생성하지 않는다.

## 재검토 조건

다중 사용자 동시 편집과 권한 관리가 파일 기반 운영의 한계를 넘을 때 재검토한다.
