# AMPLAI Foundry

AMPLAI Foundry는 자연어 의도와 원문 증거를 프로젝트별 검토 가능한 지식 변경안으로 전환하는 결정론적 Knowledge Foundry다. 저장 방식과 독립된 `MemoryObject`를 정의하고 Markdown + YAML Front Matter를 첫 repository adapter로 사용한다.

## Scope

현재 구현은 다음 범위를 포함한다.

- `MemoryObject`, lifecycle, relation domain contract
- 읽기 전용 `MarkdownMemoryRepository`와 deterministic lexical search
- immutable Source ingestion과 SHA-256 duplicate detection
- Proposal validation, diff, approval, safe apply
- `(namespace, local_id)` 기반 qualified project identity
- fail-closed `ProjectResolver`와 이동 가능한 Project Pack
- Source → Classification → Candidate → Semantic Compare → Proposal Intake
- versioned roadmap change, impact analysis, replan
- Phase 1 artifact별 최소 평가 기록과 30-case semantic golden set
- Codex Curator Harness와 Context Bundle
- Obsidian에서 바로 열 수 있는 `vault/`
- 결정론적 `lint`, `stats`, `show`, `schema`, `verify` CLI
- 53개 AMPLAI canonical/source note
- offline unit/CLI test

외부 LLM 연결, 범용 의미 변경 자동 승인, vector DB, embedding, MCP server, web UI,
중앙 server와 실행 event 저장은 포함하지 않는다. 의미가 불명확한 입력은 생성으로
추정하지 않고 `HOLD`한다. 자동 적용은 독립 duplicate evidence 연결과 Tracker
status-only 변경처럼 정책에 명시된 비파괴 범위로 제한한다.

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
amplai-foundry project validate amplai
amplai-foundry project rebuild amplai
```

### Intent-driven Intake

```bash
amplai-foundry intake process roadmap.md \
  --instruction "이 로드맵을 AMPLAI에 반영해줘" \
  --project amplai
```

이 명령은 Source 원문, 분류, Candidate, 의미 비교, review Proposal, Roadmap 분석과
최소 평가 기록을 만든다. 공식 의미는 승인·apply 전까지 변경하지 않는다. 허용된
low-risk 변경은 audit 가능한 policy approval로만 적용한다. 프로젝트나 문서 유형을
안전하게 확정하지 못하면 exit code `1`과 `HOLD`를 반환한다.

### Project Pack And Roadmap

```bash
amplai-foundry project list
amplai-foundry project pack amplai --output dist/amplai-pack.zip
amplai-foundry roadmap diff desired-roadmap.yaml
amplai-foundry roadmap approve RMAP-... --approved-by reviewer
amplai-foundry roadmap apply RMAP-...
amplai-foundry roadmap next
```

### GPT Response File

```bash
amplai-foundry ingest ~/Downloads/gpt-answer.md \
  --project amplai \
  --source-type chatgpt
```

### Clipboard Input

```bash
pbpaste | amplai-foundry ingest - \
  --project amplai \
  --source-type chatgpt
```

### Search And Curate

```bash
amplai-foundry search "MCP Memory Context" --project amplai
amplai-foundry curate prepare SRC-... \
  --project amplai \
  --output .amplai/jobs/CURATE-SRC-....md
```

### Review And Apply

```bash
amplai-foundry proposal validate .amplai/proposals/PROP-.../proposal.yaml
amplai-foundry proposal diff PROP-...
amplai-foundry proposal approve PROP-... --approved-by user
amplai-foundry proposal apply PROP-...
```

의미 변경의 `approve`와 `apply`는 human의 명시적 요청 뒤에만 실행한다. 정책상
low-risk evidence/Tracker 변경만 authority opt-in으로 자동 적용할 수 있으며,
`CONFLICT`가 있는 Proposal은 apply하지 않는다.

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
amplai-foundry verify
```

`verify`는 pytest, Ruff check/format, strict mypy, schema drift, Vault lint와 Project Pack 검증을 한 번에 실행한다. 모든 명령은 network 없이 실행된다.

## Memory Layer Direction

상위 기능은 `MemoryRepository` port에 의존한다. 현재 Markdown adapter 뒤에 SQLite 또는 PostgreSQL repository를 추가할 수 있다. FTS와 vector search는 canonical repository에서 재생성하는 파생 index로 유지한다.

현재 계약의 세부 내용은 [Memory Domain Model](docs/MEMORY-DOMAIN-MODEL.md), [Lifecycle](docs/LIFECYCLE.md), [Relationships](docs/RELATIONSHIPS.md), [Project Pack](docs/PROJECT-PACK.md), [Knowledge Intake](docs/KNOWLEDGE-INTAKE.md), [Semantic Comparison](docs/SEMANTIC-COMPARISON.md), [Messenger Proposal Control](docs/MESSENGER-PROPOSAL-CONTROL.md), [Roadmap Changes](docs/ROADMAP-CHANGES.md), [Versioning](docs/VERSIONING.md)을 참조한다.
