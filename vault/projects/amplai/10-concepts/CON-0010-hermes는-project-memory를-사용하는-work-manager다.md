---
schema_version: 1
id: CON-0010
namespace: org/default/project/amplai
project: amplai
kind: concept
status: candidate
title: Hermes는 Project Memory를 사용하는 Work Manager다
summary: Hermes는 프로젝트 지식을 검색하고 업무를 계획·분배하며 구현 결과를 취합하는 Work Manager 후보다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: depends_on, target: CON-0009}
  - {type: related_to, target: CON-0006}
  - {type: related_to, target: CON-0011}
revision: 1
tags: [hermes, work-manager, orchestration]
---
# Hermes는 Project Memory를 사용하는 Work Manager다

## Definition

Hermes는 Project Memory를 검색하고 Jira, Git, 일정과 메신저 상태를 모아 업무를 계획하는 Work Manager 후보다. 구현 작업은 적합한 Implementation Agent에 위임하고 진행 결과를 취합해 사용자와 외부 업무 시스템에 보고한다.

Hermes가 canonical knowledge를 직접 승인하거나 변경하는 권한은 이 개념에 포함하지 않는다. approve/apply 권한과 외부 시스템 update 정책은 Governor 경계에서 별도로 결정한다.

