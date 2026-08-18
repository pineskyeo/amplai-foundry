# Wave 6 — Round 12 Blocker Closure

대상: round 12 three-lens review 의 blocker 9건 (`3lens-review-round-12.md`).
결정: `D-039` (APPROVED), `D-040` (**PROPOSED — 미승인**).

## Measurements

| 항목 | 값 |
|---|---|
| full pytest | **1274 passed, 4 deselected** (round 12 target 1245 → **+29**) |
| `amplai-foundry verify` | 7/7 PASS |
| Ruff check / format | exit 0 |
| mypy | 102 source files, no issues |
| task manifest validator | PASS 14/14 |
| `git diff --check` | PASS |

## Closed — 8 of 9

### F-1 (P1) — 이미 결정된 클릭에 실패를 알리던 것

`_settle_exhausted_retry` 가 승격 전에 `committed_decision()` 을 본다. 결정이 기록돼 있으면
error code 를 `INGRESS_DECISION_COMMITTED_UNRECONCILED` 로 바꾸고 `_safe_outcome` 이 그 code 를
침묵 집합에서 걸러낸다.

**승격 자체는 유지한다.** 명령이 recovery hold 로 남아야 `stranded()` 에 보이고, 결정은 됐는데
완료 도장이 없는 상태를 사람이 확인할 수 있다. `completed` 로 끝내는 안은 상태가 깔끔해지는
대신 그 불일치를 감춰서 택하지 않았다 (`D-039`).

침묵은 취향이 아니라 `D-034` 가 정한 것이다 — 첫 성공 결정은 safe outcome 을 만들지 않는다.

| mutation | 결과 |
|---|---|
| G1 `committed_decision()` 확인 제거 (F-1 재현) | **killed** |
| G2 `_safe_outcome` 의 침묵 분기 제거 | **killed** (2건) |

### F-2 (Blocking-P2) — guard 가 호출자 하나에만 있던 것

guard 를 `_call` **안**에 넣었다. `post_message`·`read_history`·`post_ephemeral`·
`delete_message` 넷이 한 번에 덮이고, 앞으로 추가될 호출자도 덮인다. **사본을 세는 대신 셀
필요가 없게 만든 것**이 이 수정의 요지다.

| mutation | 결과 |
|---|---|
| H1 `_call` 의 BaseException guard 제거 (F-2 재현) | **killed** (3건 — 정확히 guard 없던 세 진입점) |
| H2 `finally` 의 token 비우기 제거 | **killed** (2건) |

### 이 wave 에서 내 test 가 거짓말한 것을 잡았다

처음 쓴 token 누출 test 는 `repr(f_locals)` 를 훑었다. **`urllib.request.Request` 의 repr 은
header 를 보여주지 않으므로 그 test 는 아무것도 고정하지 못했다.** H2 mutation 이 살아남아
그것을 드러냈다.

`request.headers` 를 직접 보도록 고치자 **진짜 누출이 세 경로에서 나왔다.** 첫 수정은 원본
예외의 frame 만 지웠는데, 새로 올린 예외의 traceback 첫 frame 이 `_call` 자신이라 `request` 가
token 을 든 채 살아 있었다. `finally` 에서 비워 닫았다.

**교훈**: 문자열만 훑는 누출 검사는 객체가 값을 숨기고 있으면 못 잡는다. mutation 이 살아남은
것이 test 를 의심할 근거였다.

### C-1 (Blocking-P2) — 기록되지 않은 세 번째 범위 확장

`src/amplai_foundry/governance/__init__.py` 를 `T010` 의 `allowed_paths` 에 이유와 함께 넣고,
`index.yaml` update_history 에 **"두 건" 이 틀렸고 셋이었다**고 정정했다.

### RL-1 ~ RL-5 (Blocking-P2 ×5) — 회귀 방어 공백

`T013` 이 각 검사의 성분을 하나씩 고정하면서 형제 성분을 세지 않은 것이 셋(RL-3·4·5)이다.
`F-2` 와 같은 뿌리 — 세되 끝까지 세지 않았다.

| id | 대상 | mutation |
|---|---|---|
| RL-1 | `and result.decision.replayed` | **killed** — 실제 클릭으로 고정 |
| RL-2 | transient 집합의 `GovernanceStoreError` | **killed** |
| RL-3 | root 대조의 저장된 digest 성분 | **killed** |
| RL-4 | 감사 대조의 `actor_type` 성분 | **killed** |
| RL-5 | 감사 대조의 `destination_count` 성분 | **killed** |

`RL-1` 은 기존 test 가 `decision=None` 을 넣어 관문에서 먼저 걸리는 바람에 조건에 닿지 못한
경우였다. 실제 클릭을 태워 `result.decision.replayed is False` 를 전제로 명시하고 통지가 없음을
확인한다.

`RL-3` 은 digest 형식 CHECK(`sha256:` + 64 hex)를 만족하는 **다른** digest 를 쓴다. 형식을 깨면
CHECK 가 먼저 잡아 겨냥한 성분을 고정한 것이 아니게 된다.

## Not Closed — 1 of 9

### C-2 (Blocking-P2) — FR-021

지시대로 **FR-021 을 원문으로 되돌렸다.** 그래서 원래 긴장이 열린 채로 남아 있다 — FR-021 은
retry 동작을 `preserve` 하라고 요구하는데 `T010` 이 그 분류를 바꿨다.

`D-040` 에 세 안을 적고 **PROPOSED** 로 남겼다. 승인 전까지 `spec.md` 는 원문을 유지한다.
권장은 안 B (FR-021 은 그대로 두고 FR-027 에 예외를 명시).

**round 13 을 얼리기 전에 이것이 닫혀야 한다.**

## Mutation Totals

wave 6 에서 12종을 돌렸고 전부 killed 다. round 12 target 의 21종과 합치면 33종이다.
다만 round 12 regression lens 가 추가로 설계한 33종 중 5종(`RL-6`~`RL-10`, Advisory)은
여전히 열려 있다.
