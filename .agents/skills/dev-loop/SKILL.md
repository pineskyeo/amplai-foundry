---
name: dev-loop
description: Internal V1 execution engine reused by AMPLAI V2. It consumes a READY Context Pack and contract, implements one slice, verifies with environment evidence, repairs bounded failures, converges, and obtains independent review.
user-invocable: false
disable-model-invocation: false
---

# dev-loop — Reliable Execution Engine under V2 Governance

`dev-loop`는 V1의 검증된 실행 엔진이다. V2는 이것을 재구현하지 않고 앞에 Knowledge/Context
Gate를, 뒤에 Evidence/Semantic candidate lifecycle을 붙인다.

## Inputs / hard preconditions

- feature directory와 V2 `work-contract.json`
- `knowledge-readiness.json` verdict READY 또는 explicit low-risk BYPASS
- valid `context-pack.json`
- `environment.json`
- generated `tasks.md` + task manifests
- spec/plan

```bash
python3 scripts/loopctl.py contract validate <feature>/work-contract.json
python3 scripts/loopctl.py readiness evaluate <feature>/knowledge-readiness.json
python3 scripts/loopctl.py context validate <feature>/context-pack.json
scripts/eval.sh --feature <feature> --list-slices
```

DISCOVER/BLOCKED/awaiting_approval이면 구현하지 않는다.

## Inner loop

1. dependency가 충족된 가장 앞의 open Slice를 선택한다.
2. Context Pack의 code/test scope와 task allowed path 안에서만 구현한다.
3. Bug fix는 regression RED → fix → GREEN evidence를 유지한다.
4. deterministic evaluation:

```bash
scripts/eval.sh --feature <feature> --slice <ID> --profile fast
```

5. exit code:
   - `0`: PASS, evidence trace 후 다음 Slice
   - `1`: 직접 관련 failure evidence만 최소 repair
   - `2`: environment/contract unavailable — 코드 수정 중지, BLOCKED
   - `3`: 반복 실패 — systematic-debugging과 escalation

review는 inner loop마다 부르지 않는다.

## Evidence trace

Work의 주요 상태를 append-only hash chain으로 기록한다.

```text
context_selected → slice_started → files_changed → verifier_pass/fail
→ repair_attempt → escalation → converge → doc_freshness → review
→ gardening → done
```

실행하지 않은 검증은 기록하지 않고, secret/raw production data는 trace에 넣지 않는다.

## Escalation

- IMPLEMENTATION: root cause가 확인된 최소 repair
- TASK: taskify로 acceptance/eval/scope 재구성
- PLAN: plan 수정과 영향 task 재생성
- SPEC: design/clarify
- REQUIREMENT: human decision
- ENVIRONMENT: 필요한 tool/fixture/target과 재개 명령을 handoff에 기록
- KNOWLEDGE: discovery/context를 다시 구성하고 coding을 멈춤

## Feature convergence and review

모든 Slice가 PASS하면 contract profile로 최종 검증한다.

```bash
scripts/eval.sh --feature <feature> --full
```

그 다음 `speckit-converge`를 수행한다. converge가 clean이면 **review 앞에서** documentation
freshness를 맞춘다.

```bash
python3 scripts/loopctl.py docs impact <feature>
python3 scripts/loopctl.py docs validate <feature>
```

reviewer가 보는 시점에 코드와 문서가 어긋나 있으면 안 된다. 문서를 고쳤으면 필요한 verifier와
converge를 다시 돌린다.

일반 Slice의 코드·문서·새 도구 변경에서 영향 문서를 자동 발견한다. 경로 목록의 일부나
40개 제한으로 완료하지 않는다. 문서를 그대로 두는 경우에도 `docs review` 또는
`review-batch`에 문서별 검토자·이유·방법·실행/source evidence와 최신 snapshot을 제출한다.
공백·날짜·path-only acknowledge는 semantic review를 대신하지 못한다. 필수 참조가 없으면
차단하며, Context의 ontology/binding pin·모든 claim evidence가 바뀌면 Context부터 재구성한다.

문서·dependency·candidate/index·정책·profile·검토 근거가 바뀌면 이전 검토를 재사용하지
않는다. batch 실패는 부분 승인하지 않는다. `REVIEW_BUSY`는 재시도하고, `SOURCE_DRIFT`는
impact부터 재평가한다. 강제 종료 후 미완료 stage는 정본이 아니다. 파생 impact 생성 실패는
정본 검토 기록을 유지한 채 `docs impact`로 재생성한다.

검토한 원문과 정확한 verified release manifest에 대해 같은 흐름에서
`python3 scripts/loopctl.py docs sync-views <feature>`를 실행하고 docs validate를 다시
실행한다. `offline_views.entries`가 비어 있으면 `NOT_CONFIGURED`이며 HTML 검증 증거가
아니다. 설정된 view는 원문·review·version pin·검증 근거·모든 출력 bytes가 맞아야 한다.
이 설정은 원문 검토 전에 고정한다. generated HTML만 수정하거나 기존 output을 덮어써
통과시키지 않는다. 중단된 부분 출력과 이전 검증 버전은 보존한다. PUBLIC 외부 view는
별도 승인된 유한 입력과 승인 digest만 받으며 내부 checkout을 builder에 주지 않는다.
출력 생성은 배포·latest 갱신·반출 승인이 아니다.

그 다음 독립 `code-review`를 수행한다. Review는 Context Pack, contract, actual diff, verifier
environment/evidence, active/superseded resolution, documentation impact를 함께 본다.

`CHANGES_REQUIRED`이면 finding을 task로 바꿔 inner loop로 복귀한다. 코드 의미가 다시 바뀌면
documentation freshness도 다시 실행한다.

review가 통과하면 changed scope 주변만 incremental gardening으로 정리한다.

```bash
python3 scripts/loopctl.py garden incremental <feature>
```

`garden apply`도 report-only다. 사용자 백업·tracked 문서·ADR·incident는 자동 삭제하지 않는다.
변경 작업의 scope만 검사하며 누락·한도·Git 오류는 PASS로 바꾸지 않는다. full scan은
별도 high-risk repository_gardening Work를 지정한 `garden full <feature> --report-only`다.
문서 split/merge는 `docs preserve --input <request.json>`의 section별 원문 보존 검사를
통과한다. `docs plan-retirement`는 읽기 전용 제안이다. 실제 승인 생성·삭제는 별도 범위다.
gardening이 repository를 바꿨으면 verify → converge → doc freshness로 되돌아간다.

## Semantic candidate

Diff에서 새 domain meaning을 발견해도 active ontology를 직접 변경하지 않는다. candidate와
source evidence를 남기고 semantic verifier를 통과시킨 뒤 별도 promotion gate로 보낸다.

## DONE / handoff

- all Slice PASS
- final verifier PASS and environment attached
- converge clean
- documentation impact evaluated and no STALE left
- review PASS
- incremental gardening ran; candidates carry evidence and safety
- no HUMAN_GATED candidate deleted automatically
- trace chain valid
- human gates satisfied
- handoff reproducible

V3의 parallel worker/fan-out/scheduler는 이 engine에 넣지 않는다.
