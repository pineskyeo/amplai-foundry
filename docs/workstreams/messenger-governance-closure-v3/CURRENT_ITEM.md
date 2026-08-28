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
  T001~T045 `done`, T020 `superseded`. **round 21 이 FAIL 6건을 냈다. wave 15 대기**
- Frozen source/test evidence: round 17 aggregate
  `8c7d2220b51fd53f191ea2293976549dc33cec1b7edf9ffc4eeea59b62a23939` (52 파일).
  round 16 target 44개 중 14개가 바뀌었고 wave 10 산출물 8개가 늘었다.
  세 reviewer 가 착수·종료에 확인했고 regression 은 mutation 9회 전후로도 확인했다. 무손상
- **Tests: `1496 passed, 4 deselected` (2026-08-28, `ALR-006` review 반영 후).** 기준선이 1409 에서
  두 번 움직였다. 두 방법으로 셌다 — collect 1494 / 실행 1494.

  ```text
  1409  round 21 기준선
  +20   tests/test_kit_distribute.py       (T005 배포 래퍼)
  = 1429
  +64   tests/ai/                          (T006 배포로 kit 이 설치됐다)
  +1    test_kit_distribute.py 에 1건 추가 (install record 필드명 회귀)
  = 1494
  +2    tests/ai/ 에 빈 디렉토리 회귀 test (review C-3 후속)
  = 1496
  ```

  **`round 22 는 1496 을 기준으로 센다.`** `tests/ai/` 는 kit 이 설치한 것이라 이 저장소가
  직접 쓰지 않는다 — 정본은 `tools/amplai-loop-kit/payload/tests/ai/` 이고 거기서 고친다
- (이전) Tests: full `1409 passed, 4 deselected` (wave 8 착수 시 1301 → +108, wave 11 이 +6,
  12 가 +4, 13 이 +9, 14 가 +8). 두 방법으로 셌다 — `--collect-only` 가 1409
  selected(1413 collected), round 20 기준 1401 대비 +8 이고 추가한 test 수와 같다
- Verification: Ruff check/format, mypy, `verify` 7/7, manifest validator 45/45,
  `git diff --check` 모두 PASS
- Review: **round 10~21 전부 FAIL.** 15·16 은 각 9건(P0 1), 17 은 8건(P1 3),
  18 은 5건, 19 는 9건, 20 은 6건, **21 은 6건 — P0 0 / P1 1 / Blocking-P2 5.**
  round 20 에서 처음으로 앞 라운드 blocker 가 전부 닫혔지만 **21 에서 부분 닫힘이 셋으로
  돌아왔다**
- **round 16 의 9건 중 여덟이 닫혔다.** contract reviewer 가 각각 실행으로 확인했다.
  `FR-2` 는 절반만(`F17-2`), `C16-3` 은 안 닫혔다(`F17-4`)
- **2026-08-25 round 17 완료.** 기록은 `specs/003-slack-proposal-card/evidence/3lens-review-round-17.md`
- **2026-08-26 wave 11 완료.** `T035`(구현)·`T036`(test)·`T037`(문서) 셋을 `/work` 로 탔다.
  V2 artifact 를 이 feature 에 처음 만들었다 — `work-contract.json`(`MGC-012-P5-W11`),
  `knowledge-readiness.json`(READY), `context-pack.json`, `environment.json`,
  `evidence-trace.jsonl`
- **2026-08-26 round 18 완료. FAIL — 5건 (P0 0 / P1 1 / Blocking-P2 4).** round 17 의 8건
  중 **7이 닫혔다.** 기록은 `specs/003-slack-proposal-card/evidence/3lens-review-round-18.md`
- **2026-08-26 wave 12 완료.** `T038`(구현·test)·`T039`(census)·`T040`(절차) 셋.
  `D-048` 이 `unreadable()` 의 범위를 정정했다
- **2026-08-26 round 19 완료. FAIL — 9건 (P0 0 / P1 1 / Blocking-P2 8).** round 18 의 5건 중
  둘이 닫히고 셋이 부분이었다. **수가 늘었다** (8 → 5 → 9). 다만 여덟이 문서·표의 정확성이고
  실질 코드 결함은 `F19-1` 하나다. 기록은
  `specs/003-slack-proposal-card/evidence/3lens-review-round-19.md`
- **2026-08-26 wave 13 완료.** `T041`(구현·test)·`T042`(승인문)·`T043`(표 정정) 셋.
  `D-049` 가 `LIMIT` 을 출력 상한으로 옮겼다
- **2026-08-26 round 20 완료. FAIL — 6건 (P0 0 / P1 1 / Blocking-P2 5).** **round 19 의
  9건이 전부 닫혔다** — round 17 이후 처음으로 부분 닫힘이 하나도 없다. 기록은
  `specs/003-slack-proposal-card/evidence/3lens-review-round-20.md`
- **2026-08-26 wave 14 완료.** `T044`(truncation·test)·`T045`(문서) 둘. `D-050` 이
  잘린 목록을 잘렸다고 말하게 했다
- **2026-08-27 round 21 완료. FAIL — 6건 (P0 0 / P1 1 / Blocking-P2 5).** round 20 의
  6건 중 셋이 닫히고 **셋이 부분**이다. round 20 은 부분이 0 이었다. 기록은
  `specs/003-slack-proposal-card/evidence/3lens-review-round-21.md`
- Selected next item: **wave 15 — round 21 의 blocker 6건**
- Sequence: `wave 15 → 재freeze → round 22 review → (blocker 0이면) gate → T010 → T013 → T012`

## Wave 11 결과 (2026-08-26)

round 17 의 blocker 8건을 셋으로 묶어 닫았다. 전문은
[`3lens-review-round-17.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-17.md).

### `T035` — `F17-1`·`F17-2`·`F17-7` 을 한 자리에서 닫았다

뿌리는 `_claim_one` 이 **회수(sweep)와 투기(claim)를 한 transaction 에 묶은 것**이다.
`_view` 실패가 투기를 되돌리며 회수까지 되돌려, 손상 row 가 `leased`+만료로 durable 하게
남고 guard 가 0행을 냈다.

`_sweep_recoverable()` 을 자기 transaction 으로 분리해 `F17-1` 과 `F17-7` 을 함께 닫았다.
**guard 를 넓히지 않았다** — 넓히면 round 16 `FR-1` 이 막은 "살아 있는 lease 를 지운다" 가
되살아난다. `F17-2` 는 `D-047` 로 `unreadable()` 의 조회 범위만 넓혀 닫았고 `stranded()` 의
계약은 그대로다.

guard 가 0행을 내는 durable state 를 전수로 셌다 — `state` 여섯 중 넷 + generation 불일치
= **다섯 가지**. round 17 이 "도달 경로 못 찾음" 으로 남긴 세 칸도 durable row 를 만들어
rowcount 를 관찰했다. 도달 경로 자체는 여전히 특정하지 못했고 그대로 적었다.

### `T036` — mutation 다섯이 전부 KILLED

round 17 에서 SURVIVED 했던 셋이 각각 **다른 test** 로 잡힌다. `claim_generation` guard 와
`state` guard 를 가르는 시나리오를 따로 만들었다 — 기존 race test 는 둘을 동시에 바꿔서
어느 하나를 지워도 통과했다.

### `T037` — 세었더니 범위가 두 번 다 컸다

| 항목 | round 17 | wave 11 실측 |
|---|---|---|
| "아홉" 을 주장하는 위치 | 넷 | **여덟** |
| evidence 가 빠진 manifest | 9 중 5 (자기 범위) | **20** (feature 전체, 50 선언) |
| `T033` 의 빠진 AC 절 | AC-02 | **AC-02·AC-04·AC-05** |

사용자가 스물을 전부 닫기로 정했다. `T020`(superseded, `required_evidence_present: false`)
일곱만 남았고 그것은 정합적이다.

**"아홉" 은 두 값을 섞고 있었다.** `connect()` 를 감싸는 `try` 는 **열**(당시 아홉),
`sqlite3.Error` 를 잡는 `except` handler 는 **열셋**이다. 정정하면서 여덟 곳을 전부 13 으로
바꿨다가 되잡았다 — 그 문맥은 전부 `connect()` 정규화 이야기라 **열**이 맞다.

### 범위 밖 — `OutboxDispatcher` 가 같은 형태다

`events.py:3025` 의 `claim_next` 가 ingress 와 같은 구조다. 실측했고 **head-of-line 차단이
재현된다.** 다만 `governance_outbox_payload_immutable` trigger 가 payload 11 column 을
막아 도달 경로가 더 좁고, Outbox 에는 `D-045` 의 치우기 경로가 없다. 사용자가 범위 밖에
두기로 정했다. **다음 wave 가 받는다.**

### 다음

```text
재freeze → round 18 three-lens → (blocker 0이면) gate
```

## Wave 12 결과 (2026-08-26)

round 18 의 blocker 5건을 닫았다. 전문은
[`3lens-review-round-18.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-18.md).

### round 18 이 무엇을 확인했나

**round 17 의 8건 중 7이 닫혔다.** `F17-2` 만 부분이었다. 그리고 **세 라운드 연속
이어지던 "고침은 들어갔고 test 는 부분적" 패턴이 닫혔다** — regression lens 가 wave 11 의
mutation 다섯을 독립 재현했고 다섯 다 KILLED, 죽은 test 이름과 건수까지 일치했다.

### `T038` — 목록 자체가 덜 찼던 것을 고쳤다

round 18 의 셋(`N18-1`·`N18-2`·`F18-R1`)이 wave 11 이 넣은 **두 구조**에서 나왔다.
`T035` evidence 는 "새 구조가 요구한 것 다섯" 을 적었는데 `F18-R1` 은 **그 목록의 첫째
항목**이었다 — 처리를 적고 test 를 안 만들었다.

이번에는 표를 먼저 만들고 고쳤다. 항목마다 **처리**와 **test** 두 칸이다.

| | wave 11 이 센 수 | wave 12 실측 |
|---|---|---|
| 구조 1 (`unreadable()` 자기 조회) | **0** — 구조로 세지 않았다 | 7 |
| 구조 2 (`_sweep_recoverable()` 분리) | 5 | 9 |
| 합계 | 5 | **16** |

`N18-1`(P1)은 `D-048` 로 닫았다 — `unreadable()` 의 SELECT 가 종결 상태를 제외해 `limit` 이
**후보 상한**으로 돌아온다. 실측: `completed` 150개 뒤의 손상 row 가 고침 전 `limit=100` 에
안 나왔고 고침 후 나온다.

### `T039` — "전수" 라 쓰고 범위를 안 밝힌 것

wave 11 이 `src/`·`tests/` 만 훑고 "전수" 라 적었다. 이번에는 **repository 전역**을
`.git`·`.venv`·`__pycache__` 만 빼고 세었다 — 한글 79줄 26파일, 영문 9줄.

**완료된 wave 의 manifest 와 evidence 본문은 고치지 않았다.** 그것은 그 wave 가 무엇을
계약했고 무엇을 보았는지의 기록이다. 대신 **여섯 파일에 정정 각주**를 달았다.

### `T040` — freeze 와 mutation 절차

`N18-4` 는 freeze 가 **자기 자신이 만드는 변경**을 담은 것이다. `A18-R1` 은 더 무겁다 —
**`.pyc` 캐시가 mutation 결과를 위조한다.** round 17·18 이 mutation 을 blocker 판정의
근거로 삼았다. Stop rule 다섯을 새로 남겼다.

### 다음

```text
재freeze → round 19 three-lens → (blocker 0이면) gate
```

## Wave 13 결과 (2026-08-26)

round 19 의 blocker 9건을 닫았다. 전문은
[`3lens-review-round-19.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-19.md).

### `T041` — "읽을 수 없다" 는 SQL 로 판정할 수 없다

`F19-1`(P1)이 유일한 코드 결함이었다. `D-048` 이 `completed` 하나만 뺐는데, **손상 여부는
`_view` 를 돌려야 알기 때문에** `SQL LIMIT` 은 그 판정 **전에** 자른다. 어떤 state 집합을
골라도 창 안의 읽을 수 있는 row 가 손상 row 를 밀어낸다.

벽 6종을 전수로 실측했다 — 고침 전에는 `completed` 만 막혀 있었고 **다섯이 뚫려 있었다.**
round 19 가 "재현 불가" 로 남긴 `leased` 벽도 만들어 확인했다.

`D-049` 가 `LIMIT` 을 python 출력 상한으로 옮겼다. **비용이 `limit` 이 아니라 종결 아닌 row
수에 비례한다** — `D-048` 이 거절 사유로 들었던 그 비용을 받아들인 것이다.

### `R19-1`·`R19-2` — 채워진 칸이 빈 칸보다 나쁘다

wave 12 가 "덜 세는 것" 을 고치려고 만든 AC-06 표에서 **두 칸이 거짓이었다.** 적어 둔 test
가 `unreadable()` 을 한 번도 안 부르거나 정렬을 안 본다.

**빈 칸은 `non_goals` 로 명시돼 다음 라운드가 보지만, 채워진 칸은 "이미 막았다" 로 분류돼
아무도 안 본다.** 그래서 wave 13 은 **mutation 으로 확인하기 전에는 칸을 채우지 않는다** 를
`T041` manifest 의 규칙으로 넣었다.

### 세 표가 각각 덜 셌다

| 표 | 주장 | 실측 |
|---|---|---|
| `T038` AC-06 요구 표 | test 있는 칸 12 | **10** |
| `T039` census 분류표 | 26 파일 전부 | **24** |
| `T040` freeze 표 | 결과가 "아래" 에 | **절이 비어 있었다** |

### 다음

```text
재freeze → round 20 three-lens → (blocker 0이면) gate
```

## Wave 14 결과 (2026-08-26)

round 20 의 blocker 6건을 닫았다. 전문은
[`3lens-review-round-20.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-20.md).

### round 20 이 확인한 것

**round 19 의 9건이 전부 닫혔다.** round 17 이후 처음으로 **부분 닫힘이 하나도 없다** —
18·19 에서는 매번 셋이 부분이었다. regression lens 가 `T041` evidence 의 표 다섯을 전수
검증했고 **거짓 칸이 없다** (wave 12 가 저지른 형태가 반복되지 않았다).

### `F20-1` 은 고칠 수 있는 종류가 아니다

| 방식 | 무엇이 밀어내나 |
|---|---|
| `SQL LIMIT` (`D-048` 까지) | 창 안의 **읽을 수 있는** row (round 19 `F19-1`) |
| python 출력 상한 (`D-049`) | **읽을 수 없는** 종결 row (round 20 `F20-1`) |

**둘 다 `limit` 의 본질이다.** `D-049` 는 벽의 state 를 여섯으로 전수했지만 **가독성 축을
세지 않았고**, 그 축을 `_dead_letter_unreadable` 이 스스로 만든다.

`D-050` 이 **없애는 대신 보이게** 만든다. CLI 가 `limit + 1` 을 요청해 잘림을 판정하고 한
줄로 알린다. 두 lens 가 두 라운드 연속 그 줄을 요청했다.

### 규칙이 자기 실수를 잡았다

`T044` manifest 에 "표의 칸을 mutation 으로 확인하기 전에는 채우지 않는다" 를 넣었다.
`R20-1` test 를 값이 있는 경우만 쳐서 `COALESCE` mutation 이 **SURVIVED** 했고, 제출 전에
발견해 `parametrize` 로 `None` 경우를 넓혔다. round 19 `R19-3` 이 wave 12 표에서 찾은
형태다.

**새 구조가 요구하는 것도 착수 전에 표로 만들었다.** 첫 항목(`limit + 1` 이 `--limit 0` 을
`1` 로 바꿔 service guard 를 우회한다)이 착수 전에 보였고 manifest 의 `risk.concerns` 에
적고 시작했다.

### 다음

```text
재freeze → round 21 three-lens → (blocker 0이면) gate
```

## Round 21 결과 (2026-08-27)

**FAIL — 6건 (P0 0 / P1 1 / Blocking-P2 5).** 전문은
[`3lens-review-round-21.md`](../../../specs/003-slack-proposal-card/evidence/3lens-review-round-21.md).
lens 별 기록은 같은 디렉토리의 `round-21-lens-contract.md`·`round-21-lens-failure.md`·
`round-21-lens-regression.md` 다.

세 lens 를 순차로 돌렸고 여섯 hash 시점 전부 **어긋남 0** 이다. 셋 다 aggregate 를 무손상
근거로 쓰지 않았고 mutation 은 전부 `PYTHONDONTWRITEBYTECODE=1` 로 돌렸다.

### round 20 의 6건 — 셋 닫힘, 셋 부분

| round 20 | 판정 | 이유 |
|---|---|---|
| `F20-1`(P1) | **부분** | truncation 줄은 실재하고 mutation 으로 고정됐다. 판정식이 손상 row 앞에서 거짓 음성을 낸다 |
| `F20-2` | **부분** | `unreadable()` 쪽 판정은 참값이고 `stranded()` 쪽만 무너진다. **두 목록 중 하나만 옳다** |
| `F20-3` | 닫힘 | M19 KILLED 1건 |
| `C20-1` | **부분** | 지목된 자리만 각주. 형제 둘이 남았다 |
| `C20-2` | 닫힘 | `Source` 가 "벽 다섯" 으로 정정 + `Correction` 절 |
| `R20-1` | 닫힘 | M20 KILLED (`[None]` param) |

**round 20 은 부분 닫힘이 0 이었다. 이번은 셋이다.**

### 새 blocker 6건

| id | 등급 | 한 줄 |
|---|---|---|
| `F21-1` | **P1** | `stranded()` 는 SQL 이 가져온 `limit + 1` 개에서 손상 row 를 걸러낸 뒤 반환한다. CLI 가 걸러낸 뒤의 개수로 잘림을 판정해 **손상 row 가 하나만 섞여도 잘림 표시가 꺼진다.** 후보 201 / 손상 1 / `--limit 100` → 숨은 후보 100개, 표시 없음 |
| `F21-2` | B-P2 | `--limit 9223372036854775807` → `limit + 1 = 2**63` 이 sqlite3 binding 에서 `OverflowError`. except 절이 안 잡아 **raw traceback**. `…806` 은 정상이라 wave 14 가 만든 것이다. round 16 `FR-3`·`R16-1` 이 닫은 계약이 되돌아왔다 |
| `R21-1` | B-P2 | `len(...) > limit` 둘을 `>=` 로 바꿔도 **1409개가 전부 통과**. AC-07 표 #3 의 test 칸이 경계에 안 선다 |
| `R21-2` | B-P2 | `unreadable = unreadable[:limit]` 을 지워도 **1409개가 전부 통과**. 표 #6 의 처리는 slicing 둘인데 test 는 `commands` 쪽만 센다 |
| `C21-3` | B-P2 | **`D-050` 이 `approvals.jsonl` 에 없다.** `D-047`~`D-049` 는 셋 다 `public_contract` 행이 있는데, 실제로 CLI 출력을 바꾸는 유일한 Decision 만 `gate: none` 으로 기록됐다 |
| `C21-4` | B-P2 | `C20-1` 을 고친 wave 가 같은 지적을 재생산했다. `D-049` 가 틀렸다고 지목한 주장이 `T038.yaml:38` 과 이 파일 `CURRENT_ITEM.md` 의 `T038` 절에 정정 없이 남았다 |

**두 결함을 두 lens 가 독립으로 찾았다** — `F21-2`(contract 는 `C21-2`),
`R21-1`(contract 는 `C21-1`).

### 뿌리 — "전수 표" 가 두 방향으로 실패했다

wave 14 는 round 16·18 의 뿌리를 막으려고 **새 구조가 요구하는 것을 착수 전에 표로 만드는**
도구를 썼다. 그 도구가 한 번 실제로 작동했다 — `--limit 0` 우회를 착수 전에 잡았다.

| 방향 | 무엇이 빠졌나 | blocker |
|---|---|---|
| **행이 모자라다** | "반환 개수 ≠ SQL fetch 개수인 조회에서 반환 개수로 잘림을 판정할 수 있는가", "`limit + 1` 의 정수 overflow" | `F21-1`(P1) · `F21-2` |
| **채운 칸이 거짓이다** | 여덟 칸 중 #3·#6 이 mutation 없이 채워졌다. 표는 여덟 칸인데 evidence 의 mutation 표는 **넷**(M18~M21)뿐이다 | `R21-1` · `R21-2` |

`T044` manifest 는 "표의 칸을 mutation 으로 확인하기 전에는 채우지 않는다" 를 자기 규칙으로
넣었고 그 규칙이 한 칸에서 작동해 제출 전에 SURVIVED 를 잡았다. **#3·#6 에서는 mutation 을
아예 안 돌렸다.** 칸 수와 mutation 수가 안 맞는 것이 신호였고 아무도 그 대조를 안 했다.

`F21-1` 은 축이 하나 더 있다는 것이다. round 19·20 이 `limit` 의 벽을 두 축(state ·
가독성)으로 실측했다. **세 번째 축은 반환 경로에 filter 가 있는가** 이고, 그 filter 는
`D-047` 이 round 15 `F-2` 때문에 넣은 것이다. `D-050` 이 앞의 두 결정을 안 셌다.

### 처음 증명된 것

**test 삭제 0 / skip·xfail 신규 0.** package 5 가 commit 돼 기준 commit `e13e01c` 가
생겼다. **세 라운드 연속 "기준 commit 이 없다" 로 Not Checked 이던 항목이다.** 다만
`87a0c76` 한 commit 에 wave 11~14 가 함께 들어 있어 wave 14 단독 분리는 못 한다.

## Wave 15 착수 지시

blocker 6건을 닫는다. `/work` 로 타고 `/taskify` 를 건너뛰지 않는다.

```text
전문   : specs/003-slack-proposal-card/evidence/3lens-review-round-21.md
lens별 : specs/003-slack-proposal-card/evidence/round-21-lens-{contract,failure,regression}.md
```

### 착수 전에 정할 것

- **`F21-1` 은 계약 변경이다.** 잘림 판정을 SQL fetch 개수 기준으로 옮기면 `stranded()` 의
  반환값 또는 signature 가 바뀐다. **Decision 을 먼저 받는다** (Stop rule). `D-047` 의
  skip 과 round 15 `F-2` 를 함께 세고 시작한다
- **`F21-2` 는 `A21-9` 와 함께 본다.** 같은 파일의 다른 command 는 `typer.Option(min=1)`
  로 선언 단계에서 막는다. 위쪽 끝도 선언 단계에서 막을 수 있다
- **`C21-3` 은 승인 ledger 다.** `D-050` 을 `approvals.jsonl` 에 소급 기록할지, 아니면
  기록 누락 자체를 경위와 함께 남길지 정한다. **사용자 결정 사항이다**

### 반드시 지킬 것 — 이번 라운드가 만든 것

- **표를 만들면 칸 수와 mutation 수를 대조한다.** wave 14 는 여덟 칸에 mutation 넷을
  돌렸고 안 돌린 두 칸이 둘 다 거짓이었다
- **`limit + 1` 처럼 새 구조를 넣으면 양쪽 끝을 다 센다.** wave 14 는 아래쪽 끝(`0`·`-1`)만
  셌고 위쪽 끝이 `F21-2` 가 됐다
- **정정 각주를 달 때 형제 위치를 끝까지 센다.** `C20-1` → `C21-4` 가 같은 형태의 반복이다

### 미해결 Advisory — 두 라운드째

`A21-4`(`A20-R1`, `in` 대신 `==`), `A21-5`(`A20-C3`, assert message 문구),
`A21-6`(`A20-C2`, `D-048` 의 `Rejected` pointer). 셋 다 wave 14 의 `allowed_paths` 안이었다.

## Package 5 뒤 — 로드맵 위치

`BACKLOG.md` 의 `depends_on` 과 `STATUS.md` 의 Gate 절로 확인한 사실이다.

```text
MGC-001~011   PASS        (STATUS.md Gate 절)
MGC-012       ACTIVE      ← 지금 여기. Package 5 gate 가 없다
MGC-013       Telegram adapter        deps: MGC-005, MGC-008
MGC-014       Hermes Skill            deps: MGC-006, MGC-008, MGC-009  ← 셋 다 PASS 범위
MGC-015       Activation Control      deps: MGC-010, MGC-012, MGC-013
MGC-016       Full Verification       deps: 011, 012, 013, 014, 015
```

**`MGC-014`(Hermes)는 `MGC-012` 에 의존하지 않는다.** 의존 셋이 이미 PASS 이고 코드도
실재한다 — `AuthorityService`·`ActorBindingService`(`governance/authority.py`),
`OutboxDispatcher`(`governance/events.py`), `ApplyGrant`·`ApplyJob`
(`governance/apply_jobs.py`). 순서상 병렬 착수가 가능하다.

다만 **준비물이 없다.** `specs/` 에 Hermes 디렉토리가 없고, `BACKLOG` 서술은 한 줄이며,
`CON-0010`(Hermes 정의)이 `status: candidate` 다 — 승인된 결정이 아니다. `QUE-0011`(개인
메모리를 AMPLAI intake 로 승격하는 trigger)도 열려 있다. **`/design` 이 먼저다.**

실제로 Hermes 로 대화하려면 `MGC-015`·`MGC-016` 까지 가야 하고 그 둘은 `MGC-012`·`MGC-013`
에 의존한다.

### 문서 어긋남 하나 — 정리 대상

`BACKLOG.md` 의 `status` 필드가 `STATUS.md` 의 Gate 절과 어긋난다. Gate 는 001~011 을
PASS 로 판정하는데 BACKLOG 는 `MGC-011` 만 `done` 이고 001~010 이 `planned` 다. **Gate 절이
최신이다.** 로드맵을 읽을 때 혼란을 주므로 정리할 값이 있다 — 이번 세션 범위 밖이라 손대지
않았다.

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
- Stop rule: **정규화는 위치를 먼저 정한다.** `connect()` 에 넣으면 `legacy_*.py` 의
  `try` **열**이 깨지고(round 15 `R-1`), 안 넣으면 CLI 가 traceback 을 낸다(round 16 `FR-3`,
  `R16-1`). **최종 소비자가 경계다** — CLI 에서 `sqlite3.Error` 를 잡아 둘 다 닫았다.
  (개수는 wave 11 실측이다. 오래 "아홉" 으로 적혀 있었다 — round 17 `A17-5`)
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
- Stop rule: **계약 변경은 Decision 을 먼저 받는다.** wave 9 의 `D-045`, wave 11 의
  `D-047` 이 그 예다
- Stop rule: **수치를 고칠 때 무엇을 세는 값인지 먼저 확인한다.** wave 11 이 "아홉" 을 전부
  13 으로 바꿨다가 되잡았다 — 그 자리는 `connect()` 를 감싸는 `try` 를 세고 있었고 그 값은
  **열**이다. 여섯 라운드 연속 틀린 원인이 "세지 않고 적는 것" 이었다면 이것은 **"무엇을
  세는지 확인하지 않고 값만 바꾸는 것"** 이다. 같은 뿌리다
- Stop rule: **evidence 를 찾을 때 manifest 가 선언한 경로를 읽는다.** wave 11 의 첫 대조가
  파일 이름을 `<task-id>.md` 로 추측해 `T008` 을 "파일 없음" 으로 쳤다. `evidence_paths` 가
  이미 답을 갖고 있었다
- Stop rule: **`connect()` 호출 순서에 결합된 test 가 있다.** filesystem-guard test 둘이
  `validate()` 호출을 세어 창을 잡는다. store 접근을 하나라도 더하거나 빼면 창이 밀린다 —
  wave 11 이 sweep 을 분리하며 그것을 만났고 실측으로 `+2` 를 확인해 옮겼다
- Stop rule: **AC 를 구현 뒤에 맞추지 않는다.** 못 지킬 AC 는 **착수 전에** 고치고 원문을
  남긴다. `T027` AC-06 이 그 사례이고 round 16 이 적법하다고 판정했다
- Stop rule: 재freeze → 재review 순서를 지킨다
- Stop rule: **freeze 는 마지막에 하고, freeze 자신이 만드는 변경을 target 에 담지 않는다.**
  round 18 `N18-4` 가 그것이다 — manifest 가 `evidence-trace.jsonl` 의 4줄 시점 hash 를
  담았는데 freeze 뒤에 세 줄을 더 썼다. trace 처럼 freeze 자체를 기록해야 하는 파일은
  **target 에서 빼고 그 이유를 적는다.** target 파일이 자기 자신을 목록에 안 넣는 것과 같은
  이유다
- Stop rule: **freeze target 안의 파일에 그 target 의 aggregate 값을 적지 않는다.**
  순환이 생긴다 — 적는 행위가 aggregate 를 바꾸고, 고치면 또 바뀐다. wave 14 가 착수 지시에
  값을 적었다가 두 번 다시 얼렸다. **target 파일을 가리키기만 한다.** 값은 그 파일 끝에
  있고 머리말의 재확인 script 가 다시 계산한다
- Stop rule: **aggregate 일치는 무손상 증거가 아니다.** aggregate 는 manifest **행 문자열**만
  해싱하므로 행이 가리키는 파일이 그 뒤에 바뀐 것을 못 잡는다. 실제 보장은 **행별 hash 를
  다시 계산하는 재확인 script** 다. round 18 에서 세 reviewer 가 전부 aggregate 를 무손상
  근거로 인용했다
- Stop rule: **mutation 을 돌릴 때 `.pyc` 캐시를 막는다.** round 18 `A18-R1` 이 실측했다 —
  test 파일을 같은 크기로 1초 안에 다시 쓰면 CPython 의 mtime+size 기반 bytecode 무효화가
  변경을 감지하지 못해 **직전 컴파일 결과가 재실행된다.** pristine 코드에서 FAILED 가 나온
  사례가 있다. round 17·18 이 mutation 을 blocker 판정의 근거로 삼았으므로 무겁다.
  `PYTHONDONTWRITEBYTECODE=1` 을 쓰거나 `__pycache__` 를 지운다
- Stop rule: **고칠 수 없는 것을 고치려 하지 않는다. 대신 숨기지 않는다.** round 19·20 이
  `limit` 의 본질을 두 번 실측했다 — `SQL LIMIT` 은 읽힐 벽이, python 상한은 안 읽힐 벽이
  `limit+1` 번째를 민다. `D-050` 이 truncation 표시로 **보이게** 만들었다. 없앤 것이
  아니라는 구분을 evidence 에 적는다
- Stop rule: **manifest 를 복사해 만들 때 `status` 와 `completion` 을 먼저 되돌린다.**
  wave 14 가 `T042`(done)를 복사해 `T045` 를 만들면서 착수 시점부터
  `all_acceptance_passed: true` 인 manifest 를 남겼다. `index.yaml` 에는 `ready` 로 등록해
  둘이 어긋났다. round 17 `F17-5`·round 19 `C19-2` 와 같은 형태이고 원인만 다르다
- Stop rule: **부분적으로 정확한 문서가 전부 정확해 보인다.** round 20 `C20-1`·`C20-2` 가
  그것이다 — 같은 파일 다른 자리에는 각주를 달고 한 자리만 빠뜨렸고, 같은 Decision 의 다른
  절은 정확한데 `Source` 만 근거를 과대 계상했다
- Stop rule: **표의 칸을 mutation 으로 확인하기 전에는 채우지 않는다.** round 19
  `R19-1`·`R19-2` 가 그것이다 — wave 12 의 AC-06 표가 두 칸을 "test 있음" 으로 적었는데 그
  test 들은 대상 함수를 부르지도 않았다. **빈 칸보다 나쁘다.** 빈 칸은 `non_goals` 로
  명시돼 다음 라운드가 보지만 채워진 칸은 "이미 막았다" 로 분류돼 아무도 안 본다
- Stop rule: **"전수" 라 쓸 때 그 표가 실제로 전부를 담는지 다시 센다.** round 19 `C19-4` —
  census 가 26 파일을 얻고 표는 24개만 이름을 댔다. **`N18-3` 을 고치는 task 가 같은 것을
  다시 냈다**
- Stop rule: **문서에 제목만 쓰고 내용을 비워 두지 않는다.** round 19 `C19-2` — `T040` 이
  "아래가 결과다" 라 쓰고 아래를 비운 채 freeze 됐다. `required: true` 로 선언한 출력이었다
- Stop rule: **새 구조가 요구하는 것을 세는 목록 자체가 덜 찰 수 있다.** round 16 blocker
  넷과 round 18 blocker 셋이 모두 그 자리에서 나왔다. wave 11 의 `T035` 는 "요구 다섯" 을
  적었는데 round 18 `F18-R1` 이 **그 목록의 첫째 항목**이었다 — 처리를 적고 test 를 안
  만들었다. **표를 만들 때 항목마다 '처리' 와 '그 처리를 지키는 test' 두 칸을 둔다.** test
  칸이 빈 항목이 다음 라운드의 blocker 다
- Stop rule: **되돌린 것도 test 로 묶는다.** `T029` 가 회귀를 고쳤는데 세 지점 중 둘이
  test 없이 남아 round 16 `R16-2` 가 됐다
- Stop rule: **면제를 적을 때 누구에게 적용되는지 확인한다.** `T024-A1` 이 "cli.py 는 T024
  범위 밖" 으로 면제했는데 `cli.py` 는 `T026` 범위 안이었다 (round 16 `C16-2`). 범위 밖은
  "이 task 가 안 고친다" 이지 "아무도 안 고쳐도 된다" 가 아니다
- Stop rule: **표를 만들었으면 칸 수와 mutation 수를 대조한다.** wave 14 의 AC-07 표는
  여덟 칸인데 evidence 의 mutation 표는 넷이었다. **안 돌린 두 칸이 둘 다 거짓이었다**
  (round 21 `R21-1`·`R21-2`). "mutation 으로 확인한 것만 채운다" 는 규칙을 넣어 두는 것으로는
  부족하다 — 그 규칙이 지켜졌는지 세는 자리가 따로 있어야 한다
- Stop rule: **새 구조를 넣으면 양쪽 끝을 다 센다.** wave 14 는 `limit + 1` 의 아래쪽
  끝(`0`·`-1`)만 세고 막았고, 위쪽 끝이 round 21 `F21-2` 가 됐다 — int64 최대값이
  `limit + 1` 로 overflow 해 raw traceback 을 냈다. **round 16 `FR-3` 이 닫은 계약이
  되돌아왔다**
- Stop rule: **반환 개수로 무언가를 판정하기 전에 그 경로에 filter 가 있는지 본다.**
  round 21 `F21-1`(P1)이 그것이다. `stranded()` 는 SQL 이 가져온 `limit + 1` 개에서 손상
  row 를 걸러낸 뒤 반환하는데 `D-050` 이 그 둘을 같다고 가정했다. 그 filter 는 `D-047` 이
  round 15 `F-2` 때문에 넣은 것이다 — **세 결정이 겹치는 자리에서 앞의 둘을 안 셌다**

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
