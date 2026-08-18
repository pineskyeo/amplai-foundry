# Three-Lens Review — Round 13

target aggregate: `9573d51c632f4806803e20ef92d229bf9a5a710727f778bb87f5bbe2b03b9349`
target 기록: `evidence/review-target-round-13.txt`
date: 2026-08-16

**결과: FAIL.** gate 를 열지 않는다.

## Counts

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 3 | 1 |
| Failure / Recovery | 0 | 1 | 1 | 3 |
| Regression | 0 | 1 | 3 | 3 |
| **합계** | **0** | **2** | **7** | **7** |

round 12 대비: P1 1 → 2, Blocking-P2 8 → 7.

tree 무손상 확인 — regression lens 가 src 를 mutate 했으나 aggregate 가 일치한다.

## The One Pattern Behind Most Of This

**네 라운드 연속 같은 실수다. 패턴 하나를 고칠 때 형제 위치를 세지 않는다.**

| round | finding | 고친 것 | 놓친 것 |
|---|---|---|---|
| 11 | `R-1` | `slack_projection` 사본 | `slack_http` 사본 |
| 12 | `F-2` | `post_message` | `read_history`·`post_ephemeral`·`delete_message` |
| 12 | `RL-3`~`RL-5` | 지목받은 성분 3개 | 같은 `if` 의 나머지 2개 |
| **13** | **`F-1`** | **`BaseException` arm** | **`SlackTransportError` arm** |

wave 6 evidence 는 "사본을 세는 대신 셀 필요가 없게 만들었다" 고 적었다. **절반만 사실이다.**
`_call` 안에 넣은 것은 `BaseException` guard 뿐이고, 흔한 실패(연결 거부·타임아웃·DNS·TLS)가
타는 `SlackTransportError` 경로는 여전히 호출자마다 따로다.

## Blockers

### F-1 (regression) — P1 — `slack_http.py:445`, `:471`

**bot token 이 traceback 에 실제로 샌다.** unmutated shipped code 다.

`post_message` 와 `post_ephemeral` 은 `except SlackTransportError` 에서
`_sanitized_transport_error()` → `_clear_exception_frames()` 를 돌린다. `read_history` 와
`delete_message` 는 `_call` 을 맨몸으로 부르고 sanitize 하지 않는다. `_fail()` 이
`... from error` 로 올리므로 원본 urllib frame 이 `__cause__` 로 살아 있다.

실측 (real urllib, connection refused — DNS·TLS·timeout 이 전부 이 arm 을 탄다):

```
post_message    leak=[]
read_history    leak=[('open','fullurl'),('open','req'),('_open','req'),
                      ('http_open','req'),('do_open','req'),('do_open','req')]
post_ephemeral  leak=[]
delete_message  leak=[... 동일 6 frame ...]
```

`capture_locals` 렌더러(= `--showlocals`, 구조화 log handler)로 출력하면:

```
read_history   token printed: True
   >> headers = {'Authorization': 'Bearer xoxb-test-token', ...}
delete_message token printed: True
```

round 12 `F-2` 가 서술한 그 누출이다. **드문 arm 에서 닫고 흔한 arm 에서 열려 있다.**

### P1-1 (failure) — P1 — `ingress_worker.py:444`

**`D-039` 가 추가한 `committed_decision()` 읽기가 worker 경로에서 유일하게 보호되지 않는다.**

그 호출은 `_finalize` 의 `try` **밖**에 있고, 안에서 connection 두 개를 새로 연다.
다른 모든 store 호출은 감싸져 있다.

실측:

```
attempt2 RAISED OUT OF process_next: OperationalError database is locked
state after: leased  attempts: 2  last_error: INGRESS_DECISION_UNAVAILABLE
feedback calls: []
reclaim attempt: None
```

**실패 모드가 상관돼 있다** — 재시도를 만든 그 조건(store busy)이 이 읽기도 실패시킨다.
raw 예외가 `process_next` 밖으로 나가 caller loop 를 죽이고, command 는 `attempts ==
max_attempts` 인 채 `leased` 로 남아 다시 claim 되지 않는다. **reviewer 는 아무것도 못 받는다** —
`R-3` 이 보장하려던 `unavailable` 도, `D-039` 가 의도한 침묵도 아니다. 무엇이었는지 알 수 없다.

`review-target-round-13.txt` 가 "측정하지 않았다" 고 적은 바로 그 잔여 위험이고, 측정하니
결함이었다.

`stranded()` 는 lease 만료 후 여전히 보여준다. operator 가시성은 살아남고 durable 종결과
통지는 죽는다.

### CT-1 (contract) — Blocking-P2 — `spec.md:133`

**FR-026 이 `MUST produce the unavailable safe outcome` 이라고 요구하는데 `D-039` 가 그것을
어긴다. FR-026 을 고친 Decision 이 없다.**

F-1 시나리오는 FR-026 두 번째 문장에 정확히 해당한다 — 마지막 attempt 가 **관측됐고** 실패가
**알려졌다**. 세 번째 문장(결과 미지)은 해당하지 않는다. attempt 1 이 완료했기 때문이다.

`D-039` 의 Scope audit 은 `ingress_worker.py` 와 test 파일만 바꾼다고 적었다 — spec 을 일부러
안 건드렸다. **`C-2` 사건의 거울상이다.** 지난 round 는 구현자가 Decision 없이 요구사항을
약화했고, 이번 round 는 Decision 없이 코드가 요구사항에서 벗어났다.

### CT-2 (contract) — Blocking-P2 — `contracts/interaction-feedback.md:33`

**계약은 종점을 둘로 적는데 코드에는 셋이다.**

`Two endings are possible and they are not the same.` 그리고 "worker 가 관측한 마지막 attempt
실패 → `unavailable`" 이라고 단정한다. 결정이 이미 기록된 부분집합에서 그 문장이 거짓이다.

```
grep -rn "COMMITTED_UNRECONCILED|committed_decision" specs/003-slack-proposal-card/ --exclude-dir=evidence
→ 0 hits
```

이 계약을 보고 구현하는 사람은 모든 관측된 소진에 `unavailable` 을 붙여 `D-039` 가 없앤 모순을
그대로 되살린다.

### CT-3 (contract) — Blocking-P2 — `task-manifests/index.yaml`

**wave 6 은 task manifest 도, index 항목도 없고, `done` manifest 의 기록된 scope 를 무효화했다.**

- `update_history` 마지막이 2026-08-14 (wave 5)다. wave 6 항목이 없다
- manifest 14개 전부 T001~T014 이고 wave 6 것은 없다
- `MGC-012-P5-T011.yaml` 은 `status: done` 인데 scope 에 "그 결말이 기존 `_with_feedback`
  경로를 타고 `unavailable` 을 내는지 고정한다" 고 적혀 있다. wave 6 이 정확히 그것을 바꾸고
  manifest 를 고치지 않았다

`AGENTS.md` 의 Pre-Implement Procedure(taskify → validator → tasks.md → analyze)가 wave 6 에
대해 산출물을 하나도 만들지 않았다. **`C-1` 과 같은 종류가 한 wave 뒤에 더 크게 났다.**

파일 수준은 깨끗하다 — 수정된 `src/`·`tests/` 경로는 전부 어떤 task 의 `allowed_paths` 안에 있다.

### F-2 (regression) — Blocking-P2 — `slack_http.py:544`

**credential 을 지우는 호출이 고정돼 있지 않다.** `_clear_exception_frames(error)` 를 지워도
1274건이 전부 통과한다. credential guard 인데 그것을 이름에 건 test 가 실패할 수 없다 (PBC-1).

### F-3 (regression) — Blocking-P2 — `ingress_worker.py:446`

**승격된 recovery hold 의 원인 code 가 고정돼 있지 않다.** `return ..., error_code` 를
`..., None` 으로 바꿔도 전 suite 통과. `_transition` 이 `code = error_code or
"INGRESS_DECISION_FAILED"` 를 쓰므로 durable `last_error_code` 가 조용히 generic 으로 바뀌고
governed 기록과 `stranded()` 에서 진짜 원인이 사라진다.

round 12 의 *What Verified Clean* 항목 "`R-2` 의 주 경로 — 원인 code 가 살아남는다" 를
직접 반증한다. 동작은 오늘 맞고, 붙잡는 것이 없다.

### F-4 (regression) — Blocking-P2 — `events.py:1261`, `:1262`

**감사 대조의 형제 성분 둘이 무방비다** — `definition_digest`, `destination_manifest_digest`.
둘 중 하나를 지워도 전 suite 통과. governed mutation 무결성 guard 다.

round 12 가 `RL-3`~`RL-5` 로 제기하고 **공통 뿌리로 지목한** 바로 그 결함 class 다. wave 6 은
지목받은 성분 셋만 고정하고 같은 `if` 의 나머지 둘을 열거하지 않았다. 시정 규칙으로 적은
"정의·호출 지점·같은 검사의 모든 성분" 을 스스로 적용하지 않았다.

## Tests That Pass By Construction

**세 번째 라운드 연속 발견이다.**

### PBC-1 — `tests/test_slack_http.py` token 누출 test

fixture `_OpenerRaising.open` 이 `del request, timeout` 을 한다. 그 `request` 가 **바로 guard 가
지우려는 대상**이다. 검사 대상을 fixture 가 먼저 없앤다.

```
A 현재 fake opener (`del request, timeout`) → leak=[] 전 진입점    (test PASS)
B 같은 opener 에서 `del` 만 제거          → read_history   leak=[('open','request')]
                                            post_ephemeral leak=[('open','request')]
                                            delete_message leak=[('open','request')]
```

### PBC-2 — 같은 test 가 평범한 `Exception` 을 주입하지 않는다

`except Exception` arm 과 `HTTPError` arm 을 한 번도 안 탄다. 실제 누출이 나는 경로가 거기다
(F-1).

### PBC-3 — `test_a_first_successful_decision_sends_no_safe_feedback`

여전히 tree 에 있고 여전히 docstring 에 대해 동어반복이다. `decision=None` 을 넘겨
`result.decision is not None` 관문에서 나가므로 주장하는 `replayed` 조건에 닿지 않는다.
wave 6 이 진짜 대체품을 추가했지만 빈 것을 남겨뒀다.

## Evidence Claims Corrected

1. **wave 6 mutation 은 12종이 아니라 9종이다.** `(2건)`·`(3건)` 은 죽은 *test* 수인데 main
   agent 가 mutation 수로 셌다. **round 12 에서 "21종을 19종" 이라 틀린 것과 같은 실수의 반복**
2. "21 + 12 = 33종" → 실제 21 + 9 = 30
3. `H2` 는 2건이 아니라 3건을 죽인다
4. "`F-2` … 넷이 한 번에 덮이고 사본을 셀 필요가 없게 만든 것" → `BaseException` arm 에만 해당.
   `SlackTransportError` arm 은 4개 중 2개가 무방비 (F-1)
5. round 12 의 "credential 누출 없음 — 새 실패 경로 전부에서 확인" → F-1 이 반증
6. `D-040` 상태 불일치 (Advisory) — `DECISIONS.md` 는 APPROVED 인데 `STATUS.md` 와
   `wave-6-round-12-closure.md` 는 여전히 미해결·PROPOSED 로 적는다. 권위 기록은 승인이므로
   문서가 낡은 것이다

## Verified Clean

- **`C-2` / FR-021** — byte 단위로 원문 복구 확인
- **FR-027 우선 조항** — 분류에만 좁게 걸리고 FR-021 의 메커니즘 요구를 다시 못박는다.
  다른 것을 약화하지 않고 `T010` 을 합법화한다
- **`C-1`** — `T010` manifest 에 `__init__.py` 가 이유와 함께 있고 `index.yaml` 이 "두 건" → 셋
  으로 정정돼 있다
- **safe outcome 열거** — 5곳 전부 정확히 5개. `INGRESS_DECISION_COMMITTED_UNRECONCILED` 는
  outcome 이 아니라 error code 라 enum 밖이 맞다
- **wave 6 의 9종 mutation 주장** — regression lens 가 전부 재현했고 전부 killed
- **`G1`·`H1` kill 주장** — failure lens 도 독립으로 재현
- **replay 경로** — 결정 commit 후 재시도가 `already_completed` 로 수렴. 모순 메시지 없음
- 성공 경로 무영향, `stranded()` 는 새 종점도 포함

## Remaining Advisory

| id | 내용 |
|---|---|
| `N16`/`N17` | 두 cardinality guard 의 0 방향 여전히 무방비 (round 12 `RL-7`/`RL-8` — wave 6 이 안 닫음) |
| `N24` | outcome → 사용자 문구 mapping 무방비. `DENIED`↔`UNAVAILABLE` 을 바꿔도 통과 (round 12 `RL-6`) |
| Advisory-1 | 침묵 종결이 마지막 실패의 원인 code 를 덮어쓴다. 원인 둘을 같이 남기면 닫힌다 |
| Advisory-2 | `D-039` 가 **성공한 결정**을 해제 불가 상태에 넣는다. `D-038` 항목 1 의 근거는 실패에 대해 쓰였지 성공에 대해 쓰이지 않았다 |
| Advisory-3 | `read_history`/`delete_message` 가 provider 문자열을 append-only durable code 에 쓴다. `post_message` 는 digest 로 바꾼다. 기존 결함 |
| PBC-3 | 빈 test 가 진짜 대체품 옆에 남아 있다 |

## Next Wave Must Do

1. **`/taskify` 를 반드시 탄다.** `CT-3` 이 wave 6 의 절차 생략을 잡았다
2. **설계 판단 둘** — `CT-1`(FR-026 을 고칠지 코드를 고칠지)과 `P1-1`(`committed_decision()`
   읽기 실패 처리). 나머지는 기계적이다
3. **stop rule 을 실제로 적용한다** — "정의 사본 + 호출 지점 + **같은 검사의 모든 성분**".
   wave 6 이 이 규칙을 적어놓고 스스로 어겼다
4. **test 를 쓸 때 fixture 가 검사 대상을 지우는지 본다.** PBC 가 세 라운드 연속 나왔다
