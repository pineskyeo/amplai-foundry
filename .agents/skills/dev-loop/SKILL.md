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

그 다음 독립 `code-review`를 수행한다. Review는 Context Pack, contract, actual diff, verifier
environment/evidence, active/superseded resolution, documentation impact를 함께 본다.

`CHANGES_REQUIRED`이면 finding을 task로 바꿔 inner loop로 복귀한다. 코드 의미가 다시 바뀌면
documentation freshness도 다시 실행한다.

review가 통과하면 changed scope 주변만 incremental gardening으로 정리한다.

```bash
python3 scripts/loopctl.py garden incremental <feature>
python3 scripts/loopctl.py garden apply <feature>
```

`garden apply`는 SAFE_AUTO만 지운다. EVIDENCE_REQUIRED와 HUMAN_GATED는 candidate로 남긴다.
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
