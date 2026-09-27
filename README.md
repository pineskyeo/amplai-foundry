# AMPLAI Foundry

AMPLAI Foundry는 자연어 의도와 원문 증거를 프로젝트별 검토 가능한 지식 변경안으로 전환하는 결정론적 Knowledge Foundry다. 저장 방식과 독립된 `MemoryObject`를 정의하고 Markdown + YAML Front Matter를 첫 repository adapter로 사용한다.

## Product Boundaries

이 repository에는 서로 다른 두 release train이 함께 있다.

1. **AMPLAI Platform / Foundry (`src/amplai_foundry/`, 현재 `0.4.0`)** — 장기 제품이다.
   지식 intake, provenance, project identity, governance, decision, audit/outbox와 향후
   knowledge-aware agent control plane을 소유한다.
2. **AMPLAI Loop Kit (`tools/amplai-loop-kit/`, 로컬 통합 후보 `2.5.0`)** — 위 플랫폼과 각 앱을
   개발하기 위한 제거 가능한 development runtime이다. Contract, Work, evidence, local
   supervisor, installer와 Claude Code/Codex host adapter를 소유한다.

의존 방향은 한쪽이다. **Kit은 Platform을 개발·검증할 수 있지만 Platform domain code는
Kit, `.ai-team/`, Claude Code, Codex에 import 또는 runtime 의존하지 않는다.** Host 차이는
runner/hook/skill adapter에서 끝내고 Project Store protocol과 product domain으로 새지 않게 한다.
두 구성요소는 version, changelog, compatibility matrix와 release gate를 독립적으로 관리한다.

Kit 2.5.0 후보는 runnable baseline, 문서 원본·검토·버전별 HTML과 안전한 인계를 포함한다.
[Portable Development Guide](docs/PORTABLE-DEVELOPMENT.md)가 사용 절차를 설명한다.
후보 구현과 로컬 검증은 실제 fleet 갱신·배포·RHEL7 운영 검증을 대신하지 않는다.

Agent entry point는 Claude Code에서 `/work`, `/design`, Codex에서 `$work`, `$design`이다.
공통 skill 정본은 `.agents/skills/`이고 `.claude/skills/`는 symlink mirror다.


### Platform 0.4 Control Plane

`src/amplai_foundry/control_plane/`은 기존 Governance Store를 재작성하지 않고 추가된 독립 경계다.
프로젝트 범위 bearer token, 쓰기 idempotency, Decision/Evidence canonical reference, lease 기반
Context Job, transactional outbox, connector secret reference, replayable projection과 dependency-free WSGI API를 제공한다.

```bash
amplai-foundry control-plane init --db .amplai/control-plane.db
amplai-foundry control-plane token-issue --tenant local --project amplai --permission evidence:publish --permission decision:publish --permission context:request --permission context:read --permission reference:read --permission projection:read
amplai-foundry control-plane serve --db .amplai/control-plane.db
```

## Scope

현재 구현은 다음 범위를 포함한다.

- `MemoryObject`, lifecycle, relation domain contract
- 읽기 전용 `MarkdownMemoryRepository`와 deterministic lexical search
- immutable Source ingestion과 SHA-256 duplicate detection
- Proposal validation, diff와 governed decision foundation
- `(namespace, local_id)` 기반 qualified project identity
- fail-closed `ProjectResolver`와 이동 가능한 Project Pack
- Source → Classification → Candidate → Semantic Compare → Proposal Intake
- versioned roadmap change, impact analysis, replan
- Phase 1 artifact별 최소 평가 기록과 30-case semantic golden set
- Agent-neutral Curator Harness와 Context Bundle
- Obsidian에서 바로 열 수 있는 `vault/`
- 결정론적 `lint`, `stats`, `show`, `schema`, `verify` CLI
- 53개 AMPLAI canonical/source note
- offline unit/CLI test

Platform 제품 본체에는 외부 LLM 연결, 범용 의미 변경 자동 승인, vector DB, embedding,
MCP server, web UI, 중앙 server와 agent execution event 저장을 아직 포함하지 않는다.
Loop Kit의 로컬 Project Store와 worker event는 별도 development-runtime 범위다. 의미가 불명확한 입력은 생성으로
추정하지 않고 `HOLD`한다. Intake는 canonical state를 자동 적용하지 않는다.

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
최소 평가 기록을 만든다. CLI identity는 현재 OS user ID에서 파생하고 Governance Store는
Workspace의 `.amplai/runtime/governance.db`로 고정한다. AMPLAI는 이 identity를 active
Actor binding과 Project permission으로 다시 확인한다. Intake Policy Actor는 review Proposal만 제출한다.
프로젝트나 문서 유형을 안전하게 확정하지 못하면 exit code `1`과 `HOLD`를 반환한다.

### Project Pack And Roadmap

```bash
amplai-foundry project list
amplai-foundry project pack amplai --output dist/amplai-pack.zip
amplai-foundry roadmap diff desired-roadmap.yaml
amplai-foundry roadmap show RMAP-...
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

### Proposal Review

```bash
amplai-foundry proposal validate .amplai/proposals/PROP-.../proposal.yaml
amplai-foundry proposal diff PROP-...
```

CLI의 direct `proposal approve/apply`와 `roadmap approve/apply`는 fail-closed한다.
Decision은 server-created `AuthorityContext`와 `ActionToken`을 사용하는
`DecisionService`가 소유한다. Apply는 `MGC-009`의 `ApplyGrant` 전까지
`APPLY_ACTION_DEFERRED`를 반환한다.

Accepted Decision은 append-only hash Audit와 ordered Outbox를 같은 transaction에
기록한다. YAML과 Provider message는 authority가 아닌 projection이며 destination sequence,
lease, fencing, retry/DLQ 규칙으로 전달한다.

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


## V3 DEV-03 development snapshot

Current Python package: `3.0.0.dev3`; wire schemas remain `3.0.0`.

[DEV-03 Observatory & Meta-Harness 운영·검증 설명](docs/v3/DEV03_OBSERVATORY_META.ko.md) · [Primary-source notes](docs/v3/DEV03_RESEARCH_SOURCES.md) · [로컬 실행 사용법 (Mac, 단일 운영자)](docs/v3/USING_AMPLAI_WORK.ko.md).

`amplai ops evolution-demo --output <new-empty-directory>` executes local actual V3 paired trials, canary, signed promotion and rollback. It is not a live-provider/production qualification or the final V3 release. Delivery resume state and exact test evidence are in the outer snapshot `_v3_delivery/` and `validation/` directories.
