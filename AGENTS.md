# Repository Rules

## Knowledge Safety

- 공식 지식을 임의로 덮어쓰지 않는다.
- 새 지식을 만들기 전에 ID, 제목, alias와 핵심 문구로 기존 지식을 검색한다.
- 하나의 atomic note는 하나의 핵심 질문 또는 주장만 다룬다.
- Decision 변경은 새 Decision과 `supersedes` relation으로 기록한다.
- 기존 Decision에는 `status: superseded`와 `superseded_by`를 기록한다.
- 삭제보다 `superseded`, `deprecated`, `merged`, `archived` lifecycle을 사용한다.
- AI가 공식 지식을 자동 승인하거나 자동 수정하게 만들지 않는다.

## Change Scope

- 작은 검증 가능한 변경을 우선한다.
- 상위 기능은 `MemoryRepository`에 의존하고 Markdown path에 직접 의존하지 않는다.
- 범용 write API는 명시적 review/apply 설계 전까지 추가하지 않는다.
- vector DB, embedding, MCP server, web UI, 인증, 중앙 server를 임의로 추가하지 않는다.
- Run, Step, Event, tool call과 session state를 Markdown 공식 지식에 섞지 않는다.

## Completion Gate

- 변경한 계약에 맞는 test를 추가한다.
- `python -m pytest`, Ruff, mypy와 knowledge lint가 통과해야 완료로 보고한다.
- 실행하지 않은 검증을 통과했다고 보고하지 않는다.
- 실패를 숨기지 않고 명령, exit code와 원인을 기록한다.
