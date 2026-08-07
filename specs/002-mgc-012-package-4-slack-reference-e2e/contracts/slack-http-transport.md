# Contract: Real Slack HTTP Transport And Credential Path

**이 문서는 `SlackTransport` Protocol 을 다시 쓰지 않는다.** Protocol 의 signature, 반환형,
그리고 구현체 의무 둘은 `specs/001-mgc-012-slack-reference-adapter/contracts/slack-transport.md`
C-1 이 권위다. 여기서는 그 계약을 **실제 HTTP 로 만족시키는 쪽의 의무**만 새로 쓴다.

D-027 이 이 참조 관계를 정했다 — 같은 계약을 두 디렉터리로 쪼개지 않는다.

## H-1 — Scopes And App Shape

### H-1.1 — Required Bot Token Scopes

| scope | 필요한 이유 | 근거 |
|---|---|---|
| `chat:write` | `chat.postMessage` | research P1 |
| `channels:history` | public channel 의 `conversations.history` | research P2 |
| `groups:history` | private channel 을 destination 으로 쓸 때만 | research P2 |

**`chat:write.public` 은 주지 않는다.** app 을 대상 channel 에 초대해 쓴다. 이유 없이 넓은
권한을 갖지 않는다.

**scope 가 모자라면 조용히 실패하지 않는다.** `chat:write` 만 있는 설치는 send 가 성공하고
**첫 재시도의 read 에서** `missing_scope` 를 받는다. 그 code 는 allowlist 밖이라 terminal 이고
되돌릴 수 없는 hold 를 만든다 (001 C-3). H-3 의 자가검사가 이것을 기동 시점으로 앞당긴다.

### H-1.2 — The App Must Stay Internal

**app 을 배포하지 않는다.** 자기 workspace 에만 설치한다.

비-Marketplace 로 배포하면 `conversations.history` 가 **분당 1회, 요청당 15건**이 된다
(research P4). 우리 reconcile 은 page 당 999건으로 최대 5 page 를 훑으므로 그 한도에서
**설계가 성립하지 않는다** — 한 번의 reconcile 이 최소 5분이라 C-1 의무 2 (호출 합계를
`lease_seconds` 안에) 를 만족할 방법이 없다.

internal app 은 면제다 (research P5). 이 제약은 배포 형태를 정할 때 다시 봐야 한다.

## H-2 — HTTP Implementation Obligations

001 C-1 의 의무 둘을 실제 구현이 어떻게 지키는지 규정한다.

### H-2.1 — Every Failure Becomes `SlackTransportError`

`urllib` 의 예외 표면이 넓다. **아래를 모두 감싼다.**

| 예외 | 처리 |
|---|---|
| `urllib.error.HTTPError` | `status_code` 를 싣는다. body 가 JSON 이면 `error` 도 싣는다 |
| `urllib.error.URLError` | code 없음. `transport_exception` 에 원인 class 이름 |
| `socket.timeout` / `TimeoutError` | code 없음 |
| `ssl.SSLError` | code 없음 |
| JSON 파싱 실패 | code 없음 |
| 그 밖의 모든 예외 | code 없음 |

**code 를 비우는 것이 의도다.** code 없는 실패는 001 C-3 규칙 2 로 **retryable** 이 되고
그쪽이 보수적이다 (D-020 항목 1). code 를 채우면 allowlist 밖이라 terminal 이 되어 구현
결함을 되돌릴 수 없는 hold 로 바꾼다.

`SlackProjectionDestination._rewrap` 이 두 번째 방어선으로 이미 있지만 (001), 그것에
기대지 않는다. C-1 의무 1 은 transport 쪽 의무다.

### H-2.2 — `ok: false` Is The Primary Signal

Slack 은 application error 를 **HTTP 200 + `ok: false`** 로 준다.

1. 응답 body 를 JSON 으로 파싱한다.
2. `body["ok"]` 가 참이면 성공이다.
3. 거짓이면 `body["error"]` 를 `error_code` 로 싣고 `SlackTransportError` 를 던진다.
4. HTTP status 도 함께 싣는다 — 429 판정에 쓰인다 (001 C-3 규칙 1).

**status code 만 보고 성공을 판정하지 않는다.** 200 이어도 실패일 수 있다.

### H-2.3 — Deadline Covers The Whole `deliver_next`

001 C-1 의무 2 는 호출 시간을 `OutboxConfig.lease_seconds` 보다 짧게 묶으라고 한다. 기준은
**한 `deliver_next` 안의 모든 호출 합계**다 — `reconcile()` 이 최대
`max_history_pages` 번 `read_history` 를 부르고 그 뒤 `send()` 가 한 번 더 부른다
(D-024 항목 3, wave 4 review Advisory).

구현은 **호출 단위 timeout** 을 건다 (`urlopen(..., timeout=)`). 합계 보장은 호출 단위
timeout × 최대 호출 수로 계산해 구성 시점에 검증한다. 그 검증을 어디에 둘지는 구현이
정한다 — `OutboxConfig` 를 읽을 수 있는 지점이어야 한다.

### H-2.4 — `include_all_metadata` Is Mandatory

`read_history` 는 `include_all_metadata=true` 를 **반드시** 붙인다 (001 C-1.2, research P2).

안 붙이면 `event_type` 만 오고 `event_payload` 가 안 온다. 그러면 marker 를 하나도 못 읽고
history 소진이 미전송으로 판정되어 **매 재시도마다 Card 가 한 장씩 는다.** hold 가 아니라
중복이라 조용하다. 001 의 `SlackProjectionMetadataUnreadableError` 가 지문으로 일부를
잡지만 전부는 아니다.

### H-2.5 — Newest-First Is Slack's Guarantee, Not Ours To Reorder

`conversations.history` 는 최신 message 부터 돌려준다 (research P2, R-011). 구현은 그
순서를 **그대로 넘긴다.** 정렬하거나 뒤집지 않는다.

001 C-1.2 의 최신-우선 의무는 유지된다 — 이제 근거가 "미확인" 이 아니라 공식 문서다.

## H-3 — Readback Self-Check

**adapter 는 기동 전에 자기가 보낸 message 를 되읽어 marker 가 복원되는지 확인한다**
(FR-018, 001 D-024 항목 2).

절차:

1. test 채널에 marker 를 단 message 를 하나 보낸다.
2. `conversations.history` 를 `include_all_metadata=true` 로 조회한다.
3. 그 message 의 `app_id` 와 `metadata` 네 필드가 모두 복원되는지 본다.
4. 하나라도 안 맞으면 **기동을 거부한다.**

**이것이 잡는 것**은 destination 안에서 판정할 수 없는 것들이다 — 값이 틀린 `app_id`,
`metadata` 를 안 옮기는 구현, `event_type` 개명, 그리고 H-1.1 의 scope 부족. 셋 다 없으면
결과가 hold 가 아니라 **조용한 중복 Card** 다.

`index.yaml` 의 `W3-transport-readback-selfcheck` 가 이 항목이다.

## H-4 — Credential Path

### H-4.1 — One Read Point

환경변수를 **entrypoint 한 곳에서만** 읽어 `SecretStr` 로 감싼다. core 는 계속 주입만
받는다 (R-014). repo 전체에 Slack 설정을 읽는 `os.environ` 이 지금 한 줄도 없고, 그 구조가
test 가능성을 만들었으므로 유지한다.

**변수 이름은 구현이 정한다.** 이 계약이 요구하는 것은 성질 셋이다.

- 값 하나에 변수 하나. 합쳐 담지 않는다
- 부재와 빈 문자열을 같게 다룬다 — 둘 다 "구성 안 됨"
- 읽는 지점이 하나

### H-4.2 — Secrets Never Persist

- `SecretStr` + `repr=False`. `slack.py` 의 `signing_secret` 이 이미 그 처리를 받는다
- payload·marker·receipt·log·DB·Audit 어디에도 안 들어간다
- 예외 메시지에도 안 들어간다. `SlackTransportError` 의 message 를 만들 때 요청 header 를
  넣지 않는다

## H-5 — E2E Harness

### H-5.1 — Outside The Default Suite

E2E 는 marker 로 분리하고 기본 실행에서 deselect 한다. `amplai-foundry verify` 는
건드리지 않는다 — 그것은 clean clone·offline 에서 도는 계약이다 (SC-014, FR-020).

### H-5.2 — Missing Credentials Skip Loudly

credential 이 없으면 **skip 하고 그 이유를 출력한다** (FR-019, plan P-003).

- 조용히 pass 로 보고하지 않는다. Constitution III 위반이다
- 실패시키지도 않는다. clean clone 의 기본 suite 가 빨간색이 되면 개발자가 그 실패를
  일상적으로 무시하게 되어 진짜 회귀를 가린다
- pytest 의 skip 은 이유와 함께 출력되고 요약에 집계된다. 그것이 정직한 표현이다

### H-5.3 — The Channel Is Shared State

E2E 는 실제 message 를 쌓는다. 반복 실행하면 `conversations.history` 조회 범위
(`SLACK_MAX_HISTORY_PAGES=5`, page 당 999) 안에 과거 실행의 marker 가 남는다.

**같은 `event_id` 를 재사용하지 않는다.** 각 실행이 고유한 `event_id` 를 쓰면 과거 marker
가 남아 있어도 오판하지 않는다 — `reconcile` 은 `event_id` 일치를 먼저 본다 (001 C-2.2).

채널 정리 전략이 필요한지는 실측 후 판단한다.

## H-6 — What This Contract Forbids

- `SlackTransport` Protocol 의 signature·반환형 변경
- `slack_projection.py` 와 `slack.py` 수정
- runtime dependency 추가 (R-013)
- `amplai-foundry verify` 에 network stage 추가
- durable schema·marker 형식·receipt 형식 변경
- `Retry-After` 를 dispatcher backoff 에 주입 (R-007, 별도 item)
- app 배포 (H-1.2)
