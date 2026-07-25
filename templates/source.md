---
schema_version: 1
id: SRC-YYYYMMDD-SHA256XX
namespace: org/default/project/amplai
project: amplai
kind: source
status: active
title: Source 제목
summary: Source의 성격과 공식 지식에서 사용하는 범위를 요약한다.
created_at: YYYY-MM-DD
updated_at: YYYY-MM-DD
source_refs: []
relations: []
revision: 1
tags: [source]
source_metadata:
  source_type: {source type}
  content_sha256: {64 lowercase hexadecimal characters}
  normalized_sha256: {64 lowercase hexadecimal characters}
  original_filename: {filename or null}
  media_type: text/markdown
  ingested_at: YYYY-MM-DDTHH:MM:SS+09:00
  created_by: user
---
# Source 제목

## Original Content

입력 원문 전체를 그대로 기록한다. 이 template을 직접 복사하지 않고 `amplai-foundry ingest`를 사용한다.
