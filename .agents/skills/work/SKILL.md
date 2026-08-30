---
name: work
description: AMPLAI의 기본 개발 entry point. 목표를 risk와 domain knowledge readiness로 분류하고, Context Pack과 evidence-governed contract를 준비한 뒤 dev-loop로 구현·검증·수렴·리뷰까지 완료한다.
argument-hint: "구현하거나 고칠 목표, 또는 specs/<feature> 경로"
user-invocable: true
disable-model-invocation: false
---

# work — AMPLAI V2 Controller

Claude Code는 `/work`, Codex는 `$work`로 호출한다. 이 문서의 `work`/`design` 표기는
host-neutral entry point를 뜻한다.

사용자 입력:

```text
$ARGUMENTS
```

`work`는 Skill 순서를 사용자에게 떠넘기지 않는다. 현재 Work의 상태, 위험도, 지식 준비도,
근거와 검증 환경을 확인하고 필요한 내부 capability만 선택한다.

## 절대 원칙

- `AGENTS.md`, `.ai-team/README.md`, 관련 policy를 먼저 읽는다.
- V1 dev-loop를 재구현하지 않는다.
- `Knowledge Readiness = DISCOVER|BLOCKED`인 상태에서 코딩하지 않는다.
- repository에서 확인 가능한 내용을 사용자에게 먼저 묻지 않는다.
- active와 superseded 지식이 충돌하면 임의 선택하지 않고 conflict로 표면화한다.
- LLM 자기평가를 PASS로 쓰지 않는다.
- knowledge candidate를 canonical Vault에 직접 반영하지 않는다.
- production/deploy/push/merge/release는 policy대로 gate 또는 차단한다.
- Humanize KR은 설명문에만 적용하고 코드·명령·로그·식별자는 바꾸지 않는다.

## 1. Intake / Resume

1. 입력이 새 goal인지 `specs/<feature>`인지 판별한다.
2. 기존 Work라면 다음을 읽는다.
   - `work-contract.json`
   - `knowledge-readiness.json`, `domain-discovery.json`
   - `context-pack.json`, `environment.json`, `handoff.json`
   - `spec.md`, `plan.md`, generated `tasks.md`, task manifests
   - `.specify/eval/`과 `evidence-trace.jsonl`
3. 다음으로 재개 위치를 확인한다.

```bash
python3 scripts/loopctl.py status <feature-dir>
```

이미 DONE인 Work는 증거를 확인하고 다시 실행하지 않는다.

## 2. Classify

예상 변경 경로와 Work 성격을 함께 분류한다.

```bash
python3 scripts/loopctl.py classify <path...>
```

Work type:

- `tiny_change`: 명확한 문구·국소 수정
- `bug_fix`: 기존 동작 회귀 수정
- `logic_change`, `refactor`, `new_feature`
- `domain_heavy`: ownership/invariant/source-of-truth를 알아야 하는 기능
- `architecture`, `operations`

경로 분류와 Work type 중 더 강한 route를 선택한다. 실제 diff 후 반드시 재분류한다.

## 3. V2 Contract

normal/high 또는 장기 Work는 구현 전에 `work-contract.json`을 만든다.

```bash
cp .ai-team/contracts/work-contract.template.json <feature-dir>/work-contract.json
python3 scripts/loopctl.py contract validate <feature-dir>/work-contract.json
```

Contract에는 Goal/Scope/Non-goal/Acceptance/Verifier뿐 아니라 다음이 명시돼야 한다.

- Knowledge Readiness 요구 여부
- Context Pack 및 Domain Discovery 경로
- environment fingerprint
- evidence trace
- permission/human gate

`tiny_change + low`만 compact fast path를 허용한다.

## 4. Knowledge Readiness Gate

새 Work는 readiness artifact를 만든다.

```bash
python3 scripts/loopctl.py readiness init <feature-dir>
python3 scripts/loopctl.py readiness evaluate <feature-dir>/knowledge-readiness.json --write
```

다음 여덟 항목을 근거로 판정한다.

```text
terminology / current behavior / system boundary / invariants
source of truth / contradictions / acceptance / verifier
```

- `READY`: Contract/Plan으로 진행
- `DISCOVER`: Domain Discovery를 먼저 수행
- `BLOCKED`: conflict 또는 사람이 정해야 하는 decision을 표시하고 중단

지식이 부족하면:

```bash
python3 scripts/loopctl.py discovery scan <feature-dir>
```

code/test/API/schema/config/docs/decision/git/runtime evidence와 관련 Vault knowledge를 먼저
조사한다. 후보 evidence를 실제 사실로 검토한 뒤 readiness를 갱신한다. business 또는
architecture decision만 사용자에게 질문한다.

## 5. Context Pack / Environment

Knowledge Readiness가 READY가 된 뒤 Context Pack과 verifier 환경을 고정한다.

```bash
python3 scripts/loopctl.py context build <feature-dir>
python3 scripts/loopctl.py context validate <feature-dir>/context-pack.json
python3 scripts/loopctl.py environment capture <feature-dir>
```

Context Pack은 active knowledge, active decisions, Vault refs, code/test scope, verifier profile,
unknown을 Work ID에 맞게 조립한다. superseded/rejected 지식은 기본 선택에서 제외한다.

새 session에서도 이 artifact로 같은 context를 재생성해야 한다.

## 6. Design / Plan / Slice

다음이면 internal `design` route로 이동한다.

- acceptance를 결정할 수 없음
- architecture/ownership/public contract 결정 필요
- active evidence 충돌
- high risk인데 승인된 plan 없음

그 외에는 필요한 만큼만 내부 capability를 사용한다.

```text
specify/clarify when needed → plan → taskify → analyze
```

Task는 executable Slice와 실제 Eval command를 가져야 한다. V3 scheduler는 만들지 않지만
future compatibility를 위해 work_id, slice_id, depends_on, scope, verifier_profile,
environment, evidence, git_checkpoint metadata를 보존한다.

## 7. Feedforward Guards

실행 전에 permission을 확인한다.

```bash
python3 scripts/loopctl.py permission check <action>
```

- exit 0 `allowed`: 진행
- exit 3 `gated`: 승인 전 중단
- exit 4 `prohibited`: 실행 금지

Bug fix는 regression test RED evidence를 먼저 만들고, logic/new feature는 가능한 경우 test 또는
acceptance-first를 적용한다. 별도 `/tdd` Skill은 만들지 않는다.

## 8. Execute V1 dev-loop

준비된 Context Pack과 Contract를 internal `dev-loop`에 전달한다.

```text
Slice → implement → deterministic verify
FAIL → diagnose → bounded repair → verify
PASS → next Slice
all PASS → full profile → converge
        → documentation freshness → independent review → incremental gardening
```

매 중요한 전이는 evidence trace에 남긴다.

```bash
python3 scripts/loopctl.py trace append <feature-dir> <event> --data-json '{...}'
```

같은 failure signature가 3회 반복되면 implementation/task/plan/spec/requirement/environment로
분류해 상위 단계로 escalation한다.

## 9. Documentation Freshness — review 앞

converge 다음, review 앞에서 문서를 맞춘다. reviewer가 보는 시점에 코드와 문서가 어긋나
있으면 안 된다.

```bash
python3 scripts/loopctl.py docs impact <feature-dir>
python3 scripts/loopctl.py docs validate <feature-dir>
```

behavior/API/schema/config semantics/state transition/architecture/ownership/domain rule/
operational behavior/knowledge semantics가 바뀌었으면 impact를 평가한다. formatting, comment,
local typo만 바뀐 Work는 `NOT_APPLICABLE`로 끝난다.

후보는 `.ai-team/policy/documentation.json`이 정한 다섯 축의 deterministic evidence로 먼저
좁힌다. 그다음에 의미 영향만 판단한다. 문서를 통째로 다시 쓰지 말고 impacted section만
최소 patch한다.

과거 결정은 덮어쓰지 않는다. 뒤집혔으면 옛 항목을 `superseded`로 두고 `superseded_by`로
새 결정을 가리킨다.

Documentation Freshness가 코드나 문서를 바꿨으면 필요한 verifier와 converge를 다시 실행한다.
review 결과가 코드 의미를 다시 바꾸면 Documentation Freshness도 다시 실행한다.

## 10. Incremental Gardening — review 뒤

review가 통과한 다음에 changed scope 주변만 정리한다. cleanup이 본 기능을 흔들면 안 된다.

```bash
python3 scripts/loopctl.py garden incremental <feature-dir>
python3 scripts/loopctl.py garden apply <feature-dir>     # SAFE_AUTO만 삭제한다
```

`garden apply`는 SAFE_AUTO만 지운다. EVIDENCE_REQUIRED와 HUMAN_GATED는 candidate로 남기고
사람에게 넘긴다. static reference가 없다는 사실만으로 dead code라고 판정하지 않는다 —
dynamic loading, plugin/entry point, conditional build, packaging, configuration-driven invocation을
함께 본다.

repository 전체 scan은 사용자가 명시적으로 요청한 cleanup Work에서만 한다.

```text
work_type = repository_gardening / risk = high / mode = full
```

그때도 기본은 report-only다.

```bash
python3 scripts/loopctl.py garden full --report-only
```

Gardening이 실제 file/code/config를 바꿨으면 loop로 되돌아간다.

```text
garden change → verify → converge → documentation freshness → (필요하면) review
```

SAFE_AUTO generated garbage 삭제만으로 heavyweight review를 다시 요구하지 않는다.

## 11. Knowledge change handling

이 repository에는 Cortex의 ontology/SHACL/CQ/MCP semantic runtime을 이식하지 않았다(`D-046`).
존재하지 않는 `semanticctl.py`나 ontology command를 호출하지 않는다. 개발 중 새 domain
concept/relation/invariant를 발견하면 다음 경로로 candidate를 남긴다.

```text
discovery → 원문 Source 보존 → active Vault/Decision 검색 → Proposal candidate
→ proposal validate/diff + vault/schema lint → governed review/apply 또는 reject
```

Canonical Vault를 작업 편의상 직접 덮어쓰지 않는다. 현재 governance apply가 준비되지 않았으면
Proposal과 evidence까지만 만들고 `APPLY_ACTION_DEFERRED`로 남긴다.

## 12. Handoff / DONE

중단하거나 session을 넘기기 전:

```bash
python3 scripts/loopctl.py handoff write <feature-dir> --next-action "..."
python3 scripts/loopctl.py trace verify <feature-dir>/evidence-trace.jsonl
```

DONE은 다음을 모두 만족해야 한다.

- 모든 in-scope Slice 완료
- selected verifier profile PASS + environment fingerprint
- Context Pack이 active/provenance-aware knowledge를 사용
- converge clean
- 의미 변경이 있으면 Documentation Impact가 평가되고 STALE이 남아 있지 않음
- 관련 canonical documentation이 ACTIVE이거나 의도적으로 SUPERSEDED/DEPRECATED
- 중요한 architecture decision이 historical provenance를 유지
- 필요한 범위에 Incremental Gardening 실행, candidate마다 evidence와 risk 존재
- HUMAN_GATED candidate가 자동 삭제되지 않음
- Gardening으로 바뀐 repository가 다시 verifier를 통과
- required review PASS
- required human gate 승인
- 주요 판단과 repair를 evidence trace로 재구성 가능

최종 보고는 `결론 → 사용법/변경 → 검증 → 남은 gate` 순으로 자연스러운 한국어로 한다.

<!-- AMPLAI-ASYNC-BEGIN -->
## Decision & Async Cross-App Runtime

`AMPLAI_PROJECT_HOME`과 `.ai-team/app.json`이 있으면 현재 작업을 공용 Work와 연결한다.
사용자에게 internal Decision/CR/Handoff 단계를 선택하게 하지 않는다.

1. supervised 실행이면 `AMPLAI_WORK_ID`의 Context를 먼저 읽는다.

```bash
python3 scripts/amplai.py work context --id "$AMPLAI_WORK_ID"
```

2. Open Question을 발견하면 repository에서 확인 가능한 사실을 먼저 조사하고 구조화한다.

```text
Question → Evidence mode(DIRECT|LOCAL|PARALLEL)
         → policy minimum(AUTO|CHALLENGE|HUMAN)
         → Decision → Verify
```

- LOCAL이 기본이다. 독립적인 evidence lane이 2개 이상이고 병렬 이익이 있을 때만 PARALLEL이다.
- Primary Agent가 최종 Decision owner다. Subagent는 evidence worker/challenger다.
- CHALLENGE는 독립 ACCEPT review evidence, HUMAN은 human_approval evidence가 있어야 닫힌다.
- 필수 OPEN Question이 남은 Work는 DONE으로 끝내지 않는다.

3. 다른 앱 작업이 필요하면 prose-only handoff를 만들지 않는다. 같은 CR에 target Work를 만들고
acceptance, dependency, contract/decision/evidence reference를 연결한 뒤 현재 Work를 WAITING으로
전환한다. target view가 필요할 때만 `work context --format markdown`으로 렌더링한다.

4. worker가 종료되기 전에 현재 Work를 반드시 다음 중 하나로 durable transition한다.

```text
DONE | WAITING | BLOCKED | HUMAN_REQUIRED | FAILED
```

- WAITING: 구조화된 dependency가 끝나면 Supervisor가 자동 재개한다.
- BLOCKED: 자동 해제 조건이 아직 모델링되지 않았다.
- HUMAN_REQUIRED: 사람의 결정 없이는 자동 재개하지 않는다.

`AMPLAI_LEASE_TOKEN`은 이미 환경에 있고 `scripts/amplai.py`가 직접 읽는다. 명령줄에 토큰을
쓰거나 출력하지 않는다.

죽은 upstream 때문에 막혔다면 사람에게 다음을 제안한다. Agent가 임의로 실행하지 않는다.

```text
work cancel --cascade | work retarget --depends-on | work reset-attempts
```

5. Work가 DONE이면 result evidence를 남기고 dependency를 해제한다. 독립적인 다른 앱 Work는 한
Work의 HUMAN_REQUIRED와 무관하게 계속 진행될 수 있다.

중앙 Store나 app identity가 없는 단일 repo 작업은 기존 Loop V2 경로로 정상 동작한다.
<!-- AMPLAI-ASYNC-END -->
