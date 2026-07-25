---
schema_version: 1
id: CON-0902
namespace: org/default/project/test
project: test
kind: concept
status: active
title: 존재하지 않는 relation target fixture
summary: LINK_RELATION_TARGET_MISSING 규칙을 검증하는 concept다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: [SRC-20260711-86BF88EB]
relations:
  - type: related_to
    target: CON-9999
revision: 1
tags: [concept, fixture]
---
# 존재하지 않는 relation target fixture

이 note의 `CON-9999` relation은 repository에 존재하지 않는다. Linter는 file parse를 계속하고 정확한 missing target ID를 ERROR message로 알려야 한다.
