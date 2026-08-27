# Three-Lens Review — Round 21 (MGC-012-P5 Wave 14)

**판정: FAIL.** blocker 6건 — P0 0 / **P1 1** / Blocking-P2 5. Advisory 14.

Target: `evidence/review-target-round-21.txt`, **97 파일**.
세 reviewer 가 착수·종료에 **행별 hash 재계산**으로 확인했고 여섯 시점 전부 **어긋남 0**
이다. contract lens 는 중간 시점을 더해 세 번 확인했다. **셋 다 aggregate 를 무손상 근거로
쓰지 않았다** — round 19·20 에 이어 세 라운드째 지켜졌다.

mutation 은 세 lens 전부 `PYTHONDONTWRITEBYTECODE=1` 과 `__pycache__` 삭제로 돌렸고,
작업 뒤 `git diff --stat` 이 셋 다 비어 있다.

개별 기록:
[contract](round-21-lens-contract.md) ·
[failure](round-21-lens-failure.md) ·
[regression](round-21-lens-regression.md)

## Round 20 Blockers: 셋 닫힘, 셋 부분

**round 20 은 부분 닫힘이 하나도 없었다. 이번은 셋이다.**

| round 20 | 등급 | 판정 | 확인 방법 |
|---|---|---|---|
| `F20-1` | P1 | **부분** | truncation 줄은 실재한다 — 손상 `dead_letter` **101개** / `--limit 100` → 100줄 + 잘림 줄. mutation M18(`if truncated:` → `if False:`) 2건 사망. **그러나 판정식이 손상 row 앞에서 거짓 음성을 낸다** → `F21-1` |
| `F20-2` | B-P2 | **부분** | 후보 **301개** 재현. 두 항(`commands`·`unreadable`)을 각각 지우는 mutation 이 각각 다른 test 를 죽여 "두 목록 각각 판정" 이 test 로 고정된 것을 분리 확인했다. **그러나 `stranded()` 쪽 판정만 무너진다** → `F21-1`. **두 목록 중 하나만 옳다** |
| `F20-3` | B-P2 | **닫힘** | M19(`unreadable()` 의 `WHERE state != 'completed'` 제거) → `..._skips_a_corrupt_row_that_already_completed` **1건 사망**. round 20 에서는 1401개가 전부 통과했다. failure·regression 두 lens 가 독립으로 같은 결과를 얻었다 |
| `C20-1` | B-P2 | **부분** | `T038.md:87` 에 정정 각주 실재. **형제 위치 둘이 정정 없이 남았다** → `C21-4` |
| `C20-2` | B-P2 | **닫힘** | `DECISIONS.md:1676` 의 `Source` 가 "**벽 다섯**" 으로 고쳐졌고 `Correction` 항목이 경위를 적는다. `Reason` 절(넷)과 더는 안 어긋난다 |
| `R20-1` | B-P2 | **닫힘** | M20(sweep 둘째 UPDATE 에 `COALESCE(last_error_code, …)`) → `..._leaves_the_prior_error_code_alone[None]` **1건 사망**. round 20 에서는 SURVIVED 였다. `parametrize` 의 두 값이 서로 다른 mutant 를 잡는 것까지 확인했다 |

`F20-1`·`F20-2` 를 "닫혔다" 로 적으면 틀린다. `D-050` 이 만든 신호가 **정확히 그 결함이
존재하는 상황에서 꺼진다.**

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `F21-1` | failure | **P1** | `cli.py:765`, `ingress.py:703-711`, `D-050` Decision 절 | **잘림 판정이 거짓 음성을 낸다.** `truncated = len(commands) > limit` 은 `stranded()` 가 SQL 이 가져온 `limit + 1` 개를 그대로 준다고 가정한다. `_stranded_rows` 는 `LIMIT ?` 로 `limit + 1` 개를 읽은 뒤 `_view` 가 터진 row 를 **조용히 버린다.** 창 안에 손상 row 가 하나라도 있으면 반환이 `limit` 개 이하로 떨어져 판정이 `False` 가 된다. **`D-050` 이 없애려던 침묵 그 자체다.** 실측 — 후보 **201개** / 손상 **1개** / 기본 `--limit 100` → 출력 100 + UNREADABLE 1, **잘림표시 없음, 숨은 후보 100개**. 손상 0개면 표시가 켜진다. `recovery_hold` 벽에서도 같다 |
| `F21-2` | failure · contract | B-P2 | `cli.py:753-761`, `ingress.py:694` | **`limit + 1` 이 위쪽 끝에 raw traceback 을 새로 열었다.** `--limit 9223372036854775807`(int64 max)이 `limit + 1 = 2**63` 로 sqlite3 binding 에 도착해 `OverflowError` 를 낸다. CLI 의 `except` 는 `GovernanceStoreError`·`GovernanceMigrationError`·`IngressError`·`ValueError`·`sqlite3.Error` 만 잡아 **그 어느 것도 아니다.** `…806` 은 정상이고 `limit=limit` 으로 되돌리면 둘 다 정상이다 — **wave 14 가 만든 것이다.** round 16 `FR-3`·`R16-1` 이 닫은 계약이 되돌아왔다. AC-04 는 아래쪽 끝(`0`·`-1`)만 셌다. **두 lens 가 독립으로 찾았다** (`C21-2`) |
| `R21-1` | regression · contract | B-P2 | `cli.py:765`, 표 칸은 `evidence/MGC-012-P5-T044.md:34` (AC-07 #3) | **`>` 둘을 `>=` 로 바꿔도 1409개가 전부 통과한다** (SURVIVED, 전 suite). 표 #3 의 요구가 "정확히 `limit` 개일 때 안 잘림" 이고 test 칸에 `..._stays_quiet_when_the_list_fits` 를 채웠는데 **그 test 는 후보 1개에 기본 `limit=100` 이라 경계에 안 선다.** `len == limit` 인 입력을 주는 test 가 suite 에 없다. 등가 mutant 가 아니다 — 후보 6 / `--limit 6` 에서 mutant 는 없는 잘림을 보고한다. **두 lens 가 독립으로 찾았다** (`C21-1`) |
| `R21-2` | regression | B-P2 | `cli.py:767`, 표 칸은 `evidence/MGC-012-P5-T044.md:37` (AC-07 #6) | **`unreadable = unreadable[:limit]` 을 통째로 지워도 1409개가 전부 통과한다** (SURVIVED, 전 suite). 표 #6 의 처리는 slicing **둘**인데 test 칸이 댄 `..._says_when_the_list_is_cut` 은 손상 row 0개 상황이라 `commands` 쪽만 센다. 등가 mutant 가 아니다 — 손상 6개 / `--limit 2` 에서 mutant 가 UNREADABLE 을 **3줄** 낸다. `limit + 1` 로 요청한 그 한 줄이 출력에 샌다 |
| `C21-3` | contract | B-P2 | `.ai-team/policy/approvals.jsonl`, `evidence-trace.jsonl:19`, `DECISIONS.md:1686`, `work-contract.json` `human_gates` | **승인 절차가 어긋난다.** `D-050` 은 `APPROVED` 이고 Consequence 가 "CLI 출력 형식이 바뀐다" 를 명시하며 `MGC-012-P5-W14` contract 의 `human_gates` 가 `public_contract` 를 선언한다. **그런데 `approvals.jsonl` 에 `D-050` 행이 없다** — `D-047`·`D-048`·`D-049` 는 셋 다 `public_contract` 행이 있다. `evidence-trace.jsonl` 은 `D-050` 을 `{"gate": "none"}` 으로 기록한다. **네 Decision 중 실제로 사용자-facing 출력을 바꾸는 유일한 것이 승인 ledger 를 안 탔다.** 두 파일 다 target 밖이라 hash 로는 안 잡힌다 |
| `C21-4` | contract | B-P2 | `task-manifests/MGC-012-P5-T038.yaml:38`, `CURRENT_ITEM.md:166` | **`C20-1` 을 고친 wave 가 같은 지적을 재생산했다.** `D-048` 의 `Amended` 와 `D-049` 의 `Amends` 가 틀렸다고 지목한 주장("`limit` 이 후보 상한으로 돌아온다")이 두 자리에 정정 없이 서 있다. wave 14 는 `T038.md:87` 한 자리만 각주를 달았다. `T039` 가 세운 규칙("완료된 wave 의 manifest 와 evidence 본문은 고치지 않고 **정정 각주**를 단다")이 manifest 를 명시적으로 포함하는데 manifest 쪽이 빠졌다. round 20 이 `C20-1` 에 붙인 이름 그대로 **"선택적 누락"** 이다 |

**두 결함을 두 lens 가 독립으로 찾았다** — `F21-2`(= `C21-2`), `R21-1`(= `C21-1`). 서로의
결론을 안 보고 각각 실행해서 같은 곳에 닿았다.

## The Root — "전수 표" 가 두 방향으로 실패했다

wave 14 는 round 16·18 의 뿌리를 막으려고 **새 구조가 요구하는 것을 착수 전에 표로 만드는**
도구를 썼다. `T044.md:28-38` 의 AC-07 표가 그것이고 여덟 행이다. 그 도구가 실제로 한 번
작동했다 — `--limit 0` 우회를 착수 전에 잡았고 `risk.concerns` 에 적고 시작했다.

**그런데 같은 표가 두 방향으로 뚫렸다.**

| 방향 | 무엇이 빠졌나 | 나온 blocker |
|---|---|---|
| **행이 모자라다** | 표에 없는 요구 둘 — "반환 개수 ≠ SQL fetch 개수인 조회에서 반환 개수로 잘림을 판정할 수 있는가", "`limit + 1` 의 정수 overflow" | `F21-1`(P1) · `F21-2` |
| **채운 칸이 거짓이다** | 여덟 칸 중 #3·#6 이 mutation 없이 채워졌다. 표는 여덟 칸인데 evidence 의 mutation 표는 **넷**(M18~M21) 뿐이다 | `R21-1` · `R21-2` |

`T044` manifest 는 `implementation.guidance` 에 **"표의 칸을 mutation 으로 확인하기 전에는
채우지 않는다"** 를 자기 규칙으로 넣었다. 그 규칙이 한 칸(`R20-1` 의 `COALESCE`)에서
작동해 제출 전에 SURVIVED 를 잡았다. **#3·#6 에서는 mutation 을 아예 안 돌렸다.**
칸 수와 mutation 수가 안 맞는 것이 그 신호였고 아무도 그 대조를 안 했다.

### `F21-1` — 세 결정이 겹치는 자리에서 앞의 둘을 안 셌다

`D-050` 의 Decision 절은 "`limit + 1` 을 요청해 `limit + 1` 개가 오면 잘린 것" 이다.
`unreadable()` 에서는 맞다 — 그 함수는 손상 row 만 모으므로 반환 개수 = 발견 개수다.
`stranded()` 에서는 틀리다. **SQL 이 가져온 개수와 반환 개수가 다르다.**

그 차이를 만드는 skip 은 `D-047` 이 넣었고, 그 skip 이 존재하는 이유는 round 15 `F-2`
(손상 row 하나가 목록 전체를 없앴다)다. **`D-050` 이 앞의 둘을 안 셌다.**

round 19·20 이 `limit` 의 벽을 두 축(state · 가독성)으로 실측했다. **이번 축은 세 번째다 —
반환 경로에 filter 가 있는가.**

## Verified Clean

- **행별 hash 재계산 — 여섯 시점 전부 `rows 97 | 어긋남 0`.** 세 lens 가 착수·종료에
  각각 확인했고 contract 가 중간을 더했다. aggregate 를 무손상 근거로 쓴 lens 가 없다
- **test 삭제 0 / skip·xfail 신규 0 이 처음으로 증명됐다.** package 5 가 commit 돼 기준
  commit `e13e01c` 가 생겼다. **세 라운드 연속 "기준 commit 이 없다" 로 Not Checked 이던
  항목이다.** 다만 wave 14 단독 분리는 불가하다 (아래 Not Checked)
- **test 수 두 방법 일치 — 1409.** `--collect-only` 1409 selected(1413 collected, 4
  deselected) 와 실행 `1409 passed`. round 20 의 1401 대비 **+8** 이고 wave 14 가 추가한
  test 수와 같다. 세 lens 가 각각 셌고 셋 다 1409 다
- **`D-050` 의 부품이 mutation 으로 고정돼 있다.** M18(truncation 줄)·M21(CLI guard)·
  ME(`limit + 1` → `limit`)·M-R21b(`commands[:limit]`)·M-R21c·M-R21d(두 항 각각)·
  M-R21e·M-R21h 전부 KILLED. **뚫린 것은 `R21-1`·`R21-2` 둘뿐이다**
- **`--limit 0`·`--limit -1` 이 fail-closed 다.** exit 1, raw traceback 없음. wave 14 의
  주장 5(착수 전 발견)가 사실이다 — CLI guard 가 service guard 앞에 있다
- **정확히 `limit` 개일 때 잘렸다고 말하지 않는다.** off-by-one 이 없다 (코드는 옳고
  `R21-1` 은 그것을 지키는 test 가 없다는 것이다)
- **거짓 음성 방향은 막혀 있다.** M-R21h(`> limit + 1`)가 죽는다. `R21-1` 이 뚫은 것은
  거짓 양성 방향뿐이다
- **`limit + 1` 이 두 목록을 섞지 않는다.** slicing 이 각각이고 `commands`·`unreadable` 이
  독립 변수다
- **형제 위치를 끝까지 셌다.** src 에서 `stranded()`·`unreadable()` 을 `limit` 과 부르는
  곳은 `cli.py:753-754` **둘뿐**, 잘림 계산은 `cli.py:765` **하나뿐**이다. grep 전수와 건수
  세기 **두 방법이 6으로 일치**한다
- **Outbox 에는 `F20-1` 계열이 없다.** `events.py` 의 SQL `LIMIT` 은 전부 `LIMIT 1` 이고
  `limit` parameter 나 operator 목록 조회가 없다. grep 전수했다
- **operator hold 우회 없음.** `recovery_hold` 를 건드리는 UPDATE 가 ingress 에 없다
- **`ingress.py` 는 안 바뀌었다.** `D-050` 의 Scope 대로 `cli.py` 만 바뀌었고 service
  signature 는 무변경이다
- **`connect()` 호출 수 무변경** → filesystem-guard test 둘의 창이 안 밀린다. 그 둘은
  `GovernanceStore` 를 직접 만들어 CLI 를 안 거친다
- **fixture 가 검사 대상을 안 지운다.** `_readable_wall` 은 `INSERT` 만 한다
- **`C20-2` 정정이 정확하다.** `Source` 의 "벽 다섯" 이 round 19 의 실제 실측 수와 맞고
  `Correction` 이 경위를 적는다
- **lint·type·diff.** Ruff check/format, mypy, `git diff --check` 를 evidence 와 같게 재현했다
- **pristine 전 suite PASS**, mutation 은 전부 되돌아갔고 `git diff --stat` 이 비어 있다

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A21-1` | `T044.md:28-38` | **AC-07 표가 8행인데 최소 10행이어야 한다.** 빠진 것 — (9) 반환 개수 ≠ SQL fetch 개수(`F21-1`), (10) 정수 overflow(`F21-2`). wave 14 가 "전수" 로 적은 표다 |
| `A21-2` | `cli.py:784` | **잘림 줄이 어느 목록이 잘렸는지 안 말한다.** 판정은 각각 하는데 출력은 `or` 로 합쳐 한 줄이다. `D-050` 이 "한 줄로 알린다" 로 정했으므로 결정 위반은 아니다. operator 는 `--limit` 을 올려야 어느 쪽인지 안다. **contract·failure 두 lens 가 각각 적었다** |
| `A21-3` | `T044.md:33` (표 #2) | `--limit -1` 칸의 처리가 "CLI guard 가 먼저 잡는다" 인데 **M21 로 CLI guard 를 지워도 `[-1]` 은 안 죽는다.** `-1` 은 `limit + 1 = 0` 이 되어 service guard 가 잡는다. 요구(거부)는 지켜지므로 blocker 가 아니다. **두 lens 가 각각 적었다** |
| `A21-4` | `test_slack_ack_boundary.py:3114`·`:3146` | **`A20-R1` 두 라운드째 미해결.** `poison in unreadable(...)` 이 형제들(`:2499`·`:2518`·`:2780`·`:2841`·`:2961`)의 `==` 보다 약하다. wave 14 의 `allowed_paths` 안이었다 |
| `A21-5` | `test_slack_ack_boundary.py:2962` | **`A20-C3` 두 라운드째 미해결.** assert message 가 "`limit` 은 **후보 상한**이어야 한다" 다. `D-049` 뒤에는 틀린 문구다. 같은 파일 `:3116` 은 옳게 적혀 대조가 된다 |
| `A21-6` | `DECISIONS.md:1605` | **`A20-C2` 두 라운드째 미해결.** `D-048` 의 첫 `Rejected`("`LIMIT` 을 없애고 전체를 훑기")가 `D-049` 가 실제로 채택한 방식인데 pointer 가 없다 |
| `A21-7` | `cli.py:784` | truncation 문구 전체를 `"잘렸다"` 한 단어로 줄여도 27건이 통과한다. test 가 `"잘렸다" in stdout` 만 본다 — `--limit N` 값도 "더 있다" 도 안 고정된다. `D-050` 이 문구를 계약화하지 않았으므로 결함은 아니다 |
| `A21-8` | `cli.py:747` | guard 문구를 `"XX"` 로 바꿔도 27건이 통과한다. `T044` handoff 가 "**같은 문구로** 여기서 잡는다" 를 적었는데 그 문구를 지키는 test 가 없다 |
| `A21-9` | `cli.py:199` vs `:734` | 같은 파일의 다른 command 는 `typer.Option("--limit", min=1)` 로 선언 단계에서 막고 `stranded` 는 본문 `if limit < 1` 로 막는다. `stranded` 는 `limit + 1` 때문에 본문 guard 가 필요하지만 `min=1` 을 함께 걸지 못할 이유는 없다. **`F21-2` 의 위쪽 끝도 선언 단계에서 막을 수 있다** |
| `A21-10` | `cli.py:753-754` | `stranded()` 를 먼저 부르므로 `F21-2` 의 crash 가 `unreadable()` 도래 전에 난다. 순서를 바꾸면 증상만 바뀐다. traceback 이 `ingress.py:694 in _stranded_rows` 를 가리킨다 |
| `A21-11` | `work-contract.json` `status` | wave 14 가 끝났는데 `status` 가 `"ready"` 이고 `blocked_by` 가 빈 문자열이다. 이 파일은 wave 마다 덮어쓰여 이전 값과 대조할 수 없다 |
| `A21-12` | `T045.yaml:39` | 문서 전용 task 인데 `allowed_paths` 에 `ingress.py` 가 있다. `T042` 복사의 잔재다. **실제로 쓰이지 않았다** |
| `A21-13` | `A20-F1` 의 비용 | `limit + 1` 이 `unreadable()` 의 scan 을 한 row 더 끄는 것은 `D-050` Consequence 대로다. `stranded()` 쪽은 `LIMIT` 이 하나 늘 뿐이라 비용이 사실상 안 는다. **이번에 다시 재지 않았다** |
| `A21-14` | `T045` manifest | `T042`(done) 복사본이라 착수 시점부터 `all_acceptance_passed: true` 였다. **이미 알려진 절차 위반**이다. 이번 라운드에도 그 상태 그대로임을 확인했다 |

## Not Checked

- **wave 14 단독의 test 삭제 0.** `87a0c76` 한 commit 에 wave 11~14 가 함께 들어 있다.
  위 증명은 **네 wave 를 묶어** `e13e01c` 대비로 한 것이다. round 20 시점의 test 파일
  내용은 commit 되지 않아 복원할 수 없다
- **`C21-3` 의 승인이 다른 곳에 기록됐는지.** repository 전역 `grep "D-050"` 으로
  `approvals.jsonl` 밖의 승인 record 를 못 찾았다. **대화 기록은 확인할 수 없다**
- **`F21-2` 가 다른 CLI 경로에도 있는지.** `search --limit` 은 vault 도메인이라 범위 밖이고
  `min=1` 이 붙어 있으며 `limit + 1` 구조가 없다. **위쪽 끝은 안 쟀다**
- **`F21-1` 상태에서 `--limit` 을 올리는 회수 절차의 수렴.** 손상 row 수만큼 계속 어긋나므로
  **(추정)** 수렴하지 않을 수 있다. 검증 경로는 손상 row 를 단조 증가시키며 `--limit` 을
  이분 탐색하는 실측이다
- **`R21-1`·`R21-2` 의 mutation 을 동시에 넣었을 때의 상호작용.** 각각 단독으로만 쳤다
- **v2 profile 전체.** `pytest`·`ruff`·`mypy`·`diff-check` 는 재현했지만
  `loop-runtime-doctor`·`schema`·`vault-lint`·`project-pack` 은 안 돌렸다
- **실제 sqlite 장애(disk full, read-only, WAL 손상). 다섯 라운드 연속** 같은 항목이다
- **여러 worker 를 실제 process 로 띄운 동시성** — 단일 process 안의 강제 interleaving 만 했다
- **`A20-F6` WAL checkpoint 지연의 크기**
- **`stranded()`·`unreadable()` 을 CLI 밖에서 부르는 곳** (`OutboxDispatcher`·
  `apply_jobs.py`)의 같은 truncation 형태 — target 밖이다. round 20 도 같은 이유로 안 봤다
- **`doc-impact.json` 450줄의 내용 정합성.** 생성물이고 이번 wave 의 주장과 무관하다
