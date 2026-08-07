# Feature Specification: MGC-012 Package 4 — Slack Reference E2E And Closure

**Feature Branch**: `002-mgc-012-package-4-slack-reference-e2e`

**Created**: 2026-08-07

**Status**: Derived — Package 4 planning input

**Input**: 이 문서는 새로 쓴 요구사항이 아니다. 아래 원본의 파생이다.

## Derivation

`specs/001-mgc-012-slack-reference-adapter/spec.md` 와 같은 파생 문서다. 원본은 둘이다.

| 원본 | 위치 | 고정값 |
|---|---|---|
| 외부 SPEC | `/Users/pinesky/Documents/Codex/2026-07-29/prior-conversation-with-codex-conversation-role/outputs/SPEC.md` | sha256 `e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3` |
| Frozen Acceptance | `docs/workstreams/messenger-governance-closure-v3/CURRENT_ITEM.md` A1–A15 | MGC-012 |

**이 문서와 원본이 어긋나면 원본이 이긴다.** 이 문서를 고쳐서 요구사항을 바꾸지 않는다.

`/speckit-specify` 를 돌리지 않았다. 근거는 D-019 항목 5 다 — 생성기는 자연어 설명에서 새
문장을 만드는데 A1–A15 는 frozen 이라 어긋남을 애초에 만들지 않는다. `create-new-feature.sh`
가 디렉터리와 `.specify/feature.json` 만 만들었고 본문은 파생으로 썼다.

**디렉터리가 001 이 아닌 이유는 D-027 이다.** D-019 항목 1 은 MGC-012 전체를 한 디렉터리에
담기로 했는데, `plan.md` 가 Package 3 전용이고 파일 바깥 24곳이 참조해 덮거나 이름을 바꿀
수 없다. 같은 디렉터리에 다른 이름으로 두면 speckit script 가 `plan.md` 만 찾아 조용히
Package 3 plan 을 읽는다. D-027 이 항목 1 만 supersede 했다.

## Numbering

FR 과 SC 번호를 001 에서 **이어 붙인다** — FR-013 부터, SC-009 부터다. 두 spec 이 같은
MGC-012 를 나눠 담으므로 같은 번호가 다른 뜻을 갖지 않게 한다. task ID prefix 를 이어 쓰는
D-019 항목 3 과 같은 이유다. task ID 는 `MGC-012-T006` 부터다.

## Status And Scope

| Package | 범위 | 상태 |
|---|---|---|
| 1 | Raw request verification and normalized Slack interaction contract | 완료 — gate PASS at `ef35201` |
| 2 | Durable ingress ack boundary and background decision handoff | 완료 — gate PASS at `2dcf663` |
| 3 | Ordered Slack message projection, retry and recovery | 완료 — gate PASS at `5b2024b` |
| **4** | **Slack reference E2E, activation isolation and closure review** | **이 문서의 범위** |

대응 acceptance 는 **A14 와 A15** 다.

- **A14**: Slack adapter 가 Telegram 또는 다른 Provider activation state 를 변경하지 않음
- **A15**: Slack reference E2E, 전체 regression 과 `amplai-foundry verify` 가 통과하고
  Contract·Evidence·Ops review blocker 가 0건임

A1–A13 은 Package 1–3 에서 이미 충족했다. Package 4 는 그것들을 **실제 Slack 을 상대로
한 번 관통시켜** 계약이 실물에서도 성립하는지 본다. 재구현하지 않는다.

### Out Of Scope

`CURRENT_ITEM.md` Out Of Scope 를 그대로 잇는다.

- Telegram webhook 과 callback adapter
- Hermes natural-language Skill
- production Slack credential 발급 또는 secret rotation 실행
- Slack activation rollout
- Provider message 를 authoritative Proposal state 로 사용하는 기능

Package 4 고유의 제외 셋을 더한다.

- **Activation Gate machinery.** `ActivationEvidence` 구조와
  `Global Core → Provider → Project-Provider → Feature` 4단계 Gate 는 **MGC-015** 가
  만든다. Package 4 는 A14 의 격리 검증만 한다 (D-026).
- **D-014 destination granularity.** Package 3 와 같은 이유로 뺀다. evidence migration 이
  필요하다.
- **HTTP layer.** Slack 이 우리에게 보내는 요청을 받는 web server 는 이 범위 밖이다.
  D-012 가 "HTTP layer 가 이 package 범위 밖이라 hard deadline 을 걸 대상이 없다" 로
  Package 2 에서 이미 그렇게 뒀다. **egress(우리가 Slack 을 호출하는 쪽)만 이번에 채운다.**

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Real Slack Round Trip (Priority: P1)

실제 Slack workspace 의 채널에 Proposal Card 가 실제로 뜨고, 그 message 를 되읽어 marker
로 다시 찾아낸다.

**Why this priority**: A15 의 "Slack reference E2E" 본체다. Package 3 는 transport 를
Protocol 로만 정의했고 실제 호출을 한 번도 안 했다. 계약이 fake 에서만 성립하고 실물에서
깨지는 것이 이 wave 가 막으려는 결과다.

**Independent Test**: 실제 workspace 의 test 채널에 `chat.postMessage` 로 Card 를 보내고
`conversations.history` 로 되읽어 `event_payload` 네 필드가 복원되는지 본다.

**Acceptance Scenarios**:

1. **Given** 유효한 credential 과 test 채널이 있고, **When** `SlackProjectionDestination`
   을 실제 transport 로 돌리면, **Then** 채널에 Card 가 뜨고 receipt 가
   `slack:{channel}:{ts}` 형식으로 돌아온다.
2. **Given** 1번으로 보낸 Card 가 있고, **When** `reconcile()` 을 부르면, **Then** 그
   marker 를 찾아 1번과 **같은 receipt 문자열**을 돌려준다 (C-2.3).
3. **Given** 그 Card 를 사람이 지웠고 채널 history 를 끝까지 훑을 수 있으며,
   **When** `reconcile()` 을 부르면, **Then** 미전송으로 판정한다 (D-023 항목 3).

---

### User Story 2 - Metadata Survives The Real Round Trip (Priority: P1)

Package 3 가 미확인으로 남긴 Slack 동작 셋을 실물로 확인한다.

**Why this priority**: 셋 다 틀리면 **조용한 중복 Card** 가 난다 — hold 가 아니라서
아무도 모른다. `index.yaml` coverage.deferred 의 `W3-transport-readback-selfcheck`,
`W3-history-ordering-unverified`, `W3-history-oauth-scope` 가 Package 4 owner 로 등록돼
있다.

**Independent Test**: 되읽은 message 의 `app_id` 와 `metadata` 를 직접 대조하고,
여러 message 를 보낸 뒤 `conversations.history` 의 반환 순서를 관측한다.

**Acceptance Scenarios**:

1. **Given** 우리 app 이 보낸 Card 가 있고, **When** `include_all_metadata=true` 로
   되읽으면, **Then** `event_type` 과 `event_payload` 가 모두 오고 `app_id` 가 우리
   app 의 것이다.
2. **Given** 채널에 우리 Card 가 둘 이상 있고, **When** `conversations.history` 를
   부르면, **Then** 최신 message 가 먼저 온다 (research R-004 미확인 항목).
3. **Given** `conversations.history` 에 필요한 scope 가 없는 설치라면, **When** read 를
   시도하면, **Then** `missing_scope` 가 오고 그것이 terminal 로 분류된다.

---

### User Story 3 - Other Providers Stay Untouched (Priority: P2)

Slack 경로를 실물로 끝까지 돌려도 다른 Provider 의 상태가 바뀌지 않는다.

**Why this priority**: A14 다. 외부 SPEC 의 Stop Condition 에 "Slack activation 이
Telegram 을 활성화함" 이 있다.

**Independent Test**: Telegram destination 이 있는 store 에서 Slack 경로를 성공과 hold
양쪽으로 돌리고 Telegram 쪽 row 를 대조한다.

**Acceptance Scenarios**:

1. **Given** Telegram provider destination 과 pending event 가 있고, **When** Slack
   경로가 성공으로 끝나면, **Then** Telegram destination row 와 event state 가 그대로다.
2. **Given** 같은 상태에서, **When** Slack 쪽이 terminal 실패로 hold 를 만들면,
   **Then** hold 는 Slack destination scope 에만 걸리고 Telegram 은 계속 claim 가능하다.

### Edge Cases

- **credential 이 없거나 만료된 환경에서 E2E 를 돌리면** — 건너뛰어야 하는가 실패해야
  하는가. 조용히 건너뛰면 "통과했다" 로 보고되어 Completion Gate 규칙을 어긴다.
  plan 이 정한다.
- **`amplai-foundry verify` 와의 관계** — verify 는 clean clone 에서 도는 7 stage 이고
  network 를 전제하지 않는다. E2E 를 그 안에 넣으면 offline clone 이 깨진다. 별도 경로가
  필요하다. plan 이 정한다.
- **test 채널이 더러워지는 문제** — E2E 를 돌릴 때마다 실제 message 가 쌓인다.
  `conversations.history` 의 조회 상한(`SLACK_MAX_HISTORY_PAGES=5`, page 당 999)과
  맞물린다. 정리 방법 또는 채널 분리 전략이 필요하다.
- **rate limit** — `chat.postMessage` 는 channel 당 초당 1건, `conversations.history` 는
  Tier 2 다 (research S4). E2E 가 그것을 넘기면 test 자체가 flaky 해진다.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-013**: `SlackTransport` Protocol 의 실제 HTTP 구현을 제공한다. Protocol 자체
  (`contracts/slack-transport.md` C-1) 는 **바꾸지 않는다.** 근거: A15, D-018 항목 2.
- **FR-014**: `read_history` 구현은 `include_all_metadata=true` 를 반드시 붙인다.
  근거: C-1.2, research S3.
- **FR-015**: 실패를 모두 `SlackTransportError` 로 감싸고 Slack 이 준 error code 를
  그대로 싣는다. 근거: C-1 의무 1.
- **FR-016**: 호출 시간을 `OutboxConfig.lease_seconds` 보다 짧은 deadline 으로 묶는다.
  한 `deliver_next` 안의 **모든 호출 합계**가 기준이다. 근거: C-1 의무 2, D-024 항목 3,
  wave 4 review Advisory.
- **FR-017**: credential 은 전용 경로로만 프로세스에 들어오고 DB·Audit·log 에 남지
  않는다. 현재 repo 에는 그 경로가 **없다.** 근거: A2, A7.
- **FR-018**: adapter 는 기동 전에 자기가 보낸 message 를 되읽어 marker 가 복원되는지
  확인한다 (readback 자가검사). 근거: C-1.2, D-024 항목 2,
  `index.yaml` `W3-transport-readback-selfcheck`.
- **FR-019**: E2E 는 credential 이 없으면 **명시적으로 skip 하고 그 사실을 출력한다.**
  조용히 통과하지 않는다. 근거: CLAUDE.md Completion Gate.
- **FR-020**: E2E 는 `amplai-foundry verify` 의 기본 경로에 들어가지 않는다. verify 는
  clean clone·offline 에서 도는 계약이다. 근거: A15, SPEC `clean clone Python 3.11/3.12`.
- **FR-021**: Slack adapter 는 다른 Provider 의 outbox destination row 와 event state 를
  바꾸지 않는다. 근거: A14.
- **FR-022**: 전체 regression 과 `amplai-foundry verify` 7 stage 가 통과한다.
  근거: A15, SC-006 과 같은 규칙으로 **숫자를 고정하지 않는다.**

### Deferred Requirements

- **MGC-015** — `ActivationEvidence` 와 4단계 Activation Gate. Package 4 는 격리 검증만
  한다. 근거: D-026.
- **MGC-013** — Telegram adapter. A14 검증에는 Telegram *destination* 만 필요하고 adapter
  는 필요 없다.

### Key Entities

- **실제 Slack transport** (신규): `SlackTransport` Protocol 의 HTTP 구현. Protocol 은
  `001/contracts/slack-transport.md` C-1 이 권위다. **여기서 다시 쓰지 않는다** (D-027).
- **Credential source** (신규): signing secret 과 bot token 이 프로세스에 도달하는 경로.
  형태 미정.
- **E2E harness** (신규): 실제 workspace 를 상대로 도는 test 경로. 기본 suite 와 분리된다.

## Success Criteria *(mandatory)*

### Measurable Outcomes

외부 SPEC `Verification Matrix` 에서 Package 4 에 해당하는 것을 가져온다.

- **SC-009**: 실제 Slack 채널에 Card 가 뜨고 marker read-back 으로 다시 찾힌다. `send()`
  와 `reconcile()` 이 같은 receipt 문자열을 만든다.
- **SC-010**: readback 자가검사가 통과한다 — `app_id`, `event_type`, `event_payload` 네
  필드가 모두 복원된다.
- **SC-011**: `conversations.history` 의 최신-우선 정렬을 실물로 관측해 기록한다
  (research R-004 미확인 해소).
- **SC-012**: Provider별 독립 E2E — Slack 경로가 Telegram destination 을 안 바꾼다.
- **SC-013**: credential 없는 환경에서 E2E 가 **skip 으로 보고**되고 통과로 보고되지
  않는다.
- **SC-014**: clean clone Python 3.11·3.12 에서 `amplai-foundry verify` 가 통과한다.
  network 없이 통과한다.
- **SC-015**: 전체 regression 이 회귀 없이 통과한다. **숫자를 고정하지 않는다.**
- **SC-016**: contract·failure-recovery·regression 3 lens review blocker 가 0건이다.

## Assumptions

- 테스트용 Slack workspace 와 app 을 새로 만든다. 회사 workspace 를 쓰지 않는다 (D-026).
  **아직 만들어지지 않았다** — plan 이 external dependency 로 잡는다.
- Package 1–3 의 계약은 바꾸지 않는다. Package 4 는 그 위에 실제 호출을 얹는다.

## Unknowns For Phase 0

`/speckit-plan` Phase 0 가 `research.md` 에 공식 문서로 닫아야 하는 것들이다. **추정으로
채우지 않는다.**

- **U-001**: `chat.postMessage` 와 `conversations.history` 각각에 필요한 OAuth scope.
  `index.yaml` `W3-history-oauth-scope` 가 미확인으로 등록돼 있다. 틀리면 send 는
  성공하고 첫 재시도의 read 에서 `missing_scope` → terminal → 되돌릴 수 없는 hold 다.
- **U-002**: HTTP client 를 무엇으로 하는가. 현재 runtime dependency 는
  `pydantic`·`pyyaml`·`typer` 셋뿐이다 (`pyproject.toml`). stdlib `urllib` 로 갈지 새
  dependency 를 넣을지는 Constitution II (작은 검증 가능한 변경) 판단이 필요하다.
- **U-003**: credential 이 프로세스에 도달하는 경로. repo 전체에 Slack 설정을 읽는
  `os.environ` 이 한 줄도 없고 `SlackInstallationPolicy` 를 코드에서 직접 만들어 넘긴다.
- **U-004**: E2E 를 어디서 어떻게 돌리는가. `amplai-foundry verify` 와의 관계,
  credential 부재 시 skip 방식, test 채널 오염과 rate limit 대응.
- **U-005**: `conversations.history` 가 최신 message 부터 돌려주는지 (research R-004).
  공식 문서에서 못 찾았고 page 사이 순서가 뒤집히면 중복 Card 가 난다.
