# Phase 0 Research: MGC-012 Package 4

`spec.md`의 `U-001`~`U-009`를 조사한다. Slack 사실은 공식 문서로만 고정한다. 공식
계약이 없으면 `BLOCKED`로 남긴다. 추정으로 채우지 않는다.

## Sources

| # | URL | 조회일 |
|---|---|---|
| P1 | `https://docs.slack.dev/reference/methods/chat.postMessage` | 2026-08-07 |
| P2 | `https://docs.slack.dev/reference/methods/conversations.history` | 2026-08-07 |
| P3 | `https://docs.slack.dev/apis/web-api/rate-limits` | 2026-08-07 |
| P4 | `https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/` | 2026-08-07 |
| P5 | `https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/` | 2026-08-07 |
| P6 | `https://docs.slack.dev/apis/web-api/` | 2026-08-07 |
| P7 | `https://docs.slack.dev/reference/methods/chat.delete/` | 2026-08-11 |
| P8 | `https://docs.slack.dev/tools/node-slack-sdk/reference/web-api/type-aliases/ChatPostMessageArguments/` | 2026-08-11 |
| P9 | `https://github.com/slackapi/node-slack-sdk/blob/122865134ffa20ad080fcf447cfb93b7252da157/packages/web-api/src/types/response/ConversationsHistoryResponse.ts#L26-L33` | 2026-08-11 |
| P10 | `https://docs.slack.dev/tools/node-slack-sdk/reference/web-api/interfaces/GenericMessageEvent/` | 2026-08-11 |
| P11 | `https://docs.slack.dev/reference/methods/search.messages/` | 2026-08-11 |

001 의 `research.md` S1–S4 를 대체하지 않는다. 같은 문서를 다시 조회한 것은 P1–P3 이고,
아래 R-010 이 그 재조회에서 나온 **정정**이다.

## R-009 — OAuth Scopes Are Now Pinned (closes U-001)

**Decision**: 아래 scope 를 app 에 준다.

| 용도 | bot token scope | 근거 |
|---|---|---|
| `chat.postMessage` | `chat:write` | P1 |
| `conversations.history` (public channel) | `channels:history` | P2 |
| `conversations.history` (private channel) | `groups:history` | P2 |

**Rationale**: P2 는 channel 종류별로 scope 를 나눠 넷을 적는다 — `channels:history`,
`groups:history`, `im:history`, `mpim:history`. Proposal Card 는 channel 로 나가므로 앞의
둘만 필요하다. DM 과 group DM 은 이 feature 의 destination 이 아니다.

`chat:write.public` 은 **주지 않는다.** P1 이 "For posting to all public channels" 용도로
설명하는데, 우리는 app 을 대상 channel 에 초대해 쓴다. 넓은 권한을 이유 없이 갖지 않는다.

**이것이 `index.yaml` 의 `W3-history-oauth-scope` 를 닫는다.** 그 항목의 위험 서술 —
"`chat:write` 만 있는 배포는 send 는 성공하고 첫 재시도의 read 에서 `missing_scope` →
terminal → 되돌릴 수 없는 hold" — 는 정확했다. scope 가 method 별로 완전히 분리돼 있어
그 시나리오가 실재한다.

**Alternatives considered**: user token 을 쓰는 안. P1·P2 가 user token scope 도 지원하지만
governance Card 는 app 이 보내는 것이지 사람이 보내는 것이 아니다. `app_id` 로 우리 marker
를 가리는 `read_slack_marker` 의 판정이 user token 경로에서 성립하는지 확인 안 됐다.

## R-010 — `conversations.history` Is Tier 3, And 001 Cited The Wrong Source (correction)

**Decision**: `conversations.history` 는 **Tier 3 (분당 50+)** 다. 근거는 **P2** 다.

**001 의 `research.md` 를 정정한다.** 그 문서 61행은 이렇게 적는다.

> `conversations.history` 는 Web API Tier 2 (분당 20+ 요청)다 (S4).

**S4 (= P3, rate-limits 문서) 는 어느 method 가 어느 tier 인지 적지 않는다.** 이번에
직접 조회해 확인했다 — 그 페이지는 Tier 1~4 의 정의만 싣는다. tier 를 적는 것은 method
문서인 P2 이고, 거기 값은 "Tier 3: 50+ per minute" 다.

**같은 부류의 오류가 이미 한 번 있었다.** 001 research.md 228행이 기록한다 — "이 문서의
이전 판은 '30~60초가 흔하다' 를 S4 근거로 적었다. **그 문장은 S4 에 없다.**" 출처를 안
가리키는 문장을 출처에 붙이는 실수가 두 번째다.

**영향은 안전한 쪽이다.** `SLACK_MAX_HISTORY_PAGES = 5` 의 근거 주석이
"최악 5회 호출은 Tier 2 예산의 4분의 1이다" 인데 (`slack_projection.py`), 실제 예산이 더
크므로 **5는 필요보다 보수적이다.** 값을 바꿀 이유는 없다. 고칠 것은 근거 문장이다.

**Alternatives considered**: 001 의 research.md 를 직접 수정하는 안. 그 문서는 Package 3
gate 증거라 이 wave 에서 손대지 않고, 여기에 정정을 적고 001 에 forward pointer 만 단다.

## R-011 — History Returns Newest First (closes U-005)

**Decision**: `conversations.history` 는 최신 message 부터 돌려준다. 근거는 P2 의
"The most recent messages in the time range are returned first" 다.

**이것이 `index.yaml` 의 `W3-history-ordering-unverified` 를 닫는다.** 001 research.md
130행이 "확인하지 못했다" 로 남긴 것이 확인됐다. `contracts/slack-transport.md` C-1.2 가
transport 에 요구하던 최신-우선 정렬 의무는 **Slack 이 이미 보장하는 것**이었다.

**계약을 느슨하게 만들지는 않는다.** C-1.2 의 의무는 그대로 둔다 — 그 문장은 우리 adapter
가 순서를 뒤집지 않을 것도 함께 요구하고, page **사이** 순서는 cursor 를 우리가 만들지
않아 여전히 계약 의존이다 (wave 4 review Advisory).

## R-012 — A Distribution Cliff Would Break Reconcile (new, unasked)

**Decision**: test app 을 **internal app 으로 둔다.** distribute 하지 않는다. 그리고 이
제약을 운영 문서에 남긴다.

**Rationale**: P4 가 2025-05-29 부터 비-Marketplace 배포 앱의 `conversations.history` 를
**분당 1회, 요청당 최대 15건**으로 낮췄다. 우리 reconcile 은 page 당 999건으로 최대 5 page
를 훑는 설계다 (`SLACK_HISTORY_PAGE_LIMIT`, `SLACK_MAX_HISTORY_PAGES`). 그 한도에서는
**설계가 성립하지 않는다** — 한 번의 reconcile 이 최소 5분 걸리고 lease 예산을 훨씬 넘긴다.
C-1 의무 2 (호출 합계를 `lease_seconds` 안에) 를 만족할 방법이 없다.

P5 가 범위를 좁힌다 — "Any internal customer-built apps will maintain their existing rate
limits and will not be subject to the new posted limits." **자기 workspace 에만 설치하고
배포하지 않는 앱은 면제다.** test app 이 그 경우다.

**그래서 이것은 지금의 blocker 가 아니라 기록해야 할 제약이다.** AMPLAI Slack app 을
나중에 Marketplace 밖으로 배포하면 그 순간 reconcile 설계가 깨진다. Package 4 범위에서
고칠 문제가 아니고, 배포 형태를 정할 때 판단할 근거다.

**Alternatives considered**: 지금 reconcile 을 15건/page 로 재설계하는 안. 기각한다 —
internal app 에서는 불필요하고, `SLACK_MAX_HISTORY_PAGES` 를 늘리면 Tier 예산을 오히려
더 쓴다. 배포 결정이 나기 전에 미리 고치는 것은 speculative work 다 (Constitution II).

## R-013 — HTTP Client Is The Standard Library (closes U-002)

**Decision**: `urllib.request` 를 쓴다. runtime dependency 는
`pydantic`·`pyyaml`·`typer` 셋을 유지한다.

**Rationale**: 근거 셋이다.

1. **호출량이 작다.** `chat.postMessage` 는 channel 당 초당 1건이고 (P1) governance Card
   는 사람의 결정 속도로 생긴다. `conversations.history` 는 재시도 때만 부른다 (D-023
   항목 2). connection pooling·HTTP/2·async 가 주는 이득이 없다.
2. **필요한 기능이 stdlib 에 다 있다.** POST + JSON body, Authorization header,
   query string, 그리고 **호출 단위 timeout** (`urlopen(..., timeout=)`). C-1 의무 2 가
   요구하는 deadline 을 걸 수 있다.
3. **재시도·backoff 는 이미 dispatcher 가 갖고 있다.** client library 의 retry 기능을
   쓰면 오히려 `OutboxDispatcher` 의 attempt 계약을 이중으로 돌린다 (T005 invariant).

**Alternatives considered**: `httpx` 또는 `requests`. 둘 다 dependency 와 그 transitive
tree 를 늘리는데 위 셋 중 어느 것도 해결하지 않는다. Constitution II 의 "임의로 추가하지
않는다" 에 걸린다. 다만 `urllib` 은 예외 표면이 넓다 — `HTTPError`, `URLError`,
`socket.timeout`, `ssl.SSLError` 를 전부 `SlackTransportError` 로 감싸는 것이 C-1 의무 1
이고 그것을 test 로 고정해야 한다.

## R-014 — Credentials Enter At The Composition Root (closes U-003)

**Decision**: 환경변수를 **entrypoint 한 곳에서만** 읽어 `SecretStr` 로 감싸 주입한다.
core 는 지금처럼 주입만 받는다.

**Rationale**: repo 전체에 Slack 설정을 읽는 `os.environ` 이 한 줄도 없고
`SlackInstallationPolicy` 는 생성자로 값을 받는다 (`slack.py`). 그 구조가 test 가능성을
만든 것이므로 유지한다. 읽는 지점만 하나 추가한다.

`signing_secret` 은 이미 `SecretStr` 이고 `field(repr=False)` 다 — repr 유출이 막혀 있다.
bot token 도 같은 처리를 받는다. A2·A7 이 요구하는 "저장·로그하지 않음" 은 이 두 장치와
"credential 을 payload·marker 에 넣지 않는다" 로 지킨다.

**Alternatives considered**: config 파일. 파일은 실수로 commit 되고 `.gitignore` 에 의존한다.
환경변수는 프로세스 경계에서 끝난다. keychain 류는 platform 종속이라 clean clone 계약
(SC-014) 과 안 맞는다.

**확정 (2026-08-07, T007)**: 변수는 둘이다 — `AMPLAI_SLACK_BOT_TOKEN` 과
`AMPLAI_SLACK_SIGNING_SECRET`. 부분 구성은 미구성이 아니라 **오류**다. `None` 으로
뭉뚱그리면 token 만 넣고 E2E 를 돌린 사람이 skip 만 보고 뭘 빠뜨렸는지 모른다.

## R-015 — E2E Runs Outside The Default Suite (closes U-004)

**Decision**: E2E 를 pytest marker 로 분리하고 기본 실행에서 **deselect** 한다.
credential 이 없으면 **skip 하되 그 사실을 출력한다.** `amplai-foundry verify` 는
건드리지 않는다.

**Rationale**: 제약 셋이 동시에 걸린다.

- **SC-014 는 clean clone·offline 통과를 요구한다.** verify 7 stage 안에 network test 를
  넣으면 그 계약이 깨진다 (FR-020).
- **FR-019 는 조용한 통과를 금지한다.** credential 부재를 pass 로 보고하면 Constitution
  III 위반이다. skip 은 pytest 가 이유와 함께 출력하므로 "안 돌았다" 가 보인다.
- **channel 오염과 rate limit.** E2E 가 실제 message 를 쌓는다. 기본 suite 에 들어가면
  매 실행마다 쌓이고 `SLACK_MAX_HISTORY_PAGES` 조회 범위와 맞물린다.

**Alternatives considered**: `amplai-foundry verify` 에 8번째 stage 를 추가하는 안.
SC-014 와 정면으로 충돌한다. 별도 CLI 명령을 만드는 안은 pytest 가 이미 주는 skip 보고와
marker 를 다시 구현하는 것이다.

**pyproject.toml 을 건드린다.** marker 등록이 필요하다. Package 3 는 그 파일을
`forbidden_paths` 에 뒀지만 그것은 Package 3 task 의 scope 였고 전역 금지가 아니다.
`index.yaml` 의 `W2-pytest-import-mode` 도 같은 파일을 별도 item 으로 미뤄뒀다 — Package 4
가 그 파일을 여는 김에 함께 볼지는 plan 이 정한다.

## Open Questions Carried Forward

- **OQ-003** (001 에서 이월) — Slack message metadata 의 크기 상한. P1 은 `metadata` 를
  지원한다고만 적고 상한을 말하지 않는다. marker 에 payload 본문을 안 넣는 것으로 계속
  회피한다. 실측은 실제 workspace 가 생긴 뒤 가능하다.
- **OQ-005** (신규) — `metadata` 는 workspace 구성원 누구나 읽을 수 있다. P1 이 명시한다:
  "Metadata you post to Slack is accessible to any app or user who is a member of that
  workspace." 우리 marker 는 `event_id`·`destination_ref`·`destination_sequence`·
  `payload_digest` 넷이고 credential 은 없다. 그래도 `destination_ref` 는 channel digest
  이고 `payload_digest` 는 내용 지문이라 노출 판단이 필요하다. Package 4 가 답한다.

## R-016 — Content Type Differs By Method Kind (T006 구현 중 확인)

**Decision**: `chat.postMessage` 는 **JSON**, `conversations.history` 는 **form-encoded** 로
보낸다. token 은 두 경우 다 `Authorization: Bearer` header 다.

**Rationale**: P6 가 둘을 나눠 적는다.

- JSON: "For write methods that support JSON, you may alternatively send your HTTP POST
  data as `Content-type: application/json`" 이고, 그때 token 은 "as a bearer token in the
  `Authorization` HTTP header" 다.
- 복잡한 인자를 가진 method 에 대해서는 "these methods can be difficult to properly
  construct when using a `application/x-www-form-urlencoded` Content-type, so we strongly
  recommend using JSON-encoded bodies instead" 로 **권장**한다. `metadata` 와 `blocks` 가
  정확히 그 경우다.
- 그런데 JSON 지원 범위를 **"Most write methods allow arguments with
  `application/json`"** 로 한정한다. `conversations.history` 는 **read** method 다.
  문서가 보장하지 않는 것에 기대지 않는다 — form-encoded 는 모든 method 가 받는다.

**Alternatives considered**: 둘 다 form-encoded 로 통일하는 안. `metadata` 를 JSON 문자열로
직렬화해 form field 에 넣어야 하는데 P6 가 그 방식을 권하지 않고, 중첩 구조를 form 으로
어떻게 보내는지도 명시하지 않는다. 확인 안 된 인코딩으로 marker 를 보내면 실패가
T010 에서야 드러난다.

**남은 위험**: fake server 는 우리가 보낸 것을 그대로 관측할 뿐 Slack 이 그것을 받아들이는지
모른다. 실제 확인은 T010 이다.

## R-017 — Slack Has No Documented Pre-Response Probe Identity (U-006: FACT CLOSED)

**Decision**: `chat.postMessage` 응답을 잃은 뒤 probe를 자동으로 다시 찾는 계약은
`BLOCKED`다. Undocumented field나 history scan을 쓰지 않는다.

**Rationale**:

- P1과 P8의 request argument에는 caller가 정하는 message ID나 idempotency key가 없다.
- 성공 응답 뒤의 정확한 remote identity는 Slack이 만든 `channel + ts`다.
- P7의 `chat.delete`도 `channel + ts`를 요구한다.
- `client_msg_id`는 P9와 P10에서 optional response/event field다. P2는 이 값으로 message를
  조회하거나 filter하는 argument를 제공하지 않는다.
- P11의 search는 text query다. Exact identity lookup이 아니다.

`chat.postMessage` error 목록에 `client_msg_id` 관련 이름이 있어도 request argument,
보존 기간, duplicate 판정 범위, 원본 `ts` 반환이 문서화되지 않았다. 이 단서를 계약으로
승격하지 않는다.

이 negative finding은 automatic ambiguous-message lookup과 provider-side global remote cap만
blocked로 둔다. E-8 review가 이 결론을 승인하고 E-9 governed recovery가 승인되면 local
fail-closed T013 구현 자체는 진행할 수 있다.

**Alternatives considered**:

- raw request에 `client_msg_id`를 추가하는 안. 공식 request contract가 아니므로 기각한다.
- `conversations.history`를 순회해 optional field를 찾는 안. caller-controlled identity와
  field 존재 보장이 없어 기각한다.
- metadata, text, prefix, app ID, 위치, 최근 N건, 시간 window를 조합하는 안. 전부
  heuristic이므로 금지한다.

## R-018 — Durable Intent Belongs In `GovernanceStore` (closes U-007)

**Decision**: probe lifecycle은 Markdown이나 process memory가 아닌 SQLite
`GovernanceStore`의 전용 state로 둔다. 기존 outbox/ingress/operator-hold row는 재사용하지
않는다. D-031 범위의 additive migration이 필요하다.

**Rationale**: `GovernanceStore`는 `.amplai/runtime/governance.db`의 mutable runtime
authority다. `governance_transaction`은 짧은 write, rollback, ambiguous commit 처리를 이미
제공한다. Durable publish intent가 external Git CAS 전에 intent를 기록하는 선례도 있다.

Startup probe는 Proposal event, ingress command, outbox delivery, operator hold가 아니다.
기존 table을 재사용하면 probe에 거짓 domain 의미를 준다. 전용 lifecycle state가 가장 작은
변경이다.

**Required boundary**:

1. Network post 전에 local probe intent를 commit한다.
2. Channel/app별 unresolved intent는 최대 1개다. Concurrent startup은 atomic claim을
   경쟁하고 승자 하나만 post한다.
3. Unresolved state 조회, atomic claim 또는 commit이 실패하면 post하지 않는다.
4. Slack `ts`를 받은 뒤에만 exact remote cleanup을 시도한다.
5. Slack 삭제 성공 뒤 `RESOLVED` commit이 실패하면 완료로 추정하지 않는다.
6. `ts`가 없는 ambiguous state는 자동 recovery하지 않는다.

이 결정은 기존 plan과 contract의 "durable schema 변경 없음"을 D-031 범위에서 supersede한다.
범용 write API는 추가하지 않는다.

**Alternatives considered**: 별도 JSON/file state. Filesystem CAS, schema 검증, transaction
recovery를 다시 만들어야 하므로 기각한다. Process memory는 restart safety를 제공하지
않는다.

## R-019 — No Production Slack Composition Root Exists (U-008: BLOCKED)

**Decision**: `MGC-012-T013` production wiring은 `BLOCKED`다. Repository에 존재하지 않는
worker entrypoint를 plan이 임의로 만들지 않는다.

**Rationale**: 설치된 executable은 Typer CLI 하나다. Slack worker command, daemon loop,
service unit, container entrypoint가 없다. `IngressDecisionWorker`와 `OutboxDispatcher`는
library service이며 production에서 조립되지 않는다.

향후 composition root는 아래 순서만 고정한다.

1. Settings load
2. `GovernanceStore.check_startup`
3. Probe lifecycle 판정
4. 각 startup 평가에서 operator diagnostic 1회 출력
5. `READY` 또는 `DEGRADED_CLEANUP`일 때만 Slack event claim

Diagnostic은 FR-030의 닫힌 allowlist만 사용한다. 재기동은 새 startup 평가이므로 degraded
상태를 다시 1회 출력한다.

CLI subcommand, 별도 executable, external host process 중 무엇을 쓸지는 product/runtime
결정이다. 이 plan은 선택하지 않는다.

## R-020 — Ambiguous Post Becomes Durable Hard Block (U-009: LOCAL CLOSED/REMOTE BLOCKED)

**Decision**: post intent를 commit한 뒤 Slack `ts`를 받지 못하면 state를 unresolved로
유지하고 `HARD_BLOCKED_NO_POST`를 반환한다. 이후 startup은 post 호출을 0회 수행한다.

이 결정은 **추가 post 방지**만 증명한다. 이미 Slack에 생겼을 수 있는 message를 찾거나
삭제했다고 주장하지 않는다. Slack의 공식 idempotency/lookup 계약이 없으므로 remote probe
수 상한과 automatic recovery는 계속 `BLOCKED`다.

**Rationale**: external post와 local commit은 하나의 atomic transaction으로 묶을 수 없다.
응답 유실을 "post 실패"로 간주해 재시도하면 중복 가능성이 생긴다. Durable unresolved intent를
먼저 확인하고 멈추면 적어도 이 application이 두 번째 post를 보내지는 않는다.

**Alternatives considered**:

- 자동 재시도. Provider idempotency 보장이 없어 기각한다.
- history search 후 cleanup. Exact identity가 없어 기각한다.
- operator가 `ts`를 명시하는 recovery action. Heuristic은 아니지만 새로운 governed mutation
  계약이다. 2026-08-11 clarification은 이 기능을 별도 governed recovery work item으로
  미뤘다. 그 계약이 승인되기 전에는 T013에 추가하지 않는다.

## R-021 — Hard Block Is An Outcome; Exact Cleanup May Reopen Startup

**Decision**: `HARD_BLOCKED_NO_POST`는 durable lifecycle state가 아니라 현재 startup의 typed
outcome이다. Persistence에는 `POST_INTENT_RECORDED`, `AMBIGUOUS_POST`, `CLEANUP_PENDING` 같은
원인 state를 보존한다.

이전 실행의 `CLEANUP_PENDING`에 confirmed `channel + ts`가 있으면 새 startup은 fresh probe를
먼저 게시하지 않는다. Stored exact identity로 cleanup하고 durable `RESOLVED` commit까지
성공한 뒤에만 fresh self-check를 시작한다. Cleanup 또는 commit 실패면 원인 state를 유지하고
`HARD_BLOCKED_NO_POST` outcome으로 activation과 새 post를 막는다.

**Rationale**: Outcome을 state로 저장하면 실제 장애 원인을 잃고 recovery 가능성을 판정할 수
없다. 반대로 exact provider identity가 이미 있는 `CLEANUP_PENDING`까지 영구 차단하면 D-031이
허용한 bounded recovery를 사용할 수 없다. Remote delete와 local resolution을 둘 다 확인한 뒤
새 atomic claim으로 넘어가면 heuristic 없이 restart safety와 진전을 함께 보장한다.

**Alternatives considered**:

- 모든 unresolved state를 영구 차단. Exact identity가 있는 cleanup 가능 상태까지 막으므로
  기각한다.
- Delete 성공만으로 새 probe 허용. Durable resolution 실패를 clear로 추정하므로 기각한다.
- `HARD_BLOCKED_NO_POST`를 lifecycle state로 저장. Cause state와 안전한 다음 행동을 잃으므로
  기각한다.

## R-022 — Invalid Lifecycle Records Fail Closed Without Mutation

**Decision**: malformed record, unknown state value와 unsupported schema/state version은
`LIFECYCLE_STATE_INVALID` typed failure다. Repository는 기존 row를 보존하고 readback, post,
automatic repair와 worker activation을 호출하지 않는다.

**Rationale**: remote message 존재 여부를 모르는 operational state를 정상 또는 특정 lifecycle로
추정하면 one-unresolved invariant와 no-additional-post 보장을 동시에 잃는다. Record 보존은 별도
governed recovery가 원인을 검토할 evidence도 남긴다.

**Alternatives considered**: unknown state를 `AMBIGUOUS_POST`로 치환하거나 최신 schema로 자동
migration하는 안. Cause를 변형하고 승인되지 않은 mutation이므로 기각한다.

## R-023 — Readback Failure And Cleanup Detail Are Closed Types

**Decision**: primary readback failure는 `PROBE_INPUT_INVALID`, `HISTORY_READ_FAILED`,
`PROBE_NOT_FOUND`, `APP_ID_MISMATCH`, `MARKER_UNREADABLE`, `MARKER_MISMATCH`,
`RESPONSE_CHANNEL_MISMATCH`의 closed enum이다. 마지막 code는 Slack 성공 응답의 confirmed
channel이 configured target과 다를 때 사용한다. 이 경우 configured history를 조회하지 않고
cleanup은 provider-confirmed `channel + ts`로만 시도한다.
Secondary cleanup failure는 optional `CleanupFailureDetail` 하나이며
`cleanup_failure_count`와 allowlisted `provider_error_code`만 가진다.

**Rationale**: finite taxonomy가 있어야 SC-020의 post 뒤 여섯 failure × cleanup 성공·실패
12개 조합을 재현할 수 있다. Response channel mismatch를 `PROBE_NOT_FOUND`로 alias하면 history
absence를 관측하지 않고도 관측했다고 주장하게 된다. 전용 code는 이 거짓 진단을 막는다.
Primary와 secondary를 별도 field로 두면 cleanup failure가 원래 readback cause를 감싸거나
대체하지 않는다.

Diagnostic surface도 closed다. Raw exception chain, 자유형 error와 provider body/header는
structured log, exception output, metrics, persisted failure diagnostic과 operator output에
전달하지 않는다. Metrics label은 low-cardinality outcome/diagnostic/provider code만 허용한다.

**Alternatives considered**: 자유형 mapping을 output에서 필터링하거나 원본 exception을
`__cause__`로 연결하는 안. 누락된 surface와 비밀정보 노출을 정적으로 막을 수 없어 기각한다.

## R-024 — Crash Boundaries Preserve Durable Knowledge

**Decision**: intent commit 전 crash는 post 0회이므로 다음 startup의 fresh claim을 허용한다.
Committed `POST_INTENT_RECORDED`를 다음 startup이 발견하면 실제 post 여부를 추정하지 않고 hard
block한다. Response loss는 가능한 경우 `AMBIGUOUS_POST`로 기록하고 기록 실패 시 마지막 intent를
보존한다.

성공 응답 뒤 confirmed identity commit이 실패하면 현재 process에 있는 `channel + ts`로 exact
delete를 최대 한 번 시도하고 durable `RESOLVED`를 기록한다. 둘 다 성공한 뒤의 후속 startup만
fresh claim할 수 있다. 그 전 crash나 delete/resolution failure는 마지막 committed cause와
no-post/no-activation을 유지한다.

**Rationale**: 현재 process의 confirmed identity는 heuristic이 아니지만 crash 뒤에는 사라진다.
이 짧은 exact compensation window만 사용하면 안전한 cleanup 기회를 살리면서 다음 startup이
기억에 없는 identity를 추정하는 것은 막는다.

**Alternatives considered**: identity commit 실패 즉시 영구 block은 안전하지만 known exact
identity를 버린다. 다음 startup의 자동 repost 또는 history scan은 duplicate/heuristic이므로
기각한다.

## R-025 — T013 Needs Named Readiness And Dual Completion Evidence

**Decision**: E-7~E-10은 각각 FR-037의 내용을 담은 user-approved governed workstream record가
T013 manifest에 연결돼야 한다. 완료에는 같은 revision의 composition-root integration result와
configured startup trace가 모두 필요하다.

Configured trace는 실제 target/config 경로를 필요로 하므로 T010 completion을 T013의 internal
dependency로 둔다. Provider exact recovery 미지원은 E-8의 유효한 review conclusion이며 E-9가
승인되면 T013 local implementation을 중단시키지 않는다.

**Rationale**: readiness record는 구현할 경계와 승인자를 고정하지만 실행을 증명하지 않는다.
Instrumented integration은 outcome/ordering/count matrix를 증명하지만 실제 channel/app/transport
composition을 증명하지 않는다. Configured trace는 실제 wiring을 증명하지만 failure matrix를
증명하지 않는다. 두 증거가 상호 보완적이다.

**Alternatives considered**: unit command output만으로 완료하거나 configured startup 한 번만으로
gate를 닫는 안. 각각 production wiring 또는 negative-path evidence가 없어 기각한다.

## D-031 Research Gate

| Unknown | Result | Effect |
|---|---|---|
| `U-006` explicit provider identity | `FACT CLOSED: unsupported` | Ambiguous message 자동 lookup과 provider-side global cap만 금지 |
| `U-007` durable recovery owner | `GovernanceStore` | Additive lifecycle schema 필요 |
| `U-008` production composition root | `BLOCKED` | E-7 named approval 전 `MGC-012-T013` 구현 금지 |
| `U-009` ambiguous outcome | Local no-post만 해결 | Remote recovery와 global cap 주장 금지. Operator 강제 해제도 별도 work item |

`MGC-012-T008`의 typed unit contract는 planning 가능하다. `MGC-012-T013`은 T010 completion과
E-7~E-10 named approval record가 모두 연결될 때까지 구현 금지다. FR-018 production completion,
`readback-selfcheck-wiring`, Package 4 gate PASS는 추가로 같은 revision의 dual production
evidence가 모두 연결될 때까지 금지한다.
