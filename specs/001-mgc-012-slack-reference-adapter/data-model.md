# Phase 1 Data Model — MGC-012 Package 3

**Feature**: `001-mgc-012-slack-reference-adapter` | **Date**: 2026-08-03

Package 3 는 **DB schema 를 바꾸지 않는다.** 아래는 (a) 이미 있어서 소비만 하는 것과
(b) 새로 정의하는 in-memory 구조를 나눈 것이다.

## Existing — Consumed, Not Modified

### `OutboxEventView` (`events.py:212`)

`reconcile()` 과 `send()` 의 입력이다. Package 3 가 읽는 필드는 아래다.

| 필드 | 쓰임 |
|---|---|
| `event_id` | marker 의 1차 식별자 |
| `destination_ref` | destination 자기 것과 일치 검증 → `OUTBOX_DESTINATION_MISMATCH` |
| `destination_sequence` | 순서 판정과 marker |
| `aggregate_sequence` | marker 보조 |
| `source_state_revision` | marker 보조 |
| `payload_digest` | 전송 전 무결성 검증 → `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` |
| `payload` | Slack message 본문 구성 입력 |
| `remote_receipt` | 이미 delivered 인 event 의 receipt (dispatcher 가 비교) |
| `last_error_code`, `attempts`, `claim_generation` | dispatcher 가 쓴다. destination 은 안 쓴다 |

**형식을 바꾸지 않는다.** 특히 `destination_ref` 의 `provider:{provider}:{channel_digest}`
는 D-018 항목 1 로 범위 밖이다.

### `ProjectionDestination` (`events.py:236`)

```python
class ProjectionDestination(Protocol):
    destination_ref: str

    def reconcile(self, event: OutboxEventView) -> str | None: ...
    def send(self, event: OutboxEventView) -> str: ...
```

`SlackProjectionDestination` 이 이 Protocol 의 두 번째 구현이 된다. 첫 번째는
`YamlProjectionDestination` (`projections.py:36`)이다.

## New — In Memory Only

### `SlackMessageMarker`

Slack message metadata 에 실려 나가고 reconcile 이 다시 읽는 구조다. Slack 쪽 표현은
`metadata.event_payload` 다 (research R-003).

| 필드 | 출처 | 용도 |
|---|---|---|
| `event_id` | `OutboxEventView.event_id` | 이 event 인지 판정 |
| `destination_ref` | `OutboxEventView.destination_ref` | 다른 destination 의 marker 를 배제 |
| `destination_sequence` | `OutboxEventView.destination_sequence` | 하위 sequence 선발견 판정 (R-004) |
| `payload_digest` | `OutboxEventView.payload_digest` | 같은 event 의 다른 내용 배제 |

**payload 본문을 넣지 않는다.** metadata 크기 상한을 공식 문서에서 확인하지 못했고
(`research.md` R-003 미확인), `metadata_too_large` error code 가 실재한다.

`event_type` 은 고정 문자열 하나를 쓴다. 값은 wave 1 에서 정하고 이후 바꾸지 않는다 —
바꾸면 이전에 나간 marker 를 못 읽는다.

### `SlackSendResult`

transport 의 send 반환값. `chat.postMessage` 성공 응답에서 필요한 것만 담는다 (research S1).

| 필드 | Slack 응답 필드 |
|---|---|
| `channel` | `channel` |
| `ts` | `ts` |

receipt 는 이 둘로 만든다 — `slack:{channel}:{ts}` (research R-005).

### `SlackHistoryMessage`

transport 의 read 반환 항목. `conversations.history` 응답의 message 에서 필요한 것만 담는다
(research S2·S3).

| 필드 | 용도 |
|---|---|
| `ts` | receipt 재구성 |
| `metadata` | marker 복원. `include_all_metadata=true` 로 조회해야 `event_payload` 가 온다 |
| `app_id` | 다른 app 이 심은 metadata 배제 (S3) |

### `SlackFailureClass`

`retryable` | `terminal` 두 값이다. 분류 규칙은 research R-006, plan P-002, D-020 이다.
**순서 있는 규칙이고 먼저 맞는 것이 이긴다** (contracts C-3).

1. HTTP 429 → `retryable`. allowlist 밖 code 를 달고 와도 그렇다
2. Slack error code 가 없는 모든 실패 → `retryable`. status code 로 가르지 않는다
3. Slack error code 가 allowlist 에 있으면 → `retryable`
4. 형식과 무관하게 그 밖의 모든 Slack error code → `terminal`

비교 전에 code 를 정규화한다 — 앞뒤 공백 제거, 소문자화, 빈 문자열과 str 아닌 값은 code
없음으로 취급. **형식이 이상해도 버리지 않는다** — 버리면 규칙 4 대상이 규칙 2 로 새고,
그 경로는 원인을 안 남긴다. 저장 문자열의 형식 강제는 별개이며 `persisted_code_suffix` 가
저장 직전에만 한다.

## State Transitions

Package 3 는 새 state machine 을 만들지 않는다. 기존 outbox state 를 그대로 쓴다.

```text
pending ─claim_next──▶ leased ─reconcile 성공/send 성공──▶ delivered
                          │
                          ├─ retryable 실패 ──▶ retry_wait ──▶ (재claim)
                          ├─ terminal 실패 ───▶ dead_letter + operator_hold
                          └─ reconcile 판정불가 ▶ dead_letter + operator_hold
```

전이를 실행하는 것은 전부 `OutboxDispatcher` 다 (`events.py:2833` `deliver_next`,
`events.py:2759` `fail`, `events.py:2903` `_dead_letter`). destination 은 반환값과 예외로만
전이에 관여한다.

| destination 의 행동 | dispatcher 의 전이 |
|---|---|
| `reconcile()` 이 receipt 반환 | `delivered` |
| `reconcile()` 이 `None` 반환 | `send()` 로 진행 |
| `reconcile()` 이 `OutboxReconcileError` | `dead_letter` + hold (attempts 무관) |
| `send()` 가 receipt 반환 | `delivered` |
| `send()` 가 그 밖의 예외 | `OUTBOX_DELIVERY_FAILED` 로 `retry_wait` 또는 소진 시 DLQ |

**terminal 인 Slack error 를 즉시 DLQ 로 보내려면 `OutboxReconcileError` 를 써야 한다** —
`deliver_next` 에서 `unreconcilable=True` 가 붙는 경로가 그것뿐이다 (`events.py:2856`–`2863`).
일반 예외는 attempts 를 소진해야 DLQ 로 간다. 이 선택은 wave 1 에서 확정한다.

## Validation Rules

`YamlProjectionDestination.send` 가 이미 쓰는 것과 같은 순서로 검증한다
(`projections.py:53`–`57`).

1. `event.destination_ref != self.destination_ref` → `OUTBOX_DESTINATION_MISMATCH`
2. `payload` 를 정규 JSON 으로 직렬화해 만든 digest ≠ `event.payload_digest` →
   `OUTBOX_PAYLOAD_INTEGRITY_FAILURE`
3. 위 둘을 통과한 뒤에만 transport 를 부른다

digest 계산은 `YamlProjectionDestination._payload_digest` (`projections.py:165`)와 같은
규칙이어야 한다 — `ensure_ascii=False`, `separators=(",", ":")`, `sort_keys=True`,
`sha256:` prefix. 두 destination 이 같은 event 를 다르게 판정하면 안 된다.
