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

**adapter는 Slack event claim 전에 자기가 보낸 message를 되읽어 marker가 복원되는지
확인한다** (FR-018, 001 D-024 항목 2). Production wiring 증거가 없으면 이 계약은 완료가
아니다.

절차:

1. Durable lifecycle state를 읽고 schema version, required field와 closed state enum을 검증한다.
   Malformed/unknown/unsupported record는 기존 row를 수정하지 않고
   `LIFECYCLE_STATE_INVALID`를 반환한다. Readback, post와 activation은 0회다.
2. 이전 `CLEANUP_PENDING`에 confirmed `channel + ts`가 있으면 그 identity로 exact delete를
   수행하고 durable `RESOLVED`를 commit한다. 둘 다 성공한 뒤에만 fresh self-check로 간다.
   Cleanup 또는 commit 실패면 원인 state를 보존하고 `HARD_BLOCKED_NO_POST`를 반환한다.
3. 그 밖의 unresolved/unknown state면 원인 state를 보존한 채
   `HARD_BLOCKED_NO_POST`를 반환한다.
4. Channel/app별 local probe intent를 atomic claim으로 commit한다. Concurrent startup 중
   claim 승자 하나만 다음 단계로 간다. 나머지는 post 없이 `HARD_BLOCKED_NO_POST`다.
5. 대상 채널에 marker를 단 message를 하나 보낸다 (probe).
6. 성공 응답의 `channel + ts`를 lifecycle state에 저장한다. 이 commit이 실패하면 현재
   process의 confirmed identity로 exact delete를 최대 한 번 시도하고 durable `RESOLVED`를
   기록한다. 둘 다 성공한 후속 startup만 fresh claim할 수 있다.
7. `conversations.history` 를 `include_all_metadata=true` 로 조회한다.
8. 방금 받은 `ts` 로 그 message 를 찾고, `app_id` 와 `metadata` 네 필드가 모두 복원되는지
   본다. **위치로 찾지 않는다** — 채널이 조용하다고 가정하지 않는다.
9. 성공·실패 양쪽에서 exact `channel + ts`로 삭제를 시도한다.
10. 삭제 성공은 lifecycle의 durable `RESOLVED` commit까지 성공해야 완료다. Commit 실패는
   `HARD_BLOCKED_NO_POST`이며 worker activation을 거부한다.
11. Typed outcome을 composition root에 반환한다. 이 계층은 logging하지 않는다.

**이것이 잡는 것**은 destination 안에서 판정할 수 없는 것들이다 — 값이 틀린 `app_id`,
`metadata` 를 안 옮기는 구현, `event_type` 개명, 그리고 H-1.1 의 scope 부족. 넷 다 없으면
결과가 hold 가 아니라 **조용한 중복 Card** 다.

### H-3.1 — The Probe Must Not Belong To Any Destination

**probe 의 `destination_ref` 는 `PROBE_DESTINATION_REF` 여야 한다.** 진짜 destination 의
값을 쓰면 **보내기 전에** 거부한다 (2026-08-08, D-029).

probe 는 진짜 채널에 우리 `event_type` 과 `app_id` 를 달고 남는다. 그 상태로 진짜 marker 와
같은 `destination_ref` 를 달면 `reconcile` 이 둘을 구분하지 못한다. 결과가 둘 다 나쁘다 —
wave 6 failure-recovery review 가 실측했다.

- probe 를 event N 의 marker 로 만들면, N 의 재시도에서 `reconcile` 이 probe 를 N 의 Card 로
  읽는다. **Card 는 한 장도 안 나갔는데 DELIVERED 로 기록된다.**
- probe 의 sequence 가 더 낮으면 "하위 sequence 를 먼저 만났다 → 미전송" 판정이 돈다.
  probe 는 재시작마다 새로 올라가므로 **오래된 sequence 를 가진 가장 새로운 message** 이고,
  이것이 `reconcile` 이 기대는 순서 불변을 정확히 깬다. 결과는 **중복 Card** 다.

`_our_marker` 가 `destination_ref` 불일치 marker 를 두 loop 모두에서 배제하므로
(`slack_projection.py:708`) 이 값 하나로 probe 가 `reconcile` 에 안 보인다. 나머지는 진짜와
같은 모양을 유지하므로 "실제로 나가는 것과 같은 것을 검사한다" 는 근거도 산다.

**충돌할 수 없다.** 진짜 값은 `provider:{provider}:{sha256 hexdigest}` 이고
(`events.py:1442`) hexdigest 는 소문자 hex 64자다. `PROBE_DESTINATION_REF` 의 접미는 hex 가
아니다. **길이는 진짜와 맞춘다** — 짧게 두면 probe 의 metadata 가 진짜보다 작아지고,
metadata 크기 상한이 두 값 사이에 있으면 자가검사는 통과하는데 첫 진짜 Card 가
`metadata_too_large` 로 terminal 이 된다. 그 상한은 아직 모른다 (OQ-003).

**호출자가 손으로 조립하지 않는다.** `build_probe_marker(event)` 가 `build_slack_marker` 의
결과에서 `destination_ref` 만 바꿔 준다. round 1 의 P0 가 "안내문으로 요구하기" 에서 나왔다.
보내기 전 거부 guard 는 그대로 둔다 — 두 겹이다.

### H-3.2 — Cleanup Is Attempted On Both Paths

자가검사는 성공·실패 양쪽에서 confirmed `channel + ts`로 probe 삭제를 시도한다. 이 부분은
D-030을 유지한다.

Probe는 `conversations.history` 조회 예산을 먹는다. `reconcile`은 최대 4995건을 훑고,
넘으면 판정 불가와 operator hold로 간다. Cleanup을 생략하지 않는다.

Outcome은 다음과 같다.

| Readback | Cleanup | Outcome | Worker activation |
|---|---|---|---:|
| 성공 | 성공 | `READY` | 허용 |
| 성공 | 실패 | `DEGRADED_CLEANUP` | 허용 |
| 실패 | 성공 | typed readback failure | 거부 |
| 실패 | 실패 | typed readback failure + secondary cleanup data | 거부 |

D-031은 D-030의 "readback 성공 + cleanup 실패도 startup 거부"만 supersede한다. Readback과
cleanup이 함께 실패하면 readback 원인이 primary다.

Primary failure는 closed enum이다.

- `PROBE_INPUT_INVALID` — post 전 input/isolation failure; cleanup 비적용
- `HISTORY_READ_FAILED`
- `PROBE_NOT_FOUND`
- `APP_ID_MISMATCH`
- `MARKER_UNREADABLE`
- `MARKER_MISMATCH`
- `RESPONSE_CHANNEL_MISMATCH`

Post 뒤 여섯 failure는 cleanup 성공·실패 양쪽과 조합한다. `RESPONSE_CHANNEL_MISMATCH`는
Slack 성공 응답의 confirmed channel이 configured target과 다를 때 사용한다. 이 경우 configured
channel history를 조회하지 않고 cleanup은 provider-confirmed `channel + ts`로만 시도한다.
Cleanup 성공·실패 양쪽에서 primary code를 보존한다. Cleanup failure는 optional immutable
`CleanupFailureDetail` 하나로만 전달하고 `cleanup_failure_count`와 allowlisted
`provider_error_code`만 가진다. Primary code를 감싸거나 바꾸지 않는다.

Slack cleanup 성공 뒤 durable `RESOLVED` commit이 실패하면 위 4행 matrix의 `READY` 또는
`DEGRADED_CLEANUP`을 사용하지 않는다. 결과는 `HARD_BLOCKED_NO_POST`이며 worker activation은
거부다. Remote 삭제 성공만으로 lifecycle 완료를 추정하지 않는다.

### H-3.3 — Durable State Precedes Network

Network post 전에 `GovernanceStore`의 전용 lifecycle state에 local probe intent를 commit한다.
Channel/app별 unresolved intent는 최대 1개다.

- unresolved state가 없을 때만 atomic claim으로 새 intent를 만든다
- 같은 channel/app의 concurrent startup은 claim 승자 하나만 post한다
- claim을 확보하지 못한 startup은 post 0회와 `HARD_BLOCKED_NO_POST`다
- state read/write/commit 실패는 `HARD_BLOCKED_NO_POST`다
- post 성공 응답을 받은 뒤 `channel + ts`를 저장한다
- stored `channel + ts`가 있는 cleanup만 exact recovery 가능하다
- 이전 `CLEANUP_PENDING`은 exact delete와 durable `RESOLVED` commit을 먼저 완료한다
- 그 recovery가 완료된 뒤에만 fresh atomic claim과 새 self-check post를 허용한다
- 그 밖의 unresolved state 또는 recovery 실패가 있으면 다음 startup은 새 probe를 보내지 않는다
- delete 성공 뒤 `RESOLVED` commit이 실패하면 마지막 committed state를 유지하고 worker를
  활성화하지 않는다
- intent commit 전 crash는 post 0회이며 다음 startup의 fresh claim을 막지 않는다
- 다음 startup이 기존 `POST_INTENT_RECORDED`를 발견하면 post 여부를 추정하지 않고 hard block한다
- response loss는 가능한 경우 `AMBIGUOUS_POST`를 commit하며 실패하면 마지막 intent를 보존한다
- success response 뒤 identity commit 실패는 current process의 confirmed identity로 exact delete
  1회와 durable resolution을 시도한다. 둘 다 성공한 뒤의 후속 startup만 fresh claim한다

이 state는 outbox event, ingress command, operator hold, Markdown knowledge가 아니다. 전용
runtime lifecycle이다 (R-018).

### H-3.4 — Ambiguous Post Is Not Recovered By Guessing

Post intent commit 뒤 Slack 응답을 잃으면 `AMBIGUOUS_POST`다. `ts`를 받았다고 가정하지
않는다. 이후 startup은 durable state를 `AMBIGUOUS_POST`로 보존하고 startup outcome으로
`HARD_BLOCKED_NO_POST`를 반환하며 post 호출을 0회 수행한다.

금지하는 recovery key:

- message text 또는 prefix
- shared `PROBE_DESTINATION_REF`
- metadata/app marker scan
- history 위치 또는 최근 N건
- timestamp window
- search result 후보
- undocumented `client_msg_id`

Slack 공식 contract는 caller-controlled pre-response identity와 exact lookup을 제공하지 않는다
(R-017). 따라서 ambiguous message automatic cleanup과 provider-side global cap은
`BLOCKED`다. Operator recovery mutation은 별도 governed recovery work item 승인 전에는
추가하지 않는다. T013에서 강제 해제로 blocker를 우회하지 않는다.

### H-3.5 — Outcome And Diagnostic Ownership

Readback/lifecycle 계층은 typed outcome과 safe diagnostic data만 반환한다. Logging과 stderr
output은 금지한다.

`HARD_BLOCKED_NO_POST`는 persisted lifecycle state가 아닌 startup outcome이다. Durable
state에는 `POST_INTENT_RECORDED`, `AMBIGUOUS_POST`, `CLEANUP_PENDING` 같은 원인을 보존한다.

Production composition root 한 곳만 각 startup 평가에서
`SLACK_PROBE_CLEANUP_DEGRADED`를 정확히 한 번 출력한다. Process 재기동은 새 startup
평가이므로 degraded outcome이면 다시 한 번 출력한다.

허용 field는 `diagnostic_code`, `channel_id`, `app_id`, `probe_id`,
`cleanup_failure_count`, `provider_error_code`, `operator_action`과 confirmed `message_ts`다.
`operator_action`은 stable action code다. 목록은 닫혀 있다. 확인되지 않은 `message_ts`,
credential, HTTP body, request header, exception `repr`과 그 밖의 field는 금지한다.

원본 exception chaining, 자유형 error와 raw provider data는 structured log, exception output,
metric, persisted failure diagnostic과 operator output 전부에서 금지한다. Metric label은
`outcome`, `diagnostic_code`, allowlisted `provider_error_code`만 허용한다.

Production composition root가 현재 없으므로 T013과 FR-018 production completion은
`BLOCKED`다 (R-019). `index.yaml`의 `W3-transport-readback-selfcheck`는 T008 unit evidence와
T013 production evidence가 모두 있어야 닫힌다.

### H-3.6 — Production Evidence Has Two Parts

T013 `ready` 전 E-7~E-10의 FR-037 named evidence와 user approval record가 manifest에 연결돼야
한다. T013 실행은 T010 completion 뒤에만 가능하다. 완료 evidence는 같은 revision의 다음 둘이다.

1. Approved production entrypoint를 instrumented dependency로 실행한 composition-root
   integration result. Outcome이 first claim보다 앞서고 acceptance count matrix를 만족한다.
2. 실제 production channel/app/`HttpSlackTransport` 구성으로 outcome을 claim 전에 소비한 safe
   configured startup trace.

Unit command output만 있거나 configured trace만 있으면 충분하지 않다. Trace field와 금지값은
`data-model.md`의 `StartupWiringEvidence`가 권위다.

E-8 review가 provider exact recovery 미지원을 확인해도 E-9가 승인되면 local fail-closed T013
구현을 막지 않는다. Automatic ambiguous-message cleanup과 provider-side global cap만 계속
blocked다.

## H-4 — Credential Path

### H-4.1 — One Read Point

환경변수를 **entrypoint 한 곳에서만** 읽어 `SecretStr` 로 감싼다. core 는 계속 주입만
받는다 (R-014). repo 전체에 Slack 설정을 읽는 `os.environ` 이 지금 한 줄도 없고, 그 구조가
test 가능성을 만들었으므로 유지한다.

**변수 이름은 T006·T007 구현이 확정했고, T009 가 대상 둘을 더했다** (2026-08-08, D-028).

| 변수 | 값 | secret |
|---|---|---|
| `AMPLAI_SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-` 로 시작) | 예 |
| `AMPLAI_SLACK_SIGNING_SECRET` | Signing Secret | 예 |
| `AMPLAI_SLACK_APP_ID` | app ID (`A` 로 시작) | 아니오 |
| `AMPLAI_SLACK_CHANNEL_ID` | 대상 channel ID (`C` 로 시작) | 아니오 |

`AMPLAI_` 접두는 같은 환경에 있는 다른 Slack 도구와 섞이지 않게 한다.

`app_id` 를 환경에서 받는 이유는 `reconcile` 이 이 값으로 남의 message 를 배제하기
때문이다 (`slack_projection.py:495`). **`auth.test` 가 주는 `bot_id` 는 다른 값이라 대체할 수
없다.** 값이 틀린 경우는 형식 검사로 못 잡고 H-3 의 자가검사가 기동 시점에 잡는다 — 그래서
loader 는 접두 문자를 **검사하지 않는다.** 확인하지 않은 형식 가정을 계약으로 굳히지 않는다.

성질 셋을 지킨다.

- 값 하나에 변수 하나. 합쳐 담지 않는다
- 부재와 빈 문자열을 같게 다룬다 — 둘 다 "구성 안 됨"
- 읽는 지점이 하나. `load_slack_settings` 는 `environ` 을 **요구한다** — 기본값을 주면
  `os.environ` 을 만지는 지점이 둘이 된다. 부르는 쪽이 넘긴다

**부분 구성은 미구성이 아니다.** 넷 중 **일부만** 빠지면 `ValueError` 이고 빠진 변수
**이름**을 전부 적는다. 그 판정을 `load_slack_credentials` 에 위임하지 않는다 — 위임하면
credential 이 부분일 때 그 함수가 먼저 터져서 대상 둘의 누락이 message 에 안 실리고,
고치고 다시 돌려야 나머지를 본다 (wave 6 review round 1). `None` 으로 뭉뚱그리면 token 만 넣고 E2E 를 돌린 사람이 "skip" 만
보고 자기가 뭘 빠뜨렸는지 모른다. 조용한 skip 은 조용한 pass 만큼 나쁘다.

secret 둘은 `SlackCredentials` 에, 대상 둘은 그것을 감싸는 `SlackSettings` 에 담는다.
나눠 두면 `repr=False` 같은 secret 처리 규칙이 secret 에만 붙는다.

### H-4.2 — Secrets Never Persist

- `SecretStr` + `repr=False`. `slack.py` 의 `signing_secret` 이 이미 그 처리를 받는다
- payload·marker·receipt·log·DB·Audit 어디에도 안 들어간다
- 예외 메시지에도 안 들어간다. `SlackTransportError` 의 message 를 만들 때 요청 header 를
  넣지 않는다
- **평문을 이름 있는 local 에 담지 않는다.** 값은 임시식으로만 존재하고 곧바로 `SecretStr`
  이 된다. frame local 을 찍는 도구(`pytest --showlocals`, 예외 보고기)가 그 이름을 그대로
  출력한다 (2026-08-08, wave 6 review)
- **`os.environ` 을 받은 frame 이 traceback 에 남지 않게 한다.** 남으면 그 local 의 repr 이
  환경 전체를 출력하고 token 이 거기 들어 있다. E2E fixture 는 loader 의 `ValueError` 를
  잡아 message 만 꺼내 `pytest.fail` 로 바꾼다
- **자식 process 에 진짜 credential 을 넘기지 않는다.** `env=` 로 넘긴 mapping 은
  `subprocess.run` **자신의 local** 에 담기므로, 그것이 던지면 부르는 쪽이 아무리 깨끗해도
  `--showlocals` 가 그 안의 token 을 찍는다. 네 변수를 빼고 넘기고, 값이 필요한 test 는
  자기가 만든 가짜 값을 넣는다

## H-5 — E2E Harness

### H-5.1 — Outside The Default Suite

E2E 는 marker 로 분리하고 기본 실행에서 deselect 한다. `amplai-foundry verify` 는
건드리지 않는다 — 그것은 clean clone·offline 에서 도는 계약이다 (SC-014, FR-020).

**T009 가 확정한 이름과 방법이다** (2026-08-08).

- marker 이름은 `slack_e2e` 다. `pyproject.toml` 의 `markers` 에 등록해 unknown mark 경고와
  오타 난 marker 가 조용히 아무것도 고르지 않는 상태를 막는다
- `addopts = "-q -m \"not slack_e2e\""` 가 기본 실행에서 뺀다. `verify` 는 인자 없이
  `python -m pytest` 를 부르므로 (`verification/runner.py:37`) 이 한 줄이 verify 를 offline
  으로 유지한다. verify 에 stage 를 더하지 않는다
- 명령줄의 `-m` 이 `addopts` 를 이긴다. `python -m pytest -m slack_e2e` 로 E2E 만 돌린다

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
- 기존 outbox/ingress/hold schema 재사용
- D-031 전용 lifecycle 이외의 durable schema 변경
- marker 형식·receipt 형식 변경
- history/text/prefix/time 기반 probe recovery
- readback/lifecycle 계층의 logging
- `Retry-After` 를 dispatcher backoff 에 주입 (R-007, 별도 item)
- app 배포 (H-1.2)
