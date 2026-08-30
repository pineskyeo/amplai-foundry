---
name: design
description: AMPLAI의 설계 전용 entry point. 구현하지 않고 repository evidence, Knowledge Readiness, project knowledge/software map을 이용해 요구·scope·acceptance·대안·architecture 결정을 확정한다.
argument-hint: "설계할 문제, 목표, 또는 specs/<feature> 경로"
user-invocable: true
disable-model-invocation: false
---

# design — Evidence-grounded Shape and Architecture

Claude Code는 `/design`, Codex는 `$design`으로 호출한다. 이 문서의 `design`/`work` 표기는
host-neutral entry point를 뜻한다. `design`은 source code를 구현하지 않고, `work`가 제품 결정을
다시 열지 않고 실행할 수 있도록 Design Ready 상태를 만든다.

## 1. Knowledge before design

먼저 다음을 조사한다.

- `AGENTS.md`, runtime/policy/permission
- current code/interface/test/build path
- active decisions와 Context Pack
- relevant active Vault notes, claims, decisions와 evidence
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

Canonical product docs/knowledge를 `.ai-team`에 복사하지 않는다. `.ai-team`은 index/policy만
소유한다.

## 4. Design coverage

설계 규모에 맞게 다음을 검토한다.

- Goal/non-goal, terminology와 source of truth
- current behavior와 system/ownership boundary
- public/internal contract와 compatibility
- domain invariant와 schema/lint/structural guard 가능성
- state/data lifecycle, concurrency, retry/idempotency
- failure path와 observability/evidence trace
- security/privacy/permission/human gate
- environment/fixture/target reproducibility
- migration/rollback/production impact
- independently verifiable Slice
- knowledge change candidate와 governed Proposal 필요 여부

## 5. Documentation and decision impact

`design`은 code를 구현하지 않으므로 Repository Gardening을 실행하지 않는다. Documentation
Freshness만 적용한다.

```text
design → architecture / decision → documentation / decision impact
        → update or supersede → DESIGN READY
```

architecture decision을 내렸으면 그것이 어느 문서에 반영돼야 하는지 먼저 기계적으로 찾는다.

```bash
python3 scripts/loopctl.py docs impact <feature-dir>
python3 scripts/loopctl.py docs validate --repo
```

과거 결정을 뒤집었으면 옛 항목을 지우지 않는다. 관련
`docs/workstreams/*/DECISIONS.md`에 새 Decision을 append하고 이전 항목을 `superseded`로
연결한다.
`.ai-team/knowledge/decisions.index.json`에는 `status: superseded`와 `superseded_by`를 남긴다.

중요한 architecture decision이 문서화되지 않은 상태로 `DESIGN READY`를 선언하지 않는다.

## 6. Decision and stop condition

- 모든 blocking unknown 해결 + verifier/acceptance 정의 가능 → `READY`
- architecture/public contract/knowledge/production gate 미승인 → `awaiting_approval`
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
- work가 처음 실행할 Slice
```

코드를 수정하지 않고 멈춘다. 이후 사용자는 Claude Code의 `/work <feature-dir>` 또는 Codex의 `$work <feature-dir>`를 호출한다.

<!-- AMPLAI-ASYNC-BEGIN -->
## Decision-ready and Cross-App Design

중앙 Project Store가 연결된 경우 `design`은 cross-app 상태를 설계할 수 있지만 실행하지 않는다.

- 변경이 여러 앱의 계약/동작에 영향을 주는 것이 evidence로 확인되면 CR 하나를 만든다.
- 앱별 Goal, Acceptance, Contract reference, dependency를 Work로 만들되 상태는 `DRAFT`로 둔다.
- Open Question은 `decision_class`, `reversibility`, `blast_radius`, evidence plan,
  policy minimum authority와 함께 기록한다.
- repository에서 조사 가능한 engineering choice는 AUTO/CHALLENGE 후보로 좁힌다.
- domain/product/production/safety/security/privacy/legal/destructive/public contract owner 결정만
  HUMAN으로 남긴다.
- Handoff 문서를 새로운 SSOT로 만들지 않는다. target-app Context는 CR/Work/Contract/Decision/
  Evidence에서 렌더링한다.

`DESIGN READY`에는 CR/Work ID, DRAFT dependency, blocking Question, authority, Contract와 첫 실행
Work를 포함한다. `work` 또는 사람이 명시적으로 activate하기 전에는 Supervisor가 실행하지 않는다.
<!-- AMPLAI-ASYNC-END -->
