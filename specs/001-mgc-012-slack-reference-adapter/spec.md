# Feature Specification: MGC-012 Slack Reference Adapter

**Feature Branch**: `001-mgc-012-slack-reference-adapter`

**Created**: 2026-08-03

**Status**: Derived — Package 3 planning input

**Input**: 이 문서는 새로 쓴 요구사항이 아니다. 아래 두 원본의 파생이다.

## Derivation

이 파일은 `AGENTS.md ↔ .specify/memory/constitution.md` 와 같은 파생 문서다. 원본은 둘이다.

| 원본 | 위치 | 고정값 |
|---|---|---|
| 외부 SPEC | `/Users/pinesky/Documents/Codex/2026-07-29/prior-conversation-with-codex-conversation-role/outputs/SPEC.md` | sha256 `e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3` |
| Frozen Acceptance | `docs/workstreams/messenger-governance-closure-v3/CURRENT_ITEM.md` A1–A15 | MGC-012 |

**이 문서와 원본이 어긋나면 원본이 이긴다.** 이 문서를 고쳐서 요구사항을 바꾸지 않는다.
요구사항 변경은 원본에서 하고 이 문서를 다시 파생시킨다.

생성기를 쓰지 않고 파생으로 작성했다. `/speckit-specify` 를 돌리지 않은 근거는 D-019 항목 5에
있다. 디렉터리와 `.specify/feature.json` 은 `create-new-feature.sh` 가 만들었다.

제3의 원본을 이 문서에 넣지 않는다. Slack 공식 문서의 receipt 필드와 error code 는
`/speckit-plan` Phase 0 산출물 `research.md` 에 고정한다 (D-019 항목 7).

## Status And Scope

MGC-012 는 slice 넷이다. 이 문서는 넷 전부의 acceptance 를 싣지만 **plan·taskify 대상은
Package 3 하나다.**

| Package | 범위 | 상태 |
|---|---|---|
| 1 | Raw request verification and normalized Slack interaction contract | 완료 — gate PASS at `ef35201` |
| 2 | Durable ingress ack boundary and background decision handoff | 완료 — gate PASS at `2dcf663` |
| **3** | **Ordered Slack message projection, retry and recovery** | **이 라운드의 plan 범위** |
| 4 | Slack reference E2E, activation isolation and closure review | plan 범위 밖 (D-019 항목 8) |

Package 1·2 의 acceptance (A1–A10, A12·A13 일부)는 이미 충족했고 대응 task 를 새로 만들지
않는다. Package 4 는 Slack test workspace 구성과 credential 경로가 확정되지 않아 task 를 만들지
않는다. Package 3 gate 통과 후 같은 manifest 에 새 ID 로 append 한다.

`/speckit-analyze` 가 "spec 에 있으나 task 가 없는 항목" 을 지적하면 이 절이 근거다.

### Out Of Scope

`CURRENT_ITEM.md` Out Of Scope 를 그대로 잇는다.

- Telegram webhook 과 callback adapter
- Hermes natural-language Skill
- production Slack credential 발급 또는 secret rotation 실행
- Slack activation rollout
- Provider message 를 authoritative Proposal state 로 사용하는 기능

Package 3 고유의 제외 둘을 더한다 (D-018).

- **D-014 provider outbox destination granularity.** `destination_ref` 형식
  `provider:{provider}:{channel_digest}` 는 `events.py:1442`·`:1515`·`:2446` 세 곳에서
  생성되고 그중 둘이 `reconcile_connection` 의 startup 검증 경로다. 형식을 바꾸면 이미 커밋된
  outbox row 가 `destination_manifest_digest` 불일치로 거부되므로 evidence migration 이 필요하다.
  별도 item 이다.
- **실제 network 호출.** Package 3 는 주입받은 transport Protocol 만 부른다. 실제 호출은
  Package 4 가 맡는다. dependency 는 `pydantic`·`pyyaml`·`typer` 셋을 유지한다.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ordered Card Delivery (Priority: P1)

Proposal 상태가 바뀌면 그 결정을 요청한 Slack channel 에 Card message 가 순서대로 도착한다.
오래된 event 가 최신 Card 를 덮지 않는다.

**Why this priority**: A11 의 본체다. 순서가 깨지면 사람이 낡은 Card 를 보고 결정한다.
`YamlProjectionDestination` 이 이미 같은 계약을 지키고 있어 Provider 쪽만 비어 있다.

**Independent Test**: outbox 에 destination_sequence 가 역순인 event 둘을 넣고 dispatcher 를
돌린다. 뒤 sequence 가 먼저 도착해도 앞 sequence 가 그것을 덮지 않는다.

**Acceptance Scenarios**:

1. **Given** 같은 `provider:slack:{channel_digest}` destination 에 sequence 1·2 event 가 있고,
   **When** dispatcher 가 claim 하면, **Then** sequence 1 이 확정되기 전에 sequence 2 를
   전송하지 않는다.
2. **Given** sequence 2 가 이미 전달됐고, **When** sequence 1 을 다시 전송하려 하면,
   **Then** 실패하고 최신 Card 를 덮지 않는다.
3. **Given** 같은 `supersession_key` 를 가진 pending event 가 있고, **When** 그것을 완전히
   대체하는 새 event 가 들어오면, **Then** 이전 event 는 `superseded` 가 되고 전송하지 않는다.

---

### User Story 2 - Crash Between Send And Mark (Priority: P1)

send 는 성공했는데 local mark 전에 process 가 죽는다. 재기동 후 같은 event 를 다시 보내
중복 Card 를 만들지 않는다.

**Why this priority**: SPEC.md `Outbox Ordering` 이 명시한 실패 경로이고 AC-14 Recovery 의
Provider 쪽 사례다. Package 2 review 에서 반복해서 blocker 가 나온 부류다.

**Independent Test**: transport 가 send 성공을 반환한 직후 mark 를 건너뛰고, 같은 event 로
reconcile 을 부른다.

**Acceptance Scenarios**:

1. **Given** send 성공 후 mark 전에 종료했고, **When** 재기동해 reconcile 하면, **Then**
   channel 에서 그 event 의 marker 를 찾아 기존 결과로 확정한다.
2. **Given** marker 를 찾을 수 없고 상위 sequence 도 없으면, **When** reconcile 하면,
   **Then** 미전송으로 판정하고 재전송을 허용한다.
3. **Given** reconcile 이 판정 불가면, **When** 처리하면, **Then** DLQ 와 operator hold 를
   만들고 그 destination 의 이후 event 를 멈춘다.

---

### User Story 3 - Retry And Exhaustion (Priority: P2)

일시적 Slack 오류는 재시도하고, 회복 불가 오류는 재시도하지 않는다. 재시도가 소진되면
operator 가 볼 수 있는 상태로 남는다.

**Why this priority**: A11 의 retry·DLQ 절이다. 기존 `OutboxDispatcher` 의 lease·backoff·
`max_attempts` 계약을 우회하지 않는 것이 핵심이다.

**Independent Test**: transport 가 재시도 가능 error 와 terminal error 를 각각 내도록 하고
attempts 전이를 확인한다.

**Acceptance Scenarios**:

1. **Given** transport 가 재시도 가능 error 를 내면, **When** dispatcher 가 처리하면,
   **Then** `retry_wait` 로 backoff 하고 attempts 를 올린다.
2. **Given** transport 가 terminal error 를 내면, **When** 처리하면, **Then** 재시도 없이
   DLQ 로 보낸다.
3. **Given** `operator_hold` 가 걸린 destination 이 있으면, **When** claim 하면, **Then**
   그 destination 의 event 를 claim 하지 않는다.

### Edge Cases

- destination 의 첫 event 인데 `destination_sequence` 가 1 이 아니면 어떻게 되나 —
  `YamlProjectionDestination` 은 `YAML_SEQUENCE_CAS_CONFLICT` 로 거부한다
  (`projections.py:72`). **Slack 쪽은 거부하지 않고 그냥 보낸다** (D-023). 첫 시도면
  reconcile 이 조회 없이 미전송으로 답하고, 순서 보장은 reconcile 이 아니라 `claim_next`
  가 한다 — 앞 sequence 가 `delivered` 나 `superseded` 가 아니면 다음 event 를 claim 하지
  않는다 (`events.py:2648`). 그래서 Slack 쪽에는 CAS 대응물이 필요 없다.
- payload digest 가 event 의 `payload_digest` 와 다르면 —
  `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` 로 전송 전에 멈춘다 (`projections.py:61` 대응).
  재시도하지 않고 곧바로 DLQ + operator hold 다 (D-022).
- event 의 `destination_ref` 가 destination 자신의 것과 다르면 —
  `OUTBOX_DESTINATION_MISMATCH` (`projections.py:58` 대응). 이것도 재시도 없이
  DLQ + operator hold 다 (D-022).
- 사람이 Slack 에서 Card message 를 지웠다가 다시 만든 경우 — SPEC.md 검증 항목
  "deleted/recreated Provider message" 에 있다. marker read-back 이 못 찾는 상황이다.
- 두 dispatcher 가 같은 destination 을 동시에 claim — lease 와 fencing 으로 직렬화한다.
  기존 `OutboxDispatcher.claim_next` (`events.py:2572`)가 이미 처리한다.

## Requirements *(mandatory)*

### Functional Requirements

Package 3 범위. 각 항목은 원본 근거를 단다.

- **FR-001**: `SlackProjectionDestination` 은 `ProjectionDestination` Protocol
  (`events.py:236`)을 만족한다 — `destination_ref` 속성, `reconcile()`, `send()`.
  근거: A11, 기존 Protocol.
- **FR-002**: transport 는 주입받은 Protocol 이며 send 와 read 를 모두 갖는다.
  근거: D-018 항목 2·3.
- **FR-003**: 전송 메시지는 event 식별자 marker 를 담는다. `reconcile()` 은 channel 을 조회해
  그 marker 를 찾아 판정한다. remote 가 유일한 진실 원천이다. 근거: D-018 항목 3.
- **FR-004**: 같은 destination 의 event 는 순차 전달한다. 이전 event 가 확정되기 전에 다음
  event 를 보내지 않는다. 근거: SPEC.md `Outbox Ordering`, AC-10.
- **FR-005**: 최신 event 가 이전 pending event 를 완전히 대체하면 이전 event 를 `superseded`
  처리한다. Provider destination 은 `supersession_key=proposal-card:{proposal_id}` 를 이미
  달고 있다 (`events.py:_decision_destinations`). 근거: SPEC.md `Outbox Ordering`, A11.
- **FR-006**: send 성공 후 local mark 전 종료 시 remote state 를 reconcile 한다. reconcile
  불가면 DLQ 와 operator hold 를 만든다. 근거: SPEC.md `Outbox Ordering`, AC-14, A11.
- **FR-007**: retry, DLQ 와 operator hold 는 기존 `OutboxDispatcher` 계약을 쓴다. 우회하는
  별도 경로를 만들지 않는다. 근거: A11.
- **FR-008**: `destination_ref` 형식 `provider:{provider}:{channel_digest}` 를 바꾸지 않는다.
  근거: D-018 항목 1.
- **FR-009**: payload digest 와 `destination_ref` 를 전송 전에 검증하고 불일치면 전송하지
  않는다. 근거: 기존 `YamlProjectionDestination.send` 대응 계약.
- **FR-010**: Slack 실패를 재시도 가능과 terminal 로 분류한다. 분류 근거가 되는 error code
  목록은 `research.md` 가 공식 문서에서 고정한다. 근거: D-018 항목 2, D-019 항목 7.
- **FR-011**: Slack adapter 는 Telegram 또는 다른 Provider 의 activation state 를 바꾸지
  않는다. 근거: A14.
- **FR-012**: 실제 network 호출을 하지 않는다. dependency 는 `pydantic`·`pyyaml`·`typer`
  셋을 유지한다. 근거: D-018 항목 2.

### Deferred Requirements

- Package 4 — Slack reference E2E, activation isolation, closure review. 근거: A15,
  D-019 항목 8. Slack test workspace 구성과 credential 경로는 **확인 안 됐다.**

### Key Entities

- **OutboxEventView** (`events.py:212`): `destination_ref`, `destination_sequence`,
  `aggregate_sequence`, `source_state_revision`, `payload_digest`, `payload`.
  Package 3 는 이 view 를 소비만 하고 형식을 바꾸지 않는다.
- **ProjectionDestination** (`events.py:236`): `reconcile()` 과 `send()` 를 갖는 Protocol.
  `SlackProjectionDestination` 이 두 번째 구현이 된다.
- **Slack transport Protocol** (신규): send 와 read 를 갖는다. 구체 signature 는
  `research.md` 가 Slack 공식 문서로 receipt 필드를 고정한 뒤 `contracts/` 에서 정한다.
- **Message marker** (신규): 전송 메시지에 담기는 event 식별자. reconcile 의 판정 근거다.
  전용 type 이 아니라 `build_slack_marker()` 가 만드는 dict 다. 되읽은 결과만
  `SlackMarkerView` dataclass 로 받는다.

## Success Criteria *(mandatory)*

### Measurable Outcomes

SPEC.md `Verification Matrix` 의 Outbox·Provider 절에서 Package 3 에 해당하는 것을 가져온다.

- **SC-001**: reverse-order delivery test 가 통과한다 — 낮은 sequence 가 높은 sequence 를
  덮지 못한다.
- **SC-002**: competing Dispatcher test 가 통과한다 — 같은 destination 을 두 dispatcher 가
  claim 해도 직렬화된다.
- **SC-003**: old event supersede test 가 통과한다.
- **SC-004**: "send 후 local mark 전 kill" test 가 통과한다 — 재기동 후 중복 Card 를 만들지
  않는다.
- **SC-005**: "deleted/recreated Provider message" test 가 통과한다. `plan.md` P-001 이
  hold 로 정했고 D-023 이 그것을 좁혔다 — `next_cursor` 가 남은 채 상한에 걸린 경우만
  hold 다. history 를 소진할 수 있는 작은 채널에서는 재전송된다.
- **SC-006**: `python -m pytest` 전량이 회귀 없이 통과한다. **숫자를 고정하지 않는다** —
  test 는 wave 마다 늘고, 고정 숫자를 성공 기준으로 두면 갱신 장치가 없어 판정이 불가능해진다
  (2026-08-05 `/speckit-analyze` I1). T004·T005 의 `regression` command 가 이 기준이다.
- **SC-007**: `amplai-foundry verify` 7 stage 가 통과한다.
- **SC-008**: wave 마다 contract·failure-recovery·regression 3 lens review 에서
  P0·P1·Blocking-P2 가 0건이다. 근거: D-019 항목 4, `QUALITY_GATES.md`.

## Assumptions

- Package 1·2 가 만든 durable ingress, ack boundary, background decision handoff 는 그대로
  둔다. Package 3 는 그 뒤단의 message projection 만 채운다.
- `OutboxDispatcher` 의 lease·backoff·`max_attempts`·`operator_hold` 계약은 이미 검증됐고
  (MGC-008 gate PASS at `e989566`) Package 3 는 그것을 쓴다.
- ~~Slack API 의 receipt 필드와 error code 는 아직 모른다.~~ **해소.** `research.md`
  R-005·R-006 이 S1 에서 고정했다.
- ~~Slack channel 조회로 marker 를 찾는 것이 가능한지는 확인 대상이다.~~ **해소.**
  `research.md` R-003 이 S3 로 확정했다 — `chat.postMessage` 의 `metadata` 로 심고
  `conversations.history` 를 `include_all_metadata=true` 로 읽는다.
- **남은 미확인**: `conversations.history` 가 최신 message 부터 돌려주는지 S2 에서 확인하지
  못했다 (research R-004 미확인, contracts C-1). Package 4 가 확정한다.
