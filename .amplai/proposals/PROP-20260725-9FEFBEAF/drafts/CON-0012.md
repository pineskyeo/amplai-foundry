---
schema_version: 1
id: CON-0012
namespace: org/default/project/amplai
project: amplai
kind: concept
status: candidate
title: Governor는 승인과 정책 경계를 집행한다
summary: Governor는 Work Manager와 Agent가 수행할 수 있는 작업, 승인과 apply 경계를 집행하는 정책 역할 후보다.
created_at: 2026-07-25
updated_at: 2026-07-25
source_refs: [SRC-20260725-9FEFBEAF]
relations:
  - {type: supports, target: PRI-0003}
  - {type: depends_on, target: CON-0009}
revision: 1
tags: [governor, policy, approval]
---
# Governor는 승인과 정책 경계를 집행한다

## Definition

Governor는 Work Manager와 Implementation Agent가 사용할 수 있는 capability, 필요한 승인과 canonical apply 조건을 집행하는 정책 역할 후보다. 기존 [[공식 지식 변경은 Proposal과 검토를 거친다]] 원칙을 runtime workflow에 적용한다.

Governor가 별도 service인지 domain module인지, 어떤 actor가 정책을 구성하는지는 이 개념에서 확정하지 않는다.

