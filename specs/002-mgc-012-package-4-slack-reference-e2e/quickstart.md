# Quickstart: MGC-012 Package 4

두 부분이다. **A** 는 사람이 한 번 하는 Slack 준비이고 (plan 의 external dependency
E-1~E-4; E-1·E-2는 완료), **B** 는 반복해서 도는 검증이다.

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

Scenario 1~8은 wave 7 실제 workspace 검증이다. Scenario 9~25는 D-031 offline contract 또는
blocked production evidence다.

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
| 9 | Readback 성공, cleanup 실패 | `DEGRADED_CLEANUP`, worker activation 허용 | SC-017, H-3.2 |
| 10 | Readback과 cleanup 동시 실패 | 원래 readback cause 유지, worker activation 0회 | SC-020, H-3.2 |
| 11 | 이전 unresolved lifecycle 존재 (`CLEANUP_PENDING` recovery 대상 제외) | 원인 state 보존, `HARD_BLOCKED_NO_POST`, post 0회 | SC-018, H-3.3 |
| 12 | 같은 unresolved state로 startup 10회 | 추가 post 0회 | SC-018, FR-028 |
| 13 | Post 응답 유실, `ts` 미확인 | Durable block 유지, history search 미사용 | R-017, R-020 |
| 14 | Cleanup-only operator diagnostic | 각 startup 평가에서 1건, FR-030 allowlist 밖 field 0건 | SC-017, SC-021 |
| 15 | 같은 channel/app의 concurrent startup | atomic claim 1건, post 최대 1건, 패자는 hard block | SC-019, H-3.3 |
| 16 | Slack delete 성공, `RESOLVED` commit 실패 | `HARD_BLOCKED_NO_POST`, worker activation 0회 | SC-024, H-3.2 |
| 17 | Degraded 상태에서 process 재기동 | 새 startup 평가마다 diagnostic 1건 | SC-017, H-3.5 |
| 18 | 이전 `CLEANUP_PENDING`에 confirmed `channel + ts` 존재 | exact delete와 durable `RESOLVED` commit 뒤 fresh self-check 허용 | FR-027, H-3.3 |
| 19 | 18번의 cleanup 또는 resolution commit 실패 | cause state 보존, `HARD_BLOCKED_NO_POST`, 새 post·activation 0회 | FR-027, SC-018 |
| 20 | malformed record, unknown state 또는 unsupported version | `LIFECYCLE_STATE_INVALID`, row mutation/readback/post/activation 0회 | FR-033, SC-025 |
| 21 | post 뒤 여섯 readback failure × cleanup 성공·실패 | 12개 case에서 primary code 보존, activation 0회. Response channel mismatch는 configured history 0회, confirmed identity cleanup | FR-035, SC-020 |
| 22 | readback과 cleanup 동시 실패의 diagnostic surface | secondary detail 최대 1개, raw exception/provider data 0건 | FR-034, SC-026 |
| 23 | intent 전/후와 response loss crash | durable knowledge만 사용하고 unauthorized lookup/post 0회 | FR-036, SC-027 |
| 24 | success response 뒤 identity commit 실패 | current process exact delete 1회 이하; durable resolution 뒤 후속 startup만 fresh claim | FR-036, SC-027 |
| 25 | T013 production wiring 완료 판정 | T010 완료 + E-7~E-10 승인 + same-revision dual evidence 필요 | FR-037, SC-028 |

## D. D-031 Validation Boundary

### D-1. Offline Unit Evidence

`MGC-012-T008`은 fake transport와 주입된 lifecycle state double로 아래 판정만 검증한다.

- outcome matrix 네 경로
- FR-035 일곱 primary code와 post 뒤 여섯 code × cleanup 성공·실패 12개 조합
- response channel mismatch의 configured history 0회와 provider-confirmed identity cleanup
- optional `CleanupFailureDetail` cardinality 0..1과 safe field 두 개
- unresolved, ambiguous, claim 실패 또는 resolution commit 실패 입력 시 원인 state를
  보존하고 startup outcome으로 `HARD_BLOCKED_NO_POST`
- `HARD_BLOCKED_NO_POST`를 persisted lifecycle state로 쓰지 않음
- confirmed `channel + ts`만 cleanup에 사용
- heuristic history scan 0회
- typed outcome의 FR-030 closed diagnostic field allowlist
- readback/lifecycle 계층의 operator logging 0회
- exception chain, free-form error, raw provider data와 disallowed metrics label 0건

실제 `GovernanceStore` record validation/commit, previous `CLEANUP_PENDING` recovery, crash
persistence, production output과 worker activation은 T008 완료 증거가 아니다.

### D-2. Blocked Production Evidence

`MGC-012-T013`은 아래 blocker가 모두 닫힐 때까지 실행하지 않는다.

- MGC-012-T010 live target/config evidence 완료
- E-7 entrypoint path/owner/pre-claim point의 user-approved workstream decision
- E-8 dated/versioned official provider contract review와 user approval
- E-9 governed recovery actor/permission/token/audit/transition work item 승인
- E-10 lifecycle version/migration/uniqueness/repository owner schema decision 승인

Blocker가 닫힌 뒤 T013은 실제 `GovernanceStore`로 아래를 검증한다.

- post 전 intent commit
- channel/app별 unresolved state 최대 1개
- concurrent startup의 atomic claim 성공 최대 1건과 loser post 0건
- state read/write 실패 시 post 0회
- malformed/unknown/unsupported lifecycle record의 mutation/readback/post/activation 0회
- 네 crash boundary와 success-response/identity-commit failure exact compensation
- delete 성공 뒤 `RESOLVED` commit 실패 시 worker activation 0회
- 이전 `CLEANUP_PENDING` exact recovery 성공 뒤에만 fresh claim/post 허용
- hard block에서 원인 lifecycle state 보존
- restart 10회 추가 post 0회
- production root의 cleanup-only diagnostic은 각 startup 평가에서 정확히 1건
- diagnostic은 `diagnostic_code`, `channel_id`, `app_id`, `probe_id`, confirmed
  `message_ts`, `cleanup_failure_count`, `provider_error_code`, `operator_action`만 포함
- 같은 revision의 composition-root integration result와 configured startup trace를 manifest와
  Package 4 checkpoint에 연결

실제 Slack E2E가 통과해도 production startup wiring 증거를 대체하지 않는다. T013 evidence가
없으면 FR-018, `readback-selfcheck-wiring`, Package 4 gate를 PASS로 기록하지 않는다.

### D-3. Forbidden Recovery Checks

Operator guide와 test에 아래 절차를 쓰지 않는다.

- channel history에서 probe text 검색
- prefix, metadata sentinel, app ID 조합 검색
- 첫 message 또는 최근 N건 선택
- 시간 window로 후보 선택
- undocumented `client_msg_id` 사용
- operator 입력으로 unresolved lifecycle 강제 해제

Stored exact `channel + ts`가 없으면 automatic cleanup을 시도하지 않는다.

## E. Expected Baseline

- 기존 test 가 **전량** 회귀 없이 통과한다. **숫자를 고정하지 않는다** — wave 마다 늘고
  갱신 장치가 없다 (SC-015)
- `amplai-foundry verify` 7 stage 통과. clean clone Python 3.11·3.12 에서도 통과 (SC-014)
- 위 명령 중 **실제로 돌린 것만** gate 기록에 쓴다. 안 돌린 것을 통과했다고 쓰지 않는다
- **skip 을 pass 로 적지 않는다.** E2E 가 skip 됐으면 gate 기록에 skip 이라고 쓴다
- `MGC-012-T013` blocked를 Package 4 PASS로 적지 않는다
