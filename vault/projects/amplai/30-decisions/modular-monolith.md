---
schema_version: 1
id: DEC-0004
namespace: org/default/project/amplai
project: amplai
kind: decision
status: active
title: 초기 배포는 Modular Monolith로 시작한다
summary: 초기 AMPLAI는 명확한 module 경계를 가진 단일 배포 단위로 시작한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-001, SRC-20260711-002]
relations:
  - type: implements
    target: ARC-0001
  - type: related_to
    target: QUE-0002
revision: 1
tags: [decision, architecture, deployment]
---
# 초기 배포는 Modular Monolith로 시작한다

## 결정

초기 AMPLAI 서비스는 domain module 경계를 유지하는 단일 배포 단위로 시작한다.

## 이유

초기에는 경계 학습이 운영 분산보다 중요하다. MCP Gateway와 Memory 기능은 code ownership을 분리하되 독립 서비스 비용은 실제 scaling, security 또는 release 요구가 확인된 뒤 부담한다.
