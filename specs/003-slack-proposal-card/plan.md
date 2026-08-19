# Implementation Plan: Slack Proposal Cards

**Branch**: `003-slack-proposal-card` | **Date**: 2026-08-12 | **Spec**: [spec.md](spec.md)

**Input**: Approved feature specification from `specs/003-slack-proposal-card/spec.md`.

## Summary

Slack Proposal projection을 공식 Block Kit Card로 렌더링한다. Decision result는 action-free
Result Card로 보낸다. Reviewed Proposal은 한 reviewer와 channel에 묶인 Review Card로 보낸다.

기존 ordered outbox, message metadata marker, ActionToken, signed ingress, background decision
worker를 확장한다. Raw ActionToken credential은 `send()` 호출 중 메모리에만 존재한다.
재시도는 remote marker를 먼저 reconcile하고, 미전송이 확인된 경우에만 이전 token ID
세트를 revoke한 뒤 새 세트를 발급한다.

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**: Pydantic 2, standard-library `sqlite3`, `urllib`, existing Typer/PyYAML

**Storage**: Existing SQLite governance store, schema migrations `31-32`

**Testing**: pytest, Ruff check/format, strict mypy, `amplai-foundry verify`, Slack marker E2E

**Target Platform**: Local or operator-managed Python process with one configured Slack workspace

**Project Type**: Python library and CLI; no new server or web application

**Performance Goals**: Slack interaction acknowledgement within 3 seconds; bounded Card payload;
one outbox delivery attempt remains inside its lease budget

**Constraints**: Raw credentials never durable; one reviewer; 24-hour action TTL; three actions;
no `chat.update`, modal, thread-only flow, web UI, auth system, or central server

**Scale/Scope**: One Review Card and one Result Card per Proposal review/decision snapshot; existing
destination ordering and provider isolation remain unchanged. Slack provider sequence is channel-wide,
while source-state monotonicity is checked per Proposal aggregate; non-Slack destinations keep their
pre-Card full `ChannelRef` identity.

## Constitution Check

### Pre-Research Gate

| Principle | Result | Evidence |
|---|---|---|
| Knowledge Safety | PASS | Slack는 decision surface다. Canonical Vault를 직접 수정하지 않는다. |
| Small Verifiable Change | PASS | 기존 governance/Slack adapter만 확장한다. 새 server, auth, web UI가 없다. |
| Evidence-Based Completion | PASS | contract, recovery, regression, E2E, full verification을 task acceptance에 둔다. |
| Governed Mutation Only | PASS | 모든 button은 기존 `DecisionService`와 ActionToken을 통과한다. |
| Independent Review | PASS | 구현 후 contract, failure/recovery, regression reviewer를 분리한다. |

### Post-Design Gate

| Principle | Result | Evidence |
|---|---|---|
| Secret minimization | PASS | outbox와 lifecycle table은 token ID/hash만 보유한다. Raw 값은 renderer 입력까지 메모리 전용이다. |
| Ordered delivery | PASS | Review/Result Card 모두 기존 provider destination sequence와 marker를 사용한다. |
| Fail closed | PASS | remote 판정 불가 시 replacement를 만들지 않고 기존 outbox hold로 보낸다. |
| Provider isolation | PASS | Slack rendering은 `provider:slack:*` destination 안에서만 실행한다. |
| Bounded scope | PASS | reason modal, multi-reviewer, message edit/delete, production endpoint 배선은 제외한다. |

## Architecture

### Result Card Path

1. `DecisionService`가 기존 `DecisionProjectionPayload`를 outbox에 기록한다.
2. `SlackProjectionDestination`이 semantic payload digest를 먼저 검증한다.
3. `SlackProposalCardRenderer`가 action-free Result Card와 fallback `text`를 만든다.
4. 기존 `SlackTransport.post_message()`가 같은 metadata marker와 함께 전송한다.
5. retry는 기존 `reconcile()`가 remote receipt를 회수한다.

Renderer는 outbox payload를 바꾸지 않는다. Semantic payload digest와 remote message body를
분리해 audit source of truth를 유지한다.

### Review Card Request Path

1. Caller가 기존 `ProposalSubmissionService.submit_for_review()`로 state를 `reviewed`로 만든다.
2. `ReviewCardService.request()`가 reviewer binding, `PROPOSAL_DECIDE`, channel, current snapshot을
   다시 확인한다.
3. 같은 transaction에서 secret-free request command, audit event, Slack outbox event를 만든다.
4. `ReviewProjectionPayload`는 operation type count와 title preview만 담는다.
5. 같은 idempotency key replay는 기존 event를 반환한다. 다른 fingerprint는 거부한다.

Review request와 submit transition을 새 combined API로 만들지 않는다. 기존 state transition
계약을 유지하고, reviewed-without-card 상태는 idempotent request 재실행으로 회복한다.

### Review Card Delivery Path

1. Dispatcher가 Slack outbox event를 lease한다.
2. Retry라면 `reconcile()`가 remote marker를 먼저 찾는다.
3. Remote Card가 있으면 receipt를 회수하고 token set을 유지한다.
4. Remote Card가 없다고 확정되면 `ReviewActionSetService.prepare()`가 active set을 revoke한다.
5. 같은 transaction에서 세 ActionToken과 secret-free generation row를 만들고, generation
   expiry를 실제 issuance 시각 + 24시간으로 정한다.
6. Renderer가 raw credential을 button `value`에 넣고 즉시 Slack transport에 넘긴다.
7. Renderer, exception, result, log, durable row는 raw credential을 반환하거나 기록하지 않는다.

Slack channel binding은 workspace ID와 channel ID를 권한 경계로 사용한다. 원 요청의
`message_id`는 delivery provenance이며, 새 Review Card의 Slack timestamp와 같아야 하는 권한
조건이 아니다. 첫 command가 snapshot의 designated reviewer와 destination을 정하며, Review
intent uniqueness는 caller가 reviewer나 channel을 바꿔도 같은 snapshot에 두 번째 Card를 만들지
않는다. Outbox destination identity는 선택된 channel scope를 사용한다.

Remote 판정이 ambiguous하면 `prepare()`를 호출하지 않는다. 기존 outbox reconciliation error와
operator hold가 replacement를 차단한다.

`HttpSlackTransport.max_history_pages`, transport `lease_seconds`, destination
`max_history_pages`/`max_attempts`, dispatcher `OutboxConfig`는 하나의 delivery budget으로
검증한다. Page mismatch는 destination 생성 시, lease/attempt mismatch는 event claim 전에
거부한다.

### Interaction Feedback Path

`BoundedIngressAck`는 기존 3-second contract를 유지한다. Background worker는 governed decision을
끝낸 뒤 safe outcome code만 optional feedback port에 넘긴다. Slack 구현은 그 결과를
`chat.postEphemeral`로 알린다. **safe outcome 집합과 각 값의 producer는
[contracts/interaction-feedback.md](contracts/interaction-feedback.md)의 Safe Outcomes 표가
유일한 출처다** — 여기에 값을 다시 적지 않는다. 사본을 두면 다음 변경 때 뒤처진다.
Feedback 실패는 이미 완료된 decision을 rollback하지 않고 secret-free error code만 worker
result에 남긴다.

Result Card가 성공 decision의 authoritative visible feedback다. Ephemeral feedback은 오류 원인을
안전한 사용자 문구로 바꾸는 보조 경로다.

## Implementation Phases

### Phase 1 — Result Presentation

- `SlackProposalCardRenderer`와 Result Card model 구현
- `SlackProjectionDestination.send()` presentation adapter 연결
- approved/rejected/changes-requested contract test
- production-shaped result Card live E2E 전환

### Phase 2 — Review Intent And Action Lifecycle

- schema `31` request/action-set table과 schema verification 추가
- `ReviewProjectionPayload`, request service, audit/outbox integrity 추가
- transaction-scoped ActionToken issue/revoke helper 추가
- Review Card renderer와 recovery-aware destination 연결

### Phase 3 — Interaction Feedback And Validation

- worker safe feedback port와 Slack ephemeral adapter 추가
- signed local interaction test와 race/replay/permission test 추가
- real workspace Review/Result Card marker E2E 추가
- full quality gate와 three-lens review 실행

## Project Structure

### Documentation

```text
specs/003-slack-proposal-card/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── slack-card-payloads.md
│   ├── review-card-lifecycle.md
│   └── interaction-feedback.md
└── tasks.md
```

### Source Code

```text
src/amplai_foundry/
├── cli.py                    # governance sub-app: two read-only operator queries (D-044)
└── governance/
    ├── active_proposals.py   # existing reviewed transition, unchanged contract
    ├── decisions.py          # transaction-scoped token set issue/revoke; sole clear_exception_frames
    ├── events.py             # ReviewProjectionPayload and audit/outbox source checks
    ├── ingress_worker.py     # safe feedback handoff after decision
    ├── migrations.py         # schema versions 31-32
    ├── slack.py              # existing signed Block Action parser
    ├── slack_cards.py        # pure Result/Review Card renderer
    ├── slack_http.py         # chat.postMessage/history plus postEphemeral
    ├── slack_projection.py   # payload-specific rendering and action set lifecycle
    ├── store.py              # connect() normalizes every open-phase failure (T024)
    └── review_cards.py       # review request and action-set services

tests/
├── test_cli.py
├── test_decisions.py
├── test_governance_events.py
├── test_governance_migrations.py
├── test_governance_store.py
├── test_slack_ack_boundary.py
├── test_slack_cards.py
├── test_slack_http.py
├── test_slack_projection.py
└── test_review_cards.py
```

`cli.py` 의 `governance` sub-app 은 조회 전용이다 — `stranded()` 와 `committed_decision()` 두
읽기만 노출하고 governed mutation 은 없다 (`D-044`). 계약이 "operator's entry point" 라고
서술한 수단이 실재하게 만드는 것이 목적이다.

**Structure Decision**: Existing single Python package를 유지한다. Rendering은 pure module로,
durable lifecycle은 governance service로, network는 existing HTTP adapter로 나눈다.

## Complexity Tracking

Constitution violation 없음.
