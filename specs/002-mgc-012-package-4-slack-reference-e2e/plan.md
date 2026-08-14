# Implementation Plan: MGC-012 Package 4 — Slack Reference E2E And Closure

**Branch**: `002-mgc-012-package-4-slack-reference-e2e` | **Date**: 2026-08-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-mgc-012-package-4-slack-reference-e2e/spec.md`

**Plan Status**: `MGC-012-T008` replanning ready. `MGC-012-T013`, FR-018 production completion,
and Package 4 closure remain `BLOCKED`.

**Clarification Sync**: `spec.md`의 `Session 2026-08-11` 기존 일곱 결정과 2026-08-12에
승인한 다섯 결정을 모두 반영했다.

## Summary

Package 4의 기존 HTTP transport와 E2E 설계에 D-031 probe cleanup 계약을 추가한다.
Readback 성공과 cleanup 실패를 분리하고, cleanup-only 실패는 typed degraded outcome으로
돌린다. 이전 probe recovery는 durable state와 exact identity만 사용한다.

핵심 설계는 열두 가지다.

1. **stdlib 로 간다.** `urllib.request` 를 쓰고 dependency 셋을 유지한다 (R-013).
2. **credential 은 entrypoint 한 곳에서만 읽는다.** core 는 계속 주입만 받는다 (R-014).
3. **E2E 는 기본 suite 밖이다.** marker 로 분리하고 credential 이 없으면 skip 하되 그
   사실을 출력한다. `amplai-foundry verify` 는 offline 계약을 유지한다 (R-015).
4. **app 은 internal 로 둔다.** 배포하면 `conversations.history` 가 분당 1회로 떨어져
   reconcile 설계가 성립하지 않는다 (R-012).
5. **판정과 출력의 owner를 나눈다.** Readback 계층은 typed outcome만 만든다. Production
   composition root 한 곳만 각 startup 평가에서 operator diagnostic을 1회 출력한다 (D-031).
6. **network 전에 intent를 원자적으로 확보한다.** `GovernanceStore`의 전용 lifecycle
   state에서 channel/app별 한 startup만 post 권한을 얻는다. 경쟁에서 진 startup은
   `HARD_BLOCKED_NO_POST`다 (R-018).
7. **증명하지 못한 recovery는 막는다.** Slack은 pre-response identity와 documented
   idempotency를 제공하지 않는다. Automatic ambiguous-message recovery와 provider-side global
   cap은 `BLOCKED`다. Production wiring은 T010과 E-7~E-10이 닫힌 뒤 local fail-closed로
   진행하며 operator 강제 해제는 별도 governed recovery 계약 전에는 금지한다
   (R-017, R-019, R-020).
8. **원인 state와 startup outcome을 분리한다.** `HARD_BLOCKED_NO_POST`는 durable state가
   아니라 현재 startup의 typed outcome이다. Persistence에는 `POST_INTENT_RECORDED`,
   `AMBIGUOUS_POST`, `CLEANUP_PENDING` 같은 원인을 그대로 남긴다 (R-021).
9. **손상·미지원 lifecycle은 별도 typed failure다.** Record를 수정하거나 정상으로 추정하지
   않고 readback, post와 activation을 모두 막는다 (R-022).
10. **readback 원인과 cleanup 보조정보는 닫힌 type이다.** 일곱 primary failure code와 최대
    하나의 `CleanupFailureDetail`을 사용하고, raw exception/provider data를 모든 diagnostic
    surface에서 금지한다 (R-023).
11. **crash 경계는 network 전후를 따로 판정한다.** Confirmed response 뒤 identity commit이
    실패한 현재 process만 in-memory exact identity로 한 번 보상 cleanup할 수 있다. Durable
    resolution 전에는 다음 startup을 열지 않는다 (R-024).
12. **T013 readiness와 완료 evidence를 분리한다.** E-7~E-10 named approval record가 있어야
    ready이고, 완료에는 composition-root integration result와 configured startup trace가 모두
    필요하다 (R-025).

## Technical Context

**Language/Version**: Python 3.11 / 3.12 (`pyproject.toml`, SPEC `AC-15`)

**Primary Dependencies**: `pydantic`, `pyyaml`, `typer` — **셋을 유지한다.** HTTP 는
stdlib `urllib.request` 다 (R-013)

**Storage**: SQLite `GovernanceStore`. D-031은 narrow probe lifecycle table을 위한 additive
migration을 요구한다. 기존 outbox/ingress/hold table은 재사용하지 않는다 (R-018)

**Testing**: `python -m pytest` 전량 + marker 로 분리된 E2E. 숫자를 baseline 으로 고정하지
않는다 (SC-015)

**Target Platform**: local CLI / worker process. Production Slack worker entrypoint는 없음

**Project Type**: single project — `src/amplai_foundry/`

**Performance Goals**: 별도 목표 없음. Slack 상한은 `chat.postMessage` channel 당 초당 1건
(P1), `conversations.history` **Tier 3 분당 50+** (P2, R-010 정정)

**Constraints**:
- `contracts/slack-transport.md` C-1 (001) 을 바꾸지 않는다. 실제 구현이 그 계약을 만족한다
- 한 `deliver_next` 안의 **모든 호출 합계**가 `OutboxConfig.lease_seconds` 보다 짧아야 한다
- `amplai-foundry verify` 는 network 없이 통과해야 한다 (SC-014)
- app 은 internal 이어야 한다 (R-012)
- probe recovery는 exact local identity와 confirmed Slack `channel + ts`만 사용한다
- text, prefix, metadata scan, 위치, 최근 N건, 시간 window heuristic은 금지한다
- unresolved lifecycle state가 있으면 network post 전에 판정한다. Confirmed `channel + ts`가
  있는 `CLEANUP_PENDING`만 exact cleanup과 durable `RESOLVED` commit 뒤 fresh self-check로
  이어갈 수 있고, 그 밖의 unresolved state나 recovery 실패는 `HARD_BLOCKED_NO_POST`다
- 같은 channel/app의 concurrent startup은 durable intent를 먼저 원자적으로 확보한 하나만
  post한다
- Slack 삭제 성공 뒤 `RESOLVED` commit이 실패하면 `HARD_BLOCKED_NO_POST`다
- cleanup-only diagnostic은 각 startup 평가에서 1회이며 FR-030의 닫힌 field allowlist만 쓴다
- malformed record, unknown state와 unsupported schema/state version은 typed lifecycle failure다
- readback primary failure는 FR-035의 closed enum이며 secondary cleanup detail은 0개 또는 1개다
- raw exception chain, 자유형 error, provider body/header는 log, exception output, metric,
  persisted failure diagnostic과 operator output에 전달하지 않는다
- confirmed response 뒤 identity commit 실패는 current process의 exact identity로 cleanup과
  durable resolution을 한 번 시도하며, 둘 다 성공한 뒤의 후속 startup만 fresh claim 가능하다
- Production entrypoint, official provider contract review, governed recovery, lifecycle
  schema/repository 승인 전 `MGC-012-T013`은 `BLOCKED`다. Provider exact recovery 기능 부재
  자체는 E-9 승인 뒤 local fail-closed 구현을 막지 않는다

**Scale/Scope**: 기존 `slack_http.py` rework, narrow startup/lifecycle module, additive
governance migration, unit tests, future composition-root wiring. `slack_projection.py`와
`slack.py`는 수정하지 않는다

## Constitution Check

*GATE: Phase 0 전 통과, Phase 1 후 재확인.*

| 원칙 | 판정 | 근거 |
|---|---|---|
| I. Knowledge Safety | PASS | canonical Vault는 미수정이다. D-031과 APR-016이 workstream ledger에 기록되고 D-030의 supersede 관계도 연결됐다 |
| II. Small Verifiable Change | CONDITIONAL PASS | Narrow lifecycle table은 no-heuristic crash safety에 필요하다. 범용 write API와 별도 server는 추가하지 않는다 |
| III. Evidence-Based Completion | CONDITIONAL PASS | 같은 revision의 composition-root integration result와 configured startup trace가 모두 없으면 FR-018과 Package 4를 완료로 보고하지 않는다. E2E skip도 pass로 보고하지 않는다 |
| IV. Governed Mutation Only | N/A | Package 4 는 Proposal mutation 경로를 안 만든다 |
| V. Independent Review Before Gate | PASS | wave 마다 contract·failure-recovery·regression 3 lens (D-019 항목 4). **regression lens 는 단독으로 돌린다** — wave 4 review 가 기록한 process violation 이다 |

**Phase 1 재확인**: 전용 runtime state는 Markdown knowledge와 분리된다. Existing domain table을
재사용하지 않는다. Explicit provider recovery, governed recovery work item, production
entrypoint, lifecycle schema/repository 승인은 blocker로 유지한다. Constitution 위반은 없고 scope expansion은
Complexity Tracking에 적는다.

## Decisions Carried From Research

`research.md` 가 넘긴 판단을 여기서 닫는다.

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

### P-005 — Durable Intent Precedes Every Probe Post

**결정**: Probe lifecycle service는 `GovernanceStore`에 unresolved intent가 없는지 확인하고,
새 intent를 commit한 뒤에만 Slack post를 호출한다. Scope는 channel/app이고 unresolved row는
최대 1개다 (R-018).

Concurrent startup은 같은 atomic claim을 경쟁한다. 먼저 durable intent를 확보한 하나만
post한다. 나머지는 network 호출 없이 `HARD_BLOCKED_NO_POST`를 반환한다.

`POST_INTENT_RECORDED` state에서 post 결과를 잃으면 `AMBIGUOUS_POST` 원인 state를 보존하고
startup outcome으로 `HARD_BLOCKED_NO_POST`를 반환한다. 다음 startup은 history를 검색하지
않고 멈춘다. `channel + ts`가 확인된 state만 exact delete recovery를 시도한다. 이전
`CLEANUP_PENDING`은 exact delete와 durable `RESOLVED` commit이 모두 성공한 뒤에만 fresh
self-check로 넘어간다. 어느 단계든 실패하면 마지막 원인 state를 유지하고 현재 startup은
worker를 활성화하거나 새 probe를 게시하지 않는다.

**기각**: 기존 `_reclaim_probes`처럼 shared sentinel을 최근 history에서 찾는 안. Identity가
고유하지 않고 read/metadata/delete 실패를 숨긴다. FR-026~FR-028과 충돌한다.

### P-006 — Readback Decides; The Composition Root Reports

**결정**: Readback/lifecycle 계층은 `READY`, `DEGRADED_CLEANUP`, typed hard failure,
`HARD_BLOCKED_NO_POST`를 판정한다. `HARD_BLOCKED_NO_POST`는 persisted lifecycle state가 아닌
startup outcome이다. Logging은 하지 않는다.

Production composition root 한 곳만 각 startup 평가에서
`SLACK_PROBE_CLEANUP_DEGRADED`를 operator에게 정확히 한 번 출력한다. 허용 field는
`diagnostic_code`, `channel_id`, `app_id`,
`probe_id`, `cleanup_failure_count`, `provider_error_code`, `operator_action`과 confirmed
`message_ts`다. 그 밖의 field, credential, provider body, exception `repr`은 금지한다.

### P-007 — T013 Stays Blocked

**결정**: `MGC-012-T013`은 production entrypoint, dated/versioned Slack official-contract
review, 별도 governed recovery work item과 lifecycle schema/repository scope가 확정될 때까지
`BLOCKED`다. Review가 exact recovery 미지원을 확인하면 automatic remote cleanup과 global
remote cap만 blocked로 유지한다. E-9가 승인되면 그 기능 부재 자체는 local fail-closed T013
구현의 stop condition이 아니다 (R-017, R-019, R-020).

`MGC-012-T008`은 typed unit outcome만 소유한다. T013 evidence 없이 FR-018,
`readback-selfcheck-wiring`, Package 4 gate를 닫지 않는다.

명시적 provider identity가 없는 lifecycle의 operator 강제 해제는 T013 scope가 아니다.
별도 governed recovery work item이 승인되기 전에는 T013 blocker를 우회하지 않는다.

### P-008 — Invalid Lifecycle Records Are Typed Failures

**결정**: lifecycle repository boundary에서 record schema version과 closed state enum을
검증한다. Malformed field, unknown state, unsupported version은 `LIFECYCLE_STATE_INVALID` typed
failure로 반환한다. 기존 row를 수정·삭제·보정하지 않고 readback, post와 activation을 호출하지
않는다 (R-022).

**기각**: unknown value를 가장 가까운 state로 변환하거나 migration에서 자동 고치는 안. 실제
remote side effect를 모르는 상태에서 cause를 지우므로 금지한다.

### P-009 — Primary And Secondary Failures Have Closed Shapes

**결정**: readback primary failure는 FR-035의 일곱 code만 사용한다. Post 뒤 발생하는 여섯
code는 cleanup 성공·실패 양쪽과 조합한다. Slack 성공 응답의 confirmed channel이 configured
target과 다르면 전용 `RESPONSE_CHANNEL_MISMATCH`로 판정하고 configured history는 조회하지
않는다. Cleanup은 provider-confirmed `channel + ts`만 사용한다. Cleanup 실패는 optional immutable
`CleanupFailureDetail` 하나로만 전달하고 count와 allowlisted provider code만 가진다 (R-023).

Metrics label은 `outcome`, `diagnostic_code`, allowlisted `provider_error_code`만 허용한다.
Structured log, exception output, persisted failure diagnostic과 operator output도 각 closed
allowlist를 사용하며 원본 exception chain과 raw provider data를 보존하지 않는다.

### P-010 — Confirmed Identity Enables One In-Process Compensation

**결정**: intent commit 전 crash는 remote side effect가 없으므로 다음 startup의 fresh claim을
허용한다. 기존 startup의 `POST_INTENT_RECORDED`는 post 여부를 추정할 수 없으므로 다음 startup에서
hard block한다. Response loss는 가능한 경우 `AMBIGUOUS_POST`를 commit한다 (R-024).

Slack 성공 응답을 받아 in-memory `channel + ts`가 있지만 durable identity commit이 실패하면
현재 process만 exact delete를 최대 한 번 시도하고 durable `RESOLVED`를 기록한다. 둘 다 성공한
후속 startup만 fresh claim할 수 있다. 현재 startup은 worker를 활성화하거나 새 probe를 보내지
않는다.

### P-011 — Readiness Evidence Is Not Completion Evidence

**결정**: E-7~E-10 각각은 FR-037의 내용과 사용자 승인을 담은 governed workstream record를
T013 manifest에 연결해야 한다. Slack official documentation은 E-8의 primary fact source지만
승인자는 아니다 (R-025).

T013 완료 evidence는 같은 code revision의 두 artifact다. Production composition root를
instrumented dependency로 실행한 integration result가 ordering과 count matrix를 증명하고,
configured startup trace가 실제 production channel/app/`HttpSlackTransport` 조합에서 outcome을
claim 전에 소비했음을 증명한다. 둘 중 하나만으로는 FR-018이나 Package 4 gate를 닫지 않는다.
Configured trace는 T010이 검증한 target channel/app과 credential path를 재사용하므로 T010을
T013의 internal dependency로 둔다.

## Project Structure

### Documentation (this feature)

```text
specs/002-mgc-012-package-4-slack-reference-e2e/
├── plan.md              # 이 파일
├── spec.md              # 파생 spec (원본은 외부 SPEC.md + CURRENT_ITEM.md)
├── research.md          # Phase 0 — R-009~R-025
├── data-model.md        # Phase 1 — transport + D-031 lifecycle
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
├── slack_http.py             # 기존 — stateless Slack HTTP transport
├── slack_startup.py          # 계획 — typed readback/lifecycle orchestration
└── migrations.py             # 계획 — narrow probe lifecycle migration

src/amplai_foundry/
└── (composition root)        # 미확인 — T013 blocker

tests/
├── test_slack_http.py        # 기존 — HTTP/readback unit + E2E harness
└── test_slack_startup.py     # 계획 — durable lifecycle/state matrix
```

**Structure Decision**: 기존 단일 package와 `GovernanceStore`를 유지한다. 새 lifecycle
module은 Slack HTTP와 governance persistence를 조립한다. Existing domain table에는 probe를
넣지 않는다. Production root path는 `미확인`이며 T013이 blocked 상태로 소유한다.

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

### Wave 6R — Readback Outcome Rework

- `MGC-012-T008`의 delete-only failure를 `DEGRADED_CLEANUP`으로 바꾼다
- Readback failure는 primary cause를 유지한다
- 판정 계층은 logging하지 않고 safe typed outcome만 만든다
- FR-035 일곱 primary failure code와 post 뒤 여섯 code × cleanup 성공·실패 12개 조합을 검증한다
- response channel mismatch 두 조합은 configured history 조회 0회, confirmed identity cleanup,
  primary code 보존을 검증한다
- optional `CleanupFailureDetail` cardinality와 모든 diagnostic surface의 closed field를 검증한다
- supplied lifecycle state가 claim 실패 또는 resolution 실패를 나타내면
  `HARD_BLOCKED_NO_POST`를 만든다
- persisted cause state를 `HARD_BLOCKED_NO_POST`로 덮어쓰지 않는다
- supplied lifecycle cause는 no-post typed outcome만 검증한다. 이전 `CLEANUP_PENDING`의 실제
  exact cleanup, durable resolution과 fresh self-check sequence는 T013이 소유한다
- `_reclaim_probes` history scan과 silent failure path는 제거 대상이다
- production startup 완료를 주장하지 않는다

### Wave 7 — Real Slack E2E

- marker 로 분리된 E2E. credential 없으면 skip
- User Story 1·2·3 의 acceptance scenario
- 검증: 실제 workspace. Workspace/app은 준비됐고, test channel/app invite와 현재 실행
  환경의 app/channel ID·credential 주입이 선행 조건이다

### Wave 8 — Durable Production Wiring (`MGC-012-T013`, BLOCKED)

- 전용 probe lifecycle state와 additive migration
- atomic pre-send durable intent와 concurrent startup single-winner
- one-unresolved invariant와 exact `channel + ts` recovery
- delete 성공 뒤 `RESOLVED` commit 실패의 fail-closed 판정
- malformed/unknown/unsupported lifecycle record의 typed fail-closed 판정
- 네 crash boundary와 confirmed identity commit 실패의 in-process exact compensation
- 원인 lifecycle state를 보존하는 `HARD_BLOCKED_NO_POST` restart safety
- composition root의 pre-claim 판정과 per-startup diagnostic 1회 출력
- FR-030 closed diagnostic allowlist
- E-7~E-10 named readiness approval record
- same-revision composition-root integration result와 configured startup trace
- blocker: official provider contract review, governed recovery contract, production entrypoint,
  lifecycle schema/repository 승인
- dependency: T010 live target/config evidence

### Wave 9 — Closure

- 전체 regression, `amplai-foundry verify` 7 stage
- clean clone Python 3.11·3.12 (SC-014)
- 001 research.md 에 R-010 forward pointer, `index.yaml` 의 W3 항목 셋 닫기
- Package 3·4 를 합친 MGC-012 closure review

Wave 순서는 dependency 순이다. Wave 7은 wave 5·6R을 필요로 한다. Wave 9는 T013의
production evidence를 필요로 하므로 현재 `BLOCKED`다.

## External Dependencies

아래 항목은 구현 task 밖의 external setup 또는 선행 계약이다. `/taskify` 는 이것을
`dependencies.external` 로 잡는다.

| # | 항목 | 상태 |
|---|---|---|
| E-1 | 테스트용 Slack workspace 생성 | **완료** |
| E-2 | Slack app 생성, scope 부여, workspace 설치와 `auth.test` 확인 | **완료** |
| E-3 | test channel 생성·app 초대·channel ID와 app ID 확보 | **미완** |
| E-4 | signing secret·bot token·app ID·channel ID를 현재 실행 환경에 주입 | **미완** |
| E-5 | Python 3.12 clean-clone 검증 환경 | **미완** |
| E-6 | D-031·APR-016 workstream decision ledger sync | **완료** |
| E-7 | entrypoint path, owner, pre-claim insertion point를 정한 user-approved workstream decision | **미완/BLOCKED** |
| E-8 | date/version과 exact recovery 지원 여부를 담은 official Slack contract review + user approval | **미확인/BLOCKED** |
| E-9 | actor/permission, ActionToken, audit/evidence, transition을 정한 governed recovery work item + user approval | **미완/BLOCKED** |
| E-10 | schema version/migration, unresolved uniqueness, repository owner를 정한 schema decision + user approval | **미완/BLOCKED** |

절차는 `quickstart.md` 가 쓴다. **E-3·E-4가 끝나기 전에는 wave 7을 시작할 수 없다.**
Wave 5·6R은 network 없이 돈다. E-5는 wave 9 closure 조건이다. Wave 8은 T010 완료와
E-7~E-10 승인 record가 모두 닫히기 전에는 시작하지 않는다.

## Out Of Scope For This Plan

- **Activation Gate machinery** — `ActivationEvidence` 와 4단계 Gate 는 MGC-015 다 (D-026)
- **HTTP ingress layer** — Slack 이 우리에게 보내는 요청을 받는 web server. D-012 가
  Package 2 에서 이미 범위 밖으로 뒀다
- **D-014 destination granularity** — evidence migration 이 필요한 별도 item
- **`Retry-After` 를 backoff 에 주입** — `OutboxDispatcher` 계약 변경이라 별도 item (R-007)
- **배포용 rate limit 대응** — R-012 의 cliff 는 배포 형태를 정할 때 판단한다. 지금
  고치는 것은 speculative work 다
- **Undocumented Slack identity** — `client_msg_id`, text, metadata scan을 recovery
  identity로 사용하지 않는다
- **Operator recovery mutation** — unresolved intent 강제 해제, operator 입력, 권한, 감사
  command는 별도 governed recovery work item 승인 전까지 범위 밖이다
- **Supervisor configuration** — systemd/Kubernetes restart policy는 이 application의
  계약이 아니다

## Complexity Tracking

| Scope Expansion | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| Additive probe lifecycle schema | Network 전에 durable intent를 남겨 repeated startup의 추가 post를 막는다 | Process memory는 restart에서 사라지고 history scan은 heuristic이다 |
| Narrow lifecycle service | HTTP transport, state transition, activation outcome owner를 분리한다 | `slack_http.py` 안의 best-effort scan은 persistence failure를 숨긴다 |
| Atomic unresolved uniqueness | Concurrent startup에서 post 권한을 한 startup에만 준다 | Read 후 insert는 race가 생기고 외부 supervisor 직렬화는 application 계약이 아니다 |
| In-process exact compensation | Confirmed response 뒤 identity persistence 실패 때 exact remote cleanup 기회를 잃지 않는다 | 다음 startup의 history scan은 heuristic이고 무조건 영구 block은 복구 가능한 identity도 버린다 |
| Dual production evidence | Code-level ordering과 실제 configured wiring을 각각 증명한다 | Unit output만으로 production wiring을 주장하거나 runtime trace만으로 failure matrix를 주장할 수 없다 |

Constitution 위반은 없다. 기존 "schema 변경 없음" plan boundary는 D-031이 이 narrow scope에서
supersede한다. 범용 persistence API는 만들지 않는다.
