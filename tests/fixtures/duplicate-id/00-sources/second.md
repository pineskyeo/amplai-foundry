---
schema_version: 1
id: SRC-20260711-790096C8
namespace: org/default/project/test
project: test
kind: source
status: active
title: 두 번째 중복 source
summary: 중복 ID fixture의 두 번째 source record다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: []
relations: []
revision: 1
tags: [source, fixture]
source_metadata:
  source_type: fixture
  content_sha256: f196b8a52c9367e79d67d1d0d21bd354133ccfc2a52b0783cddeaabbb2fd470f
  normalized_sha256: f473b72283cddb24211403d0443efa0132f0ba2fba64836cc85800f44b84f824
  original_filename: second.md
  media_type: text/markdown
  ingested_at: 2026-07-11T12:00:00+09:00
  created_by: test
---
## Original Content
# 두 번째 중복 source

중복 ID validator가 duplicate issue를 생성하도록 같은 canonical ID를 의도적으로 사용한다. 다른 schema field는 모두 유효한 값으로 유지한다.
