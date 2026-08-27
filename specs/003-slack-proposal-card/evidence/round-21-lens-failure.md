# Round 21 — Failure Lens (MGC-012-P5 Wave 14)

**판정: FAIL.** blocker 2건 — P0 0 / **P1 1** / Blocking-P2 1. Advisory 5.

Target: `evidence/review-target-round-21.txt`, **97 행**. 착수·종료 두 시점에 **행별 hash
재계산**으로 확인했고 둘 다 `rows 97 | 어긋남 0` 이다. **aggregate 를 무손상 근거로 쓰지
않았다** — aggregate 는 manifest 의 행 문자열만 해싱하므로 행이 가리키는 파일의 사후 변경을
못 잡는다.

mutation 은 전부 `PYTHONDONTWRITEBYTECODE=1` 로 돌렸고 착수 전에
`find . -name __pycache__ -exec rm -rf {} +` 를 했다. python 은 `.venv/bin/python` 이다.

## Verdict In One Line

`D-050` 은 **`stranded()` 가 무엇을 반환하는지 잘못 가정했다.** `stranded()` 는 SQL 이 가져온
`limit + 1` 개에서 **읽을 수 없는 row 를 걸러낸 뒤** 반환한다. CLI 는 걸러낸 뒤의 개수로
잘림을 판정한다. 그래서 **손상 row 가 창 안에 하나만 있어도 잘림 표시가 사라진다.**
round 20 `F20-1` 이 실측한 그 자리 — 기본 `--limit 100`, 후보 201개, 출력 100줄 — 가
**여전히 조용하다.**

## Round 20 Failure-Family Blockers: 두 건 닫힘, 한 건 부분

| round 20 | 등급 | 이번 판정 | 확인 방법 |
|---|---|---|---|
| `F20-1` | P1 | **부분 닫힘** | truncation 줄 자체는 실재하고 mutation MC(`truncated = False`)·ME(`limit + 1` → `limit`)가 각각 `test_stranded_says_when_the_list_is_cut`·`test_stranded_reports_a_cut_in_either_list` **2건씩 죽인다.** 그러나 판정식이 **읽을 수 없는 row 가 섞이면 거짓 음성**을 낸다 → `F21-1` |
| `F20-2` | B-P2 | **부분 닫힘** | `unreadable()` 쪽 판정은 정확하다 — 그 목록은 python scan 이 직접 세므로 `len > limit` 이 참값이다. `stranded()` 쪽 판정이 `F21-1` 로 무너진다. **두 목록 중 하나만 옳다** |
| `F20-3` | B-P2 | **닫힘** | mutation MA — `unreadable()` 의 `WHERE state != 'completed'` 를 통째로 삭제 → `test_unreadable_skips_a_corrupt_row_that_already_completed` **1건 사망**. round 20 에서는 1401개가 전부 통과했다 |
| `R20-1` | B-P2 | **닫힘** (범위 밖이지만 같이 확인) | mutation MB — sweep 둘째 UPDATE 에 `COALESCE(last_error_code, 'INGRESS_MUTANT')` → `test_the_exhaustion_sweep_leaves_the_prior_error_code_alone[None]` **1건 사망**. `parametrize` 의 `None` 칸이 죽인다 |

**`F20-1`·`F20-2` 를 "닫혔다" 로 적으면 틀린다.** `D-050` 이 만든 신호가 **정확히 그
결함이 존재하는 상황에서 꺼진다.**

## New Blockers

| id | 등급 | 위치 | 결함 | 재현 수치 |
|---|---|---|---|---|
| `F21-1` | **P1** | `cli.py:765`, `ingress.py:703-711`, `D-050` Decision 절 | **잘림 판정이 거짓 음성을 낸다.** `truncated = len(commands) > limit` 은 `stranded()` 가 SQL 이 가져온 `limit + 1` 개를 **그대로** 준다고 가정한다. `_stranded_rows` 는 `LIMIT ?` 로 `limit + 1` 개를 읽은 뒤 `_view` 가 터진 row 를 **조용히 버린다** (그 자리 주석이 "건너뛴 것은 `unreadable()` 이 낸다"). 창 안에 손상 row 가 하나라도 있으면 반환이 `limit` 개 이하로 떨어지고 판정이 `False` 가 된다. **`D-050` 이 없애려던 바로 그 침묵이다** | `dead_letter` 벽 200 + `recovery_hold` 1 = **후보 201개**, 그중 **손상 1개**, 기본값 `--limit 100` → 출력 readable 100 + UNREADABLE 1, **잘림표시 False**, **숨은 후보 100개**. 손상 0개로 같은 조건을 돌리면 잘림표시 True. 최소 재현은 후보 4 / 손상 1 / `--limit 1` — 출력 2줄, 숨은 후보 2, 표시 없음 |
| `F21-2` | B-P2 | `cli.py:753-761`, `ingress.py:694` | **`limit + 1` 이 새 crash 를 만들었다.** `--limit 9223372036854775807`(int64 max) 이 `limit + 1 = 2**63` 로 sqlite3 binding 에 도착해 `OverflowError` 를 낸다. `try` 의 except 절은 `GovernanceStoreError`·`GovernanceMigrationError`·`IngressError`·`ValueError`·`sqlite3.Error` 뿐이라 **잡히지 않고 raw traceback 이 나간다.** `D-050` 이전에는 같은 입력이 정상 처리됐다 — `--limit 9223372036854775806` 은 지금도 정상이다. round 16 `FR-3`·`R16-1` 이 닫은 "CLI 가 raw traceback 을 낸다" 와 같은 형태다 | 실제 subprocess: `exit=1` 이지만 rich traceback 이 stderr 로 나가고 마지막 줄이 `OverflowError: Python int too large to convert to SQLite INTEGER`. 경계 실측 — `9223372036854775806` exit=0 정상, `9223372036854775807` traceback |

### `F21-1` 의 뿌리 — Decision 이 반환값을 잘못 가정했다

`D-050` 의 Decision 절 문장은 이것이다.

```text
`limit + 1` 을 요청해 `limit + 1` 개가 오면 잘린 것이고
```

`unreadable()` 에서는 맞다 — 그 함수는 손상 row 만 모으므로 반환 개수 = 발견 개수다.
`stranded()` 에서는 틀리다 — **SQL 이 가져온 개수와 반환 개수가 다르다.** 그 차이를 만드는
것이 `D-047` 이 넣은 skip 이고, 그 skip 이 존재하는 이유가 round 15 `F-2`(손상 row 하나가
목록 전체를 없앴다)다. **세 결정이 같은 자리에서 겹치는데 `D-050` 이 앞의 둘을 안 셌다.**

`T044.md:28-38` 의 "`limit + 1` 이 요구하는 것 전수" 표는 8행이다. **9번째가 이것이다** —
"반환 개수 ≠ SQL fetch 개수인 조회에서 반환 개수로 잘림을 판정할 수 있는가". 표가
`--limit 0` 우회는 착수 전에 잡았지만 같은 구조의 두 번째 요구는 못 셌다.

### 표시 슬롯 손실 — 같은 뿌리의 두 번째 증상

손상 row 는 SQL 창의 슬롯을 먹고 출력에는 readable 로 안 나온다. 그래서 **출력 readable
개수가 `limit` 보다 작으면서 동시에 숨은 후보가 있는** 상태가 만들어진다.

| 벽 | 손상 | `--limit` | SQL 후보 | 출력 readable | 출력 UNREADABLE | 잘림표시 | 숨은 후보 |
|---|---|---|---|---|---|---|---|
| `dead_letter` × 10 | 0 | 3 | 11 | 3 | 0 | **True** | 8 |
| `dead_letter` × 10 | 1 | 3 | 11 | 3 | 1 | **False** | 7 |
| `dead_letter` × 10 | 3 | 3 | 11 | 1 | 3 | **False** | 7 |
| `recovery_hold` × 10 | 1 | 3 | 11 | 3 | 1 | **False** | 7 |
| `dead_letter` × 200 | 1 | 100 | 201 | 100 | 1 | **False** | 100 |
| `dead_letter` × 3 | 0 | 4 | 4 | 4 | 0 | False | 0 (옳다) |

**operator hold(`recovery_hold`)에서도 같다.** 벽의 state 를 바꿔도 결과가 같다 — 두 state 가
같은 SQL 창을 공유하기 때문이다.

## Verified Clean

- **`F20-3` 닫힘.** MA KILLED, `test_unreadable_skips_a_corrupt_row_that_already_completed` 1건.
- **`R20-1` 닫힘.** MB KILLED, `test_the_exhaustion_sweep_leaves_the_prior_error_code_alone[None]` 1건.
- **`D-050` 의 세 부품이 각각 test 로 고정돼 있다.** MC(`truncated = False`) 2건 사망,
  MD(CLI `limit < 1` guard 삭제) 2건 사망 —
  `test_an_invalid_limit_fails_closed`·`test_stranded_still_rejects_a_non_positive_limit[0]`,
  ME(`limit + 1` → `limit`) 2건 사망. **다섯 mutation 전부 KILLED.**
- **`--limit 0`·`--limit -1` 이 fail-closed 다.** exit 1, raw traceback 없음. wave 14 의
  주장 5(착수 전 발견)가 사실이다 — CLI guard 가 service guard 앞에 있다.
- **정확히 `limit` 개일 때 잘렸다고 말하지 않는다.** 후보 4 / `--limit 4` → 4줄, 표시 없음.
  off-by-one 없다.
- **`unreadable()` 쪽 판정은 참값이다.** 그 목록은 python scan 이 직접 세고 `LIMIT` 을 SQL 에
  걸지 않으므로 `len > limit` 이 정확하다. `F21-1` 은 `stranded()` 쪽에만 있다.
- **`limit + 1` 이 두 목록을 섞지 않는다.** slicing 이 각각이고 `commands`·`unreadable` 이
  독립 변수다. 한 목록의 잘림이 다른 목록의 출력 개수를 안 바꾼다.
- **형제 위치를 끝까지 셌다.** src 전체에서 `stranded()`·`unreadable()` 을 `limit` 과 함께
  부르는 곳은 `cli.py:753-754` **둘뿐**이고, 잘림을 계산하는 자리는 `cli.py:765`
  **하나뿐**이다. `is_unreadable()` 호출은 `cli.py:814` 하나다. grep 전수와 건수 세기 **두
  방법이 6으로 일치**한다.
- **Outbox 쪽에는 `F20-1` 계열이 없다.** `events.py` 의 SQL `LIMIT` 은 전부 `LIMIT 1`
  이고(단일 row 조회), `limit` 이라는 이름의 parameter 나 operator 목록 조회가 **없다.**
  grep 으로 전수했다. `OutboxDispatcher` 의 head-of-line 은 사용자가 범위 밖에 뒀다.
- **operator hold 우회 없음.** `_claim_one` 의 WHERE 는 `state = 'pending'` 과
  `state = 'retry_wait'` 뿐이고, `_sweep_recoverable` 의 두 UPDATE 는 각각 `state = 'leased'`·
  `state = 'retry_wait'` 다. `recovery_hold` 를 건드리는 UPDATE 가 ingress 에 없다 (코드 확인).
- **base line PASS.** pristine 전 suite 통과(exit 0). test 수 **두 방법 모두 1409**
  (파일별 합계 1409, pytest 요약 `1409/1413 tests collected (4 deselected)`).
- **작업 뒤 tree 원상.** `git diff --stat` 빈 출력, `git status --porcelain` 에 target 파일
  없음(다른 lens 의 미추적 보고서 1개만 있다).

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A21-F1` | `cli.py:784` | **잘림 메시지가 어느 목록이 잘렸는지 안 말한다.** `T044.md:34` 의 표 5행은 "두 목록 각각 판정" 으로 적었는데 실제로는 `or` 로 합쳐 한 줄을 낸다. 회수 행동(`--limit` 올리기)이 같으므로 blocker 는 아니다. test 도 존재만 단언한다 |
| `A21-F2` | `T044.md:28-38` | **AC-07 표가 8행인데 최소 10행이어야 한다.** 빠진 것 — (9) `stranded()` 의 반환 개수 ≠ SQL fetch 개수(`F21-1`), (10) `limit + 1` 의 정수 overflow(`F21-2`). wave 14 가 "새 구조가 요구하는 것을 전수" 라고 적은 표다 |
| `A21-F3` | `cli.py:753-754` | `stranded()` 를 먼저 부르므로 `F21-2` 의 crash 가 `unreadable()` 도래 전에 난다. 순서를 바꾸면 증상이 바뀔 뿐 결함은 남는다. **(추정)** 없이 실측했다 — traceback 이 `ingress.py:694 in _stranded_rows` 를 가리킨다 |
| `A21-F4` | `A20-F1` 의 비용 | `limit + 1` 이 `unreadable()` 의 scan 을 한 row 더 끌 수 있다는 `D-050` Consequence 는 맞다. 다만 `stranded()` 쪽은 `LIMIT` 이 하나 늘 뿐이라 비용이 사실상 안 는다. **이번에 다시 재지 않았다** — round 20 의 5.5 µs/row 를 그대로 둔다 |
| `A21-F5` | `T045` manifest | `T042`(done) 복사본이라 착수 시점부터 `all_acceptance_passed: true` 였다. **이미 알려진 절차 위반**이고 새 발견이 아니다. 여기 적는 것은 이번 라운드에도 그 상태가 그대로임을 확인했다는 뜻이다 |

## Not Checked

- 실제 sqlite 장애(disk full, read-only, WAL 손상). **다섯 라운드 연속** 같은 항목이다.
- 여러 worker 를 실제 process 로 띄운 동시성.
- `F21-1` 이 존재하는 상태에서 `--limit` 을 올리는 회수 절차가 실무에서 몇 번 만에 끝나는가
  — 손상 row 수만큼 계속 어긋나므로 **(추정)** 수렴하지 않을 수 있다. 검증 경로는 손상 row 를
  단조 증가시키며 `--limit` 을 이분 탐색하는 실측이다.
- `A20-F6` WAL checkpoint 지연.
- test 삭제 0 의 증명 — package 5 전부가 uncommitted 라 기준 commit 이 없다. **네 라운드
  연속** 같은 한계다.
- `governance decision` 경로의 새 결함 — `D-050` 이 안 건드렸고 round 20 이 확인한 상태
  그대로다. 이번에 재실행하지 않았다.

## Hash Re-Check At Close

```text
rows 97 | 어긋남 0 []
```

착수 시점과 같다. **freeze 뒤 target 안의 파일을 쓰지 않았다** — 이 보고서는 target 밖의
새 파일이고, mutation 으로 건드린 `ingress.py`·`cli.py` 는 byte 단위 원본으로 되돌렸다
(`git diff --stat` 빈 출력).
