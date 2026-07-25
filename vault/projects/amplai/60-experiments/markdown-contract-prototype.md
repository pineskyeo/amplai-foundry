---
schema_version: 1
id: EXP-0001
namespace: org/default/project/amplai
project: amplai
kind: experiment
status: active
title: Markdown Memory Contract 첫 검증
summary: 같은 Markdown을 사람이 읽고 Python이 검증하는 Foundry prototype으로 domain contract의 실용성을 시험한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF, SRC-20260711-FC31D25D]
relations:
  - type: implements
    target: DEC-0001
  - type: supports
    target: ARC-0001
revision: 1
tags: [experiment, markdown, lint]
---
# Markdown Memory Contract 첫 검증

## Hypothesis

하나의 Markdown note set이 Obsidian의 사람용 탐색성과 Python의 결정론적 validation을 동시에 제공할 수 있다.

## Method

서로 연결된 AMPLAI 원자 지식을 작성하고 schema, link, lifecycle, provenance와 hygiene lint를 offline test로 검증한다.
