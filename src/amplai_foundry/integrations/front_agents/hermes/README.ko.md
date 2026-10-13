# Hermes 키트 설치 안내 (Work 034 S1c)

Hermes Agent v0.21.6 을 AMPLAI 의 앞단 에이전트로 붙이는 설정 묶음이다 (spec F-1 의 둘째 층, plan §3.7).
AMPLAI 코어는 Hermes 를 모른다. Hermes 는 접수 어댑터 `hermes` 의 토큰을 가진 MCP 서버를 통해서만 AMPLAI 를 부른다.
다른 앞단 에이전트로 바꿀 때는 이 키트만 바꾸고 MCP 서버·도구 계약은 그대로 쓴다 (F-4, F-6).

"확인 필요" 는 이 저장소가 아직 검증하지 못한 항목이다. 추정으로 채우지 않았다.

## Files

| 파일 | 놓는 곳 | 내용 |
|---|---|---|
| `config.yaml.template` | Hermes VM `$HERMES_HOME/config.yaml` | 모델, 끈 toolset, 쓰기 승인, Docker terminal, AMPLAI MCP 연결 |
| `env.template` | Hermes VM `$HERMES_HOME/.env` (0600) | Slack bot·app 토큰, 허용 사용자, home channel, intake 토큰 |
| `SOUL.md` | Hermes VM `$HERMES_HOME/SOUL.md` | 역할과 금지 사항 (승인 불가, 코드 직접 수정 불가) |
| `vm-checklist.ko.md` | 운영자 기록 | VM 점검표 (H-9, H-14, AC-H8) |
| `kit.json` | — | 키트 버전, Hermes 버전, 도구 계약 버전 |

## Overview

```text
운영자 ── Slack 앱 1 (Hermes bot, Socket Mode) ──▶ Hermes (VM, Docker terminal)
   ▲                                                 │ stdio
   │                                                 ▼
   │                                    AMPLAI MCP 서버 (intake 토큰은 여기만)
   │                                                 │ HTTP /api/v3/intake/hermes
   │                                                 ▼
   └── Slack 앱 2 (AMPLAI alerts, Incoming Webhook) ◀── AMPLAI (notifier)
```

알림 채널(앱 2)은 Hermes 와 독립이다. Hermes 를 끄거나 Hermes 가 침해돼도 일시정지·취소 알림은 간다 (F-5, AC-F3).

## 1. Slack 앱 두 개

두 앱은 서로 다른 앱이다. 앱 2 의 webhook URL 은 Hermes VM 에 두지 않는다 (Q-10).

### 앱 1: Hermes bot

Hermes 문서(`website/docs/user-guide/messaging/slack.md`)의 절차를 따른다. 요약:

1. Slack 앱을 만든다. Hermes 는 manifest 를 만들어 준다: `hermes slack manifest --agent-view --write`.
2. Socket Mode 를 켜고 App-Level Token(`connections:write`)을 만든다 → `SLACK_APP_TOKEN` (`xapp-`).
3. Bot Token Scopes: `chat:write`, `app_mentions:read`, `channels:history`, `channels:read`, `groups:history`,
   `im:history`, `im:read`, `im:write`, `mpim:history`, `mpim:read`, `users:read`, `files:read`, `files:write`
   (선택: `groups:read`, `assistant:write`).
4. Event Subscriptions: `message.im`, `message.mpim`, `message.channels`, `app_mention` (권장: `message.groups`).
   App Home 의 Messages Tab 을 켠다.
5. 워크스페이스에 설치하고 Bot User OAuth Token 을 받는다 → `SLACK_BOT_TOKEN` (`xoxb-`).
6. 운영자의 Slack member id(`U...`)를 `SLACK_ALLOWED_USERS` 에 하나만 넣는다.

### 앱 2: AMPLAI alerts (Incoming Webhook)

보내기만 하는 앱이다. bot 토큰·이벤트 구독이 필요 없다.

1. 새 Slack 앱을 만든다 (앱 1 과 다른 앱).
2. Incoming Webhooks 를 켜고 알림 받을 운영자 채널 하나를 골라 webhook 을 만든다.
3. 받은 URL(`https://hooks.slack.com/...`)을 **AMPLAI 호스트**의 store 밖 파일에 넣는다 (아래 2-3).
   AMPLAI 는 `https://hooks.slack.com/` 이 아닌 URL 은 쓰지 않는다.

Slack 화면의 메뉴 이름은 바뀔 수 있다. Slack 공식 문서와 대조한다.

## 2. AMPLAI 호스트 설정

`~/.amplai/local.json` 은 운영자가 직접 고친다. 고친 뒤 `amplai ops local-serve` 를 다시 띄운다.

### 2-1. intake adapter 토큰과 연결표

```bash
umask 077
mkdir -p ~/.amplai/intake
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > ~/.amplai/intake/hermes.token
chmod 600 ~/.amplai/intake/hermes.token
```

연결표 `~/.amplai/intake/hermes.identities.json` (0600). v2 항목은 (provider, workspace, user id) 다:

```json
{
  "schema_version": "intake-identity-2",
  "entries": [
    {"provider": "slack", "workspace_id": "<T...>", "user_id": "<U...>", "subject_id": "<operator_subject>"}
  ]
}
```

`subject_id` 는 `local.json` 의 `operator_subject` 와 같아야 한다 (운영자 1인, H-5).

### 2-2. `intake.adapters[]`

```json
"intake": {
  "adapters": [
    {
      "adapter_id": "hermes",
      "token_file": "intake/hermes.token",
      "identity_map_file": "intake/hermes.identities.json",
      "permissions": ["goal.submit", "runtime.read", "goal.steer", "question.answer",
                      "knowledge.propose", "meta.read", "goal.cancel"],
      "rate_limits": {
        "submit": {"count": <정수>, "window_seconds": <초>},
        "steer":  {"count": <정수>, "window_seconds": <초>},
        "replan": {"count": <정수>, "window_seconds": <초>},
        "cancel": {"count": <정수>, "window_seconds": <초>}
      }
    }
  ]
}
```

- `permissions` 는 H-2 집합 안에서만 고른다. 빼면 그 일은 `FORBIDDEN` 이다.
- 빈도 제한 숫자는 설계에 없다. 운영자가 정한다.
- S1a 형식(`intake: {token_file, identity_map_file, rate_limits}`)도 그대로 읽힌다 (adapter `hermes`, H-2 전체).

### 2-3. 알림 (`notifier`)

```bash
umask 077
printf '%s\n' '<앱 2 의 webhook URL>' > ~/.amplai/secrets/notifier.url
chmod 600 ~/.amplai/secrets/notifier.url
```

```json
"notifier": {"module": "notifier.slack_webhook", "webhook_url_file": "secrets/notifier.url"}
```

파일은 보낼 때마다 읽는다. symlink, 0600 이 아닌 파일, 다른 소유자 파일은 거부한다.
알림 내용은 goal id, 누가(`intake:hermes`), 무엇을(일시정지·취소), 상태, 시각뿐이다. 이유 문장은 넣지 않는다 (H-8).

### 2-4. 토큰 폐기

`~/.amplai/intake/hermes.token` 을 지우면 다음 요청부터 401 이다 (AC-H7). 새 토큰을 만들면 VM 의 `.env` 도 바꾼다.

## 3. Hermes VM 설정

1. `vm-checklist.ko.md` 를 먼저 채운다.
2. Hermes v0.21.6 을 설치한다. 설치·고정 방법 — 확인 필요.
3. AMPLAI MCP 서버를 VM 에 설치한다. 의존성은 hash 로 고정된 목록으로만 설치한다:
   ```bash
   python3.11 -m venv ~/amplai-intake-mcp
   ~/amplai-intake-mcp/bin/python -m pip install --require-hashes \
     -r requirements-hashes.txt
   ~/amplai-intake-mcp/bin/python -m pip install --no-deps <amplai_foundry wheel>
   ```
   `requirements-hashes.txt` 는 `amplai_foundry/integrations/intake_mcp/` 에 있다 (`amplai-foundry[intake-mcp]` 의
   의존 closure 전체와 `mcp==2.3.0`, 버전과 배포 파일 hash 고정). 이 저장소에는 그 밖의 lock 파일이 없다.
   VM 에 wheel 을 전달하는 방법 — 확인 필요. `config.yaml` 의 `command` 는 이 venv 의 python 이다.
4. `config.yaml.template` → `$HERMES_HOME/config.yaml`, `env.template` → `$HERMES_HOME/.env` (0600),
   `SOUL.md` → `$HERMES_HOME/SOUL.md`. `<...>` 를 채운다.
5. `hermes gateway setup` 으로 Slack gateway 를 확인한다.

## 4. Cron 작업

Hermes cron 은 `~/.hermes/cron/jobs.json` 에 저장되고 gateway daemon 이 60초마다 돌린다. 작업은 CLI 로 만든다.
cron 작업은 활성화된 MCP 서버의 도구를 쓸 수 있다. 각 실행은 따로 떨어진 agent session 이다.

아침 보고 (평일 09:00, home channel 로):

```bash
hermes cron create "0 9 * * 1-5" \
  "Call the amplai list_goals tool and report each goal's id and status in Korean. For goals awaiting approval, remind the operator to run 'amplai approve <goal-id>' on the AMPLAI host. Do not approve anything." \
  --name amplai-morning-brief --deliver slack:<C...>
```

feed 가져오기 (10분마다):

```bash
hermes cron create "*/10 * * * *" \
  "Call the amplai pending_events tool and report any goal whose status changed since the last report. Report only ids, statuses and times." \
  --name amplai-feed-poll --deliver slack:<C...>
```

- cron 의 각 실행이 따로 떨어진 session 이라 `pending_events` 의 cursor 를 다음 실행으로 넘기는 방법 — 확인 필요.
  MCP 도구 `pending_events` 를 cursor 없이 부르거나 cursor 가 만료(`expired: true`)되면, 도구 결과에 snapshot
  (끝나지 않은 goal 상태)과 새 cursor 가 함께 들어 있다. AMPLAI HTTP feed(`/api/v3/intake/{adapter_id}/events`) 자체는
  snapshot 을 싣지 않고 `snapshot_ref` 로 `/api/v3/intake/{adapter_id}/snapshot` 을 가리킨다. MCP 서버가 그 경로를 읽어
  도구 결과에 넣는다.
- `approvals.cron_mode` 기본값은 `deny` 다. `trust: untrusted` MCP 서버의 쓰기 호출은 cron 에서 거절된다.
  이 키트는 `trust` 를 지정하지 않는다(기본 `full`). 두 cron 작업은 조회 도구만 부른다.

## 5. 확인

- AMPLAI 호스트: `amplai status` 로 Hermes 가 낸 goal 이 운영자 귀속으로 보이는지 본다.
- Hermes 에 "지금 goal 목록 보여 줘" → `list_goals` 결과가 나온다.
- Hermes 에 취소를 요청 → 알림 채널(앱 2)에 알림이 온다.
- Hermes 에 승인을 요청 → 승인 도구가 없으므로 할 수 없다고 답해야 한다.

## Hermes 설정 근거

Hermes v0.21.6 (tag `v0.21.6`, commit `818c13be`) 소스에서 확인한 위치다.

| 설정 | 근거 |
|---|---|
| `mcp_servers.<name>.command/args/env`, `${VAR}` 치환, `tools.include` | `website/docs/reference/mcp-config-reference.md:15-86,110-151`, `tools/mcp_tool_config.py:540-559` |
| MCP stdio 자식 환경은 걸러진 기본값 + 서버 `env` | `tools/mcp_tool_config.py:182-204` |
| `agent.reasoning_effort` | `website/docs/user-guide/configuration.md:1841-1850`, `gateway/run_config_loaders.py:149-159` |
| `agent.disabled_toolsets`, toolset 이름 | `hermes_cli/config_defaults.py:279`, `website/docs/reference/toolsets-reference.md:55-89` |
| `memory.write_approval`, `skills.write_approval` | `hermes_cli/config_defaults.py:1311-1317` (memory), `:1478-1482` (skills) |
| `terminal.backend/docker_volumes/docker_network` | `hermes_cli/config_defaults.py:297-391` |
| `hermes cron create <schedule> <prompt> --name --deliver` | `hermes_cli/subcommands/cron.py:23-35` |
| cron 저장 위치, 60초 tick, 전달 `slack:<chat_id>` | `website/docs/user-guide/features/cron.md:320,338,368,560-590` |
| cron 에서 MCP 도구 사용, `approvals.cron_mode: deny` | `cron/scheduler.py:438-479,1844-1853`, `hermes_cli/config_defaults.py:1681` |
| Slack 토큰·scope·event·`SLACK_ALLOWED_USERS`·`SLACK_HOME_CHANNEL` | `website/docs/user-guide/messaging/slack.md:21-218` |
| `SOUL.md` 위치 | `website/docs/user-guide/features/personality.md:9-41` |
| MCP 호출은 기본(`trust: full`) 승인 단계 없음 | `tools/mcp_tool_handlers.py:60-85` |

## 도구 계약 버전

키트는 도구 계약 `amplai-intake-tools` `1.0` 에 맞춰 확인했다 (`kit.json`). `1.0` 은 S1a intake route 위의 도구 8개다.
도구를 더하는 변경은 같은 major 의 minor(`1.x`)이고, 각 도구의 `since` 가 더해진 버전을 적는다. 이름을 바꾸거나 지우거나 입력·출력
schema 를 바꾸면 새 major 다. 키트를 새 minor 로 올릴 때는 `kit.json` 의 버전과 `tools.include` 를 함께 바꾼다.

## 열린 항목

운영자 결정이 필요한 항목이다.

- **(P2-6) `message_id` 를 모델이 채운다 (H-7)**: Hermes 는 MCP 호출에 보낸 사람(플랫폼, 사용자 id, 채널, 메시지 id)을 넣지 않는다
  (`tools/mcp_tool_handlers.py:396`, 인자는 모델이 만든 그대로). 모델은 system prompt 의 세션 정보에서 사용자 이름 또는 id 를 본다
  (`gateway/session.py:376-463`). Slack 메시지 id 를 모델에 주는 부분은 찾지 못했다. 그래서 도구의 `user_id`, `workspace_id`,
  `message_id` 는 모델이 채운다. 연결표가 운영자 1인이라 사칭 범위는 같은 운영자다. 그러나 같은 메시지 재전송을 한 번만 받는 것(H-7)은
  모델이 `message_id` 를 일관되게 채울 때만 성립한다. 서버가 보낸 사람을 검증하려면 Hermes 쪽 지원이 필요하다 — 확인 필요.
- **(P2-7) VM → AMPLAI 경로와 AC-H8**: AMPLAI 서버는 `127.0.0.1:5083` 에만 열린다 (`runtime/cli.py` `local-serve`). VM 안의
  MCP 서버가 호스트에 닿는 방법(VM 네트워크 설정 또는 S1b 의 원격 listener, H-12/OD-8) — 확인 필요. 이 경로가 정해져야 egress
  허용 목록과 AC-H8 확인(`vm-checklist.ko.md`)을 끝낼 수 있다.
- **(P2-8) Hermes 의 파일 도구가 `$HERMES_HOME/.env` 를 읽을 수 있는가**: 토큰은 MCP 서버 환경에만 들어가지만,
  `${AMPLAI_INTAKE_TOKEN}` 치환을 위해 Hermes 의 `.env` 에도 있다. Docker terminal 은 `docker_forward_env` 로 넘긴 변수만 받는다.
  그러나 terminal 밖에서 도는 Hermes 의 파일 읽기 도구가 `.env` 를 읽어 모델 문맥에 넣을 수 있는지는 확인하지 못했다 — 확인 필요.
  읽을 수 있다면 intake 토큰과 Slack 토큰이 모델에게 보인다.
- **외부 skill 설치 차단 설정**: v0.21.6 에서 해당 키를 찾지 못했다. `catalog` toolset 은 끈다. 운영 규칙으로 막는다.
- **도구 계약 1.0 에 없는 도구**: `pause_goal`, `resume_goal`, `answer_question`, `propose_knowledge`, `meta_summary` 는 AMPLAI 에
  intake route 가 아직 없다.
