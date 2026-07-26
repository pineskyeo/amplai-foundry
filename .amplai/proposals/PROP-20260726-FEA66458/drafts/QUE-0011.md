---
schema_version: 1
id: QUE-0011
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Hermes 개인 메모리를 AMPLAI Intake로 승격하는 trigger는 무엇인가
summary: Hermes의 개인 메모리와 장기 프로젝트 knowledge를 분리하면서 언제 Source intake를 만들지 결정해야 한다.
created_at: 2026-07-26
updated_at: 2026-07-26
source_refs: [SRC-20260726-FEA66458]
relations:
  - {type: related_to, target: CON-0010}
  - {type: depends_on, target: CON-0008}
revision: 1
tags: [question, hermes, intake, personal-memory]
---
# Hermes 개인 메모리를 AMPLAI Intake로 승격하는 trigger는 무엇인가

## Question

Hermes의 대화와 개인 메모리 중 어떤 내용을 AMPLAI Source intake로 올려 장기 지식 후보로 검토해야 하는가? 모든 대화를 자동 수집하면 지식 오염과 보안 문제가 생길 수 있다.

## Decision Needed

사용자의 명시적 기록 요청, 반복 업무, 구현 완료 뒤 decision 또는 architecture 변화, incident 해결, 주간 review 중 어떤 trigger를 허용할지와 authority·privacy 조건을 정해야 한다.
