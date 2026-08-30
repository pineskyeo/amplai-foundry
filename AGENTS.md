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

## Agent Curation Contract

사용자가 GPT 답변 파일 경로와 함께 AMPLAI 지식 정리를 요청하면 다음 순서를 따른다.

1. `amplai-foundry ingest {path} --project amplai --source-type chatgpt` 실행
2. 반환된 Source ID 확인
3. `amplai-foundry curate prepare {source_id} --project amplai --output .amplai/jobs/CURATE-{source_id}.md` 실행
4. 기존 active knowledge 검색
5. 원자 후보와 `CREATE`, `UPDATE`, `LINK`, `MERGE`, `SPLIT`, `SUPERSEDE`, `CONFLICT`, `IGNORE` 분류
6. `.amplai/proposals/` 아래 Proposal과 draft 생성
7. Proposal validate, diff, Vault lint 실행
8. 변경, 중복, 충돌, 미해결 질문 보고

사용자가 대화에 원문을 직접 붙여넣으면 원문 전체를 `.amplai/tmp/` 아래 UTF-8 임시 파일에 verbatim으로 저장한다. 이 파일을 `ingest`한 뒤 Source 생성 성공 시 임시 파일을 삭제하고 같은 workflow를 수행한다. 원문을 요약한 텍스트로 Source를 대체하지 않는다.

“정리해줘”의 기본 결과는 Source, Proposal, validation report다. Canonical Vault는 수정하지 않는다.

사용자가 “반영해”, “적용해”, “승인하고 적용해”, “proposal을 적용해”처럼 명시적으로 요청해도 direct CLI mutation을 실행하지 않는다.

1. Proposal diff 검토
2. 사용자 요청을 governed decision intent로 기록
3. active Actor binding과 Project permission 확인
4. `DecisionService`와 `ActionToken` 준비 여부 확인
5. 미준비 상태면 `DIRECT_MUTATION_DISABLED` 또는 `APPLY_ACTION_DEFERRED` 보고
6. `MGC-009` 이전에는 canonical Vault와 Git state 미수정

`CONFLICT` operation이 하나라도 있으면 전체 Proposal을 자동 apply하지 않는다. 충돌 없는 operation만 부분 적용할지 agent가 임의로 결정하지 않는다.

## Public Workflow

사용자가 직접 호출하는 개발 entry point는 둘뿐이다. 의미는 host와 무관하고 호출 표기만
다르다.

```text
Claude Code   /work <goal>       /design <problem>
Codex         $work <goal>       $design <problem>
```

`work`는 구현·검증·수렴·리뷰까지 완료하고, `design`은 구현하지 않고 요구·설계·결정을
확정한다.

`specify`, `clarify`, `plan`, `taskify`, `analyze`, `implement`, `converge`,
`review`, `debug` 는 **internal capability** 다. 사용자가 그 순서를 지휘하게 하지 않는다
(`D-046`).

```text
/work
  → classify / contract
  → Knowledge Readiness
       DISCOVER → repository/domain evidence scan → re-evaluate
       BLOCKED  → human business/architecture decision
  → Context Pack / Environment
  → plan/tasks/analyze when needed
  → implement ↔ verify/repair
  → evidence → converge
  → documentation freshness → review → incremental gardening
  → handoff → DONE
```

Documentation Freshness 는 review **앞**이다. reviewer 가 볼 때 코드와 문서가 이미 맞아야
한다. Gardening 은 review **뒤**다. cleanup 이 본 기능을 흔들면 안 된다.

Domain-heavy/high Work 는 terminology·current behavior·boundary·invariant·source-of-truth·
contradiction·acceptance·verifier 가 evidence-backed READY 가 되기 전에는 coding 하지 않는다.

완료는 말이 아니라 executable evidence 로 판정한다. 기본 도구는 아래다.

```bash
python3 scripts/loopctl.py doctor
python3 scripts/loopctl.py classify --working
python3 scripts/loopctl.py contract validate specs/<feature>/work-contract.json
python3 scripts/loopctl.py readiness evaluate specs/<feature>/knowledge-readiness.json
python3 scripts/loopctl.py context validate specs/<feature>/context-pack.json
python3 scripts/loopctl.py permission check <action>
python3 scripts/loopctl.py docs validate --repo
python3 scripts/loopctl.py garden full --report-only
scripts/eval.sh --feature specs/<feature> --slice S01
python3 .ai-team/verifiers/run.py --profile v2
```

보조 skill은 loop 밖에 있고 사용자가 직접 부른다. Claude Code에서는 `/grill-me`,
`/feynman`, `/eli12`, `/grilling`, Codex에서는 같은 이름에 `$`를 붙인다. 개발 절차를
지휘하지 않는다.

## Runtime Ownership

- `.ai-team/runtime/` — risk, state transition, human gate, escalation policy
- `.ai-team/policy/` — Knowledge Readiness, permission, TDD, quadrant, documentation, gardening
- `.ai-team/contracts/` — Work Contract schema
- `.ai-team/verifiers/` — verifier registry 와 environment-aware runner
- `.ai-team/knowledge/` — active source/claim/decision index 와 Context Resolver metadata
- `.ai-team/evidence/` — evidence/provenance schema. canonical evidence 사본은 두지 않는다

canonical 지식은 `vault/` 에 있고 `.ai-team/` 은 그것을 복사하지 않는다. Decision 기록은
`docs/workstreams/*/DECISIONS.md` 가 갖는다.

## What Was Not Ported

cortex 의 **semantic runtime** — ontology TTL, SHACL, competency question, 읽기 전용 MCP,
project miner — 는 가져오지 않았다 (`D-046`). 이 저장소에는 그 기반이 없고 Knowledge Vault
와 Proposal 모델이 그 자리를 대신한다. 그 층의 검사는 verifier registry 의 `vault-lint` 와
`schema` check 가 맡는다.

## Skill Layout

**공통 정본은 `.agents/skills/`다.** Codex가 이 경로를 직접 읽고,
`.claude/skills/<name>`은 모두 정확히 `../../.agents/skills/<name>`을 가리키는 symlink다.
내부 capability는 `agents/openai.yaml`의 `allow_implicit_invocation: false`로 Codex의 암묵적
호출을 막는다. `loopctl doctor`가 skill 집합·visibility·symlink 방향을 검사한다.

## Review Before Gate

`work` entry point의 review 단계는 관점이 서로 다른 독립 reviewer 셋을 쓴다 — contract,
failure/recovery, regression. P0·P1·Blocking-P2 가 하나라도 있으면 gate 를 열지 않는다.
Advisory 는 기록하고 item 별로 판단한다. review 를 실행하지 않았으면 gate 결과를 기록하지
않는다.

reviewer 는 **순차로** 돌린다. 동시에 띄우면 세션 한도로 셋 다 잃는다. 각 reviewer 에게
큰 파일을 통째로 읽지 말라는 예산 규율을 준다.

`.specify/workflows/speckit/workflow.yml` 의 `review-implementation` step 이 아직 이 관문의
문서상 근거다. `work` controller가 그것을 흡수하면 그때 정리한다.
