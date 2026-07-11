# AMPLAI Foundry

AMPLAI Foundry는 검토된 장기 프로젝트 지식을 저장하고 검증하는 AMPLAI Memory Layer의 첫 prototype이다. 저장 방식과 독립된 `MemoryObject`를 정의하고 Markdown + YAML Front Matter를 첫 repository adapter로 사용한다.

## Scope

현재 구현은 다음 범위만 포함한다.

- `MemoryObject`, lifecycle, relation domain contract
- 읽기 전용 `MarkdownMemoryRepository`
- Obsidian에서 바로 열 수 있는 `vault/`
- 결정론적 `lint`, `stats`, `show` CLI
- 26개 AMPLAI sample note
- offline unit/CLI test

LLM 연결, 자동 추출·승인·병합, vector DB, embedding, MCP server, web UI, 중앙 server와 실행 event 저장은 포함하지 않는다.

## Install

Python 3.11 이상이 필요하다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## CLI

```bash
amplai-foundry lint vault/
amplai-foundry stats vault/
amplai-foundry show DEC-0001 --vault vault/
```

`lint` exit code는 다음과 같다.

- `0`: ERROR 없음
- `1`: 한 개 이상의 lint ERROR
- `2`: vault 경로 또는 실행 자체 실패

개별 Markdown의 Front Matter parse 오류는 고칠 수 있는 lint ERROR이므로 exit code `1`이다.

## Obsidian

Obsidian에서 **Open folder as vault**를 선택하고 repository의 `vault/` directory를 연다. 전용 plugin은 필요 없다. 일반 Markdown link와 `[[Wiki Link]]`를 함께 사용할 수 있다.

## Add A Note

1. `templates/`에서 kind에 맞는 template을 복사한다.
2. `vault/projects/amplai/`의 kind directory에 파일을 만든다.
3. 기존 ID와 제목을 검색한다.
4. Front Matter의 필수 필드와 `source_refs`를 작성한다.
5. 본문은 하나의 질문 또는 주장에 집중한다.
6. 관련 Map에 structured relation과 Wiki Link를 추가한다.
7. `amplai-foundry lint vault/`와 test suite를 실행한다.

Decision을 바꿀 때 기존 note를 조용히 수정하지 않는다. 새 Decision에 `supersedes` relation을 추가하고 기존 Decision을 `superseded`로 바꾸며 `superseded_by`를 기록한다.

## Validation

```bash
python -m pytest
ruff check .
ruff format --check .
mypy src
amplai-foundry lint vault/
amplai-foundry stats vault/
```

모든 명령은 network 없이 실행된다.

## Memory Layer Direction

상위 기능은 `MemoryRepository` port에 의존한다. 현재 Markdown adapter 뒤에 SQLite 또는 PostgreSQL repository를 추가할 수 있다. FTS와 vector search는 canonical repository에서 재생성하는 파생 index로 유지한다.

다음 단계는 [Roadmap](docs/ROADMAP.md)에 제한해 기록했다. 현재 계약의 세부 내용은 [Memory Domain Model](docs/MEMORY-DOMAIN-MODEL.md), [Lifecycle](docs/LIFECYCLE.md), [Relationships](docs/RELATIONSHIPS.md)을 참조한다.
