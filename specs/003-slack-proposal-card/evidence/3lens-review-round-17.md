# Three-Lens Review — Round 17 (MGC-012-P5 Wave 8 + Wave 9 + Wave 10)

**판정: FAIL.** blocker 8건 — P0 0 / P1 3 / Blocking-P2 5. Advisory 5.

Target: `evidence/review-target-round-17.txt`, 52 파일, aggregate
`8c7d2220b51fd53f191ea2293976549dc33cec1b7edf9ffc4eeea59b62a23939`.
세 reviewer 가 착수·종료 시점에 확인했고 regression reviewer 는 mutation 9회 전후로도
확인했다 (총 4회 실행). **무손상.**

**round 16 대비 P0 가 사라졌다.** 9건 → 8건. 등급은 내려갔다.

## Round 16 Blockers: Eight Closed, One Not

contract reviewer 가 아홉을 각각 **실행으로** 확인했다.

| round 16 | 등급 | 판정 | 확인 방법 |
|---|---|---|---|
| `S-1` | P0 | 닫힘(부분) | `cleared` set 으로 loop 가 유한하게 끝난다. 3회 호출 모두 `IngressError` 로 즉시 종료. **다만 종료 후 큐가 막힌다 — `F17-1`** |
| `FR-1` | P1 | 닫힘 | `claim_generation = ?` + `state IN ('pending','retry_wait')`. 2-worker race 에서 살아 있는 lease 보존 확인 |
| `C16-1` | P1 | 닫힘 | CLI 실측: 정상 → `NONE`, 손상 → `UNKNOWN  이 command 는 읽을 수 없다` |
| `R16-1` | P1 | 닫힘 | governance schema 아닌 파일에서 두 command 모두 exit 1, traceback 없음 |
| `FR-2` | B-P2 | **절반만** | 예외가 뚫는 것은 닫혔다. **row 가 안 보이는 것과 큐가 막히는 것은 그대로 — `F17-2`** |
| `FR-3` | B-P2 | 닫힘 | read-only store 6 상태 전수 실측. exit 1, traceback 없음 |
| `R16-2` | B-P2 | 닫힘 | `test_connect_leaves_a_sqlite_error_raw_at_every_revert_point`. mutation M7 이 2건으로 KILLED |
| `C16-2` | B-P2 | 닫힘 | 두 방법 계수 재현 — `grep -c "^def test_"` = 18, `--collect-only` = 21, parametrize 1개 × 4 = +3 로 정합 |
| `C16-3` | B-P2 | **안 닫힘** | T029/T030 만 고쳤다. T026·T027 은 그대로고 **wave 10 자신이 셋을 새로 냈다 — `F17-4`** |

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `F17-1` | contract | **P1** | `ingress.py:476-478` | `FR-1` 의 `state IN ('pending','retry_wait')` guard 가 만료 lease 상태를 "다른 worker 가 가져갔다" 로 오해한다. 손상 row 가 `leased` + lease 만료면 치우는 write 가 항상 0행이고, `cleared` 판정으로 `claim_next` 가 닫힌다. 뒤 command 는 `pending` 인 채 영원히 처리되지 않는다 — **round 15 `F-1` 의 head-of-line 차단이 되돌아왔다.** `attempts=0` 이라 `stranded()`·`unreadable()` 둘 다 안 보인다 |
| `F17-2` | failure | **P1** | `ingress.py:330-347`, `522-575` | dead-letter write 가 실패하면 row 가 `pending`·`attempts=0` 으로 남는데 `_stranded_rows` 의 WHERE 가 `pending` 을 포함하지 않는다. 큐가 멈춘 채 `governance stranded` 가 `NONE` 을 낸다. `attempts` 는 rollback 되는 transaction 안에서만 증가하므로 **시간이 지나도 보이지 않는다** |
| `F17-3` | regression | **P1** | `ingress.py:478-479` | `FR-1` 이 넣은 두 guard 를 **하나씩 지워도 suite 1382건이 전부 통과한다.** 유일한 race test 가 worker B 를 정상 claim 시키는데 그 claim 이 generation 과 state 를 동시에 바꿔 두 조건이 같은 시나리오에서 참이다. 각 guard 를 가르는 시나리오는 docstring 이 스스로 이름을 대는데 test 가 없다 |
| `F17-4` | contract | B-P2 | `task-manifests/T031·T032·T033.yaml` | `C16-3` 재발. 세 manifest 가 `targeted` command_output 을 `required: true` 로 요구하는데 evidence 어디에도 없고 `required_evidence_present: true` 다. T026·T027 도 처음부터 같은 상태 — **9 manifest 중 5개.** round 16 이 이름을 댄 둘만 고쳤다 |
| `F17-5` | contract | B-P2 | `T033.yaml` AC-02, `evidence/T033.md` | AC-02 가 "`connect()` 를 감싸는 handler 를 AST 로 전수하고 표로 적고 아홉인지 **직접** 확인한다" 를 요구하는데 evidence 에 절도 표도 개수도 없다. `all_acceptance_passed: true` |
| `F17-6` | contract | B-P2 | `evidence/T031.md:26-28` | AC-07·AC-12 절이 없는데 `all_acceptance_passed: true`. AC-03 은 "round 16 reviewer 가 재현했다" 로 **남의 재현을 인용**한다. `T034` 가 `A16-5` 로 지적한 것과 같은 형태를 같은 wave 가 재생산했다 |
| `F17-7` | failure | B-P2 | `ingress.py:341-357` | 실패한 claim transaction 이 lease-expiry sweep 과 exhaustion sweep 까지 rollback 한다. 죽은 worker 의 lease 가 `leased` 로 고정되고 `attempts >= max` 인 row 가 `dead_letter` 로 못 넘어간다. `F17-1`·`F17-2` 와 겹치면 회수 경로가 이중으로 막힌다 |
| `F17-8` | regression | B-P2 | `cli.py:800` | wave 10 이 새로 놓은 **세 번째** `sqlite3.Error` 포획에 test 가 없다. 지워도 suite 가 전부 통과한다. `cli.py:751`·`781` 을 지우면 각각 4건으로 죽는 것과 대조된다 — 기존 test 4종이 `committed_decision()` 단계에서 이미 터져 안쪽에 도달하지 못한다 |

## Three Roots

**뿌리 1 — guard 를 좁게 쓰고 그 밖을 안 셌다.**

`F17-1` 과 `F17-2` 가 같은 자리에서 나왔다. `FR-1` 을 닫으려고 넣은 `state` 조건이 "0행 = 다른
worker 가 가져갔다" 만 가정하는데, 실제로 0행이 나오는 상태는 더 많다.

| durable state | 0행 | wave 10 의 해석이 맞나 |
|---|---|---|
| `leased`, lease 살아 있음 | 예 | 부분적으로 맞다 — 그 worker 도 rollback 한다 |
| `leased`, lease 만료 | 예 | **틀리다. 소유자가 없다** (`F17-1`) |
| `dead_letter` | 예 | 무해 — 이미 치워졌다 |
| `completed`/`recovery_hold` | 예 | 도달 경로 못 찾음 |
| generation 불일치 | 예 | 도달 경로 못 찾음 |

**뿌리 2 — 고침은 들어갔고 그 고침을 지키는 test 는 부분적이다.**

`F17-3` 과 `F17-8` 이다. round 15 `R-1`, round 16 `R16-2` 와 **같은 형태로 세 라운드째**다.
mutation 표에서 9개 중 3개가 SURVIVED 했다.

| mutation | 판정 |
|---|---|
| `claim_generation = ?` → `? IS NOT NULL` | **SURVIVED** |
| `state IN (...)` 조건 삭제 | **SURVIVED** |
| `cleared` 검사 → `if False:` | KILLED |
| write 실패 `raise` → `pass` | KILLED |
| `is_unreadable()` 항상 False | KILLED |
| CLI `sqlite3.Error` 제거 — `cli.py:751` | KILLED |
| CLI `sqlite3.Error` 제거 — `cli.py:781` | KILLED |
| CLI `sqlite3.Error` 제거 — `cli.py:800` | **SURVIVED** |
| `_OPEN_FAILURES` 되돌리기 | KILLED |

**뿌리 3 — evidence 규율을 지적한 wave 가 같은 것을 다시 냈다.**

`F17-4`·`F17-5`·`F17-6` 이다. `T034` 가 `C16-3`(선언한 evidence 부재)과 `A16-5`(주장만 있고
실측 없음)를 지적하면서 `T031`·`T033` 이 각각 그 둘을 재생산했다.

## Verified Clean

- **정상 경로 dead-letter.** 손상 row 를 `dead_letter` 로 옮기고 다음 row 를 claim 하며 `unreadable()` 에 나타난다.
- **연속 손상 row 2개.** 한 호출 안에서 순서대로 치우고 진행한다. 무한 루프 없다.
- **2-worker race.** 살아 있는 lease 를 지우지 않는다. `FR-1` guard 가 실제로 작동한다.
- **`cleared` set 수명.** `claim_next` 호출 안에서만 산다. worker lifetime 아니다. transient 실패에서 회복하려면 이 설계가 맞다.
- **CLI 의 `sqlite3.Error` 포획 범위.** 두 read-only command 의 try block 뿐이고 전부 `_fatal` → exit 1 이다. 조용히 성공을 반환하는 경로가 없다. 7 상태 실측에서 정상 store 만 exit 0.
- **`_finalize` 와 retry/DLQ 계약.** wave 10 의 `IngressError` 변환은 UPDATE 앞에 있어 상태 전이를 우회하지 않는다.
- **계약 구분 유지.** `interaction-feedback.md:47-54` 의 "we know a decision landed" vs "we cannot tell" 이 유지된다. `get()` signature 무변경, `committed_decision` 의 보수적 침묵은 `D-042` 대로다.
- **round 15 의 `F-1`·`C-1`·`R-1` 셋 다 닫혀 있다.** 각각 실행 확인. `R-1` 은 mutation M7 이 2건으로 KILLED — 되돌린 것이 test 로 묶여 있다.
- **test 삭제 없음.** wave 10 관련 commit 셋에서 `-def test_` 0건.
- **전 suite** `1382 passed, 4 deselected`. ruff·mypy·verify 7/7.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A17-1` | `ingress.py:411` | generation 주석이 코드와 반대다. "`generation` 은 이 claim 이 쓴 값(`+1`)" 이라 쓰였으나 실제로는 SELECT 가 읽은 claim **이전** 값이다. 결론은 맞고 전제만 틀렸다. 두 reviewer 가 독립으로 같은 것을 잡았다 |
| `A17-2` | `cli.py:806` | 없는 command_id 도 `NONE` 으로 나온다. `is_unreadable()` 이 row 없음에 `False` 를 낸다. 계약이 다루는 축은 아니다 |
| `A17-3` | `cli.py:746-753`, `777-784`, `790-798` | `ValueError` 와 `sqlite3.Error` 를 같은 tuple 로 잡아 caller 결함(`limit=0`)과 환경 장애(DB 손상)가 둘 다 exit 1 이다. script 로 구분할 수 없다 |
| `A17-4` | `ingress.py:456-482` | docstring 이 "0행이면 그 사실을 호출자에게 알려야 한다" 고 쓰는데 함수는 `rowcount` 를 읽지 않고 `None` 을 낸다. 진행 판정은 독립된 `cleared` set 이 한다 |
| `A17-5` | `cli.py:716`, `tests/test_governance_store.py:1177,1186,1220` | "legacy_*.py **아홉** handler" 가 문서 넷에 있는데 **실측 13** 이다. 두 방법으로 셌고 둘 다 13 (AST 로 `sqlite3.Error`/`DatabaseError`/`OperationalError` 를 잡는 `ExceptHandler`, 그리고 grep). 파일 수도 다섯이라 아홉은 파일 수도 handler 수도 아니다. 결정 자체는 안 바뀐다 |

## Not Checked

- `F17-1` 이 production 에서 생기는 경로. `leased` 인 동안 row 가 손상되는 구체 경로를 코드에서 특정하지 못했고, 그것을 막는 invariant 도 못 찾았다. `D-045` 자체가 "읽을 수 없는 row 는 생긴다" 를 전제로 존재한다.
- `_dead_letter_unreadable` 을 실제 sqlite 오류(disk full, read-only)로 실패시키지 않았다 — 예외 주입으로 대체했다.
- `+12` test 의 commit 단위 귀속. wave 10 이 독립 commit 이 아니라 diff 로 분리하지 못했다.
- `T034` 의 내역 주장("wave 8 이 10 + T028 이 5 + T032 가 3"). 합과 총계 18/21 은 두 방법으로 재현했으나 세 갈래 귀속은 git 에 없다.
- `cleared` set 재조우 `IngressError` 의 thread race. 실제 interleaving 을 강제하지 못했다.
- mutation 표(T031 4/4, T032, T033 3/3)의 재실행.
