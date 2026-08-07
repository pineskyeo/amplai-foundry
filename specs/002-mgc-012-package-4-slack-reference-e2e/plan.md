# Implementation Plan: MGC-012 Package 4 — Slack Reference E2E And Closure

**Branch**: `002-mgc-012-package-4-slack-reference-e2e` | **Date**: 2026-08-07 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-mgc-012-package-4-slack-reference-e2e/spec.md`

## Summary

Package 3 가 `SlackTransport` 를 Protocol 로만 정의했다. Package 4 는 **그 Protocol 의 실제
HTTP 구현 하나를 만들고**, 진짜 Slack workspace 를 상대로 한 번 관통시켜 계약이 실물에서도
성립하는지 본다. Protocol 자체와 `SlackProjectionDestination` 은 바꾸지 않는다.

핵심 설계는 넷이다.

1. **stdlib 로 간다.** `urllib.request` 를 쓰고 dependency 셋을 유지한다 (R-013).
2. **credential 은 entrypoint 한 곳에서만 읽는다.** core 는 계속 주입만 받는다 (R-014).
3. **E2E 는 기본 suite 밖이다.** marker 로 분리하고 credential 이 없으면 skip 하되 그
   사실을 출력한다. `amplai-foundry verify` 는 offline 계약을 유지한다 (R-015).
4. **app 은 internal 로 둔다.** 배포하면 `conversations.history` 가 분당 1회로 떨어져
   reconcile 설계가 성립하지 않는다 (R-012).

## Technical Context

**Language/Version**: Python 3.11 / 3.12 (`pyproject.toml`, SPEC `AC-15`)

**Primary Dependencies**: `pydantic`, `pyyaml`, `typer` — **셋을 유지한다.** HTTP 는
stdlib `urllib.request` 다 (R-013)

**Storage**: SQLite governance store. Package 4 는 schema 를 바꾸지 않는다

**Testing**: `python -m pytest` 전량 + marker 로 분리된 E2E. 숫자를 baseline 으로 고정하지
않는다 (SC-015)

**Target Platform**: local CLI / worker process

**Project Type**: single project — `src/amplai_foundry/`

**Performance Goals**: 별도 목표 없음. Slack 상한은 `chat.postMessage` channel 당 초당 1건
(P1), `conversations.history` **Tier 3 분당 50+** (P2, R-010 정정)

**Constraints**:
- `contracts/slack-transport.md` C-1 (001) 을 바꾸지 않는다. 실제 구현이 그 계약을 만족한다
- 한 `deliver_next` 안의 **모든 호출 합계**가 `OutboxConfig.lease_seconds` 보다 짧아야 한다
- `amplai-foundry verify` 는 network 없이 통과해야 한다 (SC-014)
- app 은 internal 이어야 한다 (R-012)

**Scale/Scope**: 신규 module 1~2개, 신규 test file 1개. 기존 `slack_projection.py` 와
`slack.py` 는 **수정하지 않는다**

## Constitution Check

*GATE: Phase 0 전 통과, Phase 1 후 재확인.*

| 원칙 | 판정 | 근거 |
|---|---|---|
| I. Knowledge Safety | PASS | canonical Vault 를 안 건드린다. D-019 항목 1 번복은 D-027 로 `supersedes` 관계를 기록했다 |
| II. Small Verifiable Change | PASS | dependency 셋 유지 (R-013). vector DB·MCP server·web UI 를 안 넣는다. HTTP client 를 추가하지 않는다 |
| III. Evidence-Based Completion | PASS (조건부) | E2E 는 credential 부재 시 **skip 으로 보고**하고 통과로 보고하지 않는다 (FR-019, R-015). gate 는 실제 실행 결과로만 연다 |
| IV. Governed Mutation Only | N/A | Package 4 는 Proposal mutation 경로를 안 만든다 |
| V. Independent Review Before Gate | PASS | wave 마다 contract·failure-recovery·regression 3 lens (D-019 항목 4). **regression lens 는 단독으로 돌린다** — wave 4 review 가 기록한 process violation 이다 |

**Phase 1 재확인**: 아래 Phase 1 산출물은 새 원칙을 만들지 않고 dependency 를 늘리지 않는다.
판정 유지.

위반 없음 — Complexity Tracking 는 비운다.

## Decisions Carried From Research

`research.md` 가 넘긴 판단 둘을 여기서 닫는다.

### P-003 — Missing Credentials Skip Loudly, They Do Not Fail

**결정**: credential 이 없으면 E2E 를 **skip** 한다. 실패시키지 않는다.

**근거**: 두 실패 모드를 견준다. skip 을 pass 로 **조용히** 보고하면 Constitution III
위반이고 gate 가 무효가 된다 — 그건 절대 안 된다. 그러나 credential 없는 환경에서
**실패**시키면 clean clone 의 `python -m pytest` 가 빨간색이 되어 SC-014·SC-015 와 충돌하고,
개발자가 그 실패를 일상적으로 무시하게 되어 진짜 회귀를 가린다.

pytest 의 skip 은 **이유와 함께 출력되고 요약에 집계된다.** "안 돌았다" 가 보이면서
suite 는 초록이다. 그것이 이 상황의 정직한 표현이다.

**기각**: `xfail` 로 두는 안. credential 이 있을 때도 통과를 기대하지 않는다는 뜻이 되어
의미가 반대다.

### P-004 — The Real Transport Is A New Module, Not An Extension

**결정**: 실제 HTTP 구현을 **새 module** 에 둔다. `slack_projection.py` 를 수정하지 않는다.

**근거**: Package 3 의 `slack_projection.py` 는 gate PASS 한 계약이고 wave 4 가
"production code 를 한 줄도 안 바꿨다" 를 증거로 남겼다. network 코드를 그 파일에 넣으면
188개 test 가 network 를 다루는 파일에 붙는다. 방향도 다르다 — `slack.py` 가 ingress,
`slack_projection.py` 가 egress **계약**, 새 module 이 egress **전송**이다.

**기각**: `slack_projection.py` 안에 `HttpSlackTransport` 를 넣는 안. Package 3 의 module
docstring 이 "이 module 은 Slack 을 실제로 호출하지 않는다" 로 시작한다. 그 문장을 거짓으로
만든다.

## Project Structure

### Documentation (this feature)

```text
specs/002-mgc-012-package-4-slack-reference-e2e/
├── plan.md              # 이 파일
├── spec.md              # 파생 spec (원본은 외부 SPEC.md + CURRENT_ITEM.md)
├── research.md          # Phase 0 — R-009~R-015
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1 — workspace 만드는 절차 포함
├── contracts/
│   └── slack-http-transport.md   # 실제 transport 와 credential 계약
└── tasks.md             # /taskify 산출물. 손으로 쓰지 않는다
```

**001 의 `contracts/slack-transport.md` 를 다시 쓰지 않는다.** C-1 은 거기가 권위이고
002 의 contract 는 그것을 **참조**한 뒤 구현 쪽 의무만 새로 쓴다 (D-027).

### Source Code (repository root)

```text
src/amplai_foundry/governance/
├── slack.py                  # 수정 없음 — ingress 인증
├── slack_projection.py       # 수정 없음 — egress 계약과 destination
└── slack_http.py             # 신규 — SlackTransport 의 urllib 구현

src/amplai_foundry/
└── (composition root)        # 신규 — 환경변수를 SecretStr 로 읽는 지점 하나

tests/
└── test_slack_http.py        # 신규 — 단위 test 와 marker 로 분리된 E2E
```

**Structure Decision**: 기존 단일 package 구조를 그대로 쓴다. credential 을 읽는 지점의
정확한 위치는 Phase 1 `contracts/` 가 정한다 — CLI 인지 별도 factory 인지는 기존
entrypoint 구조를 보고 결정한다.

## Implementation Waves

wave 는 `/taskify` 가 `index.yaml` 에 확정한다. 아래는 제안 경계다. task ID 는
`MGC-012-T006` 부터 이어 쓴다 (D-019 항목 3).

### Wave 5 — HTTP Transport Against A Fake Server

- `urllib` 기반 `SlackTransport` 구현
- 모든 예외를 `SlackTransportError` 로 감싸기 (C-1 의무 1) — `HTTPError`, `URLError`,
  `socket.timeout`, `ssl.SSLError`
- Slack 의 `ok:false` 응답에서 error code 를 꺼내 싣기
- 호출 단위 timeout
- 검증: `http.server` 로 띄운 local fake 를 상대로 한 단위 test. **network 없음**

### Wave 6 — Credential Path And Readback Self-Check

- 환경변수를 `SecretStr` 로 읽는 지점 하나
- 기동 전 readback 자가검사 (FR-018) — 자기가 보낸 message 를 되읽어 marker 복원 확인
- 검증: credential 부재·형식오류 fixture. secret 이 log·repr 에 안 나오는 것 고정

### Wave 7 — Real Slack E2E

- marker 로 분리된 E2E. credential 없으면 skip
- User Story 1·2·3 의 acceptance scenario
- 검증: 실제 workspace. **이 wave 는 workspace 생성이 선행 조건이다**

### Wave 8 — Closure

- 전체 regression, `amplai-foundry verify` 7 stage
- clean clone Python 3.11·3.12 (SC-014)
- 001 research.md 에 R-010 forward pointer, `index.yaml` 의 W3 항목 셋 닫기
- Package 3·4 를 합친 MGC-012 closure review

wave 순서는 dependency 순이다. wave 7 은 wave 5·6 을 모두 필요로 한다.

## External Dependencies

**task 로 만들 수 없고 사람이 해야 하는 것이다.** `/taskify` 는 이것을
`dependencies.external` 로 잡는다.

| # | 항목 | 상태 |
|---|---|---|
| E-1 | 테스트용 Slack workspace 생성 | **미완** |
| E-2 | Slack app 생성, scope `chat:write`·`channels:history` 부여, workspace 설치 | **미완** |
| E-3 | test 채널 생성과 app 초대 | **미완** |
| E-4 | signing secret·bot token 을 실행 환경에 주입 | **미완** |

절차는 `quickstart.md` 가 쓴다. **E-1~E-4 가 끝나기 전에는 wave 7 을 시작할 수 없다.**
wave 5·6 은 network 없이 돌므로 선행 조건이 없다.

## Out Of Scope For This Plan

- **Activation Gate machinery** — `ActivationEvidence` 와 4단계 Gate 는 MGC-015 다 (D-026)
- **HTTP ingress layer** — Slack 이 우리에게 보내는 요청을 받는 web server. D-012 가
  Package 2 에서 이미 범위 밖으로 뒀다
- **D-014 destination granularity** — evidence migration 이 필요한 별도 item
- **`Retry-After` 를 backoff 에 주입** — `OutboxDispatcher` 계약 변경이라 별도 item (R-007)
- **배포용 rate limit 대응** — R-012 의 cliff 는 배포 형태를 정할 때 판단한다. 지금
  고치는 것은 speculative work 다

## Complexity Tracking

Constitution Check 위반 없음. 비운다.
