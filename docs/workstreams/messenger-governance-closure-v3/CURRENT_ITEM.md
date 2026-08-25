# Current Item — MGC-012

## Goal

Slack interaction을 raw body 상태에서 인증하고 verified credential만 기존 durable
ingress 계약으로 전달한다. 성공 ack는 ingress commit 이후 3초 이내 반환하며 decision,
message projection과 retry는 background path에서 수행한다.

## Frozen Acceptance

- A1: raw request body를 deserialize 전에 확보하고 검증 byte와 fingerprint byte가 동일함
- A2: `X-Slack-Signature` HMAC을 constant-time으로 검증하고 signing secret 또는 raw body를 저장·로그하지 않음
- A3: `X-Slack-Request-Timestamp`의 허용 clock skew를 검증하고 stale 또는 malformed request를 fail-closed함
- A4: verified payload의 `api_app_id`, workspace 또는 enterprise identity가 Project installation allowlist와 일치함
- A5: 공식 `block_actions` contract만 수락하고 범용 `interaction_payload_id`를 요구하지 않음
- A6: external actor, container/view, action ID, action timestamp와 raw body digest로 deterministic ingress fingerprint를 생성함
- A7: opaque ActionToken credential은 hash로만 durable ingress에 전달하고 raw credential과 raw body는 DB·Audit·log에 남기지 않음
- A8: durable ingress commit 전 success ack를 반환하지 않으며 DB busy 또는 commit 실패 시 non-success ack를 반환함
- A9: ingress connection timeout과 전체 synchronous path가 Slack 3-second ack budget을 침범하지 않음
- A10: ack 이후 decision 처리는 기존 authority, idempotency, token과 Proposal state 계약을 background worker에서 사용함
- A11: Provider message는 ordered Outbox를 사용하며 retry, supersession, DLQ와 operator hold 계약을 우회하지 않음
- A12: 동일 verified interaction replay는 최초 durable 결과로 수렴하고 같은 key의 다른 fingerprint는 conflict로 실패함
- A13: unsupported payload, invalid signature/timestamp/app/workspace/actor/action과 malformed form/JSON test가 fail-closed함
- A14: Slack adapter가 Telegram 또는 다른 Provider activation state를 변경하지 않음
- A15: Slack reference E2E, 전체 regression과 `amplai-foundry verify`가 통과하고 Contract·Evidence·Ops review blocker가 0건임

## Slices

1. Raw request verification and normalized Slack interaction contract
2. Durable ingress ack boundary and background decision handoff
3. Ordered Slack message projection, retry and recovery
4. Slack reference E2E, activation isolation and closure review

## Current Package

- Package 1 — raw-body signature/timestamp/allowlist contract and fail-closed fixtures:
  `PASS` at `ef35201`
- Package 2 — durable ingress ack boundary and background decision handoff:
  `PASS` at `2dcf663`
- Package 3 — ordered Slack message projection, retry and recovery:
  `PASS` at `5b2024b`
- Package 4 wave 5 — real Slack HTTP transport and credential path:
  `PASS` at `dfbcd15`
- Package 4 Wave 6R — typed readback, network-free E2E harness, Provider isolation:
  `PASS` for `MGC-012-T008`, `MGC-012-T009`, `MGC-012-T011` under `APR-017` / `D-032`
- Package 5 — Slack Proposal Cards (`specs/003-slack-proposal-card`, feature `MGC-012-P5`):
  T001~T034 `done`, T020 `superseded`. **round 17 FAIL. gate 없음**
- Frozen source/test evidence: round 17 aggregate
  `8c7d2220b51fd53f191ea2293976549dc33cec1b7edf9ffc4eeea59b62a23939` (52 파일).
  round 16 target 44개 중 14개가 바뀌었고 wave 10 산출물 8개가 늘었다.
  세 reviewer 가 착수·종료에 확인했고 regression 은 mutation 9회 전후로도 확인했다. 무손상
- Tests: full `1382 passed, 4 deselected` (wave 8 착수 시 1301 → +81)
- Verification: Ruff check/format, mypy, `verify` 7/7, manifest validator 34/34,
  `git diff --check` 모두 PASS
- Review: **round 10~17 전부 FAIL.** round 15·16 은 각 9건(P0 1). **round 17 은 8건 —
  P0 0 / P1 3 / Blocking-P2 5.** 등급이 처음으로 내려갔다
- **round 16 의 9건 중 여덟이 닫혔다.** contract reviewer 가 각각 실행으로 확인했다.
  `FR-2` 는 절반만(`F17-2`), `C16-3` 은 안 닫혔다(`F17-4`)
- **2026-08-25 round 17 완료.** 기록은 `specs/003-slack-proposal-card/evidence/3lens-review-round-17.md`
- Selected next item: **wave 11** — round 17 의 blocker 8건을 닫는다
- Sequence: `wave 11(taskify → 구현) → 재freeze → round 18 review → (blocker 0이면) gate
  → T010 → T013 → T012`

## Wave 11 착수 지시

round 17 의 blocker 8건을 닫는다. 전문은
[`3lens-review-round-17.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-17.md).

### 묶어서 설계할 것 — `F17-1`·`F17-2`·`F17-7`

셋이 **한 자리**에서 나왔다. `_claim_one` 의 transaction 경계와 `_dead_letter_unreadable` 의
guard 다. **하나씩 고치면 round 10~17 이 반복한 "한 방향을 고치며 반대를 만든다" 가 여덟
번째가 된다.**

| id | 등급 | 결함 |
|---|---|---|
| `F17-1` | P1 | `state IN ('pending','retry_wait')` guard 가 만료 lease 를 "다른 worker 가 가져갔다" 로 오해한다. 손상 row 가 `leased`+만료면 write 가 항상 0행이고 뒤 command 가 영원히 안 처리된다 |
| `F17-2` | P1 | dead-letter write 실패 시 row 가 `pending`·`attempts=0` 으로 남는데 `_stranded_rows` 가 `pending` 을 안 본다. `attempts` 는 rollback 되는 transaction 안에서만 증가하므로 시간이 지나도 안 보인다 |
| `F17-7` | B-P2 | 실패한 claim transaction 이 lease-expiry sweep 과 exhaustion sweep 까지 rollback 한다 |

설계할 때 답해야 하는 것 — **guard 가 0행을 내는 상태를 전부 세고 각각의 처리를 정한다.**
round 17 이 실측한 표가 review 문서 "뿌리 1" 절에 있다.

### test 만 추가하면 닫히는 것 — `F17-3`·`F17-8`

```text
F17-3  claim_generation guard 와 state guard 를 각각 가르는 시나리오
F17-8  cli.py:800 의 세 번째 sqlite3.Error 포획
```

**지금 test 는 두 조건이 동시에 참인 시나리오뿐이라 어느 하나를 지워도 통과한다.** 가르는
시나리오는 `_dead_letter_unreadable` 의 docstring 이 스스로 이름을 댄다 — generation 만
어긋난 경우(claim → lease 만료 → `pending` 복귀)와 state 만 어긋난 경우(`completed` 가 되어
`completed_at` CHECK 위반). `F17-8` 은 `is_unreadable()` 만 실패하는 상태를 만들어야 한다.

### 문서 규율 — `F17-4`·`F17-5`·`F17-6`

`T034` 가 지적한 것을 같은 wave 가 재생산했다. wave 11 은 **자기 manifest 부터** 본다.

```text
F17-4  required: true 인 targeted command_output 이 T026·T027·T031·T032·T033 에 없다 (9 중 5)
F17-5  T033 AC-02 가 evidence 에 통째로 없는데 all_acceptance_passed: true
F17-6  T031 AC-07·AC-12 절 없음. AC-03 은 남의 재현을 인용
```

`A17-5` 도 같이 고친다 — "legacy_*.py **아홉** handler" 가 문서 넷(`cli.py:716`,
`test_governance_store.py:1177,1186,1220`)에 있는데 **실측 13** 이다. 두 방법으로 셌다.
결정 자체는 안 바뀐다.

### 순서

```text
/taskify → wave 11 구현 → 재freeze → round 18 three-lens → (blocker 0이면) gate
```

`/taskify` 를 건너뛰지 않는다. blocker 를 고치는 wave 도 탄다 — wave 9·10 둘 다 탔다.

- Stop rule: Package 5 review 가 닫히기 전에는 Package 4 구현을 재개하지 않는다
- Stop rule: **형제 위치를 끝까지 센다.** 규칙은 "정의 사본 + **호출 지점** + **같은 검사의
  모든 성분**" 이다. 여섯 라운드 연속 이것을 어겼다. wave 8 이 실측으로 보여준 값 —
  감사 성분은 69가 아니라 **72**, 호출 지점은 12가 아니라 **14**, 무방비 credential guard 는
  1개가 아니라 **4개**, `connect()` 형제는 3개가 아니라 **7개**, `_view` 호출은 3개가 아니라
  **5개**, revert 무방비 지점은 1개가 아니라 **2개**, CLI raw traceback 상태는 2개가 아니라
  **4개**. **매번 실측이 기록보다 많았다**
- Stop rule: **수치를 보고하기 전에 두 방법으로 세고 둘 다 적는다.** 여섯 라운드 연속
  틀렸다 — 12 "21종을 19종", 13 "12종을 9종", 14 "8블록을 9블록", 15 (wave 8 착수 전 수치 넷),
  16 "test 11→15" (실제 10→15, 산수도 안 맞음). **원인은 매번 같다 — 세지 않고 기억하거나
  추정한 값을 적었다**
- Stop rule: **새 구조를 넣으면 그 구조가 요구하는 것을 센다.** round 16 blocker 넷이
  wave 9 의 `while True` + transaction 밖 write 하나에서 나왔다 — 진행 보장(`rowcount`),
  경합 guard(`state`/`generation`), write 실패 처리, "모름"과 "없음"의 구분을 전부 빠뜨렸다
- Stop rule: **정규화는 위치를 먼저 정한다.** `connect()` 에 넣으면 `legacy_*.py` 아홉
  handler 가 깨지고(round 15 `R-1`), 안 넣으면 CLI 가 traceback 을 낸다(round 16 `FR-3`,
  `R16-1`). **최종 소비자가 경계다** — CLI 에서 `sqlite3.Error` 를 잡아 둘 다 닫았다
- Stop rule: review 지시를 그대로 구현하기 전에 다른 축이 무너지는지 본다. 한 방향을 고치며
  반대를 만든 것이 round 10 → 16 으로 계속 이어졌다
- Stop rule: **test 를 쓸 때 fixture 가 검사 대상을 지우는지 본다.**
- Stop rule: **mutation 이 살아남으면 코드보다 test 를 먼저 의심한다.** round 17 에서
  9개 중 셋이 SURVIVED 했다 — `claim_generation` guard, `state` guard, `cli.py:800` 의
  세 번째 포획. **세 라운드째 같은 형태다** (round 15 `R-1`, round 16 `R16-2`)
- Stop rule: **guard 를 좁히면 그 밖의 상태를 전부 센다.** round 17 `F17-1` 이 그것이다.
  `state IN ('pending','retry_wait')` 가 "0행 = 다른 worker 가 가져갔다" 만 가정했는데
  만료 lease 도 0행을 낸다. **round 15 의 head-of-line 차단이 되돌아왔다**
- Stop rule: **review 지적을 고치는 wave 가 같은 지적을 재생산하지 않는지 본다.**
  `T034` 가 `C16-3`·`A16-5` 를 지적하면서 `T031`·`T033` 이 각각 그 둘을 다시 냈다
  (round 17 `F17-4`·`F17-5`·`F17-6`)
- Stop rule: **`/taskify` 를 건너뛰지 않는다.** blocker 를 고치는 wave 도 Pre-Implement
  Procedure 를 탄다. wave 9·10 둘 다 탔다
- Stop rule: **계약 변경은 Decision 을 먼저 받는다.** wave 9 의 `D-045` 가 그 예다
- Stop rule: **AC 를 구현 뒤에 맞추지 않는다.** 못 지킬 AC 는 **착수 전에** 고치고 원문을
  남긴다. `T027` AC-06 이 그 사례이고 round 16 이 적법하다고 판정했다
- Stop rule: 재freeze → 재review 순서를 지킨다
- Stop rule: **되돌린 것도 test 로 묶는다.** `T029` 가 회귀를 고쳤는데 세 지점 중 둘이
  test 없이 남아 round 16 `R16-2` 가 됐다
- Stop rule: **면제를 적을 때 누구에게 적용되는지 확인한다.** `T024-A1` 이 "cli.py 는 T024
  범위 밖" 으로 면제했는데 `cli.py` 는 `T026` 범위 안이었다 (round 16 `C16-2`). 범위 밖은
  "이 task 가 안 고친다" 이지 "아무도 안 고쳐도 된다" 가 아니다

## Out Of Scope

- Telegram webhook와 callback adapter
- Hermes natural-language Skill
- production Slack credential 발급 또는 secret rotation 실행
- Slack activation rollout
- Provider message를 authoritative Proposal state로 사용하는 기능

## Review Team

- Slack protocol, raw-body signature and normalized contract reviewer
- credential secrecy, durable ack and replay evidence reviewer
- timeout, retry, Outbox, DLQ and operational failure reviewer
