# Quickstart: MGC-012 Package 4

두 부분이다. **A** 는 사람이 한 번 하는 Slack 준비이고 (plan 의 external dependency
E-1~E-4), **B** 는 반복해서 도는 검증이다.

## A. Slack Workspace And App Setup

**이 절은 사람이 한다. task 로 만들 수 없다.** wave 7 의 선행 조건이다.

### A-1. Workspace

`slack.com/get-started` 에서 새 workspace 를 만든다. 무료다.

**회사 workspace 를 쓰지 않는다** (D-026). test 가 실제 message 를 쌓고 실패 시 지저분한
상태를 남긴다.

### A-2. App

`api.slack.com/apps` → **Create New App** → **From scratch** → A-1 의 workspace 선택.

**배포하지 않는다.** Distribution 설정을 건드리지 말고 이 workspace 전용 internal app 으로
둔다. 배포하면 `conversations.history` 가 분당 1회로 떨어져 reconcile 설계가 성립하지
않는다 (contracts H-1.2, research R-012).

### A-3. Scopes

**OAuth & Permissions** → **Bot Token Scopes** 에 아래를 추가한다.

| scope | 필요 |
|---|---|
| `chat:write` | 필수 |
| `channels:history` | public channel 을 쓰면 필수 |
| `groups:history` | private channel 을 쓸 때만 |

**`chat:write.public` 은 추가하지 않는다** (contracts H-1.1).

scope 를 빠뜨리면 send 는 성공하고 **첫 재시도의 read 에서** `missing_scope` 로 되돌릴 수
없는 hold 가 난다. H-3 의 자가검사가 이것을 기동 시점으로 앞당기지만 애초에 맞게 준다.

### A-4. Install And Collect Credentials

**Install to Workspace** 를 누른다. 그러면 둘이 생긴다.

| 값 | 위치 | 모양 |
|---|---|---|
| Bot User OAuth Token | OAuth & Permissions | `xoxb-` 로 시작 |
| Signing Secret | Basic Information → App Credentials | 32자 hex |

**이 값을 repo 에 넣지 않는다.** 환경변수로만 준다 (contracts H-4).

### A-5. Test Channel

채널을 하나 만들고 app 을 초대한다 (`/invite @앱이름`). channel ID (`C` 로 시작) 를 기록한다.

app ID (`A` 로 시작) 도 기록한다. **Basic Information → App Credentials** 에 있다.
`auth.test` 가 주는 `bot_id` (`B` 로 시작) 는 **다른 값이라 대체할 수 없다.**

### A-6. Set The Environment Variables

이름은 T007 이 secret 둘을, T009 가 대상 둘을 확정했다 (D-028). 값 하나에 변수 하나다.

```bash
export AMPLAI_SLACK_BOT_TOKEN='xoxb-...'
```

```bash
export AMPLAI_SLACK_SIGNING_SECRET='...'
```

```bash
export AMPLAI_SLACK_APP_ID='A...'
```

```bash
export AMPLAI_SLACK_CHANNEL_ID='C...'
```

**넷 다 넣어야 한다.** 하나라도 빠지면 "구성 안 됨" 이 아니라 **오류**다 — 빠뜨린 변수
이름을 전부 알려준다. 조용히 skip 되면 뭘 빠뜨렸는지 모른 채 지나간다.

secret 둘은 `SecretStr` 로 감싸여 들어오고 repr·log·예외 message 어디에도 안 나온다.
app ID 와 channel ID 는 secret 이 아니다.

### A-7. Verify The Setup

```bash
python -m pytest tests/test_slack_http.py -m slack_e2e
```

credential 이 없으면 **skip 으로 보고된다.** 통과로 보고되면 그것 자체가 결함이다
(contracts H-5.2).

## B. Verification Commands

### B-1. Network 없이 도는 것

wave 5·6 은 선행 조건이 없다. clean clone 에서 그대로 돈다.

```bash
python -m pytest
```

```bash
python -m ruff check .
```

```bash
python -m mypy
```

```bash
amplai-foundry lint vault
```

```bash
amplai-foundry verify
```

`amplai-foundry verify` 는 7 stage 이고 **network 를 전제하지 않는다.** E2E 를 여기 넣지
않는다 (FR-020, SC-014).

### B-2. Network 가 필요한 것

A 절이 끝나야 돈다.

```bash
python -m pytest -m slack_e2e
```

## C. Scenario Matrix

wave 7 이 실제 workspace 에서 확인하는 것들이다.

| # | 상황 | 기대 | 근거 |
|---|---|---|---|
| 1 | 실제 채널로 Card 를 보낸다 | 채널에 뜨고 receipt 가 `slack:{channel}:{ts}` | SC-009 |
| 2 | 1번 직후 `reconcile()` 을 부른다 | **1번과 같은 receipt 문자열** | SC-009, 001 C-2.3 |
| 3 | 1번 message 를 되읽는다 | `app_id` 와 marker 네 필드가 모두 복원 | SC-010, H-3 |
| 4 | Card 를 여럿 보낸 뒤 history 조회 | 최신이 먼저 온다 | SC-011, R-011 |
| 5 | 1번 Card 를 사람이 지우고 `reconcile()` | history 소진이면 미전송 판정 | 001 D-023 항목 3 |
| 6 | scope 없는 설치로 read | `missing_scope` → terminal | H-1.1 |
| 7 | Telegram destination 이 있는 store 로 Slack 경로 완주 | Telegram row·event 불변 | SC-012, A14 |
| 8 | credential 없이 E2E 실행 | **skip 으로 보고.** pass 아님 | SC-013, H-5.2 |

## D. Expected Baseline

- 기존 test 가 **전량** 회귀 없이 통과한다. **숫자를 고정하지 않는다** — wave 마다 늘고
  갱신 장치가 없다 (SC-015)
- `amplai-foundry verify` 7 stage 통과. clean clone Python 3.11·3.12 에서도 통과 (SC-014)
- 위 명령 중 **실제로 돌린 것만** gate 기록에 쓴다. 안 돌린 것을 통과했다고 쓰지 않는다
- **skip 을 pass 로 적지 않는다.** E2E 가 skip 됐으면 gate 기록에 skip 이라고 쓴다
