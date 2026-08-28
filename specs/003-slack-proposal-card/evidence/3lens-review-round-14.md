# Three-Lens Review — Round 14

target aggregate: `48f79c28051b7798c37d6442fabe367fe05cb24cf39edf1805936c0e5102bbab`
target 기록: `evidence/review-target-round-14.txt`
date: 2026-08-18

**결과: FAIL.** gate 를 열지 않는다.

tree 무손상 확인 — regression lens 가 `src/` 를 mutate 했으나 종료 후 aggregate 가 일치한다.

## Counts

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 3 | 6 |
| Failure / Recovery | 0 | 1 | 2 | 3 |
| Regression | 1 | 1 | 1 | 2 |
| **합계(중복 제거)** | **1** | **2** | **5** | **11** |

`CT-C`(contract)와 regression 의 Blocking-P2 는 같은 결함이라 하나로 센다.

round 13 대비: P0 0 → **1**, P1 2 → 2, Blocking-P2 7 → 5.

**round 13 blocker 9건의 동작 수정 자체는 대체로 사실로 확인됐다.** mutation 11종 전부
재현·killed 됐고 수치도 정확했다. 그런데 **그 과정에서 P0 을 만들었고, 세 lens 가 각각
독립적으로 같은 뿌리를 짚었다.**

## The Pattern, Fifth Round Running

**형제 위치를 세지 않는다.** 이번에는 세 축에서 동시에 나왔다.

| round | 고친 것 | 놓친 것 |
|---|---|---|
| 11 `R-1` | `slack_projection` 사본 | `slack_http` 사본 |
| 12 `F-2` | `post_message` | 나머지 세 진입점 |
| 12 `RL-3`~`RL-5` | 지목받은 성분 3개 | 같은 `if` 의 나머지 2개 |
| 13 `F-1` | `BaseException` arm | `SlackTransportError` arm |
| **14 `P0-1`** | **`request = None`** | **바로 다음 줄 `body = None`** |
| **14 `BP2-2`** | **`try` 밖 store 접근** | **`try` 안 `except` 집합의 형제 둘** |
| **14 `BP2-3`** | **종점 규칙 사본 셋** | **`spec.md:102` 의 네 번째** |

wave 7 은 이 규칙을 stop rule 로 적어놓고, **자기가 편집하던 함수의 바로 다음 줄**을 놓쳤다.

## Blockers

### P0-1 (regression) — `slack_http.py:604` — `body = None` 을 깨뜨릴 수 있는 test 가 없다

**credential guard 인데 지워도 전 suite 1301건이 통과한다.** narrow 와 full 양쪽에서 0 kill.

`body` 는 직렬화된 Card payload 를 들고 있고 그 안에 **button credential**(action token)이
있다. `test_http_interruption_traceback_contains_no_button_or_bot_credential` 이 지키려는
바로 그 값이다.

무방비임을 통제 실험으로 격리했다.

```
UNMASK-CLEAN (post_message arm 이 local 을 비우고 원본 re-raise)  + body=None 유지 → 0 fail
UNMASK-CLEAN                                                      + body=None 제거 → 1 fail
                                        test_http_interruption_traceback_contains_no_button_or_bot_credential
```

즉 guard 는 **진짜 load-bearing 인데** 그 손실을 아무도 감지하지 못한다.

**뿌리는 wave 7 이 스스로 문서화한 masking 이다.** `post_message` 의 `except BaseException`
arm 이 새 예외를 올려서 `_send` frame 을 traceback 에서 떨어뜨린다. wave 7 은 AC-04 를
드러내려고 그 arm 에서 `_clear_exception_frames` 를 지웠으면서, **그 arm 이 그 밖에 무엇을
가리는지는 묻지 않았다.**

정직한 단서: 오늘 실제 누출은 없다. 가리는 그 arm 이 production 에서도 frame 을 도달 불가로
만들기 때문이다. 결함은 **보호가 검증 불가능하고, 문서화되지 않은 결합에 의존한다**는 것이다.
그리고 `post_message` 의 그 arm 은 이번 wave 가 방금 편집한 곳이다.

### P1-1 (failure) — `ingress_worker.py:466-469` — 좁힌 `except` 가 진짜 상관 실패를 여전히 놓친다

`GovernanceFilesystemError` 는 `RuntimeError` 를 상속하며
`(GovernanceStoreError, IngressError, sqlite3.Error)` 에 **들어가지 않는다.**

`store.connect()`(`store.py:189`, `:193`)가 연결마다 `filesystem_guard.validate()` 를 **두 번**
부르고, 그것이 macOS 에서 `/sbin/mount` 를 `timeout=2` 로 돌린다(`filesystem.py:105-114`).
timeout·`OSError`·비로컬 마운트면 `GovernanceFilesystemError` 다. `store.py:190` 의
`mkdir` 은 ENOSPC/EROFS/EACCES 에서 bare `OSError` 를 낸다.

**실제 코드 경로로 재현했다** — `committed_decision` 을 patch 하지 않았다.

```
ESCAPED process_next: GovernanceFilesystemError
    ingress_worker.py:332 in _finalize
    ingress_worker.py:467 in _settle_exhausted_retry
    ingress_worker.py:310 in committed_decision
    store.py:189 in connect
    filesystem.py:166 in validate
durable state=leased  attempts=2  last_error=INGRESS_DECISION_UNAVAILABLE
feedback: []      reclaimable? None
```

`OSError(28)` 와 `decisions.py:752-764` 의 `ValueError`(durable column 에 대한
`DecisionAction(...)`, `int(...)`, `_parse_timestamp(...)`)도 같은 결과다. 같은 실행의 통제
행: `GovernanceStoreError` → `recovery_hold` + `COMMITTED_UNRECONCILED` + stranded. 즉
**wave 7 의 수정은 열거한 type 에 대해서만 작동한다.**

round 13 `P1-1` 의 피해가 그대로 재현된다 — worker loop 사망, command 가
`attempts == max_attempts` 인 채 `leased`, 재claim 불가.

**왜 놓쳤나.** `evidence/MGC-012-P5-T016.md` AC-09 가 합성 조건 셋만 재고 "실측된 type 은
잡는 집합 안이다" 로 일반화했다. 무조건 두 번 도는 filesystem guard 를 세지 않았다. 그
문서는 "round 13 의 `database is locked` 는 주입된 것" 이라고 스스로 적어놓고,
**"그럼 production 에서 무엇이 이 읽기에 도달하는가"** 를 끝까지 묻지 않았다.

`except` 가 프로그래밍 오류에 대해 좁은 것은 맞다. 결함은 **축이 틀렸다**는 것이다 — 예외
class 를 열거할 것이 아니라 "`store.connect()` 의 모든 실패" 를 잡아야 한다.

### P1-2 (regression) — `tests/test_slack_http.py` — `...traceback[post_message]` 가 구조상 통과한다

`test_no_transport_entry_point_leaks_the_bot_token_in_its_traceback` 의 형제 셋은
`request = None` mutation 을 죽이는데 `post_message` param 만 못 죽인다. narrow·full 양쪽.

parametrize 목록이 네 진입점 전부를 덮는다고 광고하는데 이 guard 에 대해서는 하나가
**무력**이다. 뿌리는 P0-1 과 같다.

### BP2-1 (failure) — `stranded()` 에 사람이 부를 수 있는 진입점이 없다

`D-042` 가 침묵을 고른 근거 전체가 "침묵은 `stranded()` 로 회수할 수 있다"
(`ingress_worker.py:457`)이고, T017 이 그것을 계약에 적었다 —
*"`stranded()` lists all three endings. It is the operator's entry point for any of them."*

`grep -rn "stranded"` 의 호출자는 `tests/` 와 docstring 뿐이다. `cli.py` 에
governance-ingress command 가 없다. script 도, HTTP surface 도, operator tool 도 없다.

목록을 손에 쥐어도 해소 경로가 없다. `IngressService` 의 공개 API 는
`accept / claim_next / complete / retry / recovery_hold / get / stranded` 뿐이고
`recovery_hold` 나 `dead_letter` 에서 빼내는 것이 없다. `committed_decision()` 도 노출되지
않는다.

**계약이 존재하지 않는 메커니즘을 단정한다.** 침묵이라는 선택은 여전히 옳을 수 있으나
그 보상 통제가 실재하지 않는다.

### BP2-2 (failure) — 같은 불완전한 `except` 집합이 형제 두 곳에 더 있다

`ingress_worker.py:343`(`_finalize` → `_transition`), `:186`(`process_next` → `claim_next`).
둘 다 같은 tuple 을 쓴다.

```
=== _finalize/_transition ===   escaped: GovernanceFilesystemError
=== process_next/claim_next === ESCAPED process_next at claim_next: GovernanceFilesystemError
```

T016 evidence 의 형제 표는 "`try` **밖**에 store 접근이 있나" 만 물었고
"`try` **안**의 `except` 가 충분한가" 는 묻지 않았다.

피해는 P1-1 보다 낮다(`_transition` escape 는 `attempts < max` 라 나중에 회수됨,
`claim_next` escape 는 아무것도 strand 하지 않음). 그러나 **둘 다 caller loop 를 죽인다.**

### BP2-3 (contract) — `spec.md:102` 에 네 번째 사본이 살아 있다

```
102:- Background processing of a reviewer action runs out of retries: the reviewer is
     told the action is unavailable, and the command remains listed for operator recovery.
```

같은 파일 32줄 아래 FR-026 이 두 부분집합에서 **어떤 outcome 도 알리지 않아야 한다**고
적는다. `D-039`·`D-041`·`D-042` 가 전부 그 통지를 금지하려고 존재한다.

`T017` evidence AC-07 이 feature 디렉터리 전수 sweep 을 주장하며 다섯 위치를 열거하고
"`allowed_paths` 밖의 사본은 없다" 로 닫았다. **`spec.md` 는 `allowed_paths` 안이었고
`:102` 는 그 표에 없다.**

놓친 이유: sweep 이 `exhaust` / `retry budget` 으로 grep 했는데 `:102` 는
**"runs out of retries"** 라고 쓰여 있다. `:103` 도 같은 형태라 **두 줄을 놓쳤다.**

### BP2-4 (contract) — 세 번째 종점 조건을 Decision 없이 좁혔다

계약(`interaction-feedback.md:51-52`)과 FR-026 마지막 문장이 "**no attempt ever completed**"
라고 적는다. 코드의 실제 조건은 "**마지막** attempt 를 관측하지 못함" 이다.

도달 가능한 반례:

- attempt 1 완료 → `RETRY` (관측됨, `attempts=1 < max`)
- attempt 2 claim (`attempts=2 = max`) → worker 사망
- lease 만료 → `retry_wait` → `dead_letter`, 통지 없음, `stranded()` 에 남음

종점 1·2 는 "worker 가 마지막 attempt 를 관측" 을 요구 — 거짓. 종점 3 은 "완료된 attempt
없음" 을 요구 — 거짓(attempt 1 이 완료). **어떤 종점도 해당하지 않는다.**

이 문구는 이미 load-bearing 임이 증명됐다 — round 13 reviewer 가 `CT-1` 을 판정하며 정확히
이 조항을 파싱했다(`3lens-review-round-13.md:101`).

그리고 **사용자의 기록된 답변(`spec.md:26`)은 올바른 넓은 조건**("without any worker
observing the outcome")인데 FR-026 과 계약이 좁혔다. `D-041` 은 committed-decision 종점
추가만 승인했고 이 좁힘은 승인하지 않았다.

동작은 오늘 맞다(침묵 + `stranded()`). 그래서 P1 이 아니라 Blocking-P2 다.

### BP2-5 (contract + regression) — T018 의 블록·성분 내역이 틀렸다

두 lens 가 독립적으로 같은 값을 쟀다.

```
if( at 1254  components 9    if( at 1795  components 8
if( at 1480  components 9    if( at 1875  components 8
if( at 1557  components 9    if( at 1959  components 8
if( at 1711  components 10   if( at 2113  components 8
blocks 8   sum 69
```

| 기록된 값 | 실제 |
|---|---|
| 블록 9개 | **8개** |
| 남은 블록 8개 | **7개** |
| `L1712` 블록 9성분 | **10성분** (`policy_snapshot_id` 가 그 블록에만 있다) |
| 총 69성분 | 69 (맞다) |
| 남은 60성분 | 60 (맞다) |

T018 evidence 의 AC-01 표는 8행에 합계 68 인데 총계를 69 로 선언한다 — **표 자체가
내부 모순**이다. T020 manifest 는 7블록을 나열해 합 59 인데 "총 60" 이라 적는다. 총계만
상쇄로 맞다.

`T020.yaml` 의 acceptance AC-05 가 "여덟 블록 전부 덮인다" 를 완결 조건으로 삼는데
**그 여덟은 존재하지 않는다.** `index.yaml` 두 곳이 틀린 수를 측정 사실로 적는다.

**세 라운드 연속 수치 오류다.** round 12 "21종을 19종", round 13 "12종을 9종",
round 14 "8블록을 9블록".

## Verified Clean

세 lens 가 실측으로 확인한 것.

- **`F-1` 은 진짜로 닫혔다.** 네 진입점 × 세 실패 경로 = **12/12 leak 0**. failure lens 가
  오염 함정을 먼저 겪고 filter 를 넣었고, `_call` 을 우회하는 subclass 로 **positive
  control** 을 돌려 8건 leak 을 확인했다 — clean 이 측정 실패가 아니다
- **`_call` 이 유일한 출구다.** `_send` 호출은 `slack_http.py:521` 하나뿐이고 `_call` 안이다.
  `_fail`·`_fail_http`·`_decode` 셋 다 같은 출구를 지난다
- **mutation 11종 전부 재현·killed.** 죽은 test 수도 evidence 와 정확히 일치
  (T015 9/4/8/3, T016 2/2/1/1/1/1/1)
- **AC-04 masking 주장이 사실이다.** `post_message` 의 사본이 가리고 있었고, 한 겹짜리 fake
  opener 도 가리고 있었다. 둘 다 통제 실험으로 확인
- **`PBC-3` — wave 7 이 옳고 round 13 이 틀렸다.** 성분 셋이 각각 **다른** test 로 죽는다.
  세 번째 성분(`outcome is COMPLETED`)이 이전에 무방비였다는 것도 확인
- **T018 블록 1 kill rate 3/9 → 9/9.** 성분마다 정확히 하나의 test 가 죽고 전부
  `REVIEW_CARD_AUDIT_MISMATCH` 를 확인한다. 기존 3개가 `actor_id`·`actor_type`·
  `destination_count` 라는 것도 정확
- **총 성분 69** — 정확
- **FR-021 은 원문 그대로다.** `git diff` 에 나타나지 않는다 (`D-040` 안 B 준수)
- **기존 두 종점이 약화되지 않았다.** 문장 2 는 `D-041` 이 승인한 조건이 붙었고, "outcome
  unknown" 문장은 불변. MUST 가 내려간 곳 없음
- **Clarifications 정정이 정직하다.** 사용자 답변 원문 보존, 좁힘을 하위 항목으로, Decision
  셋 인용, FR-026 을 권위로 지목
- **`CT-3` 이 닫혔다.** 완료 task 의 status·evidence·`completed_at` 미변경(T001–T008 은
  git 으로 증명), `T011` 정정이 정확, ID 충돌 셋 전부 정정돼 남은 ID 가 전부 실재 manifest
  를 가리킨다
- **`stranded()` 가 세 종점을 전부 나열한다** — SQL 로 확인
- **carried-forward limit 셋이 악화되지 않았다** — hold 해제 불가, lease 만료 침묵,
  `_clear_exception_frames` 정의 네 사본
- **full pytest 1301 passed / 4 deselected, validator 20/20** — 두 lens 가 각각 재실행
- **`OQ-001` 판단이 옳다.** 요구사항 공백은 실재하나 보호는 계약으로 뒷받침되고
  (`review-card-lifecycle.md:110-119` 가 exception string 과 `--showlocals` 를 명시) test 도
  있다. FR 신설은 계약 변경이라 Decision 이 필요하다

## Advisory

| id | 내용 |
|---|---|
| `A-1` | AST 출구 검사가 `getattr(self, "_send")` 나 ClassDef 직속이 아닌 method 를 못 잡는다. 현실적 형태는 잡는다 |
| `A-2` | `ok:false` 경로는 `_call` 을 우회해도 clean 하다 — `_decode` 가 `from` 없이 raise 하고 `finally` 가 이미 비웠기 때문. 세 경로 중 그 하나는 증거력이 없다 |
| `A-3` | T016 AC-09 의 "실측된 type 은 잡는 집합 안이다" 일반화가 P1-1 로 반증됐다. 다음 wave 가 그 표를 확정된 것으로 재사용하지 않도록 기록 |
| `A-4` | FR-026 문장 2(객관적 사실 조건)와 문장 4(시스템 인지 조건)가 겹치는 경우 반대 MUST 를 지시한다. 문장 4 를 override 로 읽으면 해소되나 wave 가 암시하는 disjoint 집합은 아니다 |
| `A-5` | `plan.md:117-118`, `research.md:61` 에 safe outcome enum 사본 둘이 남아 있다. T017 evidence 는 "이번에는 없다" 고 적었다. **지금은 값이 일치해 무해하나 주장이 거짓이다** |
| `A-6` | 계약의 `unavailable` 행("recovery hold without a more specific public result")이 과도하게 넓다. `COMMITTED_UNRECONCILED` 도 더 구체적인 공개 결과가 없지만 침묵해야 한다. 예외가 산문에만 있다 |
| `A-7` | `T015` manifest 와 `index.yaml` 이 FR-019 를 T015 에 매핑하는데 `OQ-001` 스스로 FR-019 는 Card content 한정이라 적는다. coverage map 이 open question 이 지목한 공백을 부분적으로 가린다 |
| `A-8` | AST 검사가 module-level 함수나 subclass 의 새 진입점을 못 센다 |
| `A-9` | T017 evidence AC-06 표의 줄 번호가 실제와 어긋난다 (`:462`/`:464`/`:466` → 실제 `:466-469`/`:470-471`/`:472`). frozen set 밖이라 미관 문제 |
| `A-10` | T018 의 69성분 측정이 두 모듈만 돌렸다. 나머지 7블록은 full suite 로 재면 달라질 수 있다. T020 이 다시 재야 한다 |
| `A-11` | `..._traceback` 의 button-credential test 셋이 `post_message` 를 거치므로 `_send` frame 에 도달하지 못한다. `post_message` 자기 frame 은 유효하게 덮는다 |

## Evidence Claims Corrected

1. **T017 AC-07 의 "사본 전수" 주장이 거짓이다.** `spec.md:102`·`:103` 두 줄을 놓쳤다
2. **T017 AC-07 의 "plan/research 에 safe outcome enum 사본 없다" 도 거짓이다.** 둘 다 있다
3. **T016 AC-09 의 예외 type 일반화가 틀렸다.** filesystem guard 를 세지 않았다
4. **T016 AC-08 의 형제 표가 불완전하다.** `try` 밖만 물었고 `try` 안 `except` 의 충분성은
   묻지 않았다
5. **T018 의 블록 수·per-block 수가 틀렸다.** 8블록, `L1712` 는 10성분
6. **`/speckit-analyze` 가 `spec.md:102` 의 파일 내 모순을 놓쳤다.** analyze 가 잡아야 할
   class 다. 실행이 최종 `spec.md` 보다 앞섰거나 검사가 그 형태를 못 본다

## What The Next Wave Must Do

1. **`body = None` 을 깨뜨릴 수 있는 test 를 만든다.** `post_message` 의 masking arm 을
   먼저 이해해야 한다 — 그 arm 이 무엇을 더 가리는지 전수로 센다
2. **`except` 를 type 열거가 아니라 "`store.connect()` 의 모든 실패" 축으로 다시 짠다.**
   형제 두 곳(`_transition`, `claim_next`)도 같이 본다
3. **`stranded()` 에 진입점을 만들거나, 계약에서 그 문장을 빼고 실제 회수 수단을 적는다.**
   둘 중 하나여야 한다
4. **`spec.md:102`·`:103` 을 FR-026 에 맞춘다.** 그리고 사본을 셀 때 문구 변형을 함께 센다
5. **종점 3 의 조건을 코드에 맞춘다.** 사용자 원답변의 넓은 조건이 옳았다. Decision 이
   필요하다
6. **T018/T020 의 블록·성분 수를 정정한다.** 세 문서에 걸쳐 있다
7. **수치를 보고하기 전에 두 번 센다.** 세 라운드 연속 틀렸다
