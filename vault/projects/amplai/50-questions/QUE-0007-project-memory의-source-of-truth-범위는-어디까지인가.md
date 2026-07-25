---
schema_version: 1
id: QUE-0007
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Project Memory의 Source of Truth 범위는 어디까지인가
summary: Markdown canonical knowledge와 Jira, Git, session 등 외부 operational system의 권위를 어떻게 구분할지 결정이 필요하다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: related_to, target: CON-0007}
  - {type: depends_on, target: DEC-0001}
  - {type: related_to, target: PRI-0005}
revision: 1
tags: [question, source-of-truth, scope]
---
# Project Memory의 Source of Truth 범위는 어디까지인가

## Question

Project가 Memory를 소유한다는 원칙이 모든 업무 데이터의 단일 물리 원본을 뜻하는가? 현재 reviewed knowledge의 canonical source는 Markdown과 Git이지만 Jira issue, Git commit과 session state는 각 운영 system이 authoritative source일 수 있다.

## Decision Needed

Project Memory를 통합 조회와 governance 경계로 정의할지, 모든 데이터의 단일 저장소로 정의할지 구분해야 한다. 각 데이터 유형의 authoritative source와 derived projection을 명시해야 한다.

