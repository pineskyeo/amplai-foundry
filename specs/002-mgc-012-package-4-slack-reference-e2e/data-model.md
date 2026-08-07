# Data Model: MGC-012 Package 4

Package 4 는 **durable schema 를 바꾸지 않는다.** SQLite table 도, `OutboxEventView` 도,
marker 형식도 그대로다. 아래는 이번에 새로 생기는 **프로세스 내부 값**들이다.

001 의 `data-model.md` 를 대체하지 않는다. `SlackSendResult`·`SlackHistoryPage`·
`SlackHistoryMessage`·`SlackMarkerView` 는 거기가 권위다.

## SlackCredentials (신규)

실행 중인 프로세스가 Slack 을 부르는 데 필요한 비밀값이다. **durable 하지 않다** — 어떤
table 에도 저장되지 않고 프로세스와 함께 사라진다.

| 필드 | 형 | 설명 |
|---|---|---|
| `bot_token` | `SecretStr` | `chat.postMessage` 와 `conversations.history` 의 Authorization |
| `signing_secret` | `SecretStr` | ingress 검증용. 이미 `SlackInstallationPolicy` 가 갖는 값과 같다 |

**규칙**

- 두 값 다 `SecretStr` 이고 `repr=False` 다. `slack.py` 의 `signing_secret` 이 이미 그
  처리를 받는다 — 같은 방식을 따른다.
- **payload·marker·receipt 어디에도 안 들어간다.** marker 는 `event_id`·`destination_ref`·
  `destination_sequence`·`payload_digest` 넷뿐이고 (001 `build_slack_marker`) 이 목록은
  안 바뀐다.
- 빈 값과 공백은 생성 시점에 거부한다. `SlackInstallationPolicy.__post_init__` 과 같은
  방향이다.
- `signing_secret` 을 여기에도 두는 이유는 **한 곳에서 읽기** 위해서다. 두 값이 서로 다른
  경로로 들어오면 부분 구성이 조용히 통과한다.

## SlackApiEndpoint (신규, 상수)

| 이름 | 값 | 근거 |
|---|---|---|
| `chat.postMessage` | `https://slack.com/api/chat.postMessage` | research P1 |
| `conversations.history` | `https://slack.com/api/conversations.history` | research P2 |

**미확정**: base URL 을 주입 가능하게 할지. wave 5 의 fake server test 가 그것을 요구하면
생성자 인자로 연다. 그 판단은 구현이 한다.

## SlackHttpResponse (신규, 내부)

`urllib` 응답을 분류에 넘기기 전에 정규화한 값이다. **Protocol 에 노출하지 않는다** —
`SlackTransport` 의 반환형은 001 C-1 이 정한 `SlackSendResult`·`SlackHistoryPage` 그대로다.

| 필드 | 형 | 설명 |
|---|---|---|
| `status_code` | `int` | HTTP status |
| `body` | `Mapping[str, object]` | 파싱된 JSON |
| `retry_after_seconds` | `int \| None` | `Retry-After` header. **backoff 에 주입하지 않는다** (R-007) |

**규칙**

- Slack 은 application error 를 **HTTP 200 + `ok: false`** 로 준다 (001 research R-006).
  그래서 `status_code` 만 보고 성공을 판정하면 안 된다. `body["ok"]` 가 1차 기준이다.
- `ok: false` 면 `body["error"]` 를 `SlackTransportError.error_code` 에 그대로 싣는다.
  정규화는 그 생성자가 한다 (001 `SlackTransportError`).
- JSON 이 아닌 응답 — proxy·WAF 가 HTML 을 줄 수 있다. 파싱 실패는 **error code 없는**
  `SlackTransportError` 가 되어 retryable 로 분류된다 (001 C-3 규칙 2). 그 방향이 맞다.

## Environment Variables (신규)

이름은 `contracts/slack-http-transport.md` 가 확정한다. 여기서는 **성질**만 적는다.

- 개수는 최소로 한다. 값 하나에 변수 하나다.
- 읽는 지점은 **entrypoint 하나뿐**이다 (R-014). core 는 계속 주입만 받는다.
- 부재와 빈 문자열을 같게 다룬다 — 둘 다 "구성 안 됨" 이다. E2E 는 그때 skip 한다 (P-003).

## What Does Not Change

명시적으로 적는다. 이것들이 바뀌면 Package 3 의 gate 증거가 무효가 된다.

| 대상 | 상태 |
|---|---|
| `governance_outbox_events`·`governance_outbox_destinations` schema | 불변 |
| `OutboxEventView` | 불변 |
| marker 형식 (`event_type` + 네 필드) | 불변 |
| receipt 형식 `slack:{channel}:{ts}` | 불변 |
| `SlackTransport` Protocol signature | 불변 (001 C-1) |
| `SlackProjectionDestination` | 불변 |
| runtime dependency 셋 | 불변 (R-013) |
