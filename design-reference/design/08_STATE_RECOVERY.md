# 08 · 상태 모델, 중단과 복구

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. Immutable definition과 mutable state

Contract/Graph/Pack/ReleaseSet 정의는 content-addressed immutable object다. lifecycle은 revisioned aggregate record다. WorkDefinition을 바꿔 과거 Run이 무엇을 했는지 다시 해석하지 않는다. 한 Work는 여러 Run을 가질 수 있고, Run은 한 definition revision에만 속한다. 한 Run은 한 번의 scheduled Work attempt다. 내부 모델/tool step은 Run에 속하지만 verifier-triggered repair는 이전 Run을 종료하고 새 Run을 만든다. transport retry는 새 semantic effect를 만들지 않는다. 실패 run을 성공 상태로 다시 살리지 않는다.

Normative transition table은 `contracts/state-machines.json`이다. 모든 command는 expected_revision을 요구하며, 모든 상태 변화는 동일 transaction의 event와 함께 기록된다. event는 관측/감사 및 projections를 위한 것이며, 최초 V3는 전체 DB를 event replay만으로 재구성하는 event-sourcing framework를 요구하지 않는다. audit event와 backup 둘 다 필요하다.

## 2. 상태별 의미

Goal: `draft → discovering/awaiting_decision → ready → active → verifying → verified`. blocked는 해결될 수 있는 제약, failed는 해당 실행 경로 실패, cancelled는 요청 취소다. Contract가 바뀌면 activate_new_revision 명령으로 execution epoch를 새로 만들고 관련 완료 표시는 재검증 대상으로 바뀐다. 기존 terminal revision과 verdict는 그대로 보존한다.

Work: `pending → ready → leased → running → verifying → succeeded`. `blocked`, `awaiting_human`, `failed`, `cancelled`, `superseded` 분기가 있다. pending은 dependency를 기다림, blocked는 해결해야 할 사유가 있음이다. 둘을 같은 상태로 숨기지 않는다.

Run: `created → running → pausing/paused → verifying → succeeded|failed|cancelled|lost|unknown_effect`. provider session은 별개라 expired session을 재생성해 같은 Work를 retry할 수 있다. `unknown_effect`는 외부 상태 확인을 요구하며 성공도 실패도 아니다.

## 3. Crash point별 복구

| Crash 시점 | durable 사실 | 복구 |
|---|---|---|
| admission 전 | Intent만 있음 | compile/admission 재시작, effect 없음 |
| claim commit 후 dispatch 전 | lease+run+outbox | 같은 dispatch_id 재전송 |
| worker spawn 후 ack 전 | worker journal 또는 provider session id | exact run 조회, 중복 spawn 방지 |
| tool effect 예약 후 실행 전 | effect PREPARED | policy 재검사 후 동일 effect_id 실행 |
| remote 실행 후 receipt 저장 전 | outcome 불확정 | UNKNOWN_EFFECT, remote receipt/status 조회 |
| evidence blob 저장 후 index 전 | orphan blob | hash 검증 후 attach 또는 retention GC |
| verdict 저장 후 event 전 | 같은 transaction이면 둘 다/둘 다 아님 | transaction replay, 개별 수작업 상태 수정 금지 |
| 승인 후 graph revision 변경 | 구 revision 승인 | digest mismatch로 실행 차단 |
| release active pointer 교체 후 crash | journal+pointer | registry reconcile, 같은 revision으로 원자적 완료 확인 |

## 4. Checkpoint

checkpoint는 contract/graph/node/run refs, last accepted event sequence, worktree base+diff artifact hash, context bundle, pending tool calls, steering inbox cursor, budget snapshot, driver continuation info, sandbox recipe를 가진다. provider opaque reasoning state는 export되지 않아도 된다. AMPLAI는 비공개 chain-of-thought 수집을 요구하지 않는다.

복구 시 governing instruction/active Decision/authorization changes를 먼저 확인한다. 과거 context summary만 믿고 실행하지 않는다. provider가 session resume를 지원하지 않으면 현재 contract+checkpoint+evidence digest로 새 session을 만들되 new provider session binding을 append한다. “이어 실행”과 “동일 provider session 재개”를 구분해서 기록한다.

## 5. 외부 효과의 상태

저장 enum은 lowercase다. Effect states는 `prepared → dispatched → applied|not_applied|unknown`이고 `unknown → reconciled`에는 resolved_outcome과 근거가 필요하다. 문서에서 대문자 표기는 같은 상태의 설명용 강조다. 자체 DB state 전이는 atomic하지만 HTTP/SSH/Git/파일 서버의 effect와 DB commit은 atomic하지 않다. 외부가 idempotency key를 지원하면 같은 key로 조회/재시도한다. 지원하지 않으면 식별 가능한 target revision/receipt를 먼저 읽고 reconcile한다. 검증 불가하면 human reconciliation이다. exactly-once 실행을 주장하지 않는다.

## 6. 부정확한 성공을 금지하는 규칙

프로세스 exit 0, 모델의 완료 문구, HTTP 202/204, projection의 DONE, 이전 revision의 tests PASS는 각각 완료 증거가 아니다. success를 확정하려면 현재 definition의 mandatory acceptance coverage, 환경과 evidence hash, effective authority, docs freshness, required independent review가 모두 맞아야 한다.

## 7. 인수 조건

모든 transition에 happy-path뿐 아니라 wrong state, stale revision, unauthorized actor, replayed request, out-of-order event를 넣는다. paused run이 새 instruction을 수신했는데 context에 반영하지 않고 resume되는 경우를 잡는다. crash 복구 이후 transcript를 전부 재실행해서 tool effect가 중복 발생하는 구현은 실패다.
