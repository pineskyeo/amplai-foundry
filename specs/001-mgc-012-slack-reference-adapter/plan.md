# Implementation Plan: MGC-012 Package 3 — Ordered Slack Message Projection

**Branch**: `001-mgc-012-slack-reference-adapter` | **Date**: 2026-08-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-mgc-012-slack-reference-adapter/spec.md`

## Summary

Slack channel 로 나가는 Proposal Card 를 기존 ordered outbox 위에 얹는다. 새 dispatcher 를
만들지 않는다. `ProjectionDestination` Protocol (`events.py:236`)의 두 번째 구현
`SlackProjectionDestination` 하나를 추가하고, 그것이 주입받은 transport Protocol 을 부른다.

핵심 설계는 셋이다.

1. **remote 가 유일한 진실 원천이다.** 보낸 메시지에 Slack message metadata 로 event marker 를
   심고, `reconcile()` 은 channel 을 읽어 그 marker 를 찾아 판정한다 (research R-003).
2. **판정 불가는 fail-closed 다.** 조회 상한까지 훑어도 결론이 안 나면 `OutboxReconcileError`
   를 던지고, 기존 `deliver_next` 가 그것을 DLQ + operator hold 로 보낸다 (R-001, R-004).
3. **실제 network 호출은 없다.** transport 는 Protocol 이다. 실제 HTTP 는 Package 4 몫이다
   (D-018 항목 2).

## Technical Context

**Language/Version**: Python 3.11 / 3.12 (`pyproject.toml`, SPEC.md `AC-15`)

**Primary Dependencies**: `pydantic`, `pyyaml`, `typer` — **셋을 유지한다.** HTTP client 를
추가하지 않는다 (D-018 항목 2)

**Storage**: SQLite governance store. Package 3 는 schema 를 바꾸지 않는다

**Testing**: `python -m pytest` 전량. 숫자를 baseline 으로 고정하지 않는다 (SC-006)

**Target Platform**: local CLI / worker process

**Project Type**: single project — `src/amplai_foundry/`

**Performance Goals**: 별도 목표 없음. Slack 쪽 상한은 `chat.postMessage` channel 당 초당
1건, `conversations.history` Tier 2 (research S4)

**Constraints**: `destination_ref` 형식 `provider:{provider}:{channel_digest}` 불변
(D-018 항목 1). `OutboxDispatcher` 계약 불변 (MGC-008 gate PASS at `e989566`)

**Scale/Scope**: 신규 module 1개, 신규 test file 1개. 기존 파일은 `projections.py` 의 사전
검증 예외 종류와 그 test 만 바뀐다 (D-022)

## Constitution Check

*GATE: Phase 0 전 통과, Phase 1 후 재확인.*

| 원칙 | 판정 | 근거 |
|---|---|---|
| I. Knowledge Safety | PASS | canonical Vault 를 안 건드린다. `specs/` 문서만 만든다 |
| II. Small Verifiable Change | PASS | vector DB·MCP server·web UI 를 안 넣는다. dependency 셋 유지. 신규 module 1개 |
| III. Evidence-Based Completion | PASS (조건부) | 각 task 의 acceptance 를 repo 에 실제 있는 명령으로 쓴다. gate 는 pytest·Ruff·mypy·knowledge lint 실행 결과로만 연다 |
| IV. Governed Mutation Only | N/A | Package 3 는 Proposal mutation 경로를 안 만든다. outbox event 소비만 한다 |
| V. Independent Review Before Gate | PASS | wave 마다 contract·failure-recovery·regression 3 lens (D-019 항목 4) |

**Phase 1 재확인**: 아래 Phase 1 산출물(data-model.md, contracts/, quickstart.md)은 새 원칙을
만들지 않고 dependency 를 늘리지 않는다. 판정 유지.

위반 없음 — Complexity Tracking 는 비운다.

## Decisions Carried From Research

`research.md` 가 `plan.md` 로 넘긴 판단 둘을 여기서 닫는다.

### P-001 — Deleted Card Falls To Hold

> **좁혀짐 (D-023, 2026-08-05).** 아래 결정은 **조회 범위를 다 못 본 경우로 한정된다.**
> `next_cursor` 가 없어 history 를 끝까지 훑었는데도 marker 가 없으면 미전송으로 보고
> 보낸다. 좁히지 않으면 `destination_sequence` 가 1 인 event 가 판정 근거를 댈 수 없어
> **모든 destination 의 첫 Card 가 영구 hold** 가 된다. 아래 근거의 "중복 Card" 는 범위를
> 다 못 본 경우에만 성립한다 — history 가 소진됐다면 없는 것이 확정이라 두 장이 될 수
> 없다. 확정 계약은 contracts C-2.2 다.

**결정**: 사람이 Card 를 지워 marker 를 못 찾으면 hold 로 떨어뜨린다. 다시 보내지 않는다.

**근거**: reconcile 이 불리는 구간은 좁다. 이미 `delivered` 로 mark 된 event 는 다시 claim
되지 않으므로 reconcile 도 안 불린다. 즉 "지워진 Card" 가 문제가 되는 경우는 **send 성공과
mark 사이에 죽은 그 event 뿐**이다. 그 상황에서 marker 부재를 "안 보냈다"로 읽으면 중복
Card 를 만든다. SPEC.md `Outbox Ordering` 의 "Reconcile 불가 시 DLQ와 operator hold를
생성한다" 를 그대로 따른다.

**기각**: 못 찾으면 재전송하는 안. User Story 2 가 막으려는 결과를 만든다.

### P-002 — Transport-Level Failure Is Retryable, Unknown Slack Code Is Not

**결정**: 두 층을 나눈다.

| 층 | 처리 |
|---|---|
| **Slack error code 가 없는 모든 실패** — 연결 실패, timeout, 임의의 HTTP status | **재시도** |
| Slack 이 준 error code | research R-006 의 allowlist 만 재시도, 나머지 terminal |

판정 기준은 status code 가 아니라 **Slack 이 `ok:false` 를 줬는지** 하나다. HTTP 429 는
Slack code 유무와 무관하게 재시도이고, 규칙은 위에서부터 먼저 맞는 것이 이긴다.

**근거**: transport 실패는 "요청이 처리됐는지 모른다"는 상태다. 그런데 다음 시도는 send 전에
reconcile 을 먼저 하므로 (R-002) 이미 posted 됐으면 marker 로 확정된다. **재시도가 idempotent
하다.** 반면 Slack 이 명시적으로 준 unknown code 는 의미를 모르는 것이라 D-016 의 fail-closed
를 따라 terminal 로 둔다.

**기각 1**: 5xx 도 terminal 로 두는 안. 일시적 장애에 operator hold 를 걸어 사람을 부른다.

**기각 2 (wave 1 review, D-020 항목 1)**: code 없는 non-429 4xx 를 terminal 로 두는 안.
초기 구현이 이 안이었고 reviewer 셋이 모두 반대했다. Slack 은 application error 를 HTTP 200 +
`ok:false` 로 주므로 code 없는 4xx 는 거의 전부 중간 장비가 낸 것이고 그건 transient 다.
게다가 retryable 이 보수적인 쪽이다 — 영구 실패를 재시도로 봐도 attempt 소진 후 같은 dead
letter 와 hold 에 도달하지만, transient 를 terminal 로 보면 destination 이 즉시 멈추고 그
hold 는 되돌릴 수 없다.

## Project Structure

### Documentation (this feature)

```text
specs/001-mgc-012-slack-reference-adapter/
├── plan.md              # 이 파일
├── spec.md              # 파생 spec (원본은 외부 SPEC.md + CURRENT_ITEM.md)
├── research.md          # Phase 0 — Slack 공식 문서 사실 고정
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── slack-transport.md   # transport Protocol 과 destination 계약
└── tasks.md             # /taskify manifest 에서 생성한다. 손으로 쓰지 않는다
                         # python3 .specify/scripts/taskify_to_tasks_md.py \
                         #     specs/001-mgc-012-slack-reference-adapter/task-manifests
```

설계 소스가 `specs/` 안에 있으므로 taskify 출력 위치는 `.amplai/tasks/` 가 아니라
`<feature-directory>/task-manifests/` 다 (`.claude/skills/taskify/SKILL.md:59-60`).
`--out` 은 **필요 없다.** script 기본값이 `<manifest-dir>/../tasks.md` 인데 manifest 가
`specs/<feature>/task-manifests/` 안에 있어 그 값이 정확히 맞는다. 생성된 `tasks.md` 의
header 가 출력하는 재생성 명령에도 `--out` 이 없다. `--out` 이 필요한 것은 manifest 가
`.amplai/tasks/` 아래 있을 때다.

### Source Code (repository root)

```text
src/amplai_foundry/governance/
├── events.py            # 수정 없음 — ProjectionDestination Protocol, OutboxDispatcher
├── projections.py       # 사전 검증 예외 종류만 변경 (D-022) — 나머지는 참조 구현
├── slack.py             # 수정 없음 — Package 1·2 의 ingress 인증
└── slack_projection.py  # 신규 — SlackProjectionDestination, transport Protocol, error 분류

tests/
├── test_slack_projection.py   # 신규
└── test_governance_events.py  # D-022 의 projections.py 변경에 대한 test 추가
```

**Structure Decision**: 기존 단일 package 구조를 그대로 쓴다. `slack.py` 에 넣지 않고
`slack_projection.py` 를 새로 만든다. `slack.py` 는 ingress(들어오는 요청 인증)이고 이번 것은
egress(나가는 message)라 방향이 반대다. 두 관심사를 한 파일에 두면 Package 4 에서 실제 HTTP 를
붙일 때 ingress test 까지 흔든다.

## Implementation Waves

wave 는 `/taskify` 가 `index.yaml` 에 확정한다. 아래는 taskify 에 넘길 **제안 경계**이고 각
wave 는 그 자체로 test 가능한 vertical slice 다. wave 종료마다 contract·failure-recovery·
regression 3 lens review 를 돌린다 (D-019 항목 4).

### Wave 1 — Transport Protocol And Error Classification

- transport Protocol 정의 (send / read)
- Slack error code → retryable·terminal 분류기
- Slack error code 가 없는 실패와 Slack error code 를 다른 층으로 다루는 경계 (P-002)
- 검증: 분류기 단위 test. network 없음

### Wave 2 — Send Path

- `SlackProjectionDestination.send()` — digest·destination_ref 사전 검증, metadata marker
  구성, receipt `slack:{channel}:{ts}` 생성
- 검증: fake transport 로 send 계약 test. `OUTBOX_DESTINATION_MISMATCH`,
  `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` 포함

### Wave 3 — Reconcile Path

- `reconcile()` — 첫 시도 분기, bounded 역순 조회, 네 갈래 판정 (R-004, D-023), 판정 불가
  시 `OutboxReconcileError`
- 검증: marker 발견 / 하위 sequence 선발견 / 상한 초과 세 경우

### Wave 4 — Dispatcher Integration And Failure Matrix

- `deliver_next` 와 실제로 물려서 도는지 확인 (dispatcher 코드는 안 고친다)
- reverse-order, competing dispatcher, supersede, send-후-kill, deleted message
- 검증: SC-001 ~ SC-005

wave 순서는 dependency 순이다. wave 2 는 wave 1 의 Protocol 을, wave 3 은 wave 2 의 marker
형식을, wave 4 는 둘 다 필요로 한다.

## Out Of Scope For This Plan

- Package 4 — Slack reference E2E, activation isolation, closure review (D-019 항목 8)
- D-014 destination granularity 변경 (D-018 항목 1)
- `Retry-After` 를 backoff 에 주입하는 dispatcher 계약 변경 (research R-007)
- 이미 전달된 Card 를 `chat.update` 로 갱신하는 기능 (research R-008)

## Complexity Tracking

Constitution Check 위반 없음. 비운다.
