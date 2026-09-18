# 07 · Adaptive Runtime와 deterministic scheduler

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 적응과 강제의 분리

Strategy 선택은 비용·난이도·불확실성에 대한 제안이다. 권한, 격리, evidence, 최대 budget, mandatory verify, 감사는 strategy와 무관하게 강제한다. Tiny change도 최소 contract와 verify receipt를 가진다. entry 문서나 권한 policy처럼 짧아도 위험한 변경을 tiny로 낮추지 않는다.

선택 기준은 path-based risk floor → contract risk → semantic uncertainty → available capabilities → budget envelope → qualification 결과 순이다. model routing은 평가 결과를 참고할 수 있지만 사내 데이터 분류보다 우선할 수 없다. 자동 위험 하향은 금지한다.

| 조건 | 기본 strategy | 생략 가능 | 생략 불가 |
|---|---|---|---|
| 작고 국소적이며 deterministic test 존재 | direct | 별도 planner/taskify | scope, authority, actual verify |
| 일반 기능/버그 | bounded_loop | 장황한 중복 계획 | env/context/evidence/freshness |
| 요구·설계 불확실성이 큼 | deliberative 또는 discovery | 불필요한 구현 착수 | unknown 해결/contract gate |
| cross-app | graph of bounded nodes | node 내부 고정 stage | integration/global verify |
| production/high-risk | 위 strategy + 강화 gate | 편의성 자동 publish | exact approval+containment |

## 2. Scheduler tick

한 tick은 pending inbox를 dedupe 처리하고, 만료 lease를 reconcile하고, cancellation/revocation/replan을 먼저 처리한다. 그 다음 ready graph nodes를 dependency 상태·resource·budget·authority·driver qualification 순으로 검사한다. claim은 transaction에서 `expected_revision`과 `lease_generation`을 증가시킨다. claim record와 run allocation과 outbox dispatch는 같은 RuntimeStore transaction이다.

Network/LLM 호출은 DB transaction 안에서 실행하지 않는다. dispatch outbox는 at-least-once다. Worker는 `dispatch_id/run_id`로 dedupe하여 같은 run에 두 개 process를 만들지 않는다. receipt를 잃었을 때 새 run을 무조건 만드는 것이 아니라 기존 session/run status를 조회한다.

## 3. Lease와 fencing

Lease는 우선 작업 소유권이지 security credential이 아니다. fencing generation은 work resource별 monotonic integer다. heartbeat는 current run/worker/token/generation을 모두 확인한다. expired lease의 token으로 checkpoint, completion, ToolBroker effect 실행을 허용하지 않는다. TTL 비교는 server UTC가 기준이고 worker clock을 신뢰하지 않는다.

기본 lease TTL 120s, heartbeat 30s, dispatch acknowledgement timeout 30s는 제안 default다. 실제 workload/네트워크에서 fault injection으로 결정한다. client가 요청한 임의 TTL이 policy 최대를 초과하면 거부한다. TTL 만료로 외부 effect가 자동 undo되지는 않는다.

## 4. 동시성·공정성

Tenant → project → Goal → app/resource의 hierarchical quota를 적용한다. 한 Goal의 막대한 fan-out가 다른 프로젝트를 starvation시키지 않도록 round-robin/aging을 둔다. 우선순위 tie-break는 enqueue sequence다. exclusive resource lock은 lease generation과 결합한다. 워커는 공유 working tree가 아니라 immutable base에서 생성한 독립 worktree/sandbox를 사용한다.

기본 concurrency 1 per repo write scope, read-only 최대 4는 측정 전 conservative default다. 같은 파일을 다른 worktree에서 수정해도 merge conflict와 semantic conflict가 있으므로 integration queue에서 다시 verify한다. Git worktree isolation과 OS security isolation은 다른 속성이다.

## 5. 예산 reservation

BudgetLedger는 root Goal에 cost, model_calls, tool_calls, wall_time, repair, active slots의 `limit/reserved/consumed/released`를 관리한다. Reserve 성공 후에만 dispatch한다. exact cost가 없으면 provider rate-card pin과 상한 추정으로 reserve하며 최종 usage를 받지 못한 금액은 `unknown`으로 보존한다. token 0으로 집계하지 않는다.

Wall clock은 active execution과 human wait를 별도 집계한다. deadline 자체는 기다림을 포함한다. user 답변을 기다리는 run은 compute slot과 write lock을 checkpoint 후 반환한다. 무응답이면 승인 상태로 바뀌지 않는다.

## 6. Repair와 실패 분류

implementation defect는 같은 node 새 attempt, task boundary 문제는 graph replan, requirement conflict는 Goal revision, environment 문제는 BLOCKED다. 동일 failure signature가 기본 3회면 같은 repair를 중단한다. 전체 attempt 제한과 같은 signature 제한을 별도로 적용한다. signature는 normalized error class+verifier ID+relevant stack/code path로 만들며 raw secret/log를 포함하지 않는다.

Backoff는 exponential+jitter+cap. retry 가능한 read-only/transient failures와 불명확한 external effect를 구분한다. 429/timeout 모두 같은 처리로 반복하면 중복 부작용을 만들 수 있다. UNKNOWN_EFFECT는 reconciliation 전 retry하지 않는다.

## 7. 취소와 종료

Cancel 수신 즉시 새 dispatch·새 grant·새 effect를 막는다. 실행 중 작업에는 cancel 요청을 보내고 graceful deadline 후 sandbox/process group을 종료한다. 외부 작업의 실제 종료 확인이 없으면 `cancel_requested`와 `effect_pending`을 표시하고 성공 종료처럼 보고하지 않는다. provider에게 interrupt를 보냈다는 사실이 runtime 종료 증거는 아니다.

## 8. 인수 조건

동시에 두 scheduler tick이 실행돼도 하나의 node에 current lease가 둘 생기면 안 된다. DB commit 직후 dispatch 실패·ack 전 crash·heartbeat 지연·clock skew·budget reserve 충돌·우선순위 starvation·cancel race·provider quota stop을 시험한다. 초기 release는 single active scheduler지만 단일 process의 실수로 병렬 tick이 발생해도 데이터 invariant가 보호되어야 한다.
