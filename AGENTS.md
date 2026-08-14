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

## Codex Curation Contract

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

`CONFLICT` operation이 하나라도 있으면 전체 Proposal을 자동 apply하지 않는다. 충돌 없는 operation만 부분 적용할지 Codex가 임의로 결정하지 않는다.

## Spec-Kit Adoption

이 repository는 spec-kit(`/speckit-*`)과 기존 workstream 체계를 함께 쓴다. 원칙은 `.specify/memory/constitution.md`에 있고 그 문서는 이 파일의 파생이다. 두 문서가 어긋나면 이 파일이 이긴다.

### Pipeline

Claude에서는 skill을 `/skill-name`으로, Codex에서는 `$skill-name`으로 명시 호출한다.
아래 `/...` 표기는 canonical skill 이름의 기존 표기다. Codex adapter는
`.agents/skills/`에서 같은 Claude workflow를 읽고, `taskify`는 두 환경 모두
`.ai-team/skills/taskify` 원본을 공유한다.

```text
(/grill-me)       → 계획 심문 (선택)
/speckit-specify  → spec.md
/speckit-clarify  → spec 모호성 해소 (필수)
/speckit-plan     → plan.md
/taskify          → task manifest
/speckit-analyze  → spec/plan/tasks 정합성 (필수)
/speckit-implement→ 구현
subagent review   → contract / failure-recovery / regression 3인
/pinesky-workstream-gate → PASS 기록
```

- `/speckit-tasks`는 `/taskify`와 중복이라 **제거했다** (skill 디렉터리 삭제). 산출물을 두 벌 만들지 않는다. 그 자리를 메우던 workflow gate는 D-033으로 걷어냈고, 필수 3단계는 아래 **Pre-Implement Procedure**로 옮겼다.
- **`/speckit-implement` 앞에는 사람 승인이 없다** (D-033). 대신 Pre-Implement Procedure를 끝내고 구현 뒤 subagent review를 돌린다. review에서 P0/P1/Blocking-P2가 나오면 gate를 열지 않는다.
- **`/speckit-clarify`는 필수다** (D-037). `/speckit-specify` 뒤, `/speckit-plan` 앞에 돌린다. skill 자신이 그 순서를 요구한다 (`SKILL.md:62`). 모호한 곳이 없으면 질문하지 않고 coverage map만 보고하므로 (`:75`, `:127`) 억지 질문이 생기지 않는다.

  clarify는 **사람에게 묻고 멈춘다.** 이것은 D-033으로 없앤 승인 관문과 다르다. 승인이 아니라 모델이 갖고 있지 않은 사실을 받는 것이고, No Speculation 원칙과 같은 방향이다. 답 없이 추정으로 채우지 않는다.

  건너뛰려면 사용자가 명시적으로 그렇게 말해야 하고, 그때는 downstream rework 위험을 경고한다 (`SKILL.md:62`).
- **`/speckit-analyze`는 필수다** (D-036). `/taskify` 뒤, `/speckit-implement` 앞에 반드시 돌린다. 자세한 위치와 차단 규칙은 아래 **Pre-Implement Procedure** 4단계에 있다.
- **`/grill-me`의 후속은 `/speckit-specify`다.** grilling 산출물(다듬어진 계획·결정·확정된 fact)은 spec 입력이다. 심문이 끝나면 손으로 문서를 쓰지 말고 `/speckit-specify`에 넘긴다.
- **spec·plan 성격 문서는 손으로 쓰지 않는다.** `/speckit-specify`·`/speckit-plan`으로 만든다. `docs/workstreams/`는 **조사 기록**(fact 수집·원본 대조·측정 log) 용이지 spec 대체가 아니다. 손문서로 spec을 대신하면 `specs/`가 안 생겨 `/taskify`·`/speckit-implement`가 소비할 산출물이 없어지고, 그 상태를 근거로 speckit을 건너뛰는 순환이 생긴다. 경위는 [docs/SPECKIT-GRILLME-CHAIN.md](docs/SPECKIT-GRILLME-CHAIN.md).

### Skill Routing

겹칠 때 무엇을 부를지는 아래로 정한다.

| 하려는 일 | skill |
|---|---|
| 개념을 이해·설명 (Feynman 4단계, 모르는 곳 드러내기) | `/feynman` |
| 코드베이스·subsystem·흐름 설명 | `/eli12` |
| 계획·설계를 심문해 다듬기 | `/grill-me` |
| spec의 모호성을 질문 5개로 좁히기 | `/speckit-clarify` |
| spec 작성·갱신 | `/speckit-specify` |
| 설계 계획 | `/speckit-plan` |
| 작업 분해 → task manifest | `/taskify` |
| 구현 | `/speckit-implement` |
| spec/plan/tasks 정합성 점검 | `/speckit-analyze` |
| 요구사항 품질 체크리스트 | `/speckit-checklist` |
| 구현 후 잔여 작업 회수 | `/speckit-converge` |
| task → GitHub issue | `/speckit-taskstoissues` |
| 원칙 수립·개정 | `/speckit-constitution` |

경계가 헷갈리는 짝:

- `/feynman` vs `/eli12` — 개념이면 feynman, 이 repo의 코드면 eli12. eli12는 bug triage·code review에 쓰지 않는다 (skill 자체 선언).
- `/grill-me` vs `/speckit-clarify` — grill-me는 형식 없는 심문이고 아무 단계에서나 쓴다. 다만 **산출물은 `/speckit-specify`로 넘긴다** (Pipeline 절). speckit-clarify는 spec 파일에 답을 써넣는 파이프라인 단계다.
- `/speckit-analyze` vs `/speckit-checklist` — analyze는 artifact 3자 정합성, checklist는 요구사항 자체의 품질.

### Review Before Gate

`/speckit-implement` 다음에는 반드시 subagent review를 돌린다.

- reviewer는 관점이 서로 다른 셋이다. contract, failure/recovery, regression.
- P0, P1, Blocking-P2가 하나라도 있으면 gate를 열지 않는다.
- Advisory는 기록하고 item별로 판단한다.
- review를 실행하지 않았으면 gate 결과를 기록하지 않는다. 실행하지 않은 검증을 통과했다고 보고하지 않는다는 Completion Gate 규칙이 여기에도 적용된다.

`.specify/workflows/speckit/workflow.yml`의 `review-implementation` step이 이 관문이다.

### Scope

- spec-kit은 `specs/` 아래 문서만 만든다. canonical Vault와 Git state는 Codex Curation Contract를 따른다.
- spec-kit script는 git branch를 만들지 않는다. `specs/NNN-name/` 디렉터리만 만든다.
- `/speckit-implement`는 코드를 실제로 수정한다. 사용자 승인 없이 스스로 호출해도 된다 (D-033).
  대신 아래 **Pre-Implement Procedure**를 먼저 끝내고, 구현 뒤에는 **Review Before Gate**를
  반드시 돌린다. 이 둘이 승인을 대신하는 통제다.

### Pre-Implement Procedure

`/speckit-implement` 전에 **넷**을 순서대로 끝낸다. 사람 승인은 없앴지만 이 단계는 없애지 않았다.
2026-08-03에 이 단계가 끊겨 `tasks.md`가 영영 안 생긴 사고가 있었다.

1. `/taskify <spec.md 또는 plan.md 경로와 범위>`로 task manifest를 만든다.
   산출물은 `<manifest-dir>/index.yaml`과 `<FEATURE>-T001.yaml …`이다.
   설계 소스가 `specs/` 밖이면 `<manifest-dir>`는 `.amplai/tasks/<slug>/`다.
2. validator를 돌린다. 통과해야 다음으로 간다.

   ```text
   python3 .claude/skills/taskify/scripts/validate_task_manifest.py <manifest-dir>
   ```

3. speckit이 읽을 `tasks.md`를 **생성**한다. 손으로 쓰지 않는다.

   ```text
   python3 .specify/scripts/taskify_to_tasks_md.py <manifest-dir> --out <feature-dir>/tasks.md
   ```

   `--out`을 반드시 준다. manifest는 `.amplai/tasks/` 아래 있고 speckit은 feature dir에서
   `tasks.md`를 찾는다. 기본값으로 돌리면 speckit이 못 찾는다.

4. `/speckit-analyze`로 `spec.md`·`plan.md`·`tasks.md` 정합성을 본다. `tasks.md`가 있어야
   돌아가므로 3단계 뒤다.

   **CRITICAL 또는 HIGH가 하나라도 있으면 구현을 시작하지 않는다.** MEDIUM과 LOW는 기록하고
   item별로 판단한다. 판정 기준은 skill의 Severity Assignment 절에 있다.

   analyze는 `spec.md`·`plan.md`·`tasks.md` **세 artifact를 서로** 대조한다. **소스 코드는
   읽지 않는다.** 그래서 "spec은 A라는데 코드는 B다" 류는 못 잡는다 — 그것은 구현 뒤
   contract reviewer의 몫이다. analyze가 잡는 것은 task 없는 요구사항, 요구사항 없는 task,
   중복·모호한 요구사항, 미명세 항목이다. 둘은 서로를 대체하지 않는다.

넷 중 하나라도 실패하면 구현을 시작하지 않는다. 생성된 `tasks.md`는 순서·파일소유만 담은
목차다. 계약(acceptance, invariants, forbidden_paths)은 manifest YAML에 그대로 있다.
`blocked` task는 실행 단계에서 빠진다.
