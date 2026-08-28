# Three-Lens Review — Round 20 (MGC-012-P5 Wave 13)

**판정: FAIL.** blocker 6건 — P0 0 / P1 1 / Blocking-P2 5. Advisory 12.

Target: `evidence/review-target-round-20.txt`, 92 파일, aggregate
`174ceeb204686b32673b6cedd4b036288ba00ce92f12e2e160ffe09683d7bf59`.
세 reviewer 가 착수·종료에 **행별 hash 재계산**으로 확인했고 여섯 시점 전부 **어긋남 0** 이다.
셋 다 aggregate 를 무손상 근거로 쓰지 않았다.

**round 19 의 9건은 전부 닫혔다.** contract lens 가 각각 실행으로 확인했다.

## Round 19 Blockers: All Nine Closed

| round 19 | 등급 | 확인 방법 |
|---|---|---|
| `F19-1` | P1 | mutation M14(SQL `LIMIT` 복원) → 벽 5종 + 막힌 큐 **6건** 사망. `completed` param 만 살아남는 것이 옳다 |
| `F19-2` | B-P2 | durable state 8종 × attempts 2 = **16조합 전수**. 도달 안 하는 경로가 정확히 둘이고 `last_error_code` 도 표대로다. **셋째는 없다** |
| `R19-1` | B-P2 | M15 → `..._escapes_the_unreadable_scan` 1건 사망 |
| `R19-2` | B-P2 | M16 → `..._in_arrival_order` 1건 사망 |
| `C19-1` | B-P2 | `D-045` 각주 실재. **승인 문구 무수정** 확인. `INGRESS_COMMAND_UNREADABLE` 을 쓰는 곳이 `_dead_letter_unreadable` 하나뿐임을 코드로 확인 |
| `C19-2` | B-P2 | `T040.md` 의 freeze 절이 채워졌고 적힌 aggregate 를 **재계산해 일치** 확인 |
| `C19-3` | B-P2 | 요약 줄 정정. operator 조회 **넷을 끝까지 세어** 전수 대조 |
| `C19-4` | B-P2 | census 독립 재계수 — 현재 31파일/98줄에서 wave 13 산출물 5파일을 빼면 **정확히 26파일**, 표의 줄 수 합계도 **정확히 79** |
| `R19-3` | B-P2 | 표 칸 직접 계수 — 구조 1 = 4, 구조 2 = 6, 합 **10** ✓ |

**regression lens 가 `T041` evidence 의 표 다섯을 전수 검증했고 거짓 칸이 없다.** round 19
`R19-3` 이 wave 12 표에서 찾은 형태가 반복되지 않았다.

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `F20-1` | failure | **P1** | `ingress.py:679-690`, `D-049` Reason·Consequence | **`limit` 을 출력 상한으로 옮겨도 같은 signature 가 남는다.** `D-049` 의 근거는 "읽을 수 있는 row 는 건너뛰므로 어떤 벽도 못 민다" 인데 **벽이 읽을 수 없는 종결 row 면 건너뛰지 않는다.** 손상 `dead_letter` row 가 `limit` 개 쌓이면 큐를 막고 있는 새 손상 row 가 출력 밖으로 밀린다. **그 벽은 `_dead_letter_unreadable` 이 스스로 만든다** — 치운 손상 row 는 전부 `dead_letter` + 읽을 수 없음이고 `dead_letter` 를 벗어나는 UPDATE·DELETE 가 없다 (`D-049` 자신이 적은 사실). 임계값 실측 — 벽 99 보임 / **100 안 보임**. SQL 재계수 손상 101개, CLI 출력 100줄 |
| `F20-2` | failure | B-P2 | `ingress.py:694-702`, `D-049` Scope | **같은 뿌리가 `stranded()` 에 그대로 있다.** `D-049` 는 "`SQL LIMIT` 을 쓰는 한 어떤 state 집합을 골라도 민다" 를 근거로 쓰고 수정을 `unreadable()` 에만 적용했다. `dead_letter`+`recovery_hold` 가 `limit` 개 쌓이면 **새로 stranded 된 읽을 수 있는 row 가 영구히 안 보인다.** `unreadable()` 도 그 row 를 안 낸다 — 읽히기 때문이다. 실측: 후보 301개, CLI 100줄, 새 row 없음 |
| `F20-3` | failure | B-P2 | `ingress.py:645`, docstring `:667-668` | `unreadable()` 의 `WHERE state != 'completed'` 를 **통째로 지워도 1401개가 다 통과한다.** docstring 이 "손상된 `completed` row 는 여기 안 나온다. **그것이 옳다**" 로 계약을 단언하고 `D-048` 이 그것을 승인 근거로 삼는데 **고정하는 test 가 하나도 없다.** round 19 `A19-R2` 가 Advisory 로 적었고 wave 13 이 안 닫았다 — 그 사이 같은 문장이 두 Decision 의 근거로 승격됐다 |
| `C20-1` | contract | B-P2 | `evidence/MGC-012-P5-T038.md:87`, `:17` | **`D-049` 가 "틀렸다" 고 지목한 바로 그 문장이 정정 없이 서 있다.** `:87` 이 "`limit` 이 **후보 상한**으로 돌아왔다. `stranded()` 와 같은 규칙이다" 를 단언한다. `D-048` 의 `Amended` 와 `D-049` 의 `Amends` 가 **한 글자까지 같은 이 문장**을 틀린 것으로 명시한다. **누락이 아니라 선택적 누락이다** — 같은 파일 `:50` 에 wave 13 이 `R19-3` 정정 각주를 달았고 다른 다섯 파일에도 전부 붙였다 |
| `C20-2` | contract | B-P2 | `DECISIONS.md:1676` | **승인된 Decision 이 자기 근거를 과대 계상한다.** `D-049` 의 `Source` 가 "근거는 round 19 `F19-1` 의 **벽 6종 전수 실측**" 이라 적었는데 round 19 는 다섯만 했다 — `leased` 를 "재현 못 했다" 로 남겼다. 6종은 **승인 뒤** wave 13 이 만든 증거다. 같은 Decision 의 `Reason` 절은 정확히 넷을 댄다 — 본문과 `Source` 가 안 맞는다 |
| `R20-1` | regression | B-P2 | `ingress.py:401-407`, docstring `:379-382`·`:547-551` | `retry_wait`+소진 경로가 `last_error_code` 를 **안 건드린다**는 사실을 지키는 test 가 없다. 둘째 UPDATE 에 `COALESCE(last_error_code, '…')` 를 넣어도 **1401개가 전부 통과한다** (M17 SURVIVED). 첫째 경로는 `..._exhausted_corrupt_row_is_still_visible...` 이 지키는데 둘째는 없다. AC 이름이 "경로 **전수**" 이고 두 docstring 에 표로 못박았는데 **절반만 회귀 보호된다** |

## The Root — 벽은 두 축인데 한 축만 전수했다

`D-049` 의 Reason 은 정확하다. 그 문장에서 나오는 결론이 셋인데 wave 13 은 하나만 실행했다.

| `D-049` 가 적은 사실 | 실행한 결론 | 안 한 결론 |
|---|---|---|
| `LIMIT` 은 `_view` **전에** 자른다 | `unreadable()` 에서 `LIMIT` 을 뺐다 | — |
| `dead_letter`·`recovery_hold` 는 **영구히 쌓인다** | — | 그 벽이 `stranded()` 의 `LIMIT` 도 민다 (`F20-2`) |
| 읽을 수 있는 row 는 건너뛴다 | 벽 6종 test | **벽이 읽을 수 없으면 안 건너뛴다** (`F20-1`) |

wave 13 은 벽의 **state 를 여섯으로 전수**했지만 **벽의 가독성을 세지 않았다.** 벽은 두
축인데 한 축만 전수했고, **안 센 축을 `_dead_letter_unreadable` 이 스스로 만든다.**

더 근본적으로는 이것이 `limit` 의 본질이다. 출력이 `limit` 개로 제한되면 `limit+1` 번째는
안 보이고, **그 사실을 알리는 신호가 없다.** `A19-F2`·`A20-F2` 가 두 라운드 연속
"truncation 표시가 없다" 를 지적했고 wave 13 의 `non_goals` 였다.

## Verified Clean

- **round 19 의 9건 전부 닫힘.** 세 lens 가 각각 실행으로 확인했다.
- **mutation M14·M15·M16 전부 KILLED**, 두 lens 가 독립 재현했고 죽은 test 이름·건수까지
  일치한다. regression lens 가 추가로 python 출력 상한의 `break` 와 off-by-one 을 각각
  mutate 해 **둘 다 KILLED** 를 확인했다.
- **`T041` evidence 의 표 다섯이 전부 실측과 일치.** wave 12 의 `R19-3` 같은 거짓 칸이 없다.
- **`_sweep_recoverable` 의 두 UPDATE 사이 process 사망** — round 19 Not Checked 를 닫았다.
  trigger `RAISE(ABORT)` 와 **실제 `SIGKILL`** 두 방법으로 확인, 부분 commit 0건.
- **`unreadable()` scan 중간 예외.** 50번째 row 에서 `AttributeError` 주입 → 그대로 escape
  하고 connection 이 깨끗이 닫힌다. 잠긴 connection 도 남은 transaction 도 없다.
- **`BEGIN EXCLUSIVE` 를 쥔 채 조회.** WAL 이라 두 조회가 안 막히고 정확한 답을 낸다.
- **operator hold 우회 없음.** `recovery_hold` 에 `claim_next` 5회 → 불변.
- **`_view` 의 실제 실패는 전부 `ValueError` 계열이다.** `json.loads` 가 항상 `str` 을
  받으므로 `TypeError` 가 못 나고 `ValidationError` 는 `ValueError` 하위다.
- **`stranded()`·`get()`·`is_unreadable()` signature 무변경, CLI 출력 형식 무변경.**
  `cli.py`·`store.py`·계약문의 sha256 이 round 19 target 과 동일하다. 바뀐 src 는
  `ingress.py` 하나다.
- **`D-047`→`D-048`→`D-049` 사슬이 서로 모순되지 않는다.** 상호 각주가 붙어 있고 공통
  불변("`stranded()` 의 계약·signature·CLI 출력 형식 불변")이 코드에서 유지된다.
- **`D-045` 의 재해석 각주가 승인문과 충돌하지 않는다.** 승인문의 조건절이 성립하는 유일한
  경로가 `_dead_letter_unreadable` 이고 그 경로는 `INGRESS_COMMAND_UNREADABLE` 을 쓴다.
- **`allowed_paths` 준수.** round 19→20 diff 가 전부 세 manifest 의 `allowed_paths` 안이다.
- **test 삭제 0, skip/xfail 신규 0.** evidence 가 이름을 댄 test 26개 전부 실재한다.
- **test 수 두 방법 모두 1401.** round 19 의 1392 대비 +9 로 추가한 case 수와 같다.
- **v2 profile PASS.**

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A20-F1` | `T041.md`, `D-049` Consequence | **비용을 측정했다** — evidence 가 "측정하지 않았다" 로 남긴 값이다. 종결 아닌 row 당 약 **5.5 µs**, 선형. `dead_letter` 1000/10000/50000 에서 `unreadable(100)` 이 **13.2 / 61.3 / 276.0 ms**. 같은 수의 `completed` 를 더해도 안 는다 — SQL 제외가 실제로 작동한다. 대조로 `stranded(100)` 은 8.4 / 10.6 / 15.9 ms. 100만 row 면 **(추정) 약 5.5 s**. `dead_letter`·`recovery_hold` 는 안 빠지므로 **사고 누적과 함께 단조 증가한다** |
| `A20-F2` | `cli.py:745-766` | **`A19-F2` 두 라운드째 미해결.** truncation 표시가 없다. **이것이 `F20-1`·`F20-2` 를 발견 불가능하게 만드는 부품이다** — `--limit` 을 올리면 회수되지만 올려야 한다는 신호가 없다 |
| `A20-C1` | `approvals.jsonl` | `D-049` 승인 note 가 "두 lens 가 독립 실측" 이라 적는데 round 19 는 `F19-1` 의 lens 를 `failure` 하나로 적었다. target 밖 파일 |
| `A20-C2` | `DECISIONS.md:1605` | `D-048` 의 `Rejected` 첫 항목이 **지금 채택된 방식**인데 pointer 가 없다. `D-048` 만 읽으면 여전히 거절된 것으로 보인다 |
| `A20-C3` | `test_slack_ack_boundary.py:2962` | wave 12 test 의 assert message 가 "`limit` 은 **후보 상한**이어야 한다" 다. `D-049` 뒤에는 틀린 문구다. test 는 옳게 통과한다 |
| `A20-C4` | `T038.md:44-48` | 합계 "test 가 있는 것 10" 에 시점 한정어가 없다. `T041` 이 #4·#5 를 닫아 현재는 12 다. 표 셀도 "없었다" 로 남아 새 test 이름을 안 댄다 |
| `A20-C5` | `T040.md:167-172` | 채워 넣은 freeze block 은 **사후 재구성**이다. 각주가 그 사실을 밝히고 aggregate 는 재계산으로 맞췄지만 `rows 85 \| 어긋남 0` 은 이제 재현 불가다 — 그 85행 중 12개가 wave 13 으로 바뀌었다 |
| `A20-C6` | `ingress.py:378-381` | sweep 둘째 경로를 고정하는 test 가 없다 → `R20-1` 로 승격됐다 |
| `A20-F4` | `ingress.py:536` | "도달하지 않는 경로는 **둘**이다" 는 문장만 읽으면 틀리다. 16조합 중 12조합이 도달 안 한다. 다음 문장이 범위를 정의하므로 **뜻은 맞다** |
| `A20-F5` | `ingress.py:566` | `_dead_letter_unreadable` 의 state guard 를 넓혀도 115건이 통과한다. **(추정) 등가 mutant** — 그 state 에 도달하려면 성공한 claim 이 끼어야 하고 그러면 generation guard 가 막는다 |
| `A20-F6` | `ingress.py:679-690` | 긴 scan 이 WAL read snapshot 을 잡는 동안 checkpoint 가 밀린다. **측정 안 했다** |
| `A20-R1` | `test_slack_ack_boundary.py` | 새 벽 test 가 `poison in unreadable(...)` 로 단언한다. 벽이 전부 읽히는 row 라 기대값은 `== (poison,)` 다. **형제보다 약하다** — wave 12 test 는 `==` 를 쓴다. M14 로 죽으므로 blocker 는 아니다 |

## Not Checked

- 실제 sqlite 장애(disk full, read-only, WAL 손상). **네 라운드 연속** 같은 항목이다.
- 여러 worker 를 실제 process 로 띄운 동시성 — 단일 process 안의 강제 interleaving 만 했다.
- `A20-F6` WAL checkpoint 지연의 크기.
- `F20-1` 이 `OutboxDispatcher`·`apply_jobs.py` 에도 있는지 — target 밖이다.
- **test 삭제 0 의 증명** — package 5 전부가 uncommitted 라 기준 commit 이 없다. 세 라운드
  연속 같은 한계다.
- `D-049` 승인 record 의 `proposal_sha256` 규격 — 해싱 대상 범위가 명시돼 있지 않다.
- `T041` 이 적은 RED 쪽 개별 id 문자열 — M14 로 같은 결론을 얻었지만 문자열 대조는 안 했다.
