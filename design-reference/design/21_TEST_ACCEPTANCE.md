# 21. 구현 검증·최종 인수 기준

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 설계 검수와 제품 검증은 다르다

이번 ZIP의 JSON parse/schema/fixture/link/task-DAG 검사는 **설계 패키지가 읽히고 서로 참조되는지**를 확인한다. 제품 unit test, actual provider execution, DB crash recovery, production integration을 실행한 것이 아니다. 모든 `eval/test-catalog.json` 사례는 `specified_not_executed` 상태다. 다음 구현자는 실제 commit·환경·결과·artifact refs를 붙인 후에만 상태를 변경한다.

## 2. 검증 피라미드

| 단계 | 대상 | 요구 증거 |
|---|---|---|
| Contract conformance | schema, typed DTO, semantic refs, JCS | valid/invalid/semantic-negative fixtures + mapping parity |
| Unit/property | transitions, policy intersection, DAG compiler, budgets | 모든 edge/guard와 negative generated cases |
| Fault integration | DB/outbox/inbox/CAS/lease/effect | 각 crash point 전후 state+event+receipt 비교 |
| Driver conformance | exact binary/API/OS/model/sandbox | session/async/steering/cancel/usage/errors의 실제 결과 |
| Product E2E | intent→goal→graph→worker→artifact→verdict | current contract coverage, authentic evidence chain |
| Domain/visual | cross-app compatibility, docs/frontend | 실제 최종 렌더링+기능+사용자 rubric 결과 |
| Meta lifecycle | experiment/shadow/canary/promote/rollback | freeze/holdout/grant/pointer/rollback receipts |
| Migration/operations | active run drain, imports, old client, restore | before/after hashes, no authority upgrade, proven restore |

## 3. V3 필수 대표 인수

(A) target hint 없는 rough intent가 실제 registry와 repo facts를 통해 검증 가능한 계약으로 만들어진다. (B) `/design`은 코드 구현을 dispatch하지 않는다. (C) 서로 다른 두 테스트 앱을 병렬 작업하고 합친 뒤 integration criterion으로 완료를 판정한다. (D) 실행 중 요구 변경에서 old worker result/effect가 current goal을 오염시키지 않는다. (E) timeout 외부 write가 UNKNOWN으로 가고 reconcile 없이 반복되지 않는다. (F) protected policy/평가 데이터는 builder/meta가 통과를 위해 수정할 수 없다. (G) 메타하네스의 pass뿐 아니라 fail/inconclusive/reject/abort/rollback 경로가 동작한다. (H) Kit 제거 후 domain build와 기존 Foundry governance regression이 통과한다.

## 4. 테스트 결과 레코드

필드: test_id, requirement_ids, implementation_commit, release_set_ref, environment_ref, started/finished, status(pass/fail/inconclusive/not_run), actual result artifact, expected assertion details, deviations, reviewer. 테스트 파일 존재·TODO check-box·모델의 완료 설명만으로 pass로 표시하지 않는다. negative case는 실제 denied effect·unchanged state까지 확인해야 한다.

fake driver는 state machine unit test에 유효하지만 실제 Claude/Codex provider conformance를 대체하지 않는다. simulator는 external effect semantics 시험에 유효하지만 사내 API contract integration 검증은 별도로 남는다. unavailable environment는 skip 사유와 support limitation에 기록한다.

## 5. Release blockers

unresolved authority/security gate, unclassified external effect, missing mandatory acceptance, changed evaluator without independent baseline, broken old governance guard, unowned file overwrite, no rollback proof, counterfeit evidence, current-contract hash mismatch는 blocker다. optional driver가 unqualified이면 해당 profile disabled로 출시 범위를 표시할 수 있지만 baseline driver/runtime/meta 핵심은 stub으로 닫지 않는다.

'모든 기능을 설계'한 목표는 full V3 implementation에서 추적한다. 특정 구현 항목을 빼려면 명시적 scope-change Decision이 필요하고 이름만 바꾸어 done 처리하지 않는다.
