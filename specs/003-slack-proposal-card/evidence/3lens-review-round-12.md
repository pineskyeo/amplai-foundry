# Three-Lens Review — Round 12

target aggregate: `638241f5ed0ed2d738b05b4993465448c618922fe60e7e0f7e15857a1325343b`
target 기록: `evidence/review-target-round-12.txt`
date: 2026-08-14

**결과: FAIL.** gate 를 열지 않는다.

## Counts

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 2 | 2 |
| Failure / Recovery | 0 | 1 | 1 | 2 |
| Regression | 0 | 0 | 5 | 5 |
| **합계** | **0** | **1** | **8** | **9** |

round 11 대비: P0 0 유지, P1 3 → 1, Blocking-P2 6 → 8.

## What Verified Clean

세 lens 가 독립적으로 확인했다. 이것들은 진짜로 닫혔다.

- **`R-5` (4 대 5 불일치)** — contract lens 가 여섯 위치를 세어 전부 크기 5, 원소 동일을 확인했다.
  살아남은 4항목 사본 0건
- **`R-4` (동어반복 test)** — regression lens 가 `Q1`(round 11 이 "지워도 통과한다" 고 지목한 그
  mutation)을 재현했고 test 4건이 죽었다
- **`R-3` 의 경계** — failure lens 가 `max_attempts` 1/2/3/4 에서 양방향 확인. 재시도를 잃지도,
  침묵하지도 않는다
- **`R-2` 의 주 경로** — 예산 소비 → 소진 후 DLQ 까지 원인 code 가 살아남는다. 손상은 여전히 즉시
  terminal
- **credential 누출 없음** — 새 실패 경로 전부에서 durable row, exception, traceback locals 확인
- **evidence 의 21종 mutation 주장** — regression lens 가 전부 재현했고 21/21 killed

## Blockers

### F-1 — P1 — `ingress_worker.py:393`, `:323`

**`R-3` 수정이 `D-038` 항목 6 이 거부한 바로 그 모순을 한 분기 옆에서 만들었다.**

`_settle_exhausted_retry` 가 승격할 때 `committed_decision()` 을 보지 않는다. **앞선 attempt 가
decision 을 commit 했고 마지막 attempt 가 실패하면** reviewer 는 승인 Result Card 와
`unavailable` ephemeral 을 **둘 다** 받는다.

failure lens 실측 (`max_attempts=2`):

```
ATTEMPT1 finalize_failed INGRESS_FINALIZE_FAILED   decision: True
OUTBOX after attempt1: [..., ('OBX-...','pending','provider:slack:...')]
ATTEMPT2 recovery_hold INGRESS_DECISION_UNAVAILABLE recovery_hold
FEEDBACK after 2: [('CMD-...', UNAVAILABLE)]
PROPOSAL [('PROP-...', 'approved')]
```

계약이 승격을 안전하다고 선언한 근거는 "the outcome is known" 이다. 그 전제가 이 경우 거짓이다.
`D-038` 항목 6 이 lease 만료 종점에 통지를 넣지 않은 이유와 **완전히 같은 이유**가 여기에도
있는데 승격은 그것을 보지 않는다.

`test_a_worker_observed_exhaustion_ends_as_a_recovery_hold_with_feedback` 은 앞선 attempt 에서
decision 을 commit 하지 않으므로 이 경우를 덮지 않는다.

**이것이 같은 pattern 의 세 번째 반복이다.** round 10 `C-1` → `R-2`, round 11 → `F-1`.
한 방향을 고치면서 반대 방향을 만들었다.

### F-2 — Blocking-P2 — `slack_http.py:471`, `:481`, `slack_projection.py:891`

**`R-1` 을 두 호출 경로 중 하나에만 적용했다.**

`post_message` 는 `except BaseException` → 공유 분류기로 감싼다. `read_history` 는 `_call` 을
맨몸으로 부르고 `_call` 은 `except Exception` 에서 끝난다. `_read_page` 도 마찬가지다.

failure lens 실측 — 제3자가 `Exception` 이 아닌 `BaseException` 을 던지면:

```
read_history -> Boom         isException=False
post_message -> RuntimeError isException=True
```

dispatcher 를 통과하면 event 가 `leased` 로 남고 worker 가 매번 죽다가 lease 만료로만 수렴한다.
DLQ 는 `OUTBOX_ATTEMPTS_EXHAUSTED` 를 적어 원인을 가리지 않는다. **round 11 이 `R-1` 을 P1 로
매긴 것과 같은 형태다.**

더 나쁜 사실이 같은 줄에 있다. 이 경로의 traceback 이 **bot token 을 붙잡고 있다**.

```
read_history bearer-holding frames: [('_call','request'), ('open','request')]
post_message bearer-holding frames: []
```

`T009` 는 `_interruption_kind` **정의** 사본을 셌지만 그 guard 가 없는 **호출 지점**을 세지 않았다.
"같은 형태가 저장소에 몇 개 있는지 먼저 센다" stop rule 을 절반만 지켰다.

### C-1 — Blocking-P2 — `governance/__init__.py:54`, `:300`

**세 번째 범위 확장이 기록되지 않았다.** 이 파일은 이번 wave 에 수정됐는데(`OutboxRetryableError`
export) `T009`~`T014` 어느 `allowed_paths` 에도 없다. evidence·`STATUS.md`·`index.yaml` 이
"넓힌 범위 두 건" 이라고 적는데 실제로는 셋이다. 내용은 무해하지만 **기록된 수가 틀렸다.**

### C-2 — Blocking-P2 — `spec.md:128`

**FR-021 을 Decision 없이 약화했다.** `preserve existing … retry, hold … behavior` 를
`use … rather than bypassing them` 으로 바꿨다. 삭제된 절이 바로 `T010` 의 재분류가 위반했을 절이다.

`D-038` 에 없고, Clarifications 셋 중 어느 것도 아니고, `index.yaml` update_history 는
FR-026·FR-027 신설만 적는다. **변경 대상이 되는 요구사항을 기록 없이 느슨하게 만든 것**이
결함이다. 문구 자체는 옳을 수 있다.

### RL-1 ~ RL-5 — Blocking-P2 ×5 — 회귀 방어 공백

regression lens 가 추가 mutation 33종을 설계해 10종이 살아남았다. 그중 다섯이 blocker 다.

| id | 위치 | 살아남은 mutation |
|---|---|---|
| RL-1 | `ingress_worker.py:376` | `and result.decision.replayed` 제거 → 첫 성공 decision 이 `already_completed` 를 보낸다 (D-034 가 막으려던 이중 통지) |
| RL-2 | `store.py:61` | transient 집합에서 `GovernanceStoreError` 제거 → `R-2` 의 guard 자체 |
| RL-3 | `review_cards.py:634` | 저장된 `payload_digest` 성분 제거 |
| RL-4 | `events.py:1258` | 감사 대조의 `actor_type` 성분 제거 |
| RL-5 | `events.py:1263` | 감사 대조의 `destination_count != 1` 제거 |

**RL-1 이 특히 아프다.** `T012` 가 "성공한 첫 decision 은 여전히 safe outcome 을 내지 않는다" 를
invariant 로 적었는데 그것을 붙잡는 test 가 없다. `R-4` 를 닫은 바로 그 task 에서 `R-4` 의 결함
class 가 재발했다.

**RL-3·RL-4 는 `T013` 의 형제 성분이다.** `T013` 이 성분 하나씩 고정하면서 같은 검사의 다른
성분을 세지 않았다. `T009` 의 F-2 와 같은 실수다 — 사본을 셌지만 끝까지 세지 않았다.

## Evidence Claims Corrected

round 11 이 앞선 기록 넷을 정정했듯 round 12 도 정정한다.

1. **mutation 은 21종이지 19종이 아니다.** 5+4+4+3+3+2 = 21. main agent 의 보고가 틀렸다
2. **`T012` 의 "성공한 첫 decision" invariant 는 고정돼 있지 않다** (RL-1)
3. **`T013` 의 "세 성분 모두 test 로 도달 가능하다" 는 부분 오류다.** 셋 중 둘만 고정됐다 (RL-3)
4. **`T013` 의 "`!= 1` 은 0건도 막는다" 는 절반만 고정됐다.** `>1` 방향만이다
5. **`T009` test 주석의 서술이 틀렸다** (`tests/test_slack_projection.py`). 평범한 `RuntimeError`
   는 `except Exception` 에서 `_rewrap` 되고 공유 분류기에 닿지 않는다. 그 분기는 형제 test 가
   고정하고 있으므로 **coverage 는 있고 narrative 가 틀렸다**

## Advisory (기록 후 item 별 판단)

- `C-3` — `D-038` 항목 5 가 승격 위치를 `_transition` 이라고 적었으나 구현은 `_finalize` 다.
  `_transition` 이었으면 feedback 이 안 나갔을 것이고 `T011` evidence 가 그 이유를 적었다.
  **Decision 이 사실과 다른 상태로 남아 있다**
- `C-4` — `_abandon_review_closed` 가 transient 를 여전히 terminal 로 닫는다. 도달 가능한 경우를
  구성하지 못해 Advisory
- `F-3` — `deliver_next` 의 `fail()` 이 try 밖이라 fail 자체가 실패하면 event 가 `leased` 로 남는다.
  기존 결함이고 lease 만료가 회수한다. `R-2` 가 네 번째 호출자를 더했으므로 기록
- `F-4` — `GovernanceEventError` 가 transient 집합에 없어 ingress 와 정책이 갈릴 수 있다. 실제
  코드에서 도달 경로를 못 찾아 Advisory
- `RL-6` — 코드→값 mapping 은 안 고정됐다. `_observed_safe_outcomes` 는 **집합**만 본다
- `RL-7`, `RL-8` — 두 cardinality guard 의 0 방향. `IndexError` 로 fail-closed 라 Advisory
- `RL-9` — migration future-version guard. 기존, 이번 wave 무관
- `RL-10` — `isinstance` → `type() is`. 저장소에 해당 subclass 없음

## Process Note

세 lens 가 서로 다른 결함을 찾았고 겹치지 않았다. 각자 다른 축을 봤다는 뜻이다.

**공통 뿌리가 하나 보인다.** `F-2`, `RL-3`, `RL-4`, `RL-5` 는 전부 같은 실수다 — **사본이나
성분을 세되 끝까지 세지 않았다.** stop rule 이 "몇 개 있는지 먼저 센다" 였는데 wave 5 는
정의 사본은 셌고 호출 지점과 형제 성분은 세지 않았다. 다음 wave 는 이 규칙을 "정의·호출 지점·
같은 검사의 모든 성분" 으로 넓혀야 한다.
