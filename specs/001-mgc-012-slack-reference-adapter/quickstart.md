# Quickstart — Validating MGC-012 Package 3

**Feature**: `001-mgc-012-slack-reference-adapter` | **Date**: 2026-08-03

Package 3 를 검증하는 방법이다. **실제 Slack workspace 가 필요 없다** — Package 3 는 주입받은
transport Protocol 만 부른다 (D-018 항목 2). 실제 workspace E2E 는 Package 4 다.

계약 자체는 [contracts/slack-transport.md](./contracts/slack-transport.md) 에, 구조는
[data-model.md](./data-model.md) 에 있다. 여기서 반복하지 않는다.

## Prerequisites

- Python 3.11 이상 (`pyproject.toml` `requires-python`)
- dev extra 설치

```bash
python -m pip install -e '.[dev]'
```

- 추가 dependency 없음. Package 3 가 dependency 를 늘리면 그 자체가 D-018 항목 2 위반이다.

## Test Double

실제 Slack 대신 in-memory fake transport 를 쓴다. fake 는 `SlackTransport` (C-1)를 만족하고
아래를 할 수 있어야 한다.

- posted message 를 순서대로 보관하고 `read_history` 로 역순으로 돌려준다
- `post_message` 가 성공을 반환한 뒤 **호출자에게 알리지 않고** 죽는 상황을 흉내낸다
  (send 성공 + mark 실패 재현용)
- 지정한 Slack error code 또는 transport 층 실패를 내도록 설정된다
- 특정 message 를 history 에서 지워 "사람이 Card 를 삭제한" 상태를 만든다

fake 는 `tests/` 안에 둔다. `src/` 에 넣지 않는다.

## Scenario Checks

각 항목은 `spec.md` Success Criteria 와 1:1 이다.

| # | 시나리오 | 기대 | 대응 |
|---|---|---|---|
| 1 | 같은 destination 에 sequence 1·2 를 넣고 dispatcher 를 돌린다 | 1 이 확정되기 전에 2 를 안 보낸다 | SC-001 |
| 2 | dispatcher 둘이 같은 destination 을 동시에 claim 한다 | 한쪽만 lease 를 얻고 다른 쪽은 `OUTBOX_LEASE_CONFLICT` 또는 `None` | SC-002 |
| 3 | pending event 를 새 event 로 supersede 한다 | 이전 event 는 `superseded` 이고 Slack 으로 안 나간다 | SC-003 |
| 4 | `post_message` 성공 직후 mark 없이 중단하고 다시 `deliver_next` | reconcile 이 marker 를 찾아 같은 receipt 로 확정한다. message 가 두 개가 되지 않는다 | SC-004 |
| 5 | 4번 상태에서 history 의 그 message 를 지우고 **`next_cursor` 가 남을 만큼 history 를 쌓는다** | reconcile 이 상한까지 못 찾고 `SLACK_PROJECTION_RECONCILE_SEARCH_CAP_REACHED` → DLQ + operator hold. 재전송하지 않는다 | SC-005, plan P-001 (D-023 이 좁힘) |
| 6 | transport 가 `ratelimited` 를 낸다 | `retry_wait` 로 가고 attempts 가 오른다 | contract C-3 |
| 7 | transport 가 `channel_not_found` 를 낸다 | 재시도 없이 DLQ + hold | contract C-3 |
| 8 | transport 가 HTTP 5xx 를 낸다 | `retry_wait` | plan P-002 |
| 9 | `event.destination_ref` 를 어긋나게 준다 | `OUTBOX_DESTINATION_MISMATCH`, transport 미호출. 재시도 없이 DLQ + hold | contract C-2.1, D-022 |
| 10 | payload 를 바꿔 digest 를 어긋나게 한다 | `OUTBOX_PAYLOAD_INTEGRITY_FAILURE`, transport 미호출. 재시도 없이 DLQ + hold | contract C-2.1, D-022 |

**시나리오 5 는 history 를 소진시키면 안 된다.** D-023 이 "`next_cursor` 가 없어 history 가
소진됐으면 미전송" 규칙을 더했으므로, 작은 채널에서 Card 를 지우면 hold 가 아니라 **재전송**
된다. hold 를 증명하려면 상한에 걸리도록 history 를 충분히 쌓아야 한다.

시나리오 5 는 hold 가 걸린 뒤 **같은 destination 의 다음 event 가 claim 되지 않는 것**까지
확인한다. `claim_next` 의 `d.operator_hold = 0` 조건이 그것이다 (`events.py:2634`).

## Commands

```bash
python -m pytest tests/test_slack_projection.py
```

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

`mypy` 설정은 `strict = true` 이고 `packages = ["amplai_foundry"]` 다 (`pyproject.toml`).
인자 없이 돌리면 설정이 잡는다.

## Expected Baseline

- 기존 test 가 **전량** 회귀 없이 통과한다. 숫자를 고정하지 않는다 — wave 마다 늘고
  갱신 장치가 없다 (SC-006, `/speckit-analyze` I1)
- `amplai-foundry verify` 7 stage 통과
- 위 명령 중 **실제로 돌린 것만** gate 기록에 쓴다. 안 돌린 것을 통과했다고 쓰지 않는다
  (constitution 원칙 III)

## Wave Review

wave 마다 3 lens subagent review 를 돌린다 (D-019 항목 4). 검토 범위는 그 wave 의 diff 다.

1. contract — 계약과 acceptance 충족 여부
2. failure/recovery — 실패 경로, timeout, 복구
3. regression — 기존 동작 회귀

P0·P1·Blocking-P2 가 하나라도 있으면 다음 wave 로 가지 않는다. 기록은
`docs/workstreams/messenger-governance-closure-v3/CHECKPOINTS/` 에 남긴다.
