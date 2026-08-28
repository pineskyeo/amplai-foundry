# AMPLAI Foundry Constitution

이 문서는 `speckit-*` skill 이 판단 근거로 삼는 프로젝트 원칙이다. 내용은 `AGENTS.md`
와 `docs/workstreams/*/QUALITY_GATES.md` 에서 옮겼다. 새 원칙을 여기서 만들지 않는다.

## Core Principles

### I. Knowledge Safety

공식 지식을 임의로 덮어쓰지 않는다. 새 지식을 만들기 전에 ID, 제목, alias 와 핵심
문구로 기존 지식을 검색한다. 하나의 atomic note 는 하나의 핵심 질문 또는 주장만
다룬다. Decision 변경은 새 Decision 과 `supersedes` relation 으로 기록하고, 기존
Decision 에는 `status: superseded` 와 `superseded_by` 를 쓴다. 삭제보다
`superseded`, `deprecated`, `merged`, `archived` lifecycle 을 쓴다. AI 가 공식
지식을 자동 승인하거나 자동 수정하게 만들지 않는다.

Source: `AGENTS.md` — Knowledge Safety

### II. Small Verifiable Change

작은 검증 가능한 변경을 우선한다. 상위 기능은 `MemoryRepository` 에 의존하고
Markdown path 에 직접 의존하지 않는다. 범용 write API 는 명시적 review/apply 설계
전까지 추가하지 않는다. vector DB, embedding, MCP server, web UI, 인증, 중앙 server
를 임의로 추가하지 않는다. Run, Step, Event, tool call 과 session state 를 Markdown
공식 지식에 섞지 않는다.

Source: `AGENTS.md` — Change Scope

### III. Evidence-Based Completion (NON-NEGOTIABLE)

변경한 계약에 맞는 test 를 추가한다. `python -m pytest`, Ruff, mypy 와 knowledge
lint 가 통과해야 완료로 보고한다. **실행하지 않은 검증을 통과했다고 보고하지 않는다.**
실패를 숨기지 않고 명령, exit code 와 원인을 기록한다.

이 원칙은 다른 모든 원칙에 우선한다. 증거 없는 완료 보고는 gate 를 무효로 만든다.

Source: `AGENTS.md` — Completion Gate

### IV. Governed Mutation Only

사용자가 "반영해", "적용해", "승인하고 적용해" 처럼 명시적으로 요청해도 direct CLI
mutation 을 실행하지 않는다. Proposal diff 검토 → governed decision intent 기록 →
active Actor binding 과 Project permission 확인 → `DecisionService` 와 `ActionToken`
준비 여부 확인 순서를 따른다. 미준비 상태면 `DIRECT_MUTATION_DISABLED` 또는
`APPLY_ACTION_DEFERRED` 를 보고한다.

`CONFLICT` operation 이 하나라도 있으면 전체 Proposal 을 자동 apply 하지 않는다.
충돌 없는 operation 만 부분 적용할지 agent 가 임의로 결정하지 않는다.

Source: `AGENTS.md` — Codex Curation Contract

### V. Independent Review Before Gate

구현 완료는 gate 통과가 아니다. gate 는 서로 다른 관점의 독립 reviewer 검토를 거쳐야
열린다. blocker 가 0건이어야 PASS 다.

| Severity | 처리 |
|---|---|
| P0 | 차단 |
| P1 | 차단 |
| Blocking-P2 | 차단 |
| Advisory | 기록 후 item 별 판단 |

Source: `docs/workstreams/messenger-governance-closure-v3/QUALITY_GATES.md`

## Quality Gates

Item gate 는 다음을 모두 만족해야 PASS 다.

- Frozen acceptance 충족
- Item scope test 통과
- 기존 regression test 통과
- Ruff 통과
- mypy 통과
- Knowledge lint 통과
- Subagent contract review blocker 0건
- Subagent failure/recovery review blocker 0건
- Subagent regression review blocker 0건
- Main agent finding triage 완료

## Spec-Driven Workflow

이 프로젝트는 spec-kit 파이프라인과 기존 workstream 체계를 함께 쓴다. 역할이 다르다.

| 축 | 담당 |
|---|---|
| 무엇을 만들 것인가 | spec-kit — `spec.md`, `plan.md` |
| spec 모호성 해소 | `/speckit-clarify` — 답을 `spec.md` 에 기록 |
| 작업 분해 | `/taskify` — task manifest |
| 구현 전 정합성 | `/speckit-analyze` — spec/plan/tasks 대조 |
| 실행 | `/speckit-implement` |
| 검토와 gate | subagent review 3인 → `/pinesky-workstream-gate` |

규칙:

- `/speckit-tasks` 대신 `/taskify` 를 쓴다. 산출물이 두 벌 생기는 것을 막는다.
- `/speckit-plan` **앞에는** 반드시 `/speckit-clarify` 를 돌린다 (D-037). 모호한 곳이 없으면
  질문 없이 coverage map 만 나온다. 사람에게 묻고 멈추는 것은 승인 관문이 아니라 모르는
  사실을 받는 단계다 — 원칙 위의 No Speculation 과 같은 방향이다.
- `/speckit-implement` **앞에는** 반드시 `/speckit-analyze` 를 돌린다 (D-036). CRITICAL 또는
  HIGH 가 있으면 구현하지 않는다. analyze 는 artifact 끼리만 대조하고 소스 코드는 읽지
  않으므로 구현 뒤 subagent review 를 대체하지 않는다.
- `/speckit-implement` 다음에는 반드시 subagent review 를 돌린다. review 없이 gate 를
  기록하지 않는다.
- spec-kit 은 `specs/` 아래 문서만 만든다. canonical Vault 와 Git state 변경은 원칙
  IV 를 따른다.

## Governance

이 constitution 은 `AGENTS.md` 와 workstream `QUALITY_GATES.md` 의 파생 문서다.

- 원칙을 여기서 새로 만들지 않는다. 원본을 먼저 고치고 여기에 반영한다.
- 원본과 이 문서가 어긋나면 원본이 이긴다.
- 개정은 어느 원본 문서의 어느 절이 바뀌었는지와 함께 기록한다.
- 원칙 III 은 어떤 이유로도 완화하지 않는다.

**Version**: 1.0.0 | **Ratified**: 2026-07-31 | **Last Amended**: 2026-07-31
