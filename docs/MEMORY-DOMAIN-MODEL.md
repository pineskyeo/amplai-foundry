# Memory Domain Model

## Memory Object

`MemoryObject`는 저장 방식과 독립된 canonical contract다. Markdown adapter는 Front Matter를 field로, Front Matter 아래 Markdown을 `content`로 변환한다.

| Field | Type | Rule |
|---|---|---|
| `schema_version` | literal `1` | 현재 contract version |
| `id` | string | kind prefix를 가진 repository unique ID |
| `namespace` | string | 조직과 project scope |
| `project` | string | project key |
| `kind` | enum | 지원되는 장기 지식 kind |
| `status` | enum | lifecycle state |
| `title` | string | 사람이 읽는 단일 주제 제목 |
| `summary` | string | 한두 문장 핵심 요약 |
| `created_at` | date | 최초 생성일 |
| `updated_at` | date | 마지막 의미 변경일 |
| `source_refs` | ID list | provenance source note |
| `relations` | relation list | typed directed edge |
| `superseded_by` | optional ID | 대체한 memory |
| `merged_into` | optional ID | 병합 대상 memory |
| `revision` | integer | `1` 이상 |
| `tags` | string list | 보조 탐색 label |
| `content` | Markdown | Front Matter 아래 본문 |

## Kinds

지원 kind는 `source`, `concept`, `principle`, `decision`, `question`, `architecture`, `experiment`, `map`이다.

Agent run, step, event, tool call, checkpoint, session state와 temporary memory candidate는 이 contract의 현재 대상이 아니다.

## Repository Port

`MemoryRepository`는 `get`, `list`, `find_by_kind`, `find_referencing`, `exists`를 제공하는 읽기 port다. 현재 `MarkdownMemoryRepository`만 구현한다. 범용 write API는 review/apply contract 전까지 만들지 않는다.

## Storage Adapters

Markdown은 첫 canonical adapter다. 향후 SQLite와 PostgreSQL adapter를 같은 port 뒤에 추가할 수 있다. Vector search는 repository가 아니라 파생 index다.
