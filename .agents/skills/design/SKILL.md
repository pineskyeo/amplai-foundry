---
name: design
description: Cortex의 설계 전용 entry point. 구현하지 않고 repository evidence, Knowledge Readiness, ontology/software map을 이용해 요구·scope·acceptance·대안·architecture 결정을 확정한다.
argument-hint: "설계할 문제, 목표, 또는 specs/<feature> 경로"
user-invocable: true
disable-model-invocation: false
---

# /design — Evidence-grounded Shape and Architecture

`/design`은 source code를 구현하지 않는다. `/work`가 제품 결정을 다시 열지 않고 실행할 수
있도록 Design Ready 상태를 만드는 것이 목적이다.

## 1. Knowledge before design

먼저 다음을 조사한다.

- `AGENTS.md`, runtime/policy/permission
- current code/interface/test/build path
- active decisions와 Context Pack
- relevant ontology neighborhood, binding, evidence
- 관련 spec/docs/SOP/git history

도메인 지식이 부족하면 그럴듯한 설계를 상상하지 않는다. `knowledge-readiness.json`을 만들고
`DISCOVER`이면 repository Domain Discovery부터 실행한다.

```bash
python3 scripts/loopctl.py readiness init <feature-dir>
python3 scripts/loopctl.py discovery scan <feature-dir>
```

fact, inference, candidate, unknown, contradiction을 분리한다.

## 2. Embedded grill mode

별도 grill Skill은 없다. 다음 decision point에서만 질문한다.

- 답에 따라 architecture/scope/acceptance가 실제로 달라짐
- 두 요구 또는 active evidence가 충돌함
- repository에서 알 수 없는 business ownership
- compatibility/rollback/permission 기준을 사람이 정해야 함

각 질문에는 선택지, 실제 영향, 추천과 trade-off를 붙인다. 어린이 비유로 단순화하지 않는다.

## 3. Required artifacts

```text
spec.md                 WHAT / WHY / constraints / acceptance
plan.md                 HOW / boundary / failure / migration / validation
work-contract.json      execution/evidence/governance contract
knowledge-readiness.json
context-pack.json       readiness가 READY일 때 생성
environment.json        verifier environment baseline
```

Canonical product docs/ontology를 `.ai-team`에 복사하지 않는다. `.ai-team`은 index/policy만
소유한다.

## 4. Design coverage

설계 규모에 맞게 다음을 검토한다.

- Goal/non-goal, terminology와 source of truth
- current behavior와 system/ownership boundary
- public/internal contract와 compatibility
- domain invariant와 SHACL/structural guard 가능성
- state/data lifecycle, concurrency, retry/idempotency
- failure path와 observability/evidence trace
- security/privacy/permission/human gate
- environment/fixture/target reproducibility
- migration/rollback/production impact
- independently verifiable Slice
- semantic change candidate와 canonical promotion 필요 여부

## 5. Documentation and decision impact

`/design`은 code를 구현하지 않으므로 Repository Gardening을 실행하지 않는다. Documentation
Freshness만 적용한다.

```text
/design → architecture / decision → documentation / decision impact
        → update or supersede → DESIGN READY
```

architecture decision을 내렸으면 그것이 어느 문서에 반영돼야 하는지 먼저 기계적으로 찾는다.

```bash
python3 scripts/loopctl.py docs impact <feature-dir>
python3 scripts/loopctl.py docs validate --repo
```

과거 결정을 뒤집었으면 옛 항목을 지우지 않는다. `docs/decisions/cortex-decisions.md`는 D-NN
append-only이고 뒤집힌 항목에 `(superseded by D-MM)`을 단다.
`.ai-team/knowledge/decisions.index.json`에는 `status: superseded`와 `superseded_by`를 남긴다.

중요한 architecture decision이 문서화되지 않은 상태로 `DESIGN READY`를 선언하지 않는다.

## 6. Decision and stop condition

- 모든 blocking unknown 해결 + verifier/acceptance 정의 가능 → `READY`
- architecture/public/ontology/production gate 미승인 → `awaiting_approval`
- 근거 부족 또는 conflict 미해결 → `blocked`

```text
DESIGN READY
- feature directory / work_id
- Knowledge Readiness verdict
- Context Pack hash
- 핵심 결정과 evidence
- risk / verifier / environment
- documentation/decision impact와 처리 결과
- 남은 human gate
- /work가 처음 실행할 Slice
```

코드를 수정하지 않고 멈춘다. 이후 사용자는 `/work <feature-dir>`만 호출하면 된다.
