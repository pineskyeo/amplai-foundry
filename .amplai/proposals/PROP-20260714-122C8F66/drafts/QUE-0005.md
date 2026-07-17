---
schema_version: 1
id: QUE-0005
namespace: org/default/project/amplai
project: amplai
kind: question
status: active
title: Scope 모델의 최소 공식 field는 무엇인가
summary: Tenant, Workspace, Project, Environment와 domain applicability를 어떤 canonical field로 표현할지 결정이 필요하다.
created_at: 2026-07-14
updated_at: 2026-07-14
source_refs: [SRC-20260714-122C8F66]
relations:
  - type: depends_on
    target: ARC-0002
  - type: related_to
    target: CON-0007
  - type: related_to
    target: QUE-0001
revision: 1
tags: [question, scope, namespace, tenancy]
---
# Scope 모델의 최소 공식 field는 무엇인가

## Question

현재 `namespace`와 `project`는 canonical knowledge의 조직 및 project scope를 표현한다. Tenant, Workspace, Environment와 product 또는 equipment 같은 domain applicability를 같은 flat field에 추가하면 관계 해석과 migration이 불명확해질 수 있다.

## Decision Needed

Scope hierarchy, mandatory level, cross-scope reference 허용 조건과 `namespace` migration strategy를 결정해야 한다. Domain-specific applicability는 scope hierarchy와 분리할지, typed extension으로 둘지 별도 schema proposal에서 검토한다. 이 결정 전에는 현재 `namespace`와 `project` 계약을 유지한다.
