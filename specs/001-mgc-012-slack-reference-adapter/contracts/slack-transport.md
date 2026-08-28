# Contract — Slack Transport And Projection Destination

**Feature**: `001-mgc-012-slack-reference-adapter` | **Date**: 2026-08-03

이 문서는 Package 3 가 노출하는 두 계약을 적는다. 하나는 **주입받는** transport Protocol,
하나는 기존 `ProjectionDestination` 을 **구현하는** destination 이다.

실제 HTTP 구현은 이 계약의 소비자이고 Package 4 몫이다 (D-018 항목 2). Package 3 는 이
계약과 그 계약을 만족하는 test double 만 만든다.

## C-1 — `SlackTransport` Protocol

주입받는다. Package 3 는 구현체를 만들지 않는다 (test double 제외).

```python
class SlackTransport(Protocol):
    def post_message(
        self,
        *,
        channel: str,
        payload: Mapping[str, object],
        marker: Mapping[str, object],
    ) -> SlackSendResult: ...

    def read_history(
        self,
        *,
        channel: str,
        cursor: str | None,
        limit: int,
    ) -> SlackHistoryPage: ...
```

send 와 read 를 모두 갖는 이유는 D-018 항목 3 이다 — reconcile 이 message marker
read-back 으로 판정하므로 read 가 없으면 계약이 성립하지 않는다.

구현체의 의무 둘. signature 로 강제할 수 없어 계약으로 적는다 (wave 1 review, D-020).

- **모든 실패를 `SlackTransportError` 로 감싼다.** 다른 예외가 새어 나가면 C-3 분류가 아예
  돌지 않는다. dispatcher 의 `except Exception` 이 그것을 무조건 retryable 로 만들어,
  terminal 이어야 할 `invalid_auth` 가 조용히 재시도된 뒤 attempt 소진으로만 멈춘다.
  `SlackProjectionDestination` 도 새어 나온 예외를 재감싸지만 (T002) 그건 **두 번째
  방어선**이지 이 의무의 대체가 아니다. 구현체가 code 를 실어 보내야 원인이 남는다.
- **호출 시간을 묶는다. 기준은 `reconcile()` + `send()` 전체다.** lease 만료 뒤에
  실패하면 dispatcher 의 `fail()` 이 `_require_lease` 에서 `OutboxLeaseConflictError` 로
  터져 **terminal 판정이 통째로 버려진다** — dead letter 도 hold 도 안 생기고, 나중에
  sweep 이 `OUTBOX_LEASE_EXPIRED` 로 되돌려 영구 실패를 다시 시도한다. 한 번의
  `deliver_next` 는 `read_history` 를 최대 `max_history_pages` 회 부른 뒤 `post_message`
  를 한 번 부른다. **호출 하나하나가 lease 안에 들어와도 합이 넘으면 같은 결과다**
  (wave 3 review). 예산은 호출당이 아니라 그 합에 걸어야 한다.
- **`read_history` 는 최신 message 부터 돌려준다.** page 안에서도, page 사이에서도
  그렇다. C-2.2 의 판정은 "먼저 만난 것이 이긴다" 이므로 순서가 뒤집히면 하위 sequence
  marker 를 우리 marker 보다 먼저 만나 이미 나간 Card 를 한 장 더 만든다. **이 사실을
  Slack 공식 문서에서 확인하지 못했다** — research S2 에 근거가 없다. 그래서 계약으로
  적고 Package 4 의 확인 항목으로 남긴다. destination 은 page **안**에서는 우리 marker 를
  먼저 찾는 방식으로 이 의존을 없앴지만 (T003), page **사이**는 cursor 를 우리가 만들지
  않아 막을 수 없다.

### C-1.1 — `post_message`

- `marker` 는 Slack `metadata` 로 나간다. `event_type` + `event_payload` 형태다
  (research S3).
- 성공하면 `SlackSendResult(channel=..., ts=...)` 를 반환한다. 두 필드는
  `chat.postMessage` 성공 응답의 `channel` 과 `ts` 다 (research S1).
- 실패하면 예외를 던진다. 예외는 Slack error code 를 갖거나(응답 층), 갖지 않는다(transport
  층). 이 구분이 C-3 의 분류 입력이다.

### C-1.2 — `read_history`

- `conversations.history` 대응이다. 구현체는 `include_all_metadata=true` 를 **반드시** 붙인다.
  안 붙이면 `event_type` 만 오고 `event_payload` 가 안 온다 (research S3). 그러면 marker 판정이
  불가능하다.
- `limit` 상한은 999 다 (research S2).
- 반환은 message 목록과 `next_cursor` 를 갖는 page 다. `next_cursor` 가 없으면 마지막 page 다.
- 각 message 는 `ts`, `metadata`, `app_id` 를 갖는다 (data-model.md). **Slack 응답의
  `metadata` object 를 그대로 옮긴다. 없는 key 를 `None` 으로 채우지 않는다.**
- **기동 전에 readback 자가검사를 한다.** 자기가 방금 보낸 message 를 `read_history` 로
  되읽어 marker 가 복원되는지 확인하고, 실패하면 기동을 거부한다. 이것이 유일하게
  `app_id` 오설정·metadata 매핑 누락·`event_type` 개명을 **전부** 한 번에 잡는 검사다
  (wave 3 round 2 review). destination 은 그중 일부만 지문으로 잡을 수 있고, 못 잡는
  경우의 결과는 hold 가 아니라 **조용한 중복 Card** 다. Package 4 의 exit criteria 다.

## C-2 — `SlackProjectionDestination`

`ProjectionDestination` (`events.py:236`)을 구현한다.

```python
class SlackProjectionDestination:
    def __init__(
        self,
        transport: SlackTransport,
        *,
        destination_ref: str,
        channel: str,
        app_id: str,
        max_attempts: int,
        max_history_pages: int = SLACK_MAX_HISTORY_PAGES,
    ) -> None: ...

    destination_ref: str

    def reconcile(self, event: OutboxEventView) -> str | None: ...
    def send(self, event: OutboxEventView) -> str: ...
```

`destination_ref` 는 `provider:slack:{channel_digest}` 형식을 그대로 받는다. destination 이
그 형식을 만들거나 해석하지 않는다 (D-018 항목 1).

`channel` 은 별도로 받는다. `channel_digest` 는 digest 라 역산이 안 된다.

`app_id` 는 **우리 app 의 Slack app id** 다. `reconcile()` 이 history message 의 `app_id` 와
대조해 다른 app 이 심은 metadata 를 배제한다 (research S3, T003 AC-04). 이 값이 없으면 그
배제를 할 수 없다 — marker 의 모양은 공개된 구조라 다른 app 이 같은 모양을 심을 수 있고,
그것을 우리 marker 로 읽으면 남의 message 를 우리 Card 로 확정한다. **T003 구현 중에
추가했다** — AC-04 가 요구하는데 C-2 에 자리가 없었다. 빈 문자열은 생성자가 거부한다.
어긋나면 어떤 marker 도 우리 것으로 인정되지 않는데, 그 결과는 **hold 가 아니라 중복 Card**
다 — history 소진이 미전송으로 판정되어 (D-023 항목 3) 매 재시도마다 Card 가 한 장씩 는다.
조용히 일어나므로 더 나쁘다.

`max_attempts` 는 C-3.1 이 쓴다. **호출자는 이 값을 그 destination 을 도는
`OutboxDispatcher` 의 `OutboxConfig.max_attempts` 와 같게 준다.** 더 크면 C-3.1 이 안 돌아
retryable 원인이 그대로 사라지고, 더 작으면 아직 남은 attempt 를 두고 되돌릴 수 없는 hold 를
만든다. destination 은 dispatcher config 를 읽을 경로가 없어 검증하지 못한다. **T003 이
그 표면을 넓혔다** — `reconcile` 의 read 실패도 C-3.1 을 타므로 일시적 read 실패 하나가
남은 attempt 를 버리고 hold 를 만든다 (wave 3 round 2 review).

**생성자가 거부하는 값 다섯.** dispatcher config 와의 일치는 검증하지 못하지만 그 자체로 말이
안 되는 값은 막는다. 다섯 다 `ValueError` 다.

| 조건 | 막는 이유 |
|---|---|
| `destination_ref` 가 공백뿐이거나 보이지 않는 문자를 포함 | 어떤 event 도 claim 되지 않아 조용히 멈춘다 |
| `channel` 이 공백뿐이거나 보이지 않는 문자를 포함 | receipt 가 `slack::{ts}` 가 되고 전송 대상이 없다 |
| `app_id` 가 공백뿐이거나 보이지 않는 문자를 포함 | 어떤 marker 도 못 읽어 매 재시도마다 중복 Card 가 난다 |
| `max_history_pages < 1` | reconcile 이 아무것도 안 훑어 첫 전달부터 판정불가로 떨어진다 |
| `max_attempts < 1` | 첫 transient 실패가 곧바로 C-3.1 을 타 되돌릴 수 없는 hold 를 만든다 |

`destination_ref`·`channel`·`app_id` 는 앞뒤 공백을 **지워서 저장한다.** 그리고 지운 뒤에도
보이지 않는 문자(`\u200b`, `\ufeff`, `\u200e`)가 남으면 거부한다 — `str.strip()` 이 그것들을
지우지 않는다. `app_id` 에 섞이면 `reconcile` 이 우리 marker 를 하나도 못 알아보고 history
소진을 미전송으로 읽어 **중복 Card** 를 만든다 (wave 3 review). 검사만 하면 env var 나
YAML scalar 에서 온 개행이 그대로 Slack 에 나가 `channel_not_found` (allowlist 밖 → terminal
→ 되돌릴 수 없는 hold) 를 부르고, `reconcile()` 이 정규화된 config 로 만든 receipt 와 갈린다.

뒤 둘의 하한은 `OutboxConfig` 의 `ge=1` 과 같다 (`events.py:2553`). config 를 안 읽고도
검사된다.

### C-2.1 — `send()` 계약

| 조건 | 결과 |
|---|---|
| `event.destination_ref != self.destination_ref` | `OutboxReconcileError("OUTBOX_DESTINATION_MISMATCH")` |
| payload digest 불일치 | `OutboxReconcileError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")` |
| transport 성공 | `"slack:{channel}:{ts}"` 반환 |
| transport 실패, retryable 분류 | 원래 예외. 단 마지막 attempt 는 C-3.1 |
| transport 실패, terminal 분류 | `SlackProjectionTerminalError`. attempt 와 무관하다 |

검증 순서는 `YamlProjectionDestination.send` 와 같다 (`projections.py:57`–`61`). transport 를
부르기 전에 둘 다 통과해야 한다.

**두 검증 실패는 `OutboxReconcileError` 다** (D-022). 부모인 `GovernanceEventError` 로 던지면
`deliver_next` 의 `except OutboxReconcileError` (`events.py:2856`)가 못 잡고 generic handler 로
떨어져 원인이 `OUTBOX_DELIVERY_FAILED` 상수로 덮인다. 두 조건은 event row 의 불변 column 에서
나오므로 재시도가 확정적으로 무의미하다. `YamlProjectionDestination` 도 같이 바꿨다 — 두
destination 이 같은 조건을 다르게 다루면 안 된다.

**receipt 의 `{channel}` 은 생성자가 받은 `channel` 이다.** `SlackSendResult.channel` 이
아니다. `reconcile()` 은 `conversations.history` 응답에서 channel 을 얻지 못해 생성자 값밖에
쓸 수 없다 (C-1.2 의 message 필드는 `ts`·`metadata`·`app_id` 셋뿐이다). Slack 이 우리가 보낸
channel 과 다른 표현을 돌려주면 (이름으로 보내고 ID 를 받는 경우) 두 경로의 receipt 가
갈라지고 C-2.3 이 깨진다. `ts` 는 `SlackSendResult.ts` 를 그대로 쓴다.

### C-2.2 — `reconcile()` 계약

**먼저 첫 시도를 가른다.** 아래 둘이 **모두** 참이면 Slack 을 조회하지 않고 곧바로 `None`
을 반환한다 (D-023 항목 2).

```text
event.attempts == 1  AND  event.last_error_code is None
```

- outbox row 는 언제나 `attempts=0`·`last_error_code=NULL` 로 생성되고 (`events.py:2358`-`2359`)
  그 값을 되돌리는 곳은 `mark_delivered` 하나뿐인데 (`events.py:2729`) 그 row 는
  `delivered` 라 `claim_next` 가 다시 claim 하지 않는다. `claim_next` 는 자기 transaction 을 commit 하므로
  `attempts=1` 은 `post_message` 보다 **먼저** durable 하다. 그래서 이 조건은 "이 event 로
  Slack 을 부른 적이 한 번도 없다" 와 같다.
- **두 조건을 함께 봐야 한다.** `attempts` 증가는 `CASE WHEN attempts < max_attempts` 라
  상한에서 멈춘다 (`events.py:2672`). `max_attempts == 1` 구성에서는 두 번째 claim 도
  `attempts == 1` 로 보인다. 그 재claim 은 `last_error_code = 'OUTBOX_LEASE_EXPIRED'` 를
  요구하므로 (`events.py:2644`) 두 번째 조건이 그것을 막는다.
- 조회를 건너뛰는 것은 의도다. 찾을 marker 가 정의상 없고 `conversations.history` 는
  Tier 2 다 (research S4). 대부분의 Card 가 첫 시도에 성공하므로 평상시 history 조회가
  0회가 된다.

그 밖의 경우에만 최신부터 역순으로 최대 `max_history_pages` page 를 훑는다. 표의 판정은
넷이고, 아래 bullet 이 규정하는 두 가지 fail-closed 가 그보다 앞선다.

| 먼저 만난 것 | 반환 |
|---|---|
| 이 event 의 marker (`event_id` 일치 + `destination_ref` 일치 + `payload_digest` 일치) | `"slack:{channel}:{ts}"` |
| 같은 `destination_ref` 의 **더 낮은** `destination_sequence` marker | `None` |
| `next_cursor` 가 없어 **history 가 소진**됐고 위 둘 다 없음 | `None` |
| 상한까지 훑고 멈췄는데 위 둘 다 없음 | `OutboxReconcileError` |

- 다른 app 이 심은 metadata 는 `app_id` 로 배제한다 (research S3).
- 두 번째 규칙이 "아직 안 보냈다"의 증거다. `claim_next` 가 이전 sequence 미확정 시 다음
  event 를 claim 하지 않으므로 (`events.py:2648`) 역순 조회에서 N-1 을 N 보다 먼저 만나면
  N 은 아직 없다.
- **세 번째 규칙이 첫 Card 를 살린다** (D-023 항목 3). `destination_sequence` 가 1 이면
  두 번째 규칙이 성립할 수 없어 — 더 낮은 sequence 가 존재하지 않는다 — 그 규칙만으로는
  첫 Card 가 일시 오류를 한 번만 만나도 영구 hold 가 된다. P-001 이 막으려던 사고는
  "Card 가 채널에 **실제로 있는데** 조회 범위 밖이라 못 찾고 재전송해 **두 장**이 되는
  것" 이고, 그 사고는 범위를 다 못 본 경우에만 성립한다. history 가 소진됐으면 없는 것이
  확정이라 두 장이 될 수 없다.
- **세 번째와 네 번째를 구분하는 것은 `next_cursor` 하나다** (C-1.2). 마지막 page 를 받고도
  못 찾았으면 소진, `next_cursor` 가 남았는데 page 상한에 걸려 멈췄으면 판정 불가다.
- 네 번째가 fail-closed 다. `deliver_next` 가 `OutboxReconcileError` 를
  `fail(..., unreconcilable=True)` 로 보내고 (`events.py:2856`–`2863`), 그것이 attempts 와
  무관하게 DLQ + operator hold 를 만든다 (`events.py:2780`, `events.py:2903`).
- **네 번째의 code 는 다른 hold 원인과 구분돼야 한다** (D-023 항목 4). `max_history_pages`
  가 작아서 생긴 hold 인지 아닌지를 그 문자열로 알 수 있어야 한다. 구분이 없으면 값이
  작았다는 것을 사후에 알 방법이 없다.

### C-2.2.1 — Search Bounds

| 값 | 확정값 | 근거 |
|---|---|---|
| page 당 `limit` | **999** | S2 의 상한. Slack 은 message 수가 아니라 호출 수를 세므로 (S4) 한 번에 꽉 채우는 쪽이 손해가 없다 |
| `max_history_pages` | **5** | 판단이다. 아래 참조 |

**5 는 측정이 아니라 판단이다.** 근거로 쓸 수 있는 fact 는 셋뿐이다 — page 당 999 상한
(S2), Tier 2 분당 20+ 요청 (S4), 그리고 이 조회가 재시도 때만 일어난다는 것 (위 첫 시도
규칙). 정작 필요한 숫자인 "Card 한 장과 다음 Card 사이에 쌓이는 message 수" 는 workspace
에 달렸고 **모른다.** 최악 5회 호출은 Tier 2 예산의 4분의 1이다. 실측은 Package 4 몫이다
(D-023 항목 4).
- **`event_id` 는 같은데 `payload_digest` 가 다른 marker 를 만나면 즉시 멈춘다** —
  `SlackProjectionMarkerDigestMismatchError`. 같은 event 의 다른 내용이 이미 나갔다는
  뜻이라 정상 경로에서 나올 수 없다. 이 문서의 이전 판은 "계속 훑는다" 였는데, D-023 이
  history 소진을 미전송으로 인정하면서 그 경로가 **재전송으로 새게 됐다** (wave 3
  review). D-023 이 "Card 가 두 장이 되는 일은 어느 쪽에서도 없다" 고 약속했으므로
  fail-closed 로 고쳤다. code 를 따로 두는 이유는 원인이 상한 부족이나 지워진 Card 와
  전혀 다르기 때문이다.
- **우리 `event_type` 을 단 message 인데 `app_id` 나 `event_payload` 가 없으면 즉시
  멈춘다** — `SLACK_PROJECTION_METADATA_UNREADABLE` (D-024 항목 2). 이것은 한 message 의
  문제가 아니라 **조회 방식**의 결함이다. `include_all_metadata` 를 안 붙이면 `event_type`
  만 오고 (research S3), adapter 가 `app_id` 를 안 옮기면 대조가 전부 실패한다. 어느 쪽이든
  marker 를 하나도 못 읽고, 그러면 history 소진이 미전송으로 판정되어 **매 재시도마다 Card
  가 한 장씩 는다.** hold 가 아니라 중복이라 조용하다. `event_payload` 는 key 부재와 `None`
  을 함께 본다 — adapter 가 `md.get(...)` 로 채우면 없는 key 가 `None` 이 된다.

  **수용한 오탐이 하나 있다.** 제3의 app 이 우리 `event_type` 을 쓰면서 `app_id` 없이
  보내면 이 규칙에 걸려 되돌릴 수 없는 hold 가 된다. Slack 이 app message 에 `app_id` 를
  **항상** 붙이는지는 research S2·S3 에서 확인하지 못했다 — 확인된 것은 "각 message 는
  `app_id` 를 갖고 있어 어느 app 이 보냈는지 구분할 수 있다" 까지다. 방향 때문에
  수용한다: 안 막으면 조용한 중복 Card, 막으면 시끄러운 hold 다. 확정은 Package 4 다.
- **page 안에서는 우리 marker 가 이긴다.** 위 표는 "먼저 만난 것" 이라고 쓰지만, 한 page
  안에서는 순서에 기대지 않고 우리 marker 를 먼저 찾는다. C-1 의 정렬 의무를 signature
  로 강제할 수 없어 만든 방어선이다 (wave 3 review).

### C-2.3 — Receipt Stability

`mark_delivered` 는 이미 delivered 인 event 를 **다른** receipt 로 다시 mark 하면
`OUTBOX_DELIVERY_RESULT_CONFLICT` 를 던진다 (`events.py:2717`). 그러므로 같은 message 에
대해 `send()` 와 `reconcile()` 이 **같은 receipt 문자열**을 만들어야 한다. 둘 다
`slack:{channel}:{ts}` 로 만든다.

## C-3 — Error Classification

입력은 transport 예외, 출력은 dispatcher 로 나가는 예외 종류다.

**위에서부터 먼저 맞는 규칙이 이긴다.** 아래 표는 순서 있는 규칙이지 집합이 아니다.

| # | 입력 | 분류 | destination 이 던지는 것 | dispatcher 결과 |
|---|---|---|---|---|
| 1 | HTTP 429 | retryable | 일반 예외 | `retry_wait`, 소진 시 DLQ |
| 2 | **Slack error code 가 없는 모든 실패** — 연결 실패, timeout, 임의의 HTTP status | retryable | 일반 예외 | 위와 같음 |
| 3 | `ratelimited`, `rate_limited`, `request_timeout`, `service_unavailable`, `internal_error`, `fatal_error` | retryable | 일반 예외 | 위와 같음 |
| 4 | 그 밖의 모든 Slack error code | terminal | `OutboxReconcileError` | 즉시 DLQ + hold |

규칙 1 이 규칙 4 보다 앞이라 **HTTP 429 는 allowlist 밖 code 를 달고 와도 재시도한다.** 429 는
Slack 이 "지금 말고 나중에" 라고 답한 것이라 함께 온 code 를 확정 판정으로 읽지 않는다.
영구 실패면 attempt 를 소진하고 같은 dead letter 에 도달한다.

근거는 research R-006 과 plan P-002 다. allowlist 에 없는 code 는 전부 terminal 이다 —
D-016 의 fail-closed 원칙과 같다.

**status code 로 가르지 않는 이유** (wave 1 review, D-020): Slack 은 application error 를
HTTP 200 + `ok: false` 로 준다. 진짜 HTTP 4xx 는 429 하나뿐이고 그건 규칙 1 이 잡는다.
그래서 code 없는 4xx 는 거의 전부 proxy·WAF·load balancer 가 낸 것이고 그건 transient 다.
그리고 **retryable 이 보수적인 쪽이다** — 영구 실패를 retryable 로 잘못 봐도 attempt 를
소진하면 같은 DLQ + hold 에 도달한다(기본값이면 약 75초). 반대로 transient 를 terminal 로
보면 destination 전체가 즉시 멈추고, 그 hold 는 `governance_operator_holds` 의
`CHECK (resolved_at IS NULL)` 때문에 되돌릴 수 없다.

**error code 는 정규화 후 비교한다.** 앞뒤 공백을 없애고 소문자로 맞춘다. 빈 문자열과 str
아닌 값은 code 없음과 같게 다룬다. `is None` 만 보면 `""` 가 allowlist 를 못 만나고 terminal
로 떨어져 분류가 통째로 뒤집힌다.

**정규화는 값을 버리지 않는다.** 형식이 이상해도 (`missing_scope: chat:write` 처럼 detail 이
붙어 오거나 64자를 넘어도) 그대로 분류에 넣는다. 버리면 그 값이 code 없음이 되어 규칙 2 로
빠지고, retryable 경로는 원인을 안 남긴다. 형식이 이상한 code 는 정의상 allowlist 밖이므로
규칙 4 가 terminal 로 보낸다. 원본은 진단용으로 예외 객체에 남는다.

**terminal code 문자열은 Slack 원인을 담는다** — `SLACK_PROJECTION_TERMINAL_ERROR:{suffix}`.
dispatcher 는 예외 객체를 버리고 `code` 문자열만 dead letter 와 operator hold 에 적는다.
원인이 없으면 `invalid_auth`(token 회전 후 전량 replay), `channel_not_found`(폐기 후 재지정),
`msg_blocks_too_long`(code 버그라 replay 무의미)이 전부 같은 row 가 된다.

접미사는 **저장 직전에만** 형식을 맞춘다 — 허용 밖 문자를 `_` 로 바꾸고 64자로 자른다.
code 가 없으면 `unknown` 이다. 저장 대상 두 table 이 append-only 라 원격 문자열을 무제한
길이로 남기면 지울 수 없다. 다듬되 버리지 않는다.

**`OutboxReconcileError` 를 terminal 신호로 쓰는 이유**: `deliver_next` 에서
`unreconcilable=True` 가 붙는 경로가 그것 하나뿐이다 (`events.py:2856`–`2863`). 일반 예외는
`OUTBOX_DELIVERY_FAILED` 로 가서 attempts 를 소진해야 DLQ 에 도달한다. terminal 을 그 길로
보내면 회복 불가 오류를 `max_attempts` 번 재시도한다.

`Retry-After` header 값은 예외에 실어 올리되 backoff 계산에 넣지 않는다 (research R-007).
`fail()` 에 지연을 주입할 인자가 없고, 그걸 만드는 것은 MGC-008 계약 변경이다.

### C-3.1 — Last Attempt Carries The Retryable Cause

D-020 항목 6 을 여기서 닫는다. retryable 로 분류된 실패가 attempt 를 소진하면
`deliver_next` 의 generic handler 가 예외를 버리고 `OUTBOX_DELIVERY_FAILED` 상수만 남긴다
(`events.py:2864`). 그 상수가 dead letter 와 operator hold 에 그대로 적혀
(`events.py:2903` `_dead_letter`), 연결 실패·timeout·`ratelimited`·`internal_error` 가 전부
같은 row 가 된다.

**`event.attempts >= max_attempts` 인 호출에서만 retryable 을 terminal 로 올린다.**

```text
SLACK_PROJECTION_RETRY_EXHAUSTED:{suffix}
```

- `attempts` 는 `claim_next` 가 claim 시점에, **`attempts < max_attempts` 인 동안** 증가시킨다
  (`events.py:2672` 의 `CASE WHEN attempts < ?`). 그래서 `send()` 안에서 보는
  `event.attempts` 가 현재 시도 번호이고 상한을 넘지 않는다. 증가하지 않는 재claim 은
  lease 만료 replay 하나뿐인데 그 경로는 `deliver_next` 가 `send()` 앞에서 가로채
  `OUTBOX_POST_SEND_RECONCILE_REQUIRED` 로 끝낸다 (`events.py:2843`-`2853`).
- `fail()` 은 `exhausted or unreconcilable` 을 같은 `_dead_letter` 로 보낸다
  (`events.py:2778`). 마지막 attempt 라면 state 전이는 **바뀌지 않고** error_code 만
  달라진다. 재시도를 한 번도 줄이지 않는다.
- dispatcher 는 안 고친다. `events.py` 는 T002 의 `forbidden_paths` 다.
- `suffix` 는 `persisted_code_suffix` 가 만든다. Slack code 가 있으면 그 code, transport 층
  실패라 code 가 없으면 원인 예외의 class 이름을 `transport_{name}` 으로 넣는다. 둘 다
  없으면 `no_slack_code` 다.

마지막 attempt 가 아니면 원래 예외를 그대로 올린다. 분류 규칙 C-3 자체는 바뀌지 않는다 —
이것은 **소진 시점의 기록**이지 재분류가 아니다.

**terminal 로 분류된 실패에는 적용하지 않는다.** 마지막 attempt 에 terminal code 가 오면
`SLACK_PROJECTION_TERMINAL_ERROR:{code}` 가 이긴다 — 둘 다 dead letter 로 가지만 그쪽이 더
정확한 원인이다.

## C-4 — What This Contract Forbids

- 실제 HTTP 호출. `pydantic`·`pyyaml`·`typer` 밖의 dependency 추가 (D-018 항목 2)
- `destination_ref` 형식 변경 (D-018 항목 1)
- `OutboxDispatcher` 수정 — retry·lease·DLQ·hold 를 우회하는 별도 경로 (A11)
- `chat.update` 로 이미 나간 Card 를 갱신하는 것 (research R-008)
- Telegram 또는 다른 Provider 의 activation state 변경 (A14)
