# Three-Lens Review — Round 15 (MGC-012-P5 Wave 8)

**판정: FAIL.** blocker 9건 — P0 1 / P1 4 / Blocking-P2 4. Advisory 5.

Target: `evidence/review-target-round-15.txt`, 32 파일, aggregate
`50ce748f871b2270f8b101d7c5b9f60680240fdf42935a421be26ec7aa8ec27e`.
세 reviewer 가 각자 착수 시점에 digest 를 확인했고 regression reviewer 는 종료 시점에도
재확인했다. **review 중 무손상.**

## Process Note

첫 시도에서 contract·failure/recovery 두 reviewer가 세션 token 한도로 죽었다. 판정 없이
죽었으므로 gate 를 기록하지 않았다. 재실행 시 셋을 **순차로** 돌리고 각 reviewer 에게 예산
규율을 명시했다 — `events.py`(3494줄), `test_slack_http.py`(3553), `test_slack_ack_boundary.py`
(2384), `test_governance_events.py`(1918)를 통째로 읽지 말고 `grep -n`·`sed -n` 을 쓰라는
지시다. 첫 시도가 정확히 거기서 죽었다.

regression lens 는 단독으로 돌렸다. 그 lens 만 소스를 mutate 하며 측정하므로 다른 둘과
동시에 돌리면 서로의 결과를 오염시킨다.

## Blockers

| id | lens | 등급 | 위치 | 결함 |
|---|---|---|---|---|
| `F-1` | failure | **P0** | `ingress_worker.py:186` ← `ingress.py:335,492` | `claim_next` → `_view` 가 손상 row 에서 `JSONDecodeError`(=`ValueError`)를 낸다. `process_next` 의 tuple 은 `(GovernanceStoreError, IngressError, sqlite3.Error)` 뿐이다. `_view` 가 transaction **안**이라 claim 이 rollback 되어 row 가 `pending`·`attempts=0` 으로 영구히 남고, 뒤에 들어온 command 전부가 막힌다. `stranded()` 에도 안 보인다 |
| `C-1` | contract | P1 | `cli.py:686,704,744` | T026 AC-07 미충족. `check_startup()` 이 `connect()` **밖에서** `filesystem_guard.validate()` 를 부르고, 거기서 나는 `GovernanceFilesystemError` 는 두 command 의 except tuple 에 안 걸린다. raw traceback 이 나간다 |
| `F-2` | failure | P1 | `ingress.py:382`, `cli.py:703` | 같은 `_view` 가 `stranded()` 안에서도 터진다. 손상 row 하나가 목록 전체를 없애고, CLI 는 `command_id` 도 읽힌 row 수도 없이 파싱 오류만 낸다 |
| `F-3` | failure | P1 | `cli.py:686` → `store.py:405-422` | `check_startup()` 이 `BEGIN IMMEDIATE` + `UPDATE` (WAL/SHM 쓰기 probe) 를 한다. worker 가 write transaction 을 쥐면 두 command 가 `busy_timeout` 뒤 실패한다 — 회수 도구가 정확히 필요한 순간에 안 된다 |
| `R-1` | regression | P1 | `store.py:205-238` → `legacy_*.py` 9곳 | T024 정규화가 open 단계 `sqlite3.Error` 를 `GovernanceStoreError` 로 바꿨다. `legacy_lifecycle:157`, `legacy_migration:823,1069,1114,1169`, `legacy_recovery:206,415`, `legacy_rollback:180,549` 가 `sqlite3.Error` 를 잡아 domain error 로 닫고 있었고 그 봉쇄가 사라졌다 |
| `C-2` | contract | B-P2 | `evidence/MGC-012-P5-T026.md:86` | "traceback 을 내보내지 않는다" 가 거짓. 같은 결함을 T024 evidence 가 `T024-A1` 로 기록하며 "cli.py 는 T024 범위 밖" 을 면제 사유로 달았는데, **cli.py 는 T026 범위 안**이다 |
| `C-3` | contract | B-P2 | `cli.py:698,731`, `tests/test_cli.py:107,133` | T026 `scope.exclude` 가 "새 출력 형식(JSON 등)의 계약화" 를 금지했는데 `--json` 을 넣고 key 집합을 test 로 고정했다. manifest 에 확장 기록 없음 |
| `F-4` | failure | B-P2 | `cli.py:67-68,667` | help 와 주석이 "Read-only … No governed mutation", "**조회만이다.**" 인데 매 호출이 배타 write lock 을 잡고 `UPDATE` 를 낸다 |
| `F-5` | failure | B-P2 | `slack_http.py:388` docstring | T021 근거가 `SlackTransportError` 경로에서 거짓이다. `_call` 이 `from None` 으로 새 예외를 만들고 `_sanitized_transport_error` 가 `clear_exception_frames` 를 부르므로 `_send` frame 이 없다. 실측 traceback `[post_message, _call]` |

## Two Roots

**뿌리 1 — 정규화를 `connect()` 에만 걸었다.** T024 의 논지는 "경계에서 정규화해 호출자를
세지 않는다" 였다. 그런데 `_view` 의 `ValueError` 는 그 경계 밖이다. T024 evidence 는
`_replayed_decision` 에서 **같은 모양**을 `T024-F1` 로 기록해 놓고 `claim_next` 쪽을 찾지
않았다. 형제를 끝까지 세지 않은 것이 여섯 라운드째다.

그리고 반대 방향으로도 지나쳤다 — `sqlite3.Error` 까지 정규화해 `legacy_*.py` 9곳의 봉쇄를
없앴다 (`R-1`). "호출자를 하나도 안 건드렸다" 를 장점으로 적었는데 그것이 결함이었다.

**뿌리 2 — `check_startup()` 이 읽기 전용이 아니다.** `PRAGMA integrity_check`(전체 스캔),
migration verify, WAL 쓰기 probe 를 한다. 그것을 확인하지 않고 "조회만" 을 help 와 주석 두
곳에 썼다. `C-1`·`C-2`·`F-3`·`F-4` 가 전부 이 하나에서 나온다.

## Verified Clean

세 reviewer 가 실측으로 확인한 것. 다음 라운드에서 다시 볼 필요 없다.

**수치 (contract lens 가 독립 재계수).** 72성분 / 8블록, or-chain 0개,
`clear_exception_frames` 호출 14곳·정의 1개, 전 suite `1358 passed, 4 deselected`.
**다섯 라운드 연속 틀리던 수치가 이번엔 전부 맞다.**

**P0-1 폐쇄 실측.** `slack_http.py:590` `body = None` 을 지우면
`test_http_interruption_traceback_contains_no_button_or_bot_credential`(wave 6 부터 있던
test)과 신규 `..._leaves_the_send_frame` 둘이 죽는다. masking 이 무엇을 가렸는지의 직접 증거다.

**T023 등가성 (regression lens).** 여덟 지점 전부 HEAD 의 or-chain 과 field 집합·순서·비교
대상 expression 이 일치한다. dict literal 이 삽입 순서를 보존하고 helper 가 첫 불일치에서
raise 하므로 short-circuit 순서와 결과 code 가 같다. eager evaluation 위험도 없다 —
`decision_commands[command_id]` 네 지점 모두 같은 block 바로 앞 줄에서 대입한다.

**T023 의 mypy 보호는 실재한다.** call site 에 오타 key 와 잘못된 값 type 을 심어 측정:
`typeddict-unknown-key` 와 `typeddict-item` 두 error 가 난다. `total=False` 라 *누락* 은 못
잡지만 HEAD 의 or-chain 도 못 잡았다 — 등가다.

**credential 누출 0 (failure lens).** canary 를 심고 `{network OSError, HTTP 500,
KeyboardInterrupt, SystemExit, GeneratorExit, 직렬화 불가 payload} × {post_message,
post_ephemeral}` 9경우에서 `__cause__`/`__context__` 를 훑고 `/src/amplai_foundry/` frame 의
`f_locals` 를 `str`·`bytes`·`dict` 와 `Request.headers`·`unredirected_hdrs` 를 **객체로**
검사했다. `leaks=[]`. 검출기는 음성 대조로 검증했다 — 같은 `Request` 를 든 합성 frame 을
`/src/amplai_foundry/` 이름으로 컴파일하니 잡아낸다.

**`finally` 의미 (failure lens).** 새 `finally` 셋은 전부 단순 대입이라 예외를 낼 수 없고
in-flight 예외를 대체하지 않는다. `KeyboardInterrupt`/`SystemExit`/`GeneratorExit` 각각에서
예외 identity 가 보존되고 비우기가 실행됨을 확인했다.

**T024 `yield` 경계 (failure lens).** body 가 `Marker`/`OSError`/`sqlite3.OperationalError`/
`GovernanceFilesystemError`/`KeyboardInterrupt` 를 던질 때 전부 `e is exc` 로 그대로
전파되고 connection 이 닫힌다. `BEGIN IMMEDIATE` 중 실패해도 write lock 이 풀린다.

**T022 (regression lens).** 14 call site 전부 유지, 병합 body 가 네 사본과 문자 단위로 동일,
`decisions` 가 `slack_*` 를 하나도 import 하지 않아 cycle 없음, import 시점 side effect 없음.

**T021 정보 손실 없음 (regression lens).** 지운 `except SlackTransportError` arm 이 지키던
`__cause__=None`/`__suppress_context__=True` 는 `_call` 이 이미 만든다 — 세 진입점에서
`cause=None suppress=True token=False` 실측. 지운 `BaseException` arm 의 두 호출도 `_send` 가
같은 상수로 이미 한다. `post_ephemeral` 은 `text` 를 항상 비우게 되어 **오히려 강해졌다.**

**test 약화·삭제 0 (regression lens).** `git diff HEAD -- tests/` 의 `-` 줄은 file header
넷뿐이고 삭제된 assertion 이 0 이다.

**T025 drift 없음 (contract lens).** `spec.md:19-26` Clarifications 원문이 diff 에 없다.
`_SILENT_HOLD_CODES` 가 `INGRESS_DECISION_COMMITTED_UNRECONCILED` 하나뿐이고 `_safe_outcome`
fallthrough 가 `UNAVAILABLE` 이라 계약 표의 새 단서가 코드와 정확히 일치한다. `specs/` 안에
"no attempt ever completed" 잔재 0건. MUST 약화 없음.

**forbidden_paths 위반 0.** `vault/`, `.github/`, `filesystem.py`, `migrations.py`,
`ingress.py`, `ingress_worker.py` 전부 미수정.

**T022 의 allowed_paths 확장은 적법하다 (contract lens).** `review_cards.py` 는 호출 한 줄과
import 한 줄뿐이고 사유를 manifest 에 적었다. 위임 staticmethod 를 남기면 이 task 가
없애려는 사본이 된다는 논거가 맞다.

## Advisory

| id | 위치 | 내용 |
|---|---|---|
| `A-1` | `evidence/T021.md:57-61` | "중복이었다" 가 `_call` **내부** interruption 에만 성립한다. `_require_text`(try 안, `_call` 밖)의 non-standard BaseException 은 예전엔 `RuntimeError` 가 됐고 지금은 그대로 나간다. 실질 도달성은 사실상 0 이지만 문장이 사실보다 넓다 |
| `A-2` | `slack_http.py:503` | docstring 이 T022 가 지운 사설 이름 `_clear_exception_frames` 를 가리킨다 |
| `A-3` | `BACKLOG.md` `MGC-017` +49줄 | frozen target 안이지만 여섯 manifest 어느 allowed_paths 에도 없다. 스스로 별건임을 밝혔고 docs 라 조용한 위반은 아니다 |
| `A-4` | `plan.md:168-199` | T025 의 allowed_paths 안이지만 `scope.include` 는 `plan.md:117-118` 만 지목했다. Source tree 갱신은 범위 밖이다 |
| `A-5` | `evidence/T022.md:96` | 호출 위치를 `decisions.py:199`·`:289` 로 적었으나 실제 두 번째는 `:322` |

## Not Checked

- **착수 전 기준선 수치** — T021 무방비 4/7, T022 정의별 sweep, T023 62/72, T024 raw 누출
  6/7. 착수 전 코드 재현이 필요하고 성분당 전 suite 라 세 reviewer 모두 재계수하지 않았다.
  **내 실측만 있고 독립 확인이 없다.**
- T023 after-sweep 의 72/72 killed — key 하나씩 지우는 72회를 reviewer 는 안 돌렸다. AST test
  가 전 key 를 고정한다는 것만 코드로 확인했다.
- `R-1` 의 9개 지점 중 `legacy_recovery.py:206` 만 실행 재현. 나머지 8곳은 handler tuple 정적
  전수 (동일 pattern·원인).
- `F-1` 은 SQL 로 손상 row 를 만들어 재현했다. schema migration 이 손상 없이 읽을 수 없는
  `channel_json` 을 만들 수 있는지는 확인하지 않았다.
- T022 14 call site 중 12곳(renderer 쪽 raw ActionToken 경로)은 failure lens 가 probe 하지
  않았다.
- 동시 worker race (두 worker, 한 poison row).
- `events.py` 의 delivery worker 실패 경로 — wave 8 diff 밖.
