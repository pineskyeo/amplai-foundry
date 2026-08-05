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
- 각 message 는 `ts`, `metadata`, `app_id` 를 갖는다 (data-model.md).

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
        max_history_pages: int,
    ) -> None: ...

    destination_ref: str

    def reconcile(self, event: OutboxEventView) -> str | None: ...
    def send(self, event: OutboxEventView) -> str: ...
```

`destination_ref` 는 `provider:slack:{channel_digest}` 형식을 그대로 받는다. destination 이
그 형식을 만들거나 해석하지 않는다 (D-018 항목 1).

`channel` 은 별도로 받는다. `channel_digest` 는 digest 라 역산이 안 된다.

### C-2.1 — `send()` 계약

| 조건 | 결과 |
|---|---|
| `event.destination_ref != self.destination_ref` | `GovernanceEventError("OUTBOX_DESTINATION_MISMATCH")` |
| payload digest 불일치 | `GovernanceEventError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")` |
| transport 성공 | `"slack:{channel}:{ts}"` 반환 |
| transport 실패 | C-3 분류에 따른 예외 |

검증 순서는 `YamlProjectionDestination.send` 와 같다 (`projections.py:53`–`57`). transport 를
부르기 전에 둘 다 통과해야 한다.

### C-2.2 — `reconcile()` 계약

최신부터 역순으로 최대 `max_history_pages` page 를 훑는다. 판정은 셋이다.

| 먼저 만난 것 | 반환 |
|---|---|
| 이 event 의 marker (`event_id` 일치 + `destination_ref` 일치 + `payload_digest` 일치) | `"slack:{channel}:{ts}"` |
| 같은 `destination_ref` 의 **더 낮은** `destination_sequence` marker | `None` |
| 상한까지 훑어도 둘 다 없음 | `OutboxReconcileError` |

- 다른 app 이 심은 metadata 는 `app_id` 로 배제한다 (research S3).
- 두 번째 규칙이 "아직 안 보냈다"의 증거다. `claim_next` 가 이전 sequence 미확정 시 다음
  event 를 claim 하지 않으므로 (`events.py:2648`) 역순 조회에서 N-1 을 N 보다 먼저 만나면
  N 은 아직 없다.
- 세 번째가 fail-closed 다. `deliver_next` 가 `OutboxReconcileError` 를
  `fail(..., unreconcilable=True)` 로 보내고 (`events.py:2856`–`2863`), 그것이 attempts 와
  무관하게 DLQ + operator hold 를 만든다 (`events.py:2780`, `events.py:2903`).
- **`event_id` 는 같은데 `payload_digest` 가 다른 marker 를 만나면 판정하지 않고 계속
  훑는다.** 같은 event 의 다른 내용이 이미 나갔다는 뜻이라 정상 경로에서 나올 수 없다.
  상한까지 못 찾으면 세 번째 규칙으로 hold 가 걸린다.

### C-2.3 — Receipt Stability

`mark_delivered` 는 이미 delivered 인 event 를 **다른** receipt 로 다시 mark 하면
`OUTBOX_DELIVERY_RESULT_CONFLICT` 를 던진다 (`events.py:2717`). 그러므로 같은 message 에
대해 `send()` 와 `reconcile()` 이 **같은 receipt 문자열**을 만들어야 한다. 둘 다
`slack:{channel}:{ts}` 로 만든다.

## C-3 — Error Classification

입력은 transport 예외, 출력은 dispatcher 로 나가는 예외 종류다.

| 입력 | 분류 | destination 이 던지는 것 | dispatcher 결과 |
|---|---|---|---|
| 연결 실패 / timeout / HTTP 5xx (Slack error code 없음) | retryable | 일반 예외 | `retry_wait`, 소진 시 DLQ |
| `ratelimited`, `rate_limited`, HTTP 429 | retryable | 일반 예외 | 위와 같음 |
| `request_timeout`, `service_unavailable`, `internal_error`, `fatal_error` | retryable | 일반 예외 | 위와 같음 |
| 그 밖의 모든 Slack error code | terminal | `OutboxReconcileError` | 즉시 DLQ + hold |

근거는 research R-006 과 plan P-002 다. allowlist 에 없는 code 는 전부 terminal 이다 —
D-016 의 fail-closed 원칙과 같다.

**`OutboxReconcileError` 를 terminal 신호로 쓰는 이유**: `deliver_next` 에서
`unreconcilable=True` 가 붙는 경로가 그것 하나뿐이다 (`events.py:2856`–`2863`). 일반 예외는
`OUTBOX_DELIVERY_FAILED` 로 가서 attempts 를 소진해야 DLQ 에 도달한다. terminal 을 그 길로
보내면 회복 불가 오류를 `max_attempts` 번 재시도한다.

`Retry-After` header 값은 예외에 실어 올리되 backoff 계산에 넣지 않는다 (research R-007).
`fail()` 에 지연을 주입할 인자가 없고, 그걸 만드는 것은 MGC-008 계약 변경이다.

## C-4 — What This Contract Forbids

- 실제 HTTP 호출. `pydantic`·`pyyaml`·`typer` 밖의 dependency 추가 (D-018 항목 2)
- `destination_ref` 형식 변경 (D-018 항목 1)
- `OutboxDispatcher` 수정 — retry·lease·DLQ·hold 를 우회하는 별도 경로 (A11)
- `chat.update` 로 이미 나간 Card 를 갱신하는 것 (research R-008)
- Telegram 또는 다른 Provider 의 activation state 변경 (A14)
