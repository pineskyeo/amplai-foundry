# Phase 0 Research — MGC-012 Package 3

**Feature**: `001-mgc-012-slack-reference-adapter` | **Date**: 2026-08-03

이 문서는 Package 3 구현 전에 **Slack 공식 문서와 기존 코드에서 고정한 사실**만 담는다.
근거는 D-018 항목 2 ("Slack API의 receipt 필드와 error code는 구현 전에 공식 문서로
고정한다")와 D-019 항목 7이다.

`spec.md` 와 달리 이 문서는 외부 SPEC 의 파생이 아니다. Slack 공식 문서가 원본이다.

## Sources

| # | URL | 조회일 | 비고 |
|---|---|---|---|
| S1 | `https://docs.slack.dev/reference/methods/chat.postMessage` | 2026-08-03 | `api.slack.com/methods/...` 는 302로 여기 redirect 된다 |
| S2 | `https://docs.slack.dev/reference/methods/conversations.history` | 2026-08-03 | 동일 redirect |
| S3 | `https://docs.slack.dev/messaging/message-metadata` | 2026-08-03 | |
| S4 | `https://docs.slack.dev/apis/web-api/rate-limits` | 2026-08-03, 2026-08-05 재조회 | `Retry-After` 상한은 문서화되지 않는다 (R-007) |

코드 근거는 `src/amplai_foundry/governance/events.py` 와 `projections.py` 다. 인용한 line
번호는 조회 시점 `f595643` 기준이다.

---

## R-001 — Existing Delivery Loop Already Defines The Seam

**Decision**: `SlackProjectionDestination` 은 `ProjectionDestination` Protocol
(`events.py:236`)만 구현한다. `OutboxDispatcher` 를 고치지 않는다.

**Rationale**: `OutboxDispatcher.deliver_next` (`events.py:2833`)가 이미 전 흐름을 갖고 있다.

```text
claim_next → destination.reconcile(event)
           → receipt is None 이면 destination.send(event)
           → mark_delivered(remote_receipt=receipt)
```

- `reconcile` 이 `OutboxReconcileError` 를 던지면 `fail(..., unreconcilable=True)` 로 간다
  (`events.py:2856`–`2863`).
- `unreconcilable=True` 는 attempts 와 무관하게 `_dead_letter` 를 부른다 (`events.py:2779`–`2780`).
- `_dead_letter` (`events.py:2903`)는 `governance_outbox_dead_letters` row 와
  `governance_operator_holds` row(`scope_kind='outbox_destination'`)를 함께 만든다.
- `claim_next` 의 SQL 은 `d.operator_hold = 0` 조건을 갖는다 (`events.py:2634`). hold 가
  걸리면 그 destination 의 이후 event 가 자동으로 멈춘다.

즉 A11 의 "retry, supersession, DLQ와 operator hold 계약을 우회하지 않는다"는 **Protocol 만
구현하면 구조적으로 만족된다.** 새 retry 경로를 만들면 오히려 위반이다.

**Alternatives considered**: Slack 전용 dispatcher 를 따로 두는 안. lease·fencing·backoff·
DLQ 를 두 벌 유지하게 되어 기각한다.

## R-002 — `reconcile()` Is Called On Every Attempt, Not Only After Crash

**Decision**: reconcile 은 매 전송 시도마다 Slack read 를 한 번 한다. 이 비용을 설계에
반영한다.

**Rationale**: `deliver_next` 는 조건 없이 `destination.reconcile(event)` 를 먼저 부른다
(`events.py:2842`). `YamlProjectionDestination.reconcile` 은 local file read 라 비용이
없지만 Slack 은 network read 다.

`conversations.history` 는 Web API Tier 2 (분당 20+ 요청)다 (S4). `chat.postMessage` 는
special tier 로 **channel 당 초당 1 message** 다 (S4). read 가 write 보다 먼저 조이는
구조이므로 reconcile 의 조회 범위를 제한해야 한다 (R-004).

**Alternatives considered**: `deliver_next` 를 고쳐 crash 이후에만 reconcile 하게 하는 안.
"send 성공 여부를 모른다"는 상태와 "정상 첫 시도"를 dispatcher 가 구분할 수 없다 — 그 구분이
바로 reconcile 이 하는 일이다. 기각한다.

## R-003 — Marker Is Slack Message Metadata

**Decision**: 전송 메시지에 `metadata` 를 붙인다. `event_type` 은 고정 문자열,
`event_payload` 는 `event_id`·`destination_ref`·`destination_sequence`·`payload_digest` 를
담는다. reconcile 은 `conversations.history` 를 `include_all_metadata=true` 로 조회해
`event_payload` 를 읽는다.

**Rationale**: S3 이 문서화한 경로다.

- 붙이기: `chat.postMessage` 의 `metadata` 파라미터 — `event_type`(string)과
  `event_payload`(object) 두 키 (S3).
- 읽기: `conversations.history` 에 `include_all_metadata=true` 를 주면 `event_payload` 까지
  온다. 주지 않으면 `event_type` 만 온다 (S3).
- 각 message 는 `app_id` 를 갖고 있어 "어느 app 이 metadata 를 보냈는지" 구분할 수 있다 (S3).

이것이 D-018 항목 3 의 "remote 가 유일한 진실 원천" 을 성립시킨다. local 에 ts 를 저장해
두고 그것으로 판정하면 저장 실패 시 다시 two-phase 문제가 생긴다.

**Alternatives considered**:

- **message text 에 marker 문자열을 넣는 안.** 사람 눈에 보이고 사람이 지우거나 편집할 수
  있다. metadata 는 사람이 못 건드린다.
- **`block_id` 에 넣는 안.** `conversations.history` 응답의 block 구조 보존 여부를 공식
  문서에서 확인하지 못했다. **모른다.** metadata 는 명시적으로 문서화돼 있다.

**미확인**: metadata 의 크기 상한은 S3 에서 못 찾았다. 다만 `chat.postMessage` error code
목록에 `metadata_too_large` 와 `invalid_metadata_format`·`invalid_metadata_schema`·
`metadata_must_be_sent_from_app` 이 있다 (S1). 상한 숫자를 모르므로 `event_payload` 는 위
네 필드만 담고 payload 본문을 넣지 않는다. 실측은 Package 4 E2E 몫이다.

## R-004 — Reconcile Search Is Bounded And Fail-Closed

**Decision**: reconcile 은 `conversations.history` 를 최신부터 역순으로 훑되 조회 상한
(page 수)을 갖는다. 판정 규칙은 넷이다 (원래 셋이었다 — 아래 갱신 참조).

| 발견한 것 | 판정 |
|---|---|
| 이 event 의 marker | 전달 완료 — 그 receipt 를 반환한다 |
| 같은 `destination_ref` 의 **더 낮은** `destination_sequence` marker 를 먼저 만남 | 미전송 — `None` 을 반환해 send 로 간다 |
| `next_cursor` 가 없어 history 가 소진됐고 둘 다 못 만남 | 미전송 — `None` |
| 상한까지 훑고 멈췄는데 둘 다 못 만남 | **판정 불가 — `OutboxReconcileError` 를 던진다** |

**갱신 (D-023, 2026-08-05)**: 이 표는 원래 셋이었다. 세 번째 줄(history 소진)을 나중에
더했고, 조회 자체를 건너뛰는 첫 시도 규칙이 앞에 붙었다. 셋만으로는 `destination_sequence`
가 1 인 event 가 두 번째 규칙을 만족할 수 없어 **모든 destination 의 첫 Card 가 hold** 로
떨어졌다. 확정 계약은 contracts C-2.2 다.

**Rationale**: 세 번째가 핵심이다. "못 찾았으니 안 보낸 것" 으로 단정하면 조회 범위 밖에
있던 메시지를 중복 발행한다. SPEC.md `Outbox Ordering` 은 "Reconcile 불가 시 DLQ와
operator hold를 생성한다" 고 못박았고, `deliver_next` 는 `OutboxReconcileError` 를 그대로
그 경로로 보낸다 (R-001). 그러므로 unknown 은 fail-closed 다.

두 번째 규칙이 "안 보냈다"의 **증거**다. destination 은 순차 전달이라 sequence N 을 보내기
전에 N-1 이 이미 확정돼 있다 (`claim_next` 의 `NOT EXISTS prior ... state NOT IN
('delivered','superseded')`, `events.py:2648`). 역순 조회에서 N-1 marker 를 N marker 보다
먼저 만났다면 N 은 아직 없다.

**Alternatives considered**: 못 찾으면 미전송으로 보는 안. 중복 Card 를 만든다. User Story 2
가 막으려는 바로 그 결과다. 기각한다. (D-023 이 이 중 **history 소진** 경우만 떼어 인정했다.
범위를 다 못 본 경우와 구분되기 때문이다.)

**미확인 (wave 3 review)**: `conversations.history` 가 **최신 message 부터** 돌려준다는 것을
S2 에서 확인하지 못했다. 위 "역순으로 훑되" 는 그 가정 위에 서 있다. 순서가 뒤집히면 하위
sequence marker 를 우리 marker 보다 먼저 만나 중복 Card 가 난다. destination 은 page 안에서만
그 의존을 없앴고 page 사이는 못 막는다. contracts C-1 이 이것을 구현체 의무로 적었고 Package 4
가 확정한다.

**미확인**: 사람이 Card 를 지웠다가 다시 만든 경우 (SPEC.md 검증 항목 "deleted/recreated
Provider message"). 지워진 message 는 `conversations.history` 에 없으므로 위 표의 **네 번째**
줄(상한 도달)로만 hold 가 걸린다. history 를 소진할 수 있는 작은 채널에서는 세 번째 줄로
떨어져 재전송된다 — D-023 이 P-001 을 그렇게 좁혔다.

## R-005 — Receipt String Format

**Decision**: receipt 는 `slack:{channel}:{ts}` 로 만든다.

**Rationale**: S1 의 성공 응답 필드는 `ok`, `channel`, `ts`, `message`(내부에 `text`,
`username`, `bot_id`, `attachments`, `type`, `subtype`, `ts`)다. 이 중 message 를 유일하게
지목하는 것이 `channel` + `ts` 쌍이다. `ts` 는 S1 이 "timestamp ID" 라고 부른다.

`mark_delivered` 는 `remote_receipt` 를 비어 있지 않은 문자열로만 요구한다
(`events.py:2708`). 그리고 이미 delivered 인 event 를 같은 receipt 로 다시 mark 하면
그대로 통과하고, **다른 receipt 면 `OUTBOX_DELIVERY_RESULT_CONFLICT` 를 던진다**
(`events.py:2717`). 그래서 receipt 는 재계산해도 같은 값이 나와야 한다 — `channel` 과 `ts`
는 그 조건을 만족한다.

기존 `YamlProjectionDestination._receipt` 는 `yaml:{aggregate_sequence}:{payload_digest}`
다 (`projections.py:195`). prefix 로 destination 종류를 구분하는 형태를 따른다.

## R-006 — Slack Error Classification

**Decision**: 재시도 가능 code 를 **allowlist 로 고정하고 나머지는 전부 terminal** 로
분류한다.

재시도 가능 (allowlist):

| code / 신호 | 근거 |
|---|---|
| `ratelimited`, `rate_limited` | S1 error 목록. S4 는 HTTP 429 와 `Retry-After` header 를 명시한다 |
| HTTP 429 | S4 — "Slack will return a HTTP 429 Too Many Requests error, and a Retry-After HTTP header" |
| `request_timeout` | S1 |
| `service_unavailable` | S1 |
| `internal_error`, `fatal_error` | S1 |

나머지는 terminal 이다. 대표적으로 `channel_not_found`, `invalid_auth`, `not_authed`,
`token_expired`, `token_revoked`, `account_inactive`, `missing_scope`, `no_permission`,
`not_in_channel`, `is_archived`, `invalid_blocks`, `msg_blocks_too_long`,
`metadata_too_large`, `invalid_metadata_format`, `restricted_action*` (S1).

**Rationale**: D-016 이 이미 같은 판단을 governance event 쪽에 내렸다 — "새 event code는
목록에 없으므로 자동으로 `recovery_hold`가 된다." Slack 은 error code 가 80개가 넘고 (S1)
계속 늘어난다. 모르는 code 를 재시도로 분류하면 영구 실패를 조용히 반복하고, terminal 로
분류하면 operator hold 로 드러난다. 드러나는 쪽이 맞다.

**Alternatives considered**: terminal 을 allowlist 로 하고 나머지를 재시도로 두는 안.
D-016 의 fail-closed 원칙과 반대다. 기각한다.

**미확인**: S1 이 반환한 목록은 "80+ error codes including ..." 형태였다. 위 terminal
예시는 그 응답에 실제로 있던 code 다. **전체 목록을 다 확인했다고 주장하지 않는다.** 그래서
분류를 allowlist 방식으로 두는 것이 더 안전하다 — 목록이 불완전해도 동작이 안전한 쪽으로
떨어진다.

**미확인 2 (닫힘)**: S4 는 429 만 다루고 **5xx 응답 처리 지침이 없다.** 위 표의
`service_unavailable`·`internal_error` 는 error code 기준이지 HTTP status 기준이 아니다.
`plan.md` P-002 가 5xx 를 재시도로 정했고, wave 1 review 후 **status code 로 가르지 않는
것**으로 확정했다 (D-020). Slack code 가 없으면 status 와 무관하게 전부 재시도다. 근거는
contracts C-3 에 적었다.

## R-007 — `Retry-After` Is Not Wired Into The Existing Backoff

**Decision**: Package 3 는 `Retry-After` 를 backoff 계산에 반영하지 **않는다.** transport
Protocol 이 그 값을 error 에 실어 올리되, `OutboxDispatcher._backoff_seconds` 를 고치지
않는다.

**Rationale**: `fail()` (`events.py:2759`)은 `retry_at` 을 `_backoff_seconds(attempts)` 로만
계산한다 (`events.py:2783`). 외부에서 지연을 주입할 인자가 없다. 넣으려면 dispatcher 계약을
바꿔야 하고, 그건 A11 의 "우회하지 않는다" 와 별개로 **범위 확대**다.

`chat.postMessage` 는 channel 당 초당 1건 (S4)이고 governance Card 는 사람 결정 속도로
발생한다. 정상 운영에서 429 가 상시로 나는 부하가 아니다.

**Alternatives considered**: dispatcher 에 `retry_after_seconds` 를 받는 인자를 추가하는
안. MGC-008 에서 gate PASS 한 계약을 건드린다. 별도 item 으로 남긴다.

### Retry-After 에 대해 확인된 사실과 확인 안 된 것

S4 를 2026-08-05 에 다시 조회해 아래를 확정했다.

**확인됨**

- `Retry-After` header 의 단위는 초다.
- 문서가 주는 구체적 숫자는 **예시 하나뿐**이다 — `conversations.info` 를 30초 기다리라는 예.
- 문서는 그 값을 typical·maximum·guaranteed 로 규정하지 **않는다**.

**확인 안 됨**

- `Retry-After` 의 상한. Slack 은 문서화하지 않는다.
- 실제로 흔한 값의 범위. 관측 데이터가 없다.

> 이 문서의 이전 판은 "rate-limited tier 에서 30~60초가 흔하다" 를 S4 근거로 적었다.
> **그 문장은 S4 에 없다.** wave 1 round 4 review 에서 드러났고 여기서 삭제한다. 그 숫자를
> 근거로 삼았던 하한 상수도 함께 뺐다.

### 수용한 잔여 위험 (wave 1 review, D-020)

`OutboxConfig` 기본값(`retry_base_seconds=5`, `max_attempts=5`)이면 재시도가 5·10·20·40초에
일어나 **재시도 예산이 75초에 소진된다.** `Retry-After` 가 그보다 크면 Slack 이 기다리라고 한
창 안에서 attempt 를 다 쓰고 dead letter 와 operator hold 에 도달한다. 그리고 rate limit 은
app/workspace 범위라 같은 Slack app 의 다른 트래픽과 예산을 공유한다 — "governance card 는
사람 결정 속도로 발생한다"는 위 근거가 그 경우를 덮지 못한다.

**그 위험이 실제로 발생하는지는 판정할 수 없다.** `Retry-After` 상한이 문서에 없으므로
75초가 충분한지 부족한지를 공식 문서로 말할 수 없다. 그래서 wave 1 은 하한 상수를 두지
않는다. 근거 없는 숫자를 기계적 관문으로 만들면 만족시킨 쪽이 안전하다고 잘못 믿는다.

대신 test 가 기본 schedule 을 사실로 고정한다 — 대기 `[5, 10, 20, 40]`, 합 75. Package 4 가
Slack destination 의 `OutboxConfig` 를 만들 때 이 schedule 과 실제 관측한 `Retry-After` 값을
함께 입력으로 쓴다. 값 결정에 필요한 관측은 실제 workspace 가 있는 Package 4 몫이다.

## R-008 — Card Supersession Uses postMessage Only

**Decision**: Package 3 는 `chat.postMessage` 만 쓴다. `chat.update` 를 쓰지 않는다.

**Rationale**: supersession 은 outbox 안에서 끝난다. `supersede_pending` 은 **pending
event** 만 `superseded` 로 바꾼다 (`events.py:2797`, `OUTBOX_SUPERSEDE_INVALID`).
이미 delivered 된 event 는 대상이 아니다. 즉 superseded 된 Card 는 애초에 Slack 으로 나가지
않는다. 나간 Card 를 되돌리는 요구는 A11 에 없다.

`chat.update` 를 쓰려면 이전 message 의 `ts` 를 알아야 하고, 그러면 local 에 ts 를 저장하는
경로가 다시 생겨 R-003 의 "remote 가 유일한 진실 원천" 을 깬다.

**Alternatives considered**: 최신 Card 만 남기려고 이전 Card 를 update/삭제하는 안. Package 3
범위 밖이고 A11 에 근거가 없다. 필요하면 별도 item 이다.

---

## Resolved Unknowns

`spec.md` Assumptions 에 있던 미확인 항목의 처리 결과다.

| spec.md 미확인 | 결과 |
|---|---|
| Slack receipt 필드 | R-005 에서 `channel` + `ts` 로 고정 |
| Slack error code | R-006 에서 allowlist 방식으로 고정. 목록 완전성은 **미확인으로 남김** |
| channel 조회로 marker 를 찾는 것이 가능한가 | **가능하다** — R-003. `conversations.history` + `include_all_metadata=true` |

## Closed By Plan And Review

`plan.md` 가 정해야 했던 둘. 둘 다 사실이 아니라 판단이었고 지금은 닫혔다.

1. 사람이 Card 를 지웠다가 다시 만든 경우 hold 로 떨어뜨리는 것이 맞는가 (R-004) —
   **닫힘.** `plan.md` P-001 이 hold 로 정했다.
2. HTTP status 를 어떻게 볼 것인가 (R-006 미확인 2) — **닫힘.** `plan.md` P-002 가 5xx 를
   재시도로 정했고, wave 1 review 후 D-020 항목 1 이 status 로 가르지 않는 것으로 확정했다.
