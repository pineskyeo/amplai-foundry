# Feature Specification: MGC-012 Package 4 — Slack Reference E2E And Closure

**Feature Branch**: `002-mgc-012-package-4-slack-reference-e2e`

**Created**: 2026-08-07

**Status**: Approved — D-031 ledger synchronized

**Input**: 기존 Package 4 요구사항과 2026-08-10~11 `$grilling`에서 사용자가 승인한
D-031 probe cleanup 결정을 합친 specification이다.

## Clarifications

### Session 2026-08-11

- Q: cleanup-only degraded 진단의 “정확히 한 번”은 어느 범위에서 보장해야 합니까? → A: 각 production startup 평가마다 정확히 1회 출력한다. 재기동은 새 평가이므로 다시 1회 출력한다.
- Q: 같은 channel/app에서 두 startup이 동시에 시작되면 어느 쪽이 probe 게시 권한을 가져야 합니까? → A: durable intent를 원자적으로 먼저 확보한 하나만 게시하고 나머지는 `HARD_BLOCKED_NO_POST`로 끝난다.
- Q: Slack 삭제 성공 응답을 받았지만 durable lifecycle을 `RESOLVED`로 기록하지 못하면 현재 startup은 어떻게 처리해야 합니까? → A: 마지막 committed lifecycle state를 유지하고 `HARD_BLOCKED_NO_POST` outcome으로 worker activation을 거부한다.
- Q: 명시적 provider identity가 없어 `HARD_BLOCKED_NO_POST` outcome이 된 lifecycle을 이번 Package 4에서 해제할 수 있어야 합니까? → A: 해제 기능은 별도 governed recovery work item으로 미루고 T013은 blocked로 유지한다.
- Q: cleanup-only degraded 진단에 허용할 field 범위를 어떻게 고정해야 합니까? → A: `diagnostic_code`, `channel_id`, `app_id`, `probe_id`, confirmed `message_ts`, `cleanup_failure_count`, `provider_error_code`, `operator_action`만 허용한다.
- Q: `HARD_BLOCKED_NO_POST`를 durable lifecycle state가 아니라 startup outcome으로만 사용할까요? → A: outcome으로만 사용한다. Durable lifecycle은 `POST_INTENT_RECORDED`, `AMBIGUOUS_POST`, `CLEANUP_PENDING` 같은 원인 state를 그대로 보존한다.
- Q: 이전 실행의 `CLEANUP_PENDING`에 confirmed `channel + ts`가 있을 때 새 startup은 exact cleanup 결과에 따라 어떻게 진행해야 합니까? → A: exact cleanup과 durable `RESOLVED` commit이 모두 성공한 뒤 fresh self-check를 시작한다. Cleanup 또는 commit이 실패하면 `HARD_BLOCKED_NO_POST` outcome으로 worker activation과 새 post를 막는다.
- Q: 저장된 probe lifecycle 레코드가 손상됐거나 지원하지 않는 schema/state 버전이면 startup을 어떻게 처리할까요? → A: 별도의 typed lifecycle failure를 반환하고 기존 레코드를 보존하며 worker activation, 새 post와 자동 수정을 모두 금지한다.
- Q: readback과 cleanup이 함께 실패할 때 보조 cleanup 실패 정보를 어떤 형태와 보안 경계로 전달할까요? → A: 최대 하나의 immutable typed `CleanupFailureDetail`로 전달하고 닫힌 safe field allowlist를 모든 진단 표면에 적용하며 원본 예외 연결, 자유형 문자열과 raw provider data를 금지한다.
- Q: readback 실패를 어떤 유한한 분류로 고정해 모든 cleanup 조합을 검증할까요? → A: `PROBE_INPUT_INVALID`, `HISTORY_READ_FAILED`, `PROBE_NOT_FOUND`, `APP_ID_MISMATCH`, `MARKER_UNREADABLE`, `MARKER_MISMATCH`의 closed enum을 사용하고 게시 후 다섯 분류는 cleanup 성공·실패 양쪽을 검증한다.
- Q: process가 probe 게시 과정의 각 crash 경계에서 멈추면 durable state와 다음 startup을 어떻게 처리할까요? → A: durable intent 전에는 post하지 않고, committed intent 이후의 불확실성은 fail closed한다. 성공 응답 뒤 identity commit 실패에는 in-memory confirmed identity로 exact cleanup과 durable resolution을 한 번 시도하되 둘 다 성공한 경우에만 다음 startup의 fresh claim을 허용한다.
- Q: T013을 blocked에서 ready로 바꾸고 FR-018과 Package 4 production wiring을 완료하려면 어떤 승인·실행 증거가 필요합니까? → A: E-7~E-10 각각의 named approval record를 먼저 요구하고, 완료에는 production composition root 통합 실행 결과와 실제 production channel/app/transport 구성의 safe startup trace를 모두 요구한다.

### Session 2026-08-12

- Q: Slack 성공 응답의 confirmed channel이 configured target channel과 다르면 어떤 closed failure로 판정합니까? → A: 전용 `RESPONSE_CHANNEL_MISMATCH` primary code를 추가한다. Worker activation을 거부하고 history를 configured channel에서 찾았다고 주장하지 않으며, cleanup은 Slack이 확인한 response `channel + ts`로 시도한다. Cleanup 성공·실패 양쪽에서 primary code를 보존한다.

## Derivation

`specs/001-mgc-012-slack-reference-adapter/spec.md` 와 같은 파생 문서다. 원본은 둘이다.

| 원본 | 위치 | 고정값 |
|---|---|---|
| 외부 SPEC | `/Users/pinesky/Documents/Codex/2026-07-29/prior-conversation-with-codex-conversation-role/outputs/SPEC.md` | sha256 `e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3` |
| Frozen Acceptance | `docs/workstreams/messenger-governance-closure-v3/CURRENT_ITEM.md` A1–A15 | MGC-012 |

**A1–A15와 이 문서가 어긋나면 A1–A15가 이긴다.** 단, D-031은 사용자가 승인한 Package 4
failure-recovery refinement다. D-031은 D-030 전체를 폐기하지 않고 "readback은 통과했지만
probe 삭제만 실패하면 startup을 거부한다"는 부분만 supersede한다. 나머지 frozen
acceptance와 D-030의 삭제 시도·원인 보존 의무는 유지한다.

2026-08-11 `/speckit-specify`는 이 승인된 refinement를 specification에 반영했다. 같은 날
workstream decision workflow가 D-031과 APR-016을 기록하고 D-030에 `status: superseded`와
`superseded_by: D-031`을 연결했다.

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
- **외부 supervisor 정책.** 애플리케이션은 배포 환경의 재시작 여부를 통제한다고 주장하지
  않는다. 대신 몇 번 재시작돼도 unresolved probe가 있으면 새 probe를 만들지 않는 startup
  계약을 제공한다.
- **휴리스틱 probe 식별.** message 본문·prefix·위치·조회 건수·시간 간격으로 probe를
  추정하지 않는다. 공식적으로 확인된 명시적 식별자와 durable cleanup state가 없으면
  production wiring을 완료하지 않는다.
- **Operator recovery mutation.** `HARD_BLOCKED_NO_POST` outcome의 원인 lifecycle을 강제로
  해제하거나 원격 상태를 추정해 resolved로 바꾸는 기능은 별도 governed recovery work item으로 미룬다.
  그 계약이 승인되기 전에는 T013을 blocked로 유지한다.

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

---

### User Story 4 - Cleanup Failure Stays Bounded And Visible (Priority: P1)

운영자는 marker readback이 정상인데 probe 정리만 실패한 경우에도 Slack worker를 사용한다.
`degraded` — 핵심 기능은 시작하지만 cleanup 이상이 남은 상태다. 운영자는 degraded 상태와
복구 조치를 각 startup 평가에서 정확히 한 번 확인한다. 이전 probe가 unresolved면 exact
recovery와 durable resolution이 끝날 때까지 worker는 새 probe 없이 멈춘다.

**Why this priority**: 삭제 실패를 무조건 startup 거부로 만들면 supervisor 재시작마다
probe가 늘 수 있다. 삭제 실패를 조용히 무시하면 operator가 누적 위험을 모른다. 가용성과
조회 예산 보호를 함께 만족하려면 degraded startup, 명시적 진단, bounded recovery가 모두
필요하다.

**Independent Test**: readback 결과, 현재 probe 삭제 결과, 이전 cleanup state를 독립적으로
조합해 worker 활성화 여부, 추가 probe 수, operator 진단 수를 대조한다.

**Acceptance Scenarios**:

1. **Given** marker readback은 완전하게 성공했고 현재 probe 삭제만 실패했으며 이전
   unresolved probe가 없는 상태에서, **When** startup 검사가 끝나면, **Then** worker는
   degraded 상태로 활성화되고 해당 startup 평가에서 operator 진단이 정확히 한 번 생성된다.
2. **Given** marker readback이 실패한 상태에서, **When** startup 검사가 끝나면, **Then**
   Slack worker는 활성화되지 않고 readback의 원래 실패 원인이 보존된다.
3. **Given** 이전 실행의 unresolved probe가 durable state에 있고 exact cleanup과 durable
   resolution이 완료되지 않은 상태에서, **When** startup을 몇 번 반복하더라도, **Then**
   새 probe는 한 건도 게시되지 않는다.
4. **Given** 이전 probe의 잔여 여부를 명시적 근거로 확정할 수 없는 상태에서, **When**
   startup 검사를 시도하면, **Then** 원인 lifecycle state는 그대로 보존되고 startup outcome은
   `HARD_BLOCKED_NO_POST`이며 새 probe는 게시되지 않는다.
5. **Given** cleanup-only degraded 상태에서, **When** operator 진단을 확인하면, **Then**
   닫힌 safe field allowlist만 보이고 credential, 원문 응답, 예외 전체 표현과 확인되지 않은
   `message_ts`는 보이지 않는다.
6. **Given** Slack probe 삭제 성공 응답을 받았지만 durable lifecycle을 `RESOLVED`로 기록하지
   못한 상태에서, **When** startup 판정을 끝내면, **Then** worker는 활성화되지 않고
   마지막 committed lifecycle state가 유지되며 startup outcome은 `HARD_BLOCKED_NO_POST`다.
7. **Given** 이전 실행의 `CLEANUP_PENDING`에 confirmed `channel + ts`가 있는 상태에서,
   **When** 새 startup이 시작되면, **Then** stored exact identity로 cleanup하고 durable
   `RESOLVED` commit까지 성공한 뒤에만 fresh intent와 self-check probe를 만든다. Cleanup 또는
   commit이 실패하면 새 post와 worker activation은 각각 0건이다.

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
- **readback과 삭제가 함께 실패** — readback의 원래 원인이 startup 거부를 결정하고,
  cleanup 실패는 원인을 가리지 않는 최대 하나의 typed `CleanupFailureDetail`로 남는다.
  이 보조 객체는 `cleanup_failure_count`와 allowlisted `provider_error_code`만 가진다.
- **post는 도달했지만 응답을 잃음** — `ts`를 받았다고 가정하지 않는다. 명시적 식별과
  durable recovery가 증명되지 않으면 다음 probe를 보내지 않는다.
- **crash boundary** — intent commit 전 crash는 remote side effect가 없으므로 다음 startup의
  fresh claim을 허용한다. Committed `POST_INTENT_RECORDED`를 발견한 startup은 실제 post 여부를
  추정하지 않고 원인 state를 보존한 채 `HARD_BLOCKED_NO_POST`를 반환한다. Post 응답 불명은
  가능한 경우 `AMBIGUOUS_POST`를 commit하고, 그 commit도 실패하면 마지막 intent state를
  보존한다.
- **성공 응답 뒤 identity commit 실패** — 현재 process가 보유한 confirmed `channel + ts`로
  exact delete를 최대 한 번 수행하고 durable `RESOLVED` 기록을 시도한다. 둘 다 성공한 경우에만
  다음 startup의 fresh claim을 허용한다. Delete 또는 resolution commit이 실패하거나 process가
  그 전에 crash하면 마지막 committed cause를 보존하고 현재·다음 startup의 activation과 새
  post를 금지한다.
- **metadata가 손상된 이전 probe** — marker 복원으로 probe를 찾을 수 있다고 가정하지
  않는다. message text나 위치로 추정하지 않고 production wiring을 blocked로 유지한다.
- **이전 cleanup state 조회 실패** — "남은 probe 없음"으로 간주하지 않는다. 새 probe를
  보내지 않는 hard failure다.
- **손상되거나 지원하지 않는 lifecycle 레코드** — 구조 검증 실패, 알 수 없는 state 값 또는
  지원하지 않는 schema/state version은 정상·미해결 lifecycle로 추정하지 않는다. 기존 레코드를
  수정하지 않고 typed lifecycle failure를 반환하며 readback, 새 post와 worker activation을
  각각 0회 수행한다.
- **삭제 성공 뒤 lifecycle resolution 기록 실패** — 원격 삭제 성공만으로 완료를 추정하지
  않는다. 마지막 committed lifecycle state를 유지하고 `HARD_BLOCKED_NO_POST` outcome으로
  worker activation을 거부한다.
- **반복된 process restart** — supervisor 동작과 무관하게 unresolved state가 해소되기
  전에는 추가 probe 수가 0이어야 한다.
- **이전 `CLEANUP_PENDING` recovery** — confirmed `channel + ts`로 exact cleanup하고 durable
  `RESOLVED` commit까지 성공해야 같은 startup에서 fresh self-check를 시작할 수 있다. Delete
  failure, state write failure 또는 commit ambiguity는 원인 state를 유지하고
  `HARD_BLOCKED_NO_POST` outcome을 반환한다.
- **동일 channel/app의 동시 startup** — durable intent를 원자적으로 먼저 확보한 startup
  하나만 probe를 게시한다. 나머지는 network post 없이 `HARD_BLOCKED_NO_POST`로 끝난다.
- **operator 진단 중복** — 판정 계층은 결과만 만들고 production composition root 한 곳만
  진단을 출력한다.

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
  않는다. T007이 확정한 credential source contract를 사용한다. 근거: A2, A7.
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
- **FR-023**: startup readback 판정은 정상, cleanup-only degraded, worker activation을 막는
  hard failure를 구분한다. cleanup-only degraded는 readback이 완전하게 성공한 경우에만
  가능하다. 근거: D-031.
- **FR-024**: readback은 성공했고 현재 probe 삭제만 실패하면 Slack worker activation을
  허용한다. 이 결과를 정상으로 숨기지 않고 `DEGRADED_CLEANUP`으로 전달한다. 근거: D-031.
- **FR-025**: readback이 실패하면 Slack worker activation을 거부한다. 삭제도 함께
  실패하면 readback의 원래 원인이 우선하고 cleanup 실패는 보조 진단으로 보존된다.
  근거: D-030 유지 부분과 D-031.
- **FR-026**: probe lifecycle은 공식적으로 검증된 명시적 probe identity와 durable cleanup
  state로 추적한다. Durable cleanup state는 process 재시작 뒤에도 남는 cleanup 기록이다.
  Message 본문·prefix·위치·조회 건수·시간 간격 또는 그 밖의 휴리스틱을
  식별 근거로 사용하지 않는다. Slack 삭제 성공 응답 뒤 durable lifecycle을 `RESOLVED`로
  기록하지 못하면 완료로 추정하지 않는다. Durable lifecycle은 마지막 committed 원인 state를
  유지하고 startup은 `HARD_BLOCKED_NO_POST` outcome을 반환한다. `HARD_BLOCKED_NO_POST`는
  persisted lifecycle state가 아니다.
- **FR-027**: readback에 성공해 명시적으로 식별 가능한 미삭제 probe는 channel/app별 최대
  1개다. 이전 `CLEANUP_PENDING`에 confirmed `channel + ts`가 있으면 stored exact identity로
  cleanup하고 durable `RESOLVED` commit까지 성공한 뒤에만 fresh intent를 claim할 수 있다.
  Cleanup 또는 commit이 실패하거나 잔여 여부를 확정할 수 없으면 새 probe를 게시하지 않고
  `HARD_BLOCKED_NO_POST` outcome으로 판정한다. 같은 channel/app의 concurrent startup은
  durable intent를 원자적으로 먼저 확보한 하나만 post할 수 있다. 확보하지 못한 startup은
  network post를 0회 수행하고 `HARD_BLOCKED_NO_POST` outcome으로 판정한다.
- **FR-028**: unresolved probe가 exact cleanup과 durable `RESOLVED` commit으로 해소되기
  전까지 process startup을 반복해도 추가 probe는 0개여야 한다. Resolution이 완료된 뒤에만
  같은 startup에서 fresh self-check post를 허용한다. 외부 supervisor의 재시작 여부가 아니라
  startup 동작의 반복 안전성을 보장한다.
- **FR-029**: readback 판정 계층은 typed outcome과 안전한 진단 data만 반환하고 operator
  output을 직접 만들지 않는다. production composition root 한 곳만 outcome을 operator
  진단으로 변환한다.
- **FR-030**: cleanup-only degraded 진단은 안정적인 code
  `SLACK_PROBE_CLEANUP_DEGRADED`로 각 production startup 평가마다 정확히 한 번 출력한다.
  같은 평가 안의 중복 출력은 0건이고, process 재기동은 새 startup 평가이므로 다시 한 번
  출력한다. 허용 field는 `diagnostic_code`, `channel_id`, `app_id`, `probe_id`,
  `cleanup_failure_count`, `provider_error_code`, `operator_action`과 Slack이 확인한 경우의
  `message_ts`뿐이다. `operator_action`은 안정적인 action code다. 확인되지 않은 `message_ts`,
  credential, HTTP 원문, 예외 전체 표현과 allowlist 밖 field는 포함하지 않는다.
- **FR-031**: production startup은 Slack worker가 event를 claim하기 전에 readback 판정을
  한 번 실행하고, 같은 production channel/app/transport 계약을 사용한다. 이 wiring의
  실행 증거가 없으면 FR-018은 production 완료가 아니다.
- **FR-032**: D-031은 D-030의 "readback 성공 후 삭제만 실패해도 startup 거부" 부분과
  그에 해당하는 T008 acceptance만 supersede한다. 성공·실패 양쪽의 삭제 시도, readback
  실패 시 거부, 원래 원인 우선, probe 격리 의무는 유지한다.
- **FR-033**: probe lifecycle 레코드의 구조가 손상됐거나 state 값 또는 schema/state version을
  현재 구현이 지원하지 않으면 별도의 typed lifecycle failure로 분류한다. 기존 durable
  레코드를 보존하고 자동 보정·삭제·transition, readback, 새 probe post와 worker activation을
  각각 0회 수행한다.
- **FR-034**: readback 실패가 primary인 outcome은 optional
  `secondary_cleanup_failure: CleanupFailureDetail | null`을 최대 하나만 가진다.
  `CleanupFailureDetail`의 field는 `cleanup_failure_count`와 allowlisted
  `provider_error_code`뿐이며 primary failure를 대체하거나 감싸지 않는다. 원본 exception
  chaining, 자유형 error 문자열, provider body/header는 구조화 log, exception output, metric,
  persisted failure diagnostic과 operator output에 전달하지 않는다. Metric label은 `outcome`,
  `diagnostic_code`와 allowlisted `provider_error_code`만 허용하며 channel/app/probe/message
  identity를 label로 사용하지 않는다.
- **FR-035**: readback primary failure code는 `PROBE_INPUT_INVALID`,
  `HISTORY_READ_FAILED`, `PROBE_NOT_FOUND`, `APP_ID_MISMATCH`, `MARKER_UNREADABLE`,
  `MARKER_MISMATCH`, `RESPONSE_CHANNEL_MISMATCH`의 closed enum이다. `PROBE_INPUT_INVALID`는
  network post 전 probe 입력 또는 격리 계약 위반이며 cleanup은 적용되지 않는다. 나머지 여섯
  code는 probe post 뒤 발생하며 cleanup 성공·실패 각각과 조합할 수 있다.
  `RESPONSE_CHANNEL_MISMATCH`는 Slack 성공 응답의 confirmed channel이 configured target과 다를
  때 사용한다. 이 경우 configured channel history를 조회하지 않고 worker activation을 거부하며,
  cleanup은 confirmed response `channel + ts`로 시도한다. Provider code는 primary failure code를
  대신하지 않고 FR-030/FR-034의 allowlisted diagnostic field로만 전달한다.
- **FR-036**: probe post crash boundary는 다음 순서로 fail closed한다. Durable intent commit 전
  network post는 0회이며 crash 뒤 fresh claim이 가능하다. 기존 startup의 committed
  `POST_INTENT_RECORDED`를 발견하면 post 여부를 추정하지 않고 원인 state를 보존한 채
  `HARD_BLOCKED_NO_POST`를 반환한다. Post 응답을 잃으면 가능한 경우 `AMBIGUOUS_POST`를 durable
  commit하며 commit 실패 시 마지막 intent state를 보존한다. 성공 응답을 받았지만 confirmed
  identity commit이 실패하면 in-memory confirmed `channel + ts`로 exact delete를 최대 한 번
  수행하고 durable `RESOLVED`를 기록한다. 이 delete와 resolution commit이 모두 성공한 경우에만
  후속 startup의 fresh claim을 허용하며, 그 밖에는 activation과 새 post를 금지한다.
- **FR-037**: T013은 E-7~E-10 각각의 named readiness evidence와 approval record가 task
  manifest에 연결된 뒤에만 `ready`가 될 수 있다. E-7은 production entrypoint path, owner와
  pre-claim insertion point를 승인한 workstream decision, E-8은 검토 날짜·provider 문서 version·
  exact recovery 지원 여부를 기록한 official-contract review, E-9는 actor/permission,
  ActionToken, audit/evidence와 허용 transition을 정한 governed recovery work item 승인,
  E-10은 schema version, migration, channel/app unresolved uniqueness와 repository owner를 정한
  schema decision을 요구한다. E-8이 exact recovery 미지원을 확인하면 heuristic을 허용하지 않고
  E-9의 governed recovery 계약을 필수로 유지한다. E-7~E-10의 승인 권한자는 사용자이며
  governed workstream decision workflow로 승인한다. Slack official documentation은 E-8의
  primary evidence source이지 승인 권한자가 아니다. E-8 review가 exact recovery 미지원을
  확인해도 E-9가 승인되면 T013 구현 자체를 막지 않는다. 다만 automatic ambiguous-message
  cleanup과 provider-side global remote cap 주장은 계속 blocked다.

### Deferred Requirements

- **MGC-015** — `ActivationEvidence` 와 4단계 Activation Gate. Package 4 는 격리 검증만
  한다. 근거: D-026.
- **MGC-013** — Telegram adapter. A14 검증에는 Telegram *destination* 만 필요하고 adapter
  는 필요 없다.
- **Governed probe recovery** — 명시적 provider identity가 없는 lifecycle의 operator
  해제, 권한, 감사, evidence 계약. 별도 work item이 승인되기 전에는 Package 4와 T013에
  강제 해제 기능을 넣지 않는다.

### Key Entities

- **실제 Slack transport** (신규): `SlackTransport` Protocol 의 HTTP 구현. Protocol 은
  `001/contracts/slack-transport.md` C-1 이 권위다. **여기서 다시 쓰지 않는다** (D-027).
- **Credential source**: T007이 확정한 환경변수 네 값을 entrypoint 한 곳에서 읽어 core에
  주입한다. Credential은 DB·Audit·log에 남기지 않는다.
- **E2E harness** (신규): 실제 workspace 를 상대로 도는 test 경로. 기본 suite 와 분리된다.
- **Readback outcome**: startup이 정상, cleanup-only degraded, hard failure 중 어느 상태인지와
  operator에게 전달할 안전한 진단 data를 나타낸다. `HARD_BLOCKED_NO_POST`는 이 outcome의
  한 종류이며 persisted lifecycle state가 아니다. 판정 자체는 출력 부작용이 없다.
- **Probe cleanup state**: 명시적 probe identity, 대상 channel/app, lifecycle 상태와 recovery
  근거를 담는 durable operational state다. Markdown 공식 지식이나 추정값으로 대체하지
  않는다. Hard block에서도 `POST_INTENT_RECORDED`, `AMBIGUOUS_POST`, `CLEANUP_PENDING` 같은
  원인 state를 보존한다.
- **Operator diagnostic**: composition root가 한 번 출력하는 안정적인 event다. cleanup
  상태와 조치만 알린다. Field schema는 FR-030의 닫힌 allowlist이며 secret이나 provider
  원문을 포함하지 않는다.

### Delivery Boundaries

- **MGC-012-T008**은 readback outcome과 bounded cleanup 판정의 단위 계약을 소유한다.
  production startup이 실제로 이 판정을 소비한다고 주장하지 않는다.
- **MGC-012-T013**은 production composition root wiring, operator 진단 1회 출력,
  `HARD_BLOCKED_NO_POST` 반복 안전성을 소유한다.
- E-7 production entrypoint, E-8 official-contract review, E-9 governed recovery work item 또는
  E-10 lifecycle schema approval이 없으면 T013은 blocked다. Provider가 exact recovery를
  지원하지 않는다는 E-8 결론은 automatic remote recovery만 막고 E-9가 승인된 T013의 local
  fail-closed 구현을 막지 않는다.
- Operator 강제 해제는 T013 scope가 아니다. 별도 governed recovery 계약이 승인되지 않은
  상태를 T013 구현으로 우회하지 않는다.
- T013의 acceptance와 실행 증거가 없으면 `readback-selfcheck-wiring`, FR-018, Package 4
  production startup을 완료로 표시하지 않는다.
- T013 configured startup trace는 T010이 검증한 실제 target channel/app과 credential path를
  재사용하므로 T010 완료를 T013 실행의 내부 선행 조건으로 둔다.

### T013 Production Evidence

FR-018과 Package 4 production wiring 완료에는 다음 두 증거가 모두 필요하다.

1. **Composition-root integration result** — 승인된 production entrypoint를 deterministic
   instrumented dependency로 실행해 lifecycle 판정이 첫 event claim보다 앞서고 각 outcome의
   post, claim, activation과 diagnostic count가 acceptance와 일치함을 보이는 command output.
2. **Configured startup trace** — 같은 production entrypoint가 실제 production
   `channel_id`, `app_id`와 `HttpSlackTransport` 구성을 사용해 readback outcome을 소비한 뒤
   claim 여부를 결정했음을 보이는 safe trace. Trace는 code revision, entrypoint ID, startup
   evaluation ID, confirmed channel/app ID, outcome, ordered observation name과 post/claim/activation/
   diagnostic count만 포함한다. Credential, HTTP body/header, exception text와 message content는
   포함하지 않는다.

두 증거는 같은 approved code revision을 가리키고 T013 manifest와 Package 4 gate checkpoint에
연결되어야 한다. Unit test output만 있거나 configured trace만 있는 경우는 필요조건이지만
충분조건이 아니다.

### Acceptance Coverage

| Requirement | Acceptance Evidence |
|---|---|
| `FR-023`~`FR-025` | User Story 4 scenarios 1~2와 readback/delete 동시 실패 edge case |
| `FR-026`~`FR-028` | User Story 4 scenarios 3·4·6과 반복 startup edge case |
| `FR-029`~`FR-030` | User Story 4 scenarios 1·5와 operator 진단 중복 edge case |
| `FR-031` | `MGC-012-T013` production startup 실행 증거 |
| `FR-032` | Derivation의 D-030/D-031 supersede 경계 |

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
- **SC-017**: readback 성공 후 cleanup만 실패한 startup은 Slack worker를 활성화하고,
  해당 startup 평가에서 operator에게 안전한 degraded 진단을 정확히 1건 제공한다. 같은
  평가의 중복 진단은 0건이며 process 재기동 뒤 새 startup 평가는 다시 1건을 제공한다.
- **SC-018**: 이전 unresolved probe의 exact cleanup과 durable resolution이 완료되지 않은
  상태에서 startup을 10회 반복해도 새 probe 게시 수는 0건이다. 둘 다 성공한 startup만
  그 뒤 fresh self-check probe를 최대 1건 게시한다.
- **SC-019**: readback에 성공해 명시적으로 식별 가능한 미삭제 probe는 각 channel/app에
  최대 1개다. 같은 channel/app에서 동시에 시작한 startup 중 durable intent 확보 성공과
  Slack post 수행은 각각 최대 1건이며, 나머지 startup의 post 수행은 0건이다.
- **SC-020**: `HISTORY_READ_FAILED`, `PROBE_NOT_FOUND`, `APP_ID_MISMATCH`,
  `MARKER_UNREADABLE`, `MARKER_MISMATCH`, `RESPONSE_CHANNEL_MISMATCH` 각각을 cleanup 성공·실패와
  조합한 12개 validation에서 worker activation은 0회이고 선택한 primary readback code가 100%
  보존된다. Response channel mismatch 두 case는 configured history 조회가 0회이고 cleanup은
  confirmed response `channel + ts`만 사용한다.
  `PROBE_INPUT_INVALID` validation은 network post와 cleanup이 각각 0회다.
- **SC-021**: cleanup 관련 operator 진단에서 FR-030 allowlist 밖 field, credential,
  provider 원문, 예외 전체 표현과 확인되지 않은 `message_ts` 노출은 각각 0건이다.
- **SC-022**: production startup 경로가 readback outcome을 소비하는 실행 증거가 없으면
  FR-018 완료와 Package 4 gate PASS 보고도 0건이다.
- **SC-023**: probe recovery 검토에서 본문·prefix·위치·조회 건수·시간 기반 식별은 0건이며,
  모든 cleanup 대상은 명시적 identity와 durable state로 추적된다.
- **SC-024**: Slack 삭제 성공 뒤 durable `RESOLVED` 기록이 실패한 모든 startup에서 worker
  activation과 추가 probe post는 각각 0건이고 결과는 `HARD_BLOCKED_NO_POST`다.
- **SC-025**: 손상되거나 지원하지 않는 lifecycle fixture 각각에서 typed lifecycle failure가
  반환되고 기존 durable record 변경, readback, probe post와 worker activation은 각각 0건이다.
- **SC-026**: readback과 cleanup이 함께 실패하는 모든 acceptance case에서 primary readback
  failure는 1개로 유지되고 `secondary_cleanup_failure`는 0개 또는 1개다. 구조화 log,
  exception output, metric label, persisted failure diagnostic과 operator output에서 FR-034의
  surface별 allowlist 밖 값, raw provider data, 자유형 error 문자열과 원본 exception chain은
  각각 0건이다.
- **SC-027**: FR-036의 네 crash boundary 각각에서 승인되지 않은 remote lookup과 추가 probe
  post는 0건이다. 성공 응답 뒤 identity commit 실패 case는 exact delete와 durable resolution이
  모두 성공한 경우에만 다음 startup의 fresh claim이 1건 이하이며, 나머지는 0건이다.
- **SC-028**: E-7~E-10 named readiness evidence 중 하나라도 없으면 T013 `ready` 전환은 0건이다.
  동일 revision의 composition-root integration result와 configured startup trace 중 하나라도
  없으면 FR-018 완료와 Package 4 gate PASS 보고는 각각 0건이다.

## Assumptions

- 테스트용 Slack workspace 와 app 은 회사 workspace와 분리해 만들었고 `auth.test`로
  required scope를 확인했다 (D-026). 현재 실행 환경에는 credential, channel ID와 app ID가
  주입되지 않았으므로 T010 live E2E는 계속 blocked다.
- Package 1–3 의 계약은 바꾸지 않는다. Package 4 는 그 위에 실제 호출을 얹는다.
- channel/app별 상한 1개와 안정적인 diagnostic code는 사용자가 승인한 정책 계약이며,
  임의의 구현 magic number나 탐지 heuristic이 아니다.
- 현재 repository에는 `verify_marker_readback`을 부르는 production entrypoint가 없다.
  T013은 entrypoint와 안전한 recovery 설계가 승인되기 전까지 blocked다.
- durable cleanup state는 runtime operational state다. Markdown canonical knowledge에
  기록하지 않는다.

## Planning Research Requirements

기존 U-001~U-005는 현재 `plan.md`와 `research.md`가 다뤘다. D-031은 아래 research를 새로
요구한다. **공식 근거나 repository contract 없이 추정으로 채우지 않는다.**

- **U-006 — Explicit identity**: R-017의 official-contract review는 response loss 뒤 exact
  lookup에 쓸 caller-controlled identity를 확인하지 못했다. 이 부재는 heuristic recovery와
  provider-side global remote cap 주장을 막지만, E-9 governed recovery가 승인된 local
  fail-closed T013 구현 자체를 막지는 않는다.
- **U-007 — Durable recovery boundary**: probe intent와 cleanup lifecycle을 어느 runtime
  boundary가 소유해야 process crash 뒤에도 `HARD_BLOCKED_NO_POST`를 결정할 수 있는가.
  기존 공식 지식과 session state에는 섞지 않으며, 저장 위치와 원자성은 plan에서 정한다.
- **U-008 — Production composition root**: Slack worker가 실제로 활성화되는 entrypoint와
  readback outcome 소비 지점은 어디인가. 현재 repository에 없으므로 존재한다고 가정하지
  않는다.
- **U-009 — Ambiguous send outcome**: provider가 message를 받았지만 호출자가 identifier를
  받지 못한 crash/network 경계에서 local additional post 0건만 증명한다. Official recovery가
  없으면 remote message 상한과 automatic cleanup은 blocked로 남긴다. T013 구현 readiness는
  E-8 review 결과와 E-9 governed recovery approval로 판정하며 Package 4 안에서 강제 해제를
  구현하지 않는다.

**Research stop rule**: E-8 official-contract review와 E-9 governed recovery approval 중 하나라도
없으면 T013을 blocked로 유지한다. Provider가 exact recovery를 지원하지 않는다는 결론이 나도
message text, prefix, 위치, 최근 N건, 시간 window 같은 대체 heuristic을 넣지 않는다.
Automatic remote recovery와 global cap은 blocked로 남기되, E-7~E-10과 T010 선행 증거가
닫히면 local fail-closed T013 구현과 dual production evidence 수집은 진행할 수 있다.
