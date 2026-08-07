# Phase 0 Research: MGC-012 Package 4

`spec.md` 의 U-001~U-005 를 닫는다. Slack 쪽 사실은 공식 문서로만 고정하고, 못 찾은 것은
**미확인으로 남긴다.** 추정으로 채우지 않는다.

## Sources

| # | URL | 조회일 |
|---|---|---|
| P1 | `https://docs.slack.dev/reference/methods/chat.postMessage` | 2026-08-07 |
| P2 | `https://docs.slack.dev/reference/methods/conversations.history` | 2026-08-07 |
| P3 | `https://docs.slack.dev/apis/web-api/rate-limits` | 2026-08-07 |
| P4 | `https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/` | 2026-08-07 |
| P5 | `https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/` | 2026-08-07 |

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

**미확정**: 환경변수 이름과 개수는 `contracts/` 가 정한다. 이 결정은 "어디서 읽는가" 만
고정한다.

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
