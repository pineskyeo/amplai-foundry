# Three-Lens Review — Round 16 (MGC-012-P5 Wave 8 + Wave 9)

**판정: FAIL.** blocker 9건 — P0 1 / P1 3 / Blocking-P2 5. Advisory 10.

Target: `evidence/review-target-round-16.txt`, 44 파일, aggregate
`b5a88e45b332fccabb1cd8abe91be8dec9b3f8181b82e44f0593cd0bd002aecf`.
세 reviewer 가 착수·종료 시점에 확인했고 regression reviewer 는 mutation 전후로도 확인했다.
**무손상.**

## Round 15 Blockers: All Nine Closed

contract reviewer 가 아홉을 각각 **실행으로** 확인했다.

| round 15 | 등급 | 확인 방법 |
|---|---|---|
| `F-1` | P0 | 손상 row 뒤 command 가 같은 호출에서 `completed` 됨을 실행 확인 |
| `C-1` | P1 | filesystem 실패 시 `exit 1` + `_fatal` 문구, traceback 없음 |
| `F-2` | P1 | `_stranded_rows` 가 row 단위로 걸러 읽을 수 있는 것을 계속 낸다 |
| `F-3` | P1 | worker 가 write lock 을 쥔 채 `exit 0`, **0.02s** (wave 8 은 5.41s 실패) |
| `R-1` | P1 | `_configure` 를 깨뜨려 `sqlite3.OperationalError` 가 그대로 나옴 확인. 아홉 handler 를 AST 로 독립 재계수 |
| `C-2` | B-P2 | 거짓 invariant 가 취소선 + 정정 절로 남음 |
| `C-3` | B-P2 | `--json` → `exit 2 No such option` |
| `F-4` | B-P2 | `set_trace_callback` 으로 SQL 전수 관측, write 0건 |
| `F-5` | B-P2 | docstring 이 두 경로 frame 을 명시하고 interruption 으로 한정 |

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `S-1` | (main agent 자체 재현) | **P0** | `ingress.py:431-446` | `_dead_letter_unreadable` 이 `rowcount` 를 안 본다. UPDATE 가 0행이면 `claim_next` 의 `while True` 가 같은 row 를 다시 뽑아 **무한히 돈다.** 50회 초과 실측 |
| `FR-1` | failure | P1 | `ingress.py:438-446` | 같은 UPDATE 에 `state`·`claim_generation` guard 가 없다. claim rollback 과 write 사이에 다른 worker 가 정상 claim 하면 **살아 있는 lease 를 지운다.** 그 worker 는 결정을 이미 commit 했을 수 있고 `complete()` 가 `INGRESS_LEASE_CONFLICT` 로 실패한다. row 가 그 사이 `completed` 면 `completed_at` CHECK 위반으로 `IntegrityError` 가 `claim_next` 밖으로 나간다 |
| `C16-1` | contract | P1 | `ingress.py:448-462` → `cli.py:748-757` | `get()` 이 읽을 수 없는 row 에 `None` 을 내고 `committed_decision()` 이 그것을 "결정 없음" 으로 바꾼다. `governance decision` 이 **exit 0 + `NONE`**. 계약(`interaction-feedback.md:48-51`)이 "*we know a decision landed*" 와 "*we cannot tell*" 을 명시적으로 나누는데 회수 도구가 합쳐 **거짓 음성**을 낸다 |
| `R16-1` | regression | P1 | `cli.py:709,735,758` | `check_startup()` 제거로 **governance schema 가 아닌 store 파일**에서 raw `sqlite3.OperationalError` 가 두 command 를 뚫는다. wave 8 은 `GovernanceMigrationError` 로 exit 1 이었다. 주석이 "조용히 틀린 답을 내지 않는다" 로 정당화했지만 traceback 이 된다는 것은 안 적혀 있고 test 도 없다 |
| `FR-2` | failure | B-P2 | `ingress.py:312-316,437` | dead-letter write 가 실패하면 예외가 `claim_next` 를 그대로 뚫고 poison row 는 `pending` 에 남는다. 뒤 command 도 claim 되지 않고 `stranded()` 에도 안 걸린다 — **`D-045` 가 "Rejected" 로 거절한 그 상태**로 되돌아간다 |
| `FR-3` | failure | B-P2 | `cli.py:735,758` + `store.py:42-46` | `_OPEN_FAILURES` 축소로 read-only store 의 `PRAGMA journal_mode = WAL` 이 내는 `sqlite3.OperationalError` 가 CLI except 를 빠져나가 **raw traceback** 이다 |
| `R16-2` | regression | B-P2 | `store.py:266-272` | T029 revert 의 세 지점 중 `_raw_connection` 의 PRAGMA 만 test 가 없다. 그 자리를 다시 감싸는 mutation 이 `test_governance_store.py` 40건을 전부 통과한다 |
| `C16-2` | contract | B-P2 | `evidence/T028.md:54`, `T026.md:49-50` | "`tests/test_cli.py` 에 다섯을 더했다 (11 → 15)". 산수가 안 맞고(11+5=16) wave 8 기준은 **10** 이다. 같은 거짓이 두 파일에 |
| `C16-3` | contract | B-P2 | `evidence/T029.md`, `T030.md` | manifest 가 `required: true` 로 요구한 `ruff-format` command_output 이 없다. T029 는 `targeted` 도 없다. 그런데 `required_evidence_present: true` 다 |

## Two Roots

**뿌리 1 — 새 구조를 넣고 그 구조가 요구하는 것을 안 채웠다.**

`S-1`, `FR-1`, `FR-2`, `C16-1` 넷이 T027 의 dead-letter 경로 하나에서 나왔다. round 15 의
P0 를 고치려고 `while True` + transaction 밖 write 를 도입했는데, 그 구조가 필요로 하는 넷을
빠뜨렸다.

| 구조가 요구하는 것 | 빠진 결과 |
|---|---|
| 진행 보장 (`rowcount`) | `S-1` 무한 루프 |
| 경합 guard (`state`, `claim_generation`) | `FR-1` 살아 있는 lease 삭제 |
| write 실패 처리 | `FR-2` D-045 가 거절한 상태로 복귀 |
| "모른다" 와 "없다" 의 구분 | `C16-1` 거짓 음성 |

**뿌리 2 — 한 방향을 고치며 반대를 만들었다.**

`FR-3` 과 `R16-1` 은 둘 다 CLI 의 raw traceback 인데 trigger 가 다르다. `FR-3` 은 T029 의
`_OPEN_FAILURES` 축소가, `R16-1` 은 T028 의 `check_startup()` 제거가 원인이다. round 15 가
`C-1` 로 잡은 것과 **같은 모양**이 두 갈래로 다시 열렸다.

저장소가 여섯 라운드째 걸리는 stop rule 이다 — "review 지시를 그대로 구현하기 전에 다른 축이
무너지는지 본다".

교훈은 위치다. `connect()` 에서 전부 정규화하면 `legacy_*.py` 아홉 handler 가 깨지고(`R-1`),
아무것도 정규화하지 않으면 CLI 가 traceback 을 낸다. **정규화할 곳은 CLI 경계다** — 거기서
`sqlite3.Error` 를 잡으면 두 갈래가 한 번에 닫히고 legacy handler 는 그대로다.

## Verified Clean

**두 worker, 하나의 poison row.** thread barrier 로 동시 `claim_next`. poison 은
`dead_letter`, healthy 는 다른 worker 에게 leased. **lost update·spin·healthy 건너뜀 없다.**
`BEGIN IMMEDIATE` 가 직렬화한다. (`FR-1` 은 dead-letter write 를 인위적으로 지연시켜야 나는
좁은 창이다.)

**worker 가 unreadable row 의 lease 를 쥔 채 죽는 경우.** lease 만료 → sweep → 재claim →
`dead_letter` → `unreadable()` 에 보인다. 영구 은닉 없다.

**`_finalize` 의 새 `IngressError`.** code 만 나가고 chain·frame 어디에도 credential 이 없다
(`_COMMAND_COLUMNS` 가 `credential_hash` 를 안 뽑는다).

**좁힌 `_OPEN_FAILURES` — worker 3 지점.** `process_next`·`_finalize`·`_settle_exhausted_retry`
의 except tuple 이 전부 `sqlite3.Error` 를 포함해 raw escape 가 **없다.** 새는 곳은 CLI 뿐이다.

**write 경합 중 recovery CLI.** worker 가 `BEGIN IMMEDIATE` 를 쥔 채 두 command 가 `rc=0`.
`check_startup()` 제거의 효과는 실제로 성립한다.

**`claim_next` refactor 등가성.** HEAD 의 `claim_next` body 와 `_claim_one` body 를 difflib
로 대조. 차이는 마지막 `return self._view(claimed)` 를 감싼 6줄뿐이다. lease sweep 과 두
UPDATE 는 동일하다.

**`get()` 의 `ValueError` 삼킴 — 잃은 caller 없음.** `src/` caller 는 `ingress_worker.py:310`
하나이고 `None` 을 "결정 없음" 으로만 쓴다. test caller 29곳 중 exception 을 기대하던 것은
없다. (그래도 `C16-1` 은 남는다 — 계약이 요구하는 구분이다.)

**wave 8 tests 약화 없음.** `--json` 제거는 code path 자체가 사라진 것이라 보호 손실이 아니고
T028 evidence 가 그 축소를 적었다.

**schema 와 enum 일치.** `provider`/`action`/`state` 는 migration CHECK 과 pydantic enum 이
값 집합까지 같다. 이 셋으로는 healthy row 가 dead-letter 되지 않는다.

**dead_letter row 가 목록에서 사라지지 않는다.** `_stranded_rows` SQL 의
`state IN ('dead_letter','recovery_hold')` 에 attempts 조건이 없다.

**전 suite** `1370 passed, 4 deselected` (reviewer 둘이 각자 실행). ruff·mypy·verify 7/7.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `FR-4` | `ingress.py:201,206` | `accept()` frame 의 `raw_credential`(plaintext)·`credential_hash` 가 이후 실패 경로 traceback 에 살아남는다. sink 로 가는 경로는 못 찾아 P0 로 안 올렸다. `slack_http` 는 같은 class 를 결함으로 보고 scrub 한 전례가 있다 |
| `FR-5` | `ingress.py:610` | `ChannelRef` 가 모르는 key 하나면 `ValidationError`=`ValueError` 라 **재시도 없이 통지 없이** dead-letter 된다. rolling deploy 로 field 가 늘면 안 올라간 worker 가 정상 command 를 terminal 로 보낸다 |
| `R16-3` | `ingress.py:532-539,576-582` | `_finalize` 의 두 `IngressError` guard 가 unkilled. 새 code 라 회귀는 아니다 |
| `R16-4` | `ingress.py:496-512` | `stranded(limit=N)` 이 SQL `LIMIT N` 뒤에 거르므로 손상 row 가 있으면 N 개 미만을 낸다 |
| `R16-5`/`A16-3` | `cli.py:736-738` | `stranded()` 와 `unreadable()` 이 각각 새 connection·새 clock 을 쓴다. 두 목록이 다른 snapshot 이다 |
| `R16-6` | `tests/test_cli.py:327-336` | `test_the_reads_reject_a_json_flag` 가 `exit_code != 0` 만 보고 `decision` 은 안 본다 |
| `A16-1` | `DECISIONS.md` D-045 | 승인문은 "`stranded()` 가 그것을 낸다" 인데 code 는 `unreadable()` 이 낸다. operator 가 보는 출력은 의도와 같으나 Decision 문구는 안 고쳤다 |
| `A16-2` | `evidence/T029.md` | manifest guidance 가 "직접 전수" 를 요구했는데 round 15 표를 옮겼다. 독립 재계수를 안 했다 |
| `A16-5` | `evidence/T027.md` | AC-07·AC-09 의 실측 출력이 없다. 주장만 있다 |
| `A16-6` | `store.py` `_configure` | governance CLI 매 connection 이 `PRAGMA journal_mode = WAL`·`synchronous = FULL` 을 낸다. help 의 "SELECT statements only" 보다 넓다 |

## On T027's Amended AC-06

contract reviewer 판정: **round 12 `C-2` 형태가 아니다.** 원문을 지우지 않았고, 사유가
실재하며, acceptance 가 약해지지 않았다.

다만 둘을 지적했다. "**구현 불가능하다**" 는 과장이다 — `stranded()` 의 반환 model 을 바꾸면
가능했고, 불가능한 것이 아니라 그 설계를 택하지 않은 것이다. 그리고 "구현 전" 이라는 시점
주장은 manifest 가 untracked 라 저장소에서 검증할 수 없다.

## Not Checked

- 착수 전 기준선 수치 (wave 8 의 무방비 4/62/6-of-7) — 세 라운드 모두 재현 비용으로 넘겼다.
  **내 실측만 있고 독립 확인이 없다.**
- wave 8 의 `events.py`·`slack_projection.py`·`decisions.py`·`slack_cards.py`·`review_cards.py`
  변경 — round 15 가 Verified Clean 으로 닫아 round 16 은 다시 보지 않았다.
- `FR-2` 의 `_dead_letter_unreadable` 이 `config.busy_timeout_ms` 를 안 쓰는 점이 발생률을
  얼마나 올리는지.
- `R16-2` 판정은 targeted run + grep 이다. 전체 suite 를 그 mutation 상태로 돌리지 않았다.
- 동시 worker race 의 넓은 조합, `governance_events`/`apply_jobs` 의 `claim_next`.
