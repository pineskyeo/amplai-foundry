# Three-Lens Review — Round 18 (MGC-012-P5 Wave 11)

**판정: FAIL.** blocker 5건 — P0 0 / P1 1 / Blocking-P2 4. Advisory 12.

Target: `evidence/review-target-round-18.txt`, 79 파일, aggregate
`1e73c67229788184f3bed599f4c5a688984078c9d48244290df13828f71e9dd7`.
세 reviewer 가 착수·종료에 확인했고 regression reviewer 는 mutation 10회 전후로도 확인했다.
**78건 무손상.** 나머지 1건은 `N18-4` 다 — freeze 절차 자체의 결함이라 blocker 로 잡았다.

**round 17 대비 8건 → 5건.** P1 이 셋에서 하나로 줄었다.

## Round 17 Blockers: Seven Closed, One Partial

세 reviewer 가 각각 **실행으로** 확인했다.

| round 17 | 등급 | 판정 | 확인 방법 |
|---|---|---|---|
| `F17-1` | P1 | 닫힘 | 손상 row 를 durable `leased`+만료로 못박고 `claim_next` 실행 → 뒤 command 반환, poison 은 `dead_letter`. mutation 2종(sweep 호출 삭제, sweep 을 transaction 안으로)으로 되돌리면 각각 2건·3건 FAIL |
| `F17-2` | P1 | **부분** | 기법은 맞고 M4 로 고정돼 있다. **다만 `limit` 창 밖의 손상 row 를 못 본다 — `N18-1`** |
| `F17-3` | P1 | 닫힘 | guard 둘을 각각 mutate → **서로 다른 test** 가 죽는다. round 17 의 핵심 지적이 닫혔다 |
| `F17-4` | B-P2 | 닫힘 | 선언 206개를 독립 집계, 미충족 7건은 전부 `T020`(superseded, `required_evidence_present: false`)이라 정합적 |
| `F17-5` | B-P2 | 닫힘 | `T033.md` 에 AC-02·AC-04·AC-05 실재. AC-02 의 표를 AST 로 독립 재현 — `legacy_*.py` 10, `src` 14, handler 13 |
| `F17-6` | B-P2 | 닫힘 | `T031.md` 에 AC-07·AC-12 실재, AC-03 이 자기 실행으로 대체됐고 **reviewer 가 그 실행을 재현했다** |
| `F17-7` | B-P2 | 닫힘 | clear 를 실패시켜 `claim_next` 를 `IngressError` 로 끝내도 만료 lease 회수가 `retry_wait`+`INGRESS_LEASE_EXPIRED` 로 durable 하게 남는다 |
| `F17-8` | B-P2 | 닫힘 | 안쪽 tuple 에서 `sqlite3.Error` 제거 → 해당 test 가 "raw traceback 이 나갔다" 로 죽는다 |

## New Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `N18-1` | contract + failure | **P1** | `ingress.py:589-624`, `cli.py:745` | `unreadable(limit=N)` 이 `LIMIT` 을 **전체 durable row** 에 걸고 그 다음 `_view` 로 거른다. `received_at, command_id` 오름차순이라 오래된 `completed` row 가 앞을 채운다. 손상 row 앞에 N 개 이상이 있으면 `governance stranded` 가 **`NONE`** 을 낸다. `_stranded_rows` 는 SQL 에서 state 로 먼저 걸러 이 truncation 이 없다. **`D-047` 의 Consequence 가 command 100건 이후 거짓이 된다.** 두 lens 가 독립으로 잡았다 |
| `N18-2` | contract | B-P2 | `ingress.py:380-386` vs `:530-533` | 손상 row 가 `leased`+만료+`attempts >= max` 이면 `_sweep_recoverable` 의 둘째 UPDATE 가 바로 `dead_letter` 로 옮긴다(첫째 UPDATE 가 `retry_at = now` 를 쓰므로 같은 transaction 에서 `retry_at <= now` 가 참). `_dead_letter_unreadable` 에 도달하지 않아 `last_error_code` 가 **`INGRESS_LEASE_EXPIRED`** 로 남는다. `D-045` 는 `INGRESS_COMMAND_UNREADABLE` 을 요구한다. **`ingress.py:513-524` 의 "전수" 표에 이 경로가 없다** |
| `N18-3` | contract | B-P2 | `T029.yaml:93` 외 5 파일 21줄 | `A17-5` census 가 `src/`·`tests/` 로 **조용히 좁혀졌다**. `T037.md` 는 "전수로 세니 여덟" 이라 쓰고 범위를 밝히지 않는다. spec 쪽에 21줄 6파일이 아직 아홉을 현재 값으로 주장하고, `T029.yaml:93` 은 `invariants:` 아래 **현재형 규범 문장**이다. `work-contract.json:112` 의 AC-010 은 "문서 **전부**" 였다 |
| `N18-4` | contract | B-P2 | `evidence/review-target-round-18.txt` | freeze manifest 가 `evidence-trace.jsonl` 의 **4줄 시점 hash** 를 담았는데 파일은 7줄이다 — **freeze 뒤에 trace 를 더 썼다.** 재확인 script 가 그 한 건에서 assert 실패한다. 그리고 aggregate 는 **manifest 행 문자열**만 해싱하므로 어떤 파일 내용 변조도 못 잡는다 — **aggregate 일치가 무손상 증거가 아니다** |
| `F18-R1` | regression | B-P2 | `ingress.py:332`, `:349` | **`_sweep_recoverable()` 자체의 실패 경로에 test 가 없다.** `T035` 가 "새 구조가 요구한 것" 다섯 중 **첫째**로 스스로 적은 요구다. mutation M8(sweep 호출을 `try/except: pass` 로 감쌈) → **SURVIVED**. 기존 `..._filesystem_failure_in_claim_next_...` 는 `window=(1, 10**6)` 으로 store 를 통째로 막아 sweep 이 삼켜도 뒤이은 `_claim_one` 이 대신 실패한다. **round 17 `F17-8` 과 같은 형태** — 넓게 망가뜨리는 test 가 안쪽 지점에 도달하지 못한다 |

## Two Roots

**뿌리 1 — 새 구조가 요구하는 것을 또 덜 셌다.**

`N18-1`·`N18-2`·`F18-R1` 셋이 wave 11 이 넣은 두 구조에서 나왔다.

| 구조 | 놓친 것 |
|---|---|
| `unreadable()` 을 자기 조회로 넓힘 | `limit` 의 의미가 "후보 상한" 에서 "표 상한" 으로 바뀐 결과 (`N18-1`), `limit < 1` guard 의 test (`F18-R2`) |
| `_sweep_recoverable()` 분리 | sweep 이 만든 새 상태 전이 경로 (`N18-2`), sweep 자체의 실패 test (`F18-R1`) |

`T035` evidence 는 "새 구조가 요구한 것 다섯" 을 세었고 그중 **다섯째를 실측으로 찾아 적었다.**
그런데 **그 목록 자체가 여전히 덜 셌다.** `F18-R1` 은 그 목록의 **첫째 항목**이고, 처리를
적었으나 test 를 안 만들었다.

**뿌리 2 — 세는 범위를 밝히지 않았다.**

`N18-3` 이다. `T037` 이 "전수" 라 쓰고 `src/`·`tests/` 만 훑었다. round 17 이 "넷" 이라 한
것을 "여덟" 로 정정하면서, 정정 자체가 또 부분이었다.

## Verified Clean

- **round 17 의 세 SURVIVED 가 전부 잡힌다.** regression lens 가 M1~M5 를 독립 재현했고
  다섯 다 KILLED, 죽은 test 이름과 건수까지 `T036` 주장과 일치한다. **세 라운드 연속
  이어지던 "고침은 들어갔고 test 는 부분적" 패턴이 닫혔다.**
- **`M1` 과 `M2` 가 서로 다른 test 를 죽인다.** 교차 확인했다 — M1 은 state 가 `pending` 인
  시나리오라 state guard 를 지워도 통과하고, M2 는 generation 이 맞는 시나리오라 generation
  guard 를 지워도 통과한다.
- **filesystem-guard window 이동이 정당하다.** window 1~12 를 실측으로 훑었다.
  `..._finalize_transition_...` 은 **7·8 에서만**, `..._exhausted_retry_...` 는 **7~10 에서만**
  통과한다. 후자의 구간이 주석이 적은 `committed_decision()` 범위(7-10)와 정확히 같다 —
  **때리는 지점이 그대로다.** 창만 옮겨 통과시킨 것이 아니다.
- **`_dead_letter_unreadable` guard 의 state 표.** contract lens 가 **세 번째 방법**(durable
  row 를 6 state 로 못박고 raw SQL rowcount 를 rollback 하며 관찰)으로 독립 재현했다.
  `ingress.py` 표와 완전히 같다.
- **`A17-5` 의 두 계수.** AST 로 `legacy_*.py` 의 `try` **10**, `except` handler **13**,
  `src` 전체 `try` **14**. 한 줄 grep 이 12 를 내는 것도 재현했다. `store.py` 의 줄번호 23개가
  전부 정확하다.
- **durable state 전수 × 큐 진행.** 손상 row 를 6 state × attempts 1·5·9 로 못박고 실제
  `claim_next` 를 태웠다. **8 조합 전부** 뒤 command 를 claim 했다. 큐가 막히는 조합 0개.
- **sweep 자체가 실패해도 되돌릴 수 없는 상태가 남지 않는다.** `disk I/O error` 주입 →
  durable state 무변경, 복구 후 다음 호출이 정상 claim.
- **operator hold 와 살아 있는 lease 를 sweep 이 우회하지 않는다.**
- **4-worker 동시 claim, head 가 손상.** 1명만 성공, 예외 0건, 중복 치우기 0건.
- **`_finalize` 가 sweep 을 우회하지 않는다.** 늦게 깨어난 worker 의 `complete()` 는
  `INGRESS_LEASE_CONFLICT` 로 막힌다.
- **`governance decision` 은 truncation 이 없다.** id 단위 조회라 `N18-1` 의 영향을 안 받는다.
- **`stranded()` 계약 무변경**, `get()` signature 무변경, `committed_decision` 의 보수적
  침묵 유지.
- **test 삭제 0건.** `git diff -- tests/ | grep -c "^-def test_"` = 0. 제거된 `assert` 도 0.
- **test 수 두 방법.** `--collect-only` 1388, 전체 실행 `1388 passed, 4 deselected`.
  round 17 의 1382 대비 **+6** 이고 diff 의 `+def test_` 도 6이다.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A18-R1` | mutation 절차 전반 | **`.pyc` 캐시가 mutation 결과를 위조한다.** test 파일을 같은 크기로 1초 안에 다시 쓰면 CPython 의 mtime+size 기반 무효화가 변경을 감지하지 못해 직전 컴파일 결과가 재실행된다. regression lens 가 pristine 값에서 FAILED 를 얻고 원인을 특정했다. **round 17·18 이 mutation 을 근거로 삼으므로 무겁다.** 다음 라운드는 `PYTHONDONTWRITEBYTECODE=1` 을 쓰거나 `__pycache__` 를 지운다 |
| `F18-R2` | `ingress.py:608-609` | `unreadable()` 의 `limit < 1` guard 에 test 가 없다 (M9 SURVIVED). `stranded(limit=0)` 만 치는 test 하나뿐이다. guard 가 없으면 `LIMIT 0` 이 빈 결과를 내 거짓 음성이 된다 |
| `A18-1` | `cli.py:736` | 손상 `completed` row 가 `governance stranded` 의 `UNREADABLE` 줄에 뜬다. 그 command 의 docstring 은 "no worker will claim again" 이다. `D-047` 을 문자 그대로 따른 결과지만 회수가 필요 없는 row 가 회수 목록에 섞인다 |
| `A18-2` | `T033.md:56-57` | `src` 전체 14 중 `store.py` 를 `265, 286` 으로 인용하는데 실측은 `275, 296` 이다. wave 11 이 `store.py` 머리말을 늘린 뒤 인용을 갱신하지 않았다. 나머지 12개는 정확하다 |
| `A18-3` | `ingress.py:332`, `349-386` | 빈 큐 poll 1회의 `connect()` 가 **2회**(sweep + claim)다. sweep 은 조건 없이 매 loop 반복마다 write transaction 을 연다. single-writer sqlite 에서 idle worker fleet 의 write 경합이 는다. 정확성 문제는 아니고 비용 측정 기록이 없다 |
| `A18-4` | `ingress.py:371-377` | sweep 이 `last_error_code` 를 조건 없이 덮는다. 앞선 attempt 의 `DECISION_COMMITTED_UNRECONCILED` 를 덮는 것을 실측했다. **wave 11 이 새로 만든 것은 아니다** — 같은 UPDATE 가 전에는 claim transaction 안에 있었다 |
| `A18-R2` | `test_slack_ack_boundary.py:2287` | `..._filesystem_failure_in_claim_next_...` 의 docstring 이 "세 지점 중 첫째(`ingress_worker.py:186`)" 라 하는데, sweep 이 밖으로 나오면서 `window=(1, 10**6)` 이 **먼저 sweep 의 connect 를 때린다.** `_claim_one` 자체의 실패가 이제 독립적으로 고정되지 않는다. `F18-R1` 과 같은 뿌리 |
| `A18-R3` | `test_slack_ack_boundary.py:2783` | `..._does_not_roll_back_another_rows_recovery` 만 `monkeypatch` 대신 class attribute 직접 대입을 쓴다. 형제 test 와 다르다 |
| `A18-5` | `cli.py:734` | `governance stranded` 의 `--limit` 에 `min=1` 이 없다. 같은 파일 `:199` 에는 있다 |
| `A18-6` | `events.py:3035-3059`, `:3151`, `:3348` | **형제 위치.** `OutboxDispatcher.claim_next` 가 sweep 둘을 claim transaction 안에 그대로 두고 있다 — round 17 `F17-7` 과 같은 형태다. `_move_exhausted` 는 소진 row **전부**를 view 로 만들므로 하나만 못 읽어도 claim 전체가 rollback 된다. 사용자가 범위 밖으로 정했다 |
| `A18-7` | trace 순서 | `review_target_frozen`(seq 7)이 manifest 작성 이후에 쓰였고 `verify_pass`(seq 5) timestamp 도 manifest 보다 늦다. `N18-4` 와 같은 뿌리 |
| `A18-8` | `T033.md` | AC-02 표의 `store.py` 줄번호 외에는 전부 정확하다 (교차 확인됨) |

## Not Checked

- `A18-6` 의 outbox 도달 경로. corrupt vector 7개를 시도해 전부 막혔다 — **"막혀 있다" 를
  증명한 것이 아니라 "뚫지 못했다"** 다. `payload_json` 은 immutability trigger 가,
  나머지는 CHECK 가 막았다.
- `apply_jobs.py:867` 의 같은 형태 sweep. 얼린 target 밖이다.
- 실제 sqlite 장애(disk full, read-only, WAL 손상)로 sweep·치우기를 실패시키기. 전부 예외
  주입으로 대체했다 — round 17 의 같은 항목이 그대로 남는다.
- sweep commit 과 `_claim_one` transaction 시작 **사이**의 실제 interleaving 강제.
- `_sweep_recoverable` 의 두 UPDATE 사이에 process 가 죽는 경우. 한 transaction 이라 atomic
  하다고 읽었고 실행으로는 확인 못 했다.
- `unreadable()` 의 실제 지연 비용.
- `N18-2` 의 도달성. "readable 인 채 `max_attempts` 만큼 claim 되고 그 뒤 손상" 이라는 좁은
  순서를 요구한다 — 그 순서가 실제로 일어나는지는 확인 못 했다.
- M3·M4 를 contract lens 는 안 돌렸다 (regression lens 가 돌렸다).
- ruff / mypy / verifier 7 check 를 regression lens 는 안 돌렸다 (별도로 PASS 확인됨).
