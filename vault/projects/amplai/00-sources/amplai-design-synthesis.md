---
schema_version: 1
id: SRC-20260711-FC31D25D
namespace: org/default/project/amplai
project: amplai
kind: source
status: active
title: AMPLAI 초기 아키텍처 설계 종합
summary: Foundry 구현 과정에서 명시적 요구를 원자 지식과 최소 아키텍처 경계로 정리한 설계 기록이다.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: []
relations: []
revision: 1
tags: [source, architecture, synthesis]
source_metadata:
  source_type: repository-bootstrap
  content_sha256: fc31d25da7c2bc7b6adc535a56c0520e74cc1c1db419428712781423ea65df84
  normalized_sha256: 3433ec15b5503405b2316f43cb8caa91f57174dff9c36b90e3dfd255a4ee1154
  original_filename: amplai-design-synthesis.md
  media_type: text/markdown
  ingested_at: 2026-07-11T18:00:00+09:00
  created_by: user
---
## Original Content
# AMPLAI 초기 아키텍처 설계 종합

## Source

Foundry 구현 시 저장 독립적인 Memory Object를 중심에 두고 Markdown을 첫 adapter로 배치했다. 공식 지식과 실행 메모리를 분리하고, 검색 계층을 재생성 가능한 파생물로 다루는 최소 설계를 이 기록에서 종합했다.
