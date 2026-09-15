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
- knowledge candidate를 canonical knowledge에 직접 반영하지 않는다.
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

code/test/API/schema/config/docs/decision/git/runtime evidence와 관련 repository knowledge knowledge를 먼저
조사한다. 후보 evidence를 실제 사실로 검토한 뒤 readiness를 갱신한다. business 또는
architecture decision만 사용자에게 질문한다.

## 5. Context Pack / Environment

Knowledge Readiness가 READY가 된 뒤 Context Pack과 verifier 환경을 고정한다.

```bash
python3 scripts/loopctl.py context build <feature-dir>
python3 scripts/loopctl.py context validate <feature-dir>/context-pack.json
python3 scripts/loopctl.py environment capture <feature-dir>
```

Context Pack은 active knowledge, active decisions, repository knowledge refs, code/test scope, verifier profile,
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
operational behavior/knowledge semantics가 바뀌었으면 impact를 평가한다. 문서 본문 변경도
계약 변경일 수 있으므로 단순 typo라는 말로 면제하지 않는다. 실제 source 비교에서 의미
영향이 없는 코드 formatting/comment만 `NOT_APPLICABLE` 후보가 된다.

후보는 `.ai-team/policy/documentation.json`이 정한 다섯 축의 deterministic evidence로 먼저
좁힌다. 그다음에 의미 영향만 판단한다. 문서를 통째로 다시 쓰지 말고 impacted section만
최소 patch한다.

과거 결정은 덮어쓰지 않는다. 뒤집혔으면 옛 항목을 `superseded`로 두고 `superseded_by`로
새 결정을 가리킨다.

Documentation Freshness가 코드나 문서를 바꿨으면 필요한 verifier와 converge를 다시 실행한다.
review 결과가 코드 의미를 다시 바꾸면 Documentation Freshness도 다시 실행한다.

### Exact document review

일반 `work`가 영향 문서를 자동으로 찾는다. 사용자가 별도 문서 명령이나 대상 경로를
지정할 때까지 기다리지 않는다. staged/unstaged/new/rename/delete, 문서만의 계약 변경과
새 도구도 검사한다. 한도·읽기 실패·미분류 소유 문서는 미완료이며 영향 0건 PASS가 아니다.

소유 metadata는 JSON frontmatter 또는 legacy source의 `.amplai.json` sidecar 한 곳이다.
미분류 문서나 과거 기록을 자동으로 active 정본으로 승격하지 않는다. `context_dependencies`에
선언한 로컬 ontology/binding pin과 claim의 모든 evidence도 Context Pack에 묶인다.
새 semantic service나 임의 cross-repo 접근을 뜻하지 않는다.

문서를 최소 수정하거나, 그대로 유효한 이유를 실제 source/test evidence와 비교한다.
검토자는 문서별 이유·방법·근거를 작성한 뒤 현재 impact snapshot으로 제출한다.

```bash
python3 scripts/loopctl.py docs review <feature-dir> --document docs/guide.md \
  --outcome reviewed_unchanged --reason "변경이 기존 설명에 영향을 주지 않는 구체적 이유" \
  --reviewer "검토자 식별자" --method "source와 실행 근거 비교" \
  --evidence <feature-dir>/review-evidence.json --snapshot <dependency_snapshot_hash>
python3 scripts/loopctl.py docs review-batch <feature-dir> --input <feature-dir>/document-reviews.json
python3 scripts/loopctl.py docs validate <feature-dir>
```

batch는 `snapshot`, `reviews`, `reference_reviews` 배열을 받는다. 각 문서 항목은 단일 명령과
같은 필드를 갖는다. 경로만 인정하거나 공백·날짜만 바꿔 `update`로 통과시키지 않는다.
필수 참조·외부 의존·구조적 링크의 부재는 prose 분류로 면제할 수 없다.

`.ai-team/knowledge/document-reviews.json`이 검토 기록의 정본이다. `doc-impact.json`은
파생 결과이므로 직접 고치지 않는다. `REVIEW_BUSY`는 다른 저장이 끝난 뒤 재시도한다.
`SOURCE_DRIFT`는 impact부터 다시 검사한다. 저장 후 파생 결과 생성만 실패했다면 impact를
다시 실행한다. 미완료 `.pending-*`는 검토 근거로 사용하거나 자동 삭제하지 않는다.
동일 요청 재전송은 이력을 늘리지 않으며 이전의 서로 다른 검토는 보존한다.

### Offline audience views

영향 원문의 검토가 끝나면 같은 개발 흐름에서 HTML을 생성·검증한다. 사용자가 별도
문서 작업을 요청하기를 기다리지 않는다.

```bash
python3 scripts/loopctl.py docs sync-views <feature-dir>
python3 scripts/loopctl.py docs validate <feature-dir>
```

`.ai-team/policy/documentation.json`의 `offline_views.entries`가 명시적으로 선택한 view만
생성한다. 각 항목은 `release_manifest`, `output`, `audience`, `security`, `history`를 갖는다.
이 설정도 검토 입력이므로 원문 검토 전에 고정한다. 빈 목록은 `NOT_CONFIGURED`이며 HTML
검증 완료를 뜻하지 않는다. normal docs validate는 설정된 view의 원문·검토·출력 drift도
검사한다. sync는 생성 후 Work impact projection을 다시 계산한다.

release manifest는 UI/backend/contracts/ontology/Kit의 정확한 revision과 로컬 byte hash,
문서 source/snapshot/review hash, 그 구성 전체를 묶는 PASS 실행 근거를 갖는다. `verified`
문구 하나나 임의 Git HEAD는 근거가 아니다. 실제 실행된 verifier artifact로만 구성한다.
`docs resolve-release --index <registry.json> --release latest`는 registry의 명시적인 검증
release만 고른다. registry나 latest를 이 도구가 자동 승인·갱신하지 않는다.

출력은 `specs/` 또는 `.ai-team/local/document-views/` 아래의 새 전용 디렉터리를 쓴다.
부모 디렉터리는 미리 존재해야 한다. 같은 입력의 재실행은 동일 bytes를 확인하고, 다른
출력이나 기존 빈 디렉터리는 덮어쓰지 않는다. 변경된 release는 새 output 경로를 선택한다.
중단 후 불완전한 폴더는 배포하지 않고 보존한다. 여러 view의 전체 교체나 실제 공개는
이 명령의 기능이 아니다.

`audiences`와 선택적 `sections`는 검토된 소유 원문의 metadata다. sections는 독자별로
유일한 Markdown heading 제목을 선택한다. runbook의 배포·복구 절차를 정형 보고서로
대체하지 않는다. HTML은 원문을 escape하고 로컬 CSS·목차·표를 포함한다. 원시 HTML,
script, 외부 리소스를 실행하지 않으며 handoff/agent trace/raw evidence는 guide가 아니다.

PUBLIC/EXTERNAL_APPROVED 출력은 내부 checkout에서 직접 만들지 않는다. 기존 승인
절차로 확인한 `approved-input.json` 한 파일만 든 별도 입력 폴더와, 별도로 전달된 승인
digest를 `docs build-approved`에 제공한다. 자체 선언된 PUBLIC 값은 반출 승인이 아니다.
외부 builder는 내부 checkout을 읽지 않는다. history는 명시적으로 선택하며 현재 운영
지침이 아니라는 표지를 유지한다. 실제 게시·배포·권한 변경 승인은 별도다.

## 10. Incremental Gardening — review 뒤

review가 통과한 다음에 changed scope 주변만 정리한다. cleanup이 본 기능을 흔들면 안 된다.

```bash
python3 scripts/loopctl.py garden incremental <feature-dir>
```

`garden apply`는 호환용 report-only다. 어떤 safety label도 자동 삭제 권한이 아니다.
`.bak/.orig`, untracked, 파일 나이와 Git history만으로 사용자 백업을 제거하지 않는다.
불완전한 참조 scan은 PASS가 아니다. static reference가 없다는 사실만으로 dead code라고 판정하지 않는다 —
dynamic loading, plugin/entry point, conditional build, packaging, configuration-driven invocation을
함께 본다.

repository 전체 scan은 사용자가 명시적으로 요청한 cleanup Work에서만 한다.

```text
work_type = repository_gardening / risk = high / mode = full
```

그때도 기본은 report-only다.

```bash
python3 scripts/loopctl.py garden full <gardening-feature-dir> --report-only
```

Gardening이 실제 file/code/config를 바꿨으면 loop로 되돌아간다.

```text
garden change → verify → converge → documentation freshness → (필요하면) review
```

`--apply-safe`는 승인 없이 실행되지 않는다. 실제 retirement와 승인 생성은 별도 사람 작업이다.
이 baseline의 internal retirement/recovery는 trusted authority·verifier adapter를 요구하며
public destructive CLI가 없다. 본 구현의 적용 검증은 disposable Git fixture에 한정한다.

문서를 split/merge할 때는 `docs preserve --input <request.json>`으로 원본
repo·commit·path·section과 정확한 목적지·review evidence를 연결한다. 전체 원문 bytes를
유지한 section만 보존 완료다. 요약이나 Git history만 남기면 미처리 section으로 남는다.
`docs plan-retirement --input <request.json>`은 참조·보존·복구·정확한 승인 필요성을
보고한다. planning input/output은 검사 checkout 밖에 둔다. checkout 안에 두면 그 파일도
참조 snapshot에 포함되며 대상 이름 참조는 보수적으로 HOLD다. 출력은 삭제 승인이 아니다.

## 11. Knowledge change handling

Repository-local knowledge and approval rules remain authoritative. Record new meaning
as a candidate with source evidence. Do not copy another product's knowledge or silently
promote a candidate. Use only the repository's declared verifier and promotion route.

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
