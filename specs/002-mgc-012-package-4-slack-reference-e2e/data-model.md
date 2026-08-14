# Data Model: MGC-012 Package 4

Package 4의 기존 transport model은 유지한다. D-031은 probe lifecycle만 위한 additive durable
state를 추가한다. `OutboxEventView`, marker 형식, receipt 형식은 바꾸지 않는다.

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

Wave 5 구현은 fake server 검증을 위해 `base_url` 생성자 주입을 열었고, 기본값은 실제 Slack
API base다. Core contract의 endpoint 의미는 바뀌지 않는다.

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

## ReadbackOutcome (D-031)

Readback 판정 계층이 composition root에 넘기는 immutable result다. 직접 logging하지 않는다.

| 상태 | Worker activation | 의미 |
|---|---:|---|
| `READY` | 허용 | Readback과 cleanup이 모두 성공 |
| `DEGRADED_CLEANUP` | 허용 | Readback은 성공했고 exact cleanup만 실패 |
| typed hard failure | 거부 | Readback 또는 필수 state operation 실패 |
| `HARD_BLOCKED_NO_POST` | 거부 | Unresolved/ambiguous lifecycle 또는 필수 recovery 실패로 post 금지 |

`HARD_BLOCKED_NO_POST`는 startup outcome이며 `ProbeCleanupLifecycle.state`에 저장하지 않는다.
Durable state에는 그 판정을 만든 원인 state를 보존한다.

| 필드 | 규칙 |
|---|---|
| `status` | 위 네 outcome class 중 하나 |
| `primary_failure_code` | 실패 outcome이면 closed `ReadbackFailureCode` 또는 typed lifecycle code, 아니면 null |
| `secondary_cleanup_failure` | `CleanupFailureDetail \| None`, cardinality 0..1 |
| `diagnostic_data` | 아래 safe field allowlist를 만족하는 immutable data |

### ReadbackFailureCode

| Code | 경계 |
|---|---|
| `PROBE_INPUT_INVALID` | Network 전 probe marker/input 또는 isolation contract 위반. Cleanup 비적용 |
| `HISTORY_READ_FAILED` | Post 뒤 history transport/provider failure |
| `PROBE_NOT_FOUND` | Confirmed `message_ts`가 history 결과에 없음 |
| `APP_ID_MISMATCH` | `app_id` 누락 또는 expected ID와 불일치 |
| `MARKER_UNREADABLE` | Metadata/event payload 구조 복원 불가 |
| `MARKER_MISMATCH` | 복원된 event type 또는 payload value 불일치 |
| `RESPONSE_CHANNEL_MISMATCH` | Slack 성공 응답의 confirmed channel이 configured target과 다름. Configured history는 조회하지 않고 confirmed `channel + ts`로만 cleanup |

목록은 closed enum이다. Provider error code는 이 code를 대체하지 않는다.

### CleanupFailureDetail

| 필드 | 규칙 |
|---|---|
| `cleanup_failure_count` | 1 이상의 정수 |
| `provider_error_code` | allowlisted safe provider code 또는 null |

Immutable value이며 outcome당 최대 하나다. Free-form message, raw response/header와 exception
object/cause를 저장하지 않는다.

**Safe diagnostic fields**

- `diagnostic_code`
- `channel_id`
- `app_id`
- local `probe_id`
- confirmed Slack `message_ts`
- `cleanup_failure_count`
- allowlisted `provider_error_code`
- stable `operator_action`

목록은 닫혀 있다. 확인되지 않은 `message_ts`, credential, HTTP body, request header,
exception `repr`과 그 밖의 field는 포함하지 않는다.

Structured log, exception output, persisted failure diagnostic과 operator output은 각 계약의
closed allowlist만 사용한다. Metrics label은 `outcome`, `diagnostic_code`, allowlisted
`provider_error_code`만 허용하고 channel/app/probe/message identity는 label로 사용하지 않는다.

## ProbeIdentity (D-031)

| 필드 | 필수 | 설명 |
|---|---:|---|
| `probe_id` | 예 | Network 전에 생성·저장하는 local explicit identity |
| `app_id` | 예 | Slack installation/app scope |
| `channel_id` | 예 | Startup self-check 대상의 stable Slack ID |
| `message_ts` | 아니오 | Slack 성공 응답 뒤에만 저장하는 provider identity |

`probe_id`는 local lifecycle identity다. Slack message identity라고 주장하지 않는다.
`message_ts`가 없으면 history scan으로 채우지 않는다.

## ProbeCleanupLifecycle (D-031)

`GovernanceStore`의 narrow operational state다. Markdown canonical knowledge가 아니다.

| 필드 | 규칙 |
|---|---|
| `schema_version` | 현재 구현이 지원하는 exact lifecycle record version |
| `probe_id` | Primary identity |
| `app_id`, `channel_id` | Unresolved uniqueness scope |
| `state` | 아래 transition만 허용 |
| `message_ts` | `POST_CONFIRMED` 이후만 존재 |
| `readback_status` | `UNKNOWN`, `PASSED`, `FAILED` |
| `cleanup_attempts` | 0 이상 |
| `last_error_code` | allowlisted safe provider code 또는 null |
| `created_at`, `updated_at`, `resolved_at` | UTC timestamp. `resolved_at`은 terminal state만 |

### State Transitions

```text
none
  → POST_INTENT_RECORDED
      → POST_CONFIRMED
          → RESOLVED
          → CLEANUP_PENDING
      → AMBIGUOUS_POST
      → RESOLVED  # current-process confirmed identity compensation only

CLEANUP_PENDING
  → RESOLVED
```

Rules:

- `POST_INTENT_RECORDED`를 commit하기 전에는 network post를 호출하지 않는다.
- Record decode 전에 `schema_version`과 closed state enum을 검증한다. Malformed field, unknown
  state와 unsupported version은 `LIFECYCLE_STATE_INVALID` typed failure이며 row를 수정하지 않는다.
- Channel/app별 non-resolved row는 최대 1개다. 이 constraint와 intent insert는 같은 atomic
  claim에 속한다.
- Concurrent startup 중 atomic claim 승자 하나만 post한다. 나머지는 post 0회와
  `HARD_BLOCKED_NO_POST`를 반환한다.
- `message_ts`가 없는 state는 automatic remote lookup 또는 delete를 하지 않는다.
- `CLEANUP_PENDING`은 stored `channel + ts`로만 recovery한다. Exact delete와 durable
  `RESOLVED` commit이 모두 성공한 뒤에만 같은 startup이 fresh self-check를 시작한다.
- `CLEANUP_PENDING` cleanup 또는 resolution commit이 실패하면 마지막 committed cause state를
  유지하고 startup outcome으로 `HARD_BLOCKED_NO_POST`를 반환한다. 새 intent와 post는 없다.
- `POST_INTENT_RECORDED`와 `AMBIGUOUS_POST`도 원인 state를 그대로 보존한다.
  `HARD_BLOCKED_NO_POST`로 state transition하지 않는다.
- State read/write/commit ambiguity는 "clear"로 취급하지 않는다. Post하지 않는다.
- `RESOLVED` transition은 confirmed cleanup과 durable commit이 모두 성공한 뒤에만 가능하다.
- Slack delete 성공 뒤 `RESOLVED` commit이 실패하면 마지막 committed state를 유지한다.
  현재 startup은 `HARD_BLOCKED_NO_POST`이며 worker를 활성화하지 않는다.
- Slack 성공 응답 뒤 confirmed identity commit이 실패한 현재 process만 in-memory
  `channel + ts`로 exact delete를 최대 한 번 시도할 수 있다. Delete와 durable resolution이
  모두 성공하면 `POST_INTENT_RECORDED → RESOLVED` compensating transition을 허용한다. 이
  transition은 confirmed response와 exact delete success evidence가 같은 call context에 있을
  때만 가능하며 다음 startup은 사용할 수 없다.

## StartupWiringEvidence (T013)

T013의 runtime domain entity가 아니라 gate artifact schema다. 두 artifact는 같은 code revision을
가리킨다.

| Artifact | 필수 내용 |
|---|---|
| `CompositionRootIntegrationResult` | revision, approved entrypoint ID, outcome case, ordered observation, post/claim/activation/diagnostic count, command result |
| `ConfiguredStartupTrace` | revision, entrypoint ID, startup evaluation ID, confirmed channel/app ID, outcome, ordered observation name, post/claim/activation/diagnostic count |

Configured trace는 credential, HTTP body/header, exception text와 message content를 포함하지
않는다. 두 artifact 모두 T013 manifest와 Package 4 checkpoint에 연결돼야 한다.

## OperatorDiagnostic (D-031)

Composition root가 `DEGRADED_CLEANUP` outcome을 받아 각 startup 평가에서 한 번 만드는
output이다. Process 재기동은 새 startup 평가다.

| 필드 | 값 |
|---|---|
| `diagnostic_code` | `SLACK_PROBE_CLEANUP_DEGRADED` |
| `channel_id` | Startup 대상의 stable Slack ID |
| `app_id` | Slack installation/app ID |
| `probe_id` | Local lifecycle ID |
| `message_ts` | Slack이 확인한 경우만 존재 |
| `cleanup_failure_count` | 0 이상 |
| `provider_error_code` | safe allowlist |
| `operator_action` | stable action code |

이 표는 닫힌 allowlist다. Readback/lifecycle service는 이 entity를 persistence하거나 출력하지
않는다.

## What Does Not Change

명시적으로 적는다. 이것들이 바뀌면 Package 3 의 gate 증거가 무효가 된다.

| 대상 | 상태 |
|---|---|
| `governance_outbox_events`·`governance_outbox_destinations` schema | 불변 |
| D-031 전용 probe lifecycle schema | additive migration 필요 (R-018) |
| `OutboxEventView` | 불변 |
| marker 형식 (`event_type` + 네 필드) | 불변 |
| receipt 형식 `slack:{channel}:{ts}` | 불변 |
| `SlackTransport` Protocol signature | 불변 (001 C-1) |
| `SlackProjectionDestination` | 불변 |
| runtime dependency 셋 | 불변 (R-013) |

## Blocked Design Boundary

Slack은 response loss 뒤 exact message lookup을 위한 documented caller-controlled identity를
제공하지 않는다 (R-017). `AMBIGUOUS_POST`는 durable하게 막을 수 있지만 자동 cleanup할 수
없다. Production composition root도 없다 (R-019).

따라서 이 data model은 T013의 안전 경계를 정의하지만 구현 가능 상태를 주장하지 않는다.
Operator recovery transition은 별도 governed recovery work item 승인 전까지 정의하지 않는다.
