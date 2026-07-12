---
schema_version: 1
id: PRI-0001
namespace: org/default/project/amplai
project: amplai
kind: principle
status: active
title: 입력은 느슨하게 받고 내부 지식은 엄격하게 관리한다
summary: 다양한 외부 입력은 수용하되 공식 지식으로 승격할 때는 공통 계약과 검토를 적용한다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86C363BF]
relations:
  - type: supports
    target: CON-0008
  - type: related_to
    target: PRI-0003
revision: 1
tags: [principle, ingestion, governance]
---
# 입력은 느슨하게 받고 내부 지식은 엄격하게 관리한다

## Principle

Source 형식과 표현이 달라도 ingestion 입구에서 수용할 수 있다. 그러나 내부의 Canonical Knowledge는 동일한 Memory Object 계약, provenance, lifecycle과 review를 충족해야 한다. 입력 유연성이 공식 지식의 불명확함으로 전파되지 않게 한다.
