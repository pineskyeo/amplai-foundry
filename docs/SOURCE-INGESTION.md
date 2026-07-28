# Source Ingestion

## Contract

Source는 수집 당시 원문 bytes를 보존하는 ingest-only immutable record다. Source 생성은 `amplai-foundry ingest`만 수행한다. Proposal과 일반 수정 workflow는 Source를 생성하거나 변경하지 않는다.

`content_sha256`은 입력 UTF-8 bytes의 exact hash다. `normalized_sha256`은 CRLF를 LF로 바꾸고 마지막 개행 차이를 제거한 hash다.

동일 exact 또는 normalized hash가 존재하면 새 파일을 만들지 않는다. 기존 Source ID를 `duplicate_of`로 반환한다.

## Commands

```bash
amplai-foundry ingest response.md --project amplai --source-type chatgpt --json
cat response.md | amplai-foundry ingest - --project amplai --source-type chatgpt
amplai-foundry source list --project amplai
amplai-foundry source show SRC-... --project amplai
amplai-foundry source verify SRC-... --project amplai
```

파일 입력은 `.md`와 `.txt`만 허용한다. stdin은 `-`를 사용한다. 빈 입력과 non-UTF-8 입력은 거부한다.

## Identity And Path

새 ID는 `SRC-YYYYMMDD-{SHA256 8자리}` 형식이다. 파일은 `vault/projects/{project}/00-sources/{id}.md`에 저장해 title 차이가 ID-level atomic create를 우회하지 못하게 한다. 같은 32-bit hash prefix에 서로 다른 원문이 충돌하면 두 번째 입력을 fail-closed로 거부한다.

`project`, path project, namespace suffix는 일치한다. `--namespace` 생략 시 `org/default/project/{project}`를 사용한다.

Project ID는 `^[a-z0-9][a-z0-9_-]{0,63}$`만 허용한다. Project root와 최종 Source path를 resolve한 뒤 각각 `vault/projects`와 해당 project root 내부인지 확인한다. Containment 실패 시 파일을 만들지 않는다.

## Integrity

Source 본문에서 `## Original Content` 뒤의 모든 bytes가 원문이다. Linter와 `source verify`는 이 영역을 저장된 hash와 비교한다.

Source 생성 후 Front Matter, 원문, hash, relation, lifecycle을 수정하지 않는다. Proposal validator와 apply gate가 Source mutation을 각각 거부한다. 변경된 Source는 lint ERROR다.
