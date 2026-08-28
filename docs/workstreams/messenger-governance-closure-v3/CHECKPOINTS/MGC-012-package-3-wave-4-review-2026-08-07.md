# MGC-012 Package 3 Wave 4 Review — 2026-08-07

대상은 MGC-012-T004 (순차 전달·경쟁 dispatcher·supersession) 와 MGC-012-T005 (crash
recovery·판정 불가 hold·실패 행렬) 다. reviewer 셋을 관점별로 돌렸다 — contract,
failure/recovery, regression. 이 문서는 근거 기록이고 승인 주체가 아니다 (D-004).

**두 task 모두 production code 를 한 줄도 안 바꿨다.** `git status --porcelain src/` 가
review 내내 비어 있었다. Package 3 의 계약이 T001~T003 에서 이미 닫혀 있었다는 뜻이고,
wave 4 가 한 일은 그 계약이 실제 `OutboxDispatcher` 와 물려 도는지를 증명한 것이다.

## Process Violation — 기록된 교훈을 어겼다

wave 3 review 가 남긴 문장이 있다.

> mutation 을 돌리는 lens 와 코드를 읽는 lens 를 동시에 돌리면 안 된다. 다음 wave 부터
> regression lens 를 따로 돌린다.

**wave 4 는 round 1 과 round 2 모두 셋을 동시에 돌렸다.** 그 결과가 round 2 의 NEW-4 다 —
contract lens 의 첫 full-suite 실행에서 `test_the_hold_gate_alone_...` 이 한 번 실패했고
재현되지 않았다. 같은 시각 failure-recovery lens 는 main checkout 의 `events.py` 를
mutate·복원하는 중이었다고 스스로 보고했다.

regression lens 가 메커니즘을 실증했다. `_imported_roots(MODULE_PATH)` 가 AC-05 의
dependency 검사를 위해 `slack_projection.py` 를 **runtime 에 disk 에서 읽어** `ast.parse`
한다. 그 파일을 10ms 주기로 고쳐 쓰는 writer 를 붙이면 10/10 실패한다.

확정하지는 않는다 — round 1 이 어느 test 로 실패했는지 기록이 없다. 다만 NEW-4 는 이후
격리 조건에서 **100회 반복 0 실패**, regression lens 의 격리 worktree 40회 + 공유 checkout
40회도 0 실패다. wave 3 과 wave 4 에서 같은 현상이 두 번 나왔으므로 교훈을 다시 적는다.

**wave 5 부터 regression lens 는 반드시 단독으로 돌린다. mutation 은 격리 `git worktree`
에서만 한다.**

## Round 1

| lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---|---|---|---|
| contract | 0 | 0 | 4 | 6 |
| failure-recovery | 0 | 1 | 1 | 6 |
| regression | 0 | 0 | 0 | 5 |

### P1 — `OUTBOX_POST_SEND_RECONCILE_REQUIRED` 에 repo 전체 test 가 없었다

crash 후 Card 가 지워진 상황에서 결과가 갈린다. 재전송 갈래는 test 가 있었고 hold 갈래는
없었다. HEAD 전수 grep 결과 그 문자열은 `events.py:2852`, `contracts/slack-transport.md`,
`DECISIONS.md` 셋뿐이고 `tests/` 에 0건이었다. guard 를 지워도 suite 가 전부 통과했다.

AC-09 를 추가하고 test 로 고정했다. guard 를 `if False:` 로 바꾸면 event 가 `DELIVERED` 로
새는 것을 mutation 으로 확인했다 — 정확히 중복 Card 다.

### Blocking-P2 5건

- hold 의 `scope_ref` 미검증 — `_terminal_rows` 가 `reason_code` 만 읽었다. `_hold_scopes` 추가
- T005 `guidance`·`scope.include` 가 D-023 이전 문장 — 인용한 `plan.md` P-001 은 이미
  "좁혀짐" banner 를 달고 있는데 banner 이전 문장만 옮겨져 있었다
- `run: amplai-foundry lint` 가 실행 불가 (`Missing argument 'VAULT'`) — 선언된 command 와
  실제로 돌린 command 가 달랐다
- `index.yaml` 의 FR-011 이 간접 coverage note 없이 `covered`
- acceptance 자기 수정에 decision 기록 없음 → D-025 신설

### Advisory 로 고친 것 4건

- **`operator_hold` gate 가 repo 전체에서 아무 test 도 안 죽였다.** `= 0` → `IN (0, 1)` 로
  바꿔도 897개가 통과했다. production 에서 hold 는 언제나 dead letter 와 함께 생겨 앞
  sequence gate 가 늘 같이 막기 때문이다. synthetic test 로 teeth 를 줬다
- **AC-01 순서 test 가 이름과 다른 것을 검사했다.** loop 가 매번 시계를 밀어 gate 가 지키는
  창을 안 만들었다. 시계 밀기 전 호출을 넣었다
- 동어반복 assertion 교체 — `posted` 의 ts 오름차순은 counter 때문에 항상 참이었다
- 과장된 주석 정정 — supersede 경계 test 는 dispatcher guard 만 진다

## Round 2

round 1 처리가 test 를 725줄 바꿨으므로 3 lens 를 다시 돌렸다. 각 lens 에 round 1 findings
를 넘기고 "닫혔다는 주장을 액면으로 받지 말라" 고 지시했다.

| lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---|---|---|---|
| contract | 0 | 0 | 5 | 6 |
| failure-recovery | 0 | 0 | 0 | 7 |
| regression | 0 | 0 | 0 | 3 |

### NEW-1 — round 1 수정이 낸 새 결함. 가장 컸다

round 1 이 AC-08·AC-09 의 경계를 **`attempts` 단독**으로 적었다. 거짓이다. `deliver_next`
의 guard 는 두 조건의 AND 다 (`events.py:2844`-`2847`).

```text
last_error_code == 'OUTBOX_LEASE_EXPIRED'  AND  attempts >= max_attempts
```

probe 로 반증했다 — `max_attempts=2` 에서 send 성공 후 `OUTBOX_DELIVERY_FAILED` 로 실패를
기록하고 Card 를 지우면 `attempts(2) >= max(2)` 인데도 guard 가 안 걸리고 **재전송한다**.

```text
before: attempts=1 last_error_code=OUTBOX_DELIVERY_FAILED
after : attempts=2 state=delivered
attempted=[1, 1]
```

AC-08·AC-09 문언을 고치고 반대쪽 경계를 AC-10 과 test 로 고정했다. approved contract
(`contracts/slack-transport.md` C-2, D-023 마지막 문단) 는 처음부터 두 조건을 정확히 쓰고
있었다 — round 1 이 옮기다 잃은 것이다.

### 나머지 Blocking-P2 4건

- `scope.include` 에 남은 "activation state" 가 같은 파일의 AC-07 note 를 부정 → 문구 통일
- `quickstart.md` 에 실행 불가 `amplai-foundry lint` 잔존 → `lint vault`. 같은 파일의
  "test 709건" 고정 숫자도 SC-006 에 맞춰 제거
- NEW-4 (hold gate test 1회 실패) → 격리 100회 반복 0 실패. 원인은 위 Process Violation
- D-025 자가 승인 → 아래

### D-025 — 초판이 자가 승인이었다

contract lens 판정이 옳다. D-021·D-022 의 Source 는 "**사용자 결정.** 선택지 셋을 제시하고
사용자가 N번째를 골랐다" 이고, D-024 가 지적받은 결함은 "기록이 없다" 가 아니라 "사용자
선택 없이 구현 중에 넣었다" 였다. 초판 D-025 는 그 문장을 인용해 놓고 `accepted` 로
자가 승인했고, Source 에 contract lens 의 판정을 정당화 근거로 들었다 — D-004 가 금지한
바로 그것이다.

`proposed` 로 내리고 선택지 셋을 제시했다. **사용자가 첫째(사후 승인)를 골랐다.**

초판의 사실 오류도 함께 고쳤다.

- "activation 이 붙은 것은 legacy migration 도메인뿐" → 거짓. `activated_from_status` 등이
  더 있다. 결론(provider activation table 없음)은 유지
- "원문이 요구하던 hold 갈래는 하나도 빠지지 않았다" → 거짓. 소진 갈래의 hold 요구가
  재전송으로 바뀌었다. "주고받았다" 로 다시 씀
- "원문대로면 모든 첫 Card 가 영구 hold" → 조건부다. production code 를 안 고쳤으므로
  당장은 test 하나가 실패할 뿐이다
- 제목·범위가 실제 변경(AC 5건 + scope + guidance)을 축소 → 다시 씀
- "멈춘다 vs 고친다" 이분법 → T003 AC-05 선례(AC 유지 + `all_acceptance_passed: false`)를
  세 번째 선택지로 추가

## Mutation Evidence

regression lens 가 격리 `git worktree` 에서 돌렸다. **16종 전부 사망, survivor 0.**

| mutation | killer |
|---|---|
| `events.py:2634` `operator_hold = 0` → `IN (0, 1)` | `test_the_hold_gate_alone_...` (유일) |
| 앞 sequence gate `NOT EXISTS` 삭제 | AC-01 순서 test 외 3개 |
| post-send guard 무력화 | `test_the_last_attempt_holds_...` (유일) |
| guard 의 `last_error_code` 검사 제거 | exhaustion test 3개 |
| guard 의 `attempts >= max_attempts` 제거 | 재전송 test (유일) |
| `_never_attempted` 두 조건 각각 | lease-replay test / 16개 |
| history 소진 branch 삭제 / `break` 로 전환 | 12개 |
| search cap raise → `return None` | 4개 |
| digest mismatch raise → continue | digest fail-closed test |
| `_our_marker` destination_ref 대조 제거 | foreign marker test |
| `_receipt` 가 응답 channel 사용 | receipt channel test |
| `_raise_for_failure` 소진 branch 삭제 | 9개 |
| `mark_delivered` 의 `delivered_sequence` update 제거 | delivered_sequence test 2개 |
| 앞 sequence gate 에서 `'superseded'` 제거 | supersession test 3개 |

guard 의 두 조건이 **각각 다른 test 에 잡힌다.** 한쪽만 지워도 살아남는 구멍이 없다.

## Evidence

| command | 결과 |
|---|---|
| `python -m pytest` | **898 passed** |
| `python -m pytest tests/test_slack_projection.py` | 188 passed |
| `python -m ruff check .` | All checks passed |
| `python -m mypy` | Success, 99 source files |
| `amplai-foundry lint vault` | 0 errors, 0 warnings, 56 notes |
| `amplai-foundry verify` | **7/7 PASS** |
| `git status --porcelain src/` | empty — production code 무변경 |

## Delivered Scope

- **T004** — AC-01~04. 순차 전달, 경쟁 dispatcher 직렬화, supersession,
  `delivered_sequence` 추적. `deliver_next` 를 실물로 통과시킨 통합 test 8개
- **T005** — AC-01~10. crash recovery, 지워진 Card 의 세 갈래(상한 도달 hold / 소진 재전송 /
  lease-replay 상한 hold), hold gate, 실패 행렬(ratelimited·channel_not_found·5xx),
  다른 Provider 격리. test 12개

## Accepted Advisory

수용하고 기록만 한다.

- **`max_attempts=1` 에서는 post 를 한 번도 안 한 event 도 post-send guard 에 걸려 영구
  hold 가 된다.** 이번 변경 이전부터 있던 production 동작이다. fail-closed 선택이고,
  Package 4 에서 하한을 2 로 올릴지 판단할 근거로 남긴다
- **synthetic hold gate test 는 production 이 못 만드는 상태를 못박는다.**
  `reconcile_connection` 이 그 상태를 `OUTBOX_OPERATOR_HOLD_MISMATCH` 로 능동 거부한다
  (`events.py:2251`-`2261`). 그래도 두는 이유는 그 gate 가 유일한 killer 를 잃으면 즉시
  무방비로 돌아가기 때문이다. gate 를 JOIN 으로 바꾸는 올바른 refactor 는 이 test 를
  **시끄럽게** 죽인다 — 조용한 green 이 아니다
- **page 사이의 순서는 여전히 무방비다.** destination 은 page **안**에서만 우리 marker 를
  먼저 찾는다. cursor 를 우리가 만들지 않아 여기서 못 막는다 (Package 4, C-1.2 readback
  자가검사)
- `SLACK_PROJECTION_MARKER_DIGEST_MISMATCH` 와 `SLACK_PROJECTION_METADATA_UNREADABLE` 은
  destination 층 test 만 있다. 둘 다 `OutboxReconcileError` 하위라 dispatcher 경로는
  search-cap test 가 실물로 증명했다. code 문자열만 미고정

## Deferred

- `OQ-003` — Slack message metadata 크기 상한. 공식 문서 미확인. marker 에 payload 본문을
  안 넣는 것으로 회피 중이고 실측은 Package 4
- lease 가 `reconcile` 도중 만료되면 terminal 판정이 DLQ·hold 어디에도 안 남는다. 고치려면
  `events.py` 를 건드려야 해서 이 wave 소유가 아니다. contract C-1.2 문구 보강은 Package 4
- taskify manifest validator 가 `done` task 에 `completed_at` 을 요구하는데 workstream
  관례는 gate 이후에 채운다. T002·T003 이 HEAD 에서도 실패한다. 별도 item

## Verdict

**round 2 종료 시점 P0 0건, P1 0건, Blocking-P2 0건.**

round 2 처리는 test 와 문서만 바꿨고 `src/` 는 무변경이다. wave 2 선례 — "round 3 처리는
docstring·주석·checkpoint 문구뿐이라 production code 가 안 바뀌었고 4라운드를 돌리지
않는다" — 를 따라 round 3 을 돌리지 않는다.

gate 기록은 별도 문서다.
