# MGC-012 Package 3 Wave 1 Review

- Item: `MGC-012` — Slack Reference Adapter
- Package: 3 — Ordered Slack message projection, retry and recovery
- Wave: 1 — `MGC-012-T001` transport Protocol and error classification
- Date: 2026-08-03 ~ 2026-08-05
- Gate: **wave gate PASS.** 이 문서가 그 근거다.

> **범위 주의.** 이것은 wave gate 이지 Package gate 도 Item gate 도 아니다. MGC-012 의
> Item Gate 는 `QUALITY_GATES.md` 기준이고 Package 3 는 wave 2·3·4 가 남아 있어 아직
> 열리지 않았다. `STATUS.md`·`CURRENT_ITEM.md`·`PROGRESSION_QUEUE.md` 의 마지막 PASS 는
> 여전히 `2dcf663` (Package 2) 이다.

## Scope

변경 대상은 둘이다.

- `src/amplai_foundry/governance/slack_projection.py` (신규)
- `tests/test_slack_projection.py` (신규)

`forbidden_paths`(`events.py`, `projections.py`, `slack.py`, `pyproject.toml`)는 건드리지
않았다.

## Rounds

lens 는 매 라운드 셋 전부 돌렸다 (D-019 항목 4). 이름은 `workflow.yml` 의
`review-implementation` gate 를 따른다.

| Round | contract | failure/recovery | regression |
|---|---|---|---|
| 1 | BP2 3, P1 1 | **P0 1**, P1 4, BP2 2 | **P0 1**, BP2 2 |
| 2 | P1 3, BP2 1 | P1 1, BP2 3 | 0 |
| 3 | P1 3, BP2 2 | P1 1, BP2 1 | 0 |
| 4 | P1 1, BP2 4 | P1 1, BP2 2 | 0 |

표의 건수 합은 중복을 포함한다 — 여러 lens 가 같은 결함을 각자 보고한 경우가 있다.
P0 는 2건이고 둘 다 round 1 에서 나왔다.

## P0

1. **bare 4xx 를 terminal 로 분류했다.** Slack 은 application error 를 HTTP 200 +
   `ok:false` 로 주므로 code 없는 4xx 는 거의 전부 중간 장비가 낸 transient 다. 그것을
   terminal 로 보면 destination 이 즉시 멈추고 그 hold 는 `CHECK (resolved_at IS NULL)`
   때문에 되돌릴 수 없다. 반대 방향의 손해는 attempt 소진까지의 몇십 초뿐이다. 분기를
   삭제했다. → D-020 항목 1
2. **CI 의 `ruff format --check .` 가 실패했다.** manifest 의 lint command 가
   `ruff check` 만 담고 있어 안 잡혔다. 포맷을 고치고 command 와 evidence 에 추가했다.

## Recurring Defect Class

**AC 문구가 자기 test 와 어긋나는 것**이 네 라운드 연속 나왔다 — round 1 AC-05, round 2
AC-05(반대 방향), round 3 AC-03, round 4 AC-09. 원인은 AC 를 고칠 때 code 만 보고 다른 AC 를
안 본 것이다.

round 3 에서 "AC-01~09 전부를 1:1 대조했다"고 적었으나 **round 4 contract lens 가 그것을
반증했다** — `AC-08` marker 를 단 test 가 하나도 없었고 두 test 가 `AC-01` 을 잘못 달고
있었다. round 4 에서 marker 를 교정하고 AC-09 문구를 test 에 맞췄다.

## Round 3 Key Findings

- **형식 위반 code 를 버리는 것이 round 2 P1 을 되살렸다.** persistence 검증과 classification
  입력을 한 필드에 합친 것이 원인이다. `missing_scope: chat:write` 같은 값이 code 없음이
  되어 retryable 로 새고, 그 경로는 원인을 안 남긴다. 분류는 정규화된 값으로 하고 형식
  강제는 저장 직전에만 하도록 분리했다. 버리지 않고 다듬는다.
- **retry 예산 하한 180 이 틀렸다.** 자기 주석의 유도는 120 을 주고, 합에 거는 것 자체가
  틀렸다. round 3 에서 하한을 마지막 대기에 걸고 값을 60 으로 바꿨으나 **round 4 에서 그것도
  무너졌다** — 아래 참조.
- **D-020 의 `events.py` 인용 두 개가 한 줄씩 어긋났다** (`:2645`→`:2644`, `:2846`→`:2845`).

## Round 4 Key Findings

- **하한 상수의 근거가 처음부터 없었다.** `research.md` R-007 의 "Slack `Retry-After` 는
  30~60초가 흔하다" 는 S4 에 없는 문장이다. 2026-08-05 에 S4 를 다시 조회해 확인했다 —
  문서가 주는 숫자는 `conversations.info` 를 30초 기다리라는 **예시 하나**뿐이고 상한을
  문서화하지 않는다. 그 문장이 하한 60 의 유일한 근거였고 D-020·CHECKPOINTS·code 주석까지
  여섯 곳에 복제돼 있었다.
- **하한 60 은 자기가 대체한 규칙보다 약했다.** 대기가 `b,2b,4b,8b` 라 합 ≥ 180 이면 마지막
  대기 ≥ 96 > 60 이다. 즉 옛 규칙이 새 규칙을 함의한다. 그리고 새 하한이 허용하는 최소
  schedule(`base=8`)은 attempt 넷이 60초 안에 들어가 `base=12`(셋)보다 나쁘다. round 3 이
  근거로 든 반례가 성립하지 않았다.
- **결론: 하한 상수와 그 test 를 뺐다.** 상한을 모르면 "예산이 충분하다"를 판정할 수 없다.
  대신 기본 schedule(`[5,10,20,40]`, 합 75)을 사실로만 고정한다. 근거 없는 숫자를 기계적
  관문으로 만들면 만족시킨 쪽이 안전하다고 잘못 믿는다.
- **non-str `error_code` 를 통째로 버리고 있었다.** round 3 이 세운 "다듬되 버리지 않는다"를
  같은 fix 안에서 어겼다. 문자열로 만들어 보존하도록 고쳤다.
- **sanitize 손실이 구분 불가를 만들었다.** `###` 와 `%%%` 가 둘 다 `___` 가 되고 64자 접두가
  같은 두 code 가 같은 row 를 남겼다. 손실이 일어나면 원본 digest 8자를 붙인다.
- **`status_code` 에는 type 방어가 없었다.** `error_code` 오염은 retryable 로 흐르는데 이쪽은
  terminal 로 흘러 되돌릴 수 없는 hold 를 만든다. 방어를 넣었다.

## Deferred

- **retryable 경로의 원인 소실** — `deliver_next` 의 generic handler 가 예외를 버리고
  `OUTBOX_DELIVERY_FAILED` 만 남긴다. 고치려면 `events.py` 를 건드려야 하고 그건 T001 의
  `forbidden_paths` 다. T002 로 넘겼다. → D-020 항목 6
- **`Retry-After` 를 backoff 에 주입하는 것** — MGC-008 에서 gate PASS 한 dispatcher 계약을
  바꾼다. 별도 item 이다. wave 1 은 하한 상수와 test 만 남긴다. → D-020 항목 5, R-007

## Verification

전부 실제 실행했다. reviewer 도 독립으로 재실행해 같은 결과를 확인했다.

round 4 반영 후 값이다. 전부 실제 실행했고 reviewer 도 각 라운드에서 독립으로 재실행했다.

| Command | Result |
|---|---|
| `python -m pytest` | 781 passed (baseline 709 + 신규 72) |
| `python -m pytest tests/test_slack_projection.py` | 72 passed |
| `python -m ruff check .` | 통과 |
| `python -m ruff format --check .` | 128 files 통과 |
| `python -m mypy` | 99 source files 통과 |
| `python -m mypy tests/test_slack_projection.py` | 통과 |
| `amplai-foundry verify` | 7/7 PASS |

test 수 계보: round 1 종료 시 24, round 2 종료 시 45, round 3 반영 후 62. baseline 709 는
`git stash -u` 대조로 확인했다 — 기존 파일의 collection 은 하나도 안 움직였다. round 4
반영 후 72다.

## Gate Decision

`.specify/workflows/speckit/workflow.yml` 의 `review-implementation` 기준을 적용한다.

- reviewer 는 관점이 다른 셋이다 — contract, failure/recovery, regression. 네 라운드 모두
  셋 전부 돌렸다 (D-019 항목 4).
- 마지막 라운드(round 4) blocker 를 전부 반영한 뒤 **P0·P1·Blocking-P2 가 0건**이다.
- 반영 후 검증을 전부 실제 실행했다. 위 표가 그 결과다.

**PASS.** `MGC-012-T001` 을 `done` 으로 옮기고 `completion` 에 이 문서를 evidence 로 적는다.

닫지 않은 것은 아래 Deferred 에 있고, 각각 D-020 항목이나 후속 task 에 주인이 있다.

### 남은 Advisory

gate 를 막지 않지만 기록한다.

- `persisted_code_suffix` 이름과 허용 문자 집합이 `contracts/slack-transport.md` 에 없다.
  `data-model.md` 에만 있다.
- signature test 가 상속 멤버와 annotation-only 속성을 못 본다. 상위 Protocol 에 method 를
  더하거나 class body 에 속성을 두면 통과한다.
- `governance/__init__.py` export 경계는 T004·T005 에서 정한다. 지금은 module 전체가
  미export 라 일관적이다.
- T002 가 `scope_boundary_must_expand` 로 멈추면 D-020 항목 6 을 이어받을 task 가 없다.

## Status

`MGC-012-T001` 은 `done` 이다. wave 2 는 `MGC-012-T002` 부터다.
