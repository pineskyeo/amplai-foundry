# 32. Tool Broker·외부 Effect 프로토콜

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 모든 도구가 같은 위험은 아니다

`pure_read`, `sandbox_write`, `external_idempotent_write`, `external_nonidempotent_write`, `production_control`로 분류한다. pure_read도 secret/PII 접근·네트워크 유출 위험이 있으므로 권한 검증은 남는다. sandbox_write는 workspace scope와 quota를 enforce한다. external write는 EffectService를 거쳐 receipt를 남긴다. production_control은 기본 금지이며 도메인 시스템 자체의 검증·승인 경계를 대체하지 않는다.

기존 Cortex/Synapse production business behavior는 AMPLAI control logic로 옮기지 않는다. AMPLAI는 개발·검증·승인된 배포 업무를 돕는 플랫폼이며 tester 제어 경로에 새 자율판단을 삽입하지 않는다.

## 2. ToolRegistry

각 tool definition은 ID/version/input schema/output schema/effect class/required capabilities/allowed endpoints/timeout/max result bytes/idempotency support/reconcile method/compensation policy/redaction/owner를 가진다. command alias나 shell string이 schema·permissions를 우회하지 못한다. shell은 별도 high-granularity sandbox capability이며 production network secrets를 주지 않는다.

MCP/ACP/provider native tool은 transport mapping이다. AMPLAI policy가 실제 해당 tool에 강제되는지 qualification한다. MCP Tasks는 remote async 상태 조회 보조이며 experimental 지원 범위는 별도 capability로 표시한다. [R19,R20,R21]

## 3. Prepare → dispatch → reconcile

1. EffectRequest scope/run/lease/fence/contract revision/tool args digest를 검증한다.
2. authoritative grant와 현재 revocation generation을 확인한다.
3. unique effect_key에 request digest를 bind하고 PREPARED receipt + outbox를 commit한다.
4. dispatcher는 재전송 가능성을 확인하고 exact idempotency key로 call한다.
5. response 관측을 APPLIED/NOT_APPLIED/UNKNOWN으로 분류하고 output evidence를 저장한다.
6. timeout/network disconnect는 NOT_APPLIED가 아니다. query/reconcile을 수행한다.
7. UNKNOWN에서 재호출은 별도 권한과 외부 시스템 의미 검증을 요구한다.

provider의 HTTP 500도 업무 action이 실행된 후 응답만 실패했을 수 있다. `retryable`은 네트워크 에러 문자열이 아니라 tool contract에 따라 결정한다. tool이 idempotency와 조회를 모두 제공하지 않으면 고위험 자동 retry를 금지하고 human reconciliation으로 보낸다.

## 4. Fencing 한계

CP monotonic fencing은 stale worker의 새 broker request를 막는다. worker가 이전에 direct credential로 외부 endpoint에 보낸 요청까지 되돌리지는 못한다. 따라서 credentials/egress를 broker-bound로 만들고 가능한 endpoint도 generation/fence를 검증한다. endpoint가 지원하지 않는 경우 dispatch 시간창의 비원자성·unknown status를 운영상 드러낸다.

## 5. 승인과 결과

grant max-use 소비와 effect PREPARED 기록을 같은 authority service command 또는 transactional equivalent로 처리해야 한다. 분산된 stores이면 issue/consume receipt + idempotent inbox를 사용하고 부분 실패를 reconcile한다. 단순 양쪽 update를 이어 쓰고 atomic이라 부르지 않는다.

compensation은 새로운 승인된 effect다. 원래 실패를 삭제하지 않는다. 예를 들어 잘못 생성된 external ticket을 close하는 것은 ticket이 없었던 상태와 같지 않다. evidence에는 original effect와 compensating effect를 연결한다.

## 6. 반드시 시험할 race

dispatch 직전 revoke, dispatch 직후 timeout, response 저장 전 crash, 같은 effect key 다른 args, stale worker heartbeat 재등장, child tool orphan, external API가 동일 key를 다른 semantics로 처리, callback 타 project 위장, reconcile 결과 뒤늦게 도착, partial batch 일부만 applied. batch는 item receipts와 aggregate partial state를 가지며 all-or-nothing 지원 없는 endpoint에 원자적 batch를 약속하지 않는다.
