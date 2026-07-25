---
schema_version: 1
id: SRC-20260711-86BF88EB
namespace: org/default/project/test
project: test
kind: source
status: active
title: 깨진 link fixture source
summary: 깨진 relation fixture가 사용할 정상 provenance source다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: []
relations: []
revision: 1
tags: [source, fixture]
source_metadata:
  source_type: fixture
  content_sha256: 86bf88eb1d61abf96cb6f1e24175ec817ad68a629372ac5ed706b32b8798cbdd
  normalized_sha256: e0598b8af6e05915f082e1f3a331a3ed32299bfd32d8b35e07c8a6bb1fc23f13
  original_filename: source.md
  media_type: text/markdown
  ingested_at: 2026-07-11T12:00:00+09:00
  created_by: test
---
## Original Content
# 깨진 link fixture source

이 source는 concept의 provenance reference만 정상으로 유지한다. Relation target 한 개만 의도적으로 깨뜨려 link rule의 결과를 분리해 확인한다.
