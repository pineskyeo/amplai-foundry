# MGC-012-P5 Three-Lens Review — Round 11

## Target

| Field | Value |
|---|---|
| Feature | `MGC-012-P5` — Slack Proposal Cards (`specs/003-slack-proposal-card`) |
| Round | 11 |
| Frozen at | 2026-08-13 Asia/Seoul |
| Aggregate SHA-256 | `36e3923f66599997f2e4eb56d535a63276b7d6b8f8ee93a1bf3d555cdb7ec247` |
| Files in target | 36 |
| Manifest | `evidence/review-target.txt` |
| Reviews | wave 4 (`MGC-012-P5-T004`…`T007`) on top of the round 10 implementation |

세 lens 모두 시작 시점에 aggregate 를 독립 재계산해 일치를 확인했다. contract 와 failure lens 는
종료 시점에도 재확인했다.

## Verdict

**FAIL. gate 를 열지 않는다.**

| Lens | P0 | P1 | Blocking-P2 | Advisory | Result |
|---|---:|---:|---:|---:|---|
| Contract | 0 | 0 | 3 | 6 | FAIL |
| Failure / Recovery | 0 | 3 | 1 | 3 | FAIL |
| Regression | 0 | 0 | 5 | 9 | FAIL |

Regression 은 mutation 43종 실행 / 29 killed / **14 survived**. kill rate 67% 다.

## What Round 11 Settled

round 10 의 **P0 5건은 하나도 재현되지 않았다.** 전부 KILLED 이거나, 살아남았어도 2차 guard 가
결과를 막는 것이 실험으로 증명됐다.

| Round 10 | Round 11 |
|---|---|
| P0 7 / P1 10 / Blocking-P2 7 (regression) | P0 0 / P1 0 / Blocking-P2 5 |
| mutation 36종, 12 killed (33%) | mutation 43종, 29 killed (67%) |
| `slack_cards.py` 14종 중 4 killed | 14종 중 11 killed |
| `events.py` 6종 중 0 killed | 5종 중 4 killed |

convergence wave 가 죽였다고 주장한 **15개 site 전부**를 regression lens 가 evidence 파일을
믿지 않고 직접 mutate 해 KILLED 를 재현했다. wave 의 주장은 사실이었다.

`slack_http.py` 는 5종 전부 killed 로 이 저장소에서 가장 촘촘한 영역이다.

## Consolidated Findings

중복을 제거한 목록이다. 이것이 wave 5 의 범위다.

| ID | 등급 | 대상 | 내용 | lens |
|---|---|---|---|---|
| `R-1` | **P1** | `slack_http.py:596-603` | round 10 `C-1` 의 **두 번째 사본이 고쳐지지 않았다.** bare `BaseException` 이 dispatcher 밖으로 그대로 샌다 | failure |
| `R-2` | **P1** | `slack_projection.py:899`, `events.py:3231-3238` | 일시적 store 실패가 재시도 예산을 쓰지 않고 destination 전체를 되돌릴 수 없이 멈춘다. T004 가 반대편으로 치우쳤다 | failure |
| `R-3` | **P1** | `ingress.py:282-301`, `contracts/interaction-feedback.md` | ingress 재시도 소진이 영구 무응답이다. 계약이 침묵의 근거로 쓴 전제가 코드에서 거짓이다 | failure, contract |
| `R-4` | Blocking-P2 | `tests/test_slack_ack_boundary.py:1723-1738` | producer 계약을 고정한다는 test 가 동어반복이라 producer 부재를 못 잡는다 | contract |
| `R-5` | Blocking-P2 | `spec.md:120` FR-024 | 공개 outcome 5개 중 4개만 열거한다. `unavailable` 이 빠졌다 | contract |
| `R-6` | Blocking-P2 | `slack_projection.py:1002,1012`, `slack_http.py:591,602` | `generator_exit` 분기가 두 파일 모두 test 0건이다 | failure |
| `R-7` | Blocking-P2 | `review_cards.py:634`, `events.py:1247` | review-card 무결성 대조의 `payload_json`·`actor_id` 성분이 각각 무방비다 | regression |
| `R-8` | Blocking-P2 | `review_cards.py:226` | outbox cardinality 를 약화해도 잡히지 않는다 | regression |
| `R-9` | Blocking-P2 | `migrations.py:4193,4950` | migration history version 연속성과 `store_kind` 검사가 무방비다 | regression |

### R-1 — 결함의 사본 하나만 고쳤다

`src/amplai_foundry/governance/slack_http.py:596-603` 이 round 10 `C-1` 과 같은 코드를 그대로
갖고 있다.

```python
def _raise_sanitized_interruption(kind: str) -> NoReturn:
    ...
    raise BaseException("Slack message delivery interrupted.") from None
```

실물 `deliver_next` 관측값이 round 10 `C-1` 과 동일하다.

```
ESCAPED ('BaseException', False)
EVENT [('leased', 1, None, 'worker')]
DLQ (0,)   HOLDS (0,)
```

Review event 는 T004 가 `slack_projection.py:941-963` 에서 잡는다. **non-review event 는
`:942` 의 `if not is_review: raise` 로 그대로 통과한다.** 둘은 같은 destination 을 공유한다.

`grep -rn "base_exception" tests/` 는 0건이다. T004 의 negative verification 이
`slack_projection.py` 사본만 확인했다. 같은 결함을 고칠 때 저장소 전체에서 같은 형태를 찾지
않은 것이 원인이다.

수렴하기는 한다 — lease 만료 × `max_attempts` 뒤 `events.py:3219-3229` 가
`OUTBOX_POST_SEND_RECONCILE_REQUIRED` 로 dead letter 를 만든다. 그 값은 원인이 아니고 그때까지
worker 는 매번 죽는다.

### R-2 — T004 가 반대편으로 치우쳤다

`REVIEW_CARD_PREPARE_FAILED` 는 `OutboxReconcileError` 라 `unreconcilable=True` 로 즉시 dead
letter + operator hold 다. 재시도 예산 3을 한 번도 쓰지 않는다.

```
FIRST dead_letter REVIEW_CARD_PREPARE_FAILED attempts 1
SECOND-CLAIM None
DEST-ROW [(..., 1, 0)]                       # operator_hold = 1
HOLD-RESOLVE BLOCKED IntegrityError governance operator hold is append-only
```

lock 이 풀린 뒤에도 그 destination 의 모든 event 가 영구히 claim 되지 않는다
(`events.py:3004` `WHERE d.operator_hold = 0`). hold 를 푸는 governed API 가 없다.

**같은 저장소가 스스로 반대로 적어놨다.** `slack_projection.py:446-449` 는 "transient 를
terminal 로 분류하면 destination 전체가 즉시 멈추고, 그 hold 는 되돌릴 수 없다" 고 쓴다.
`ingress_worker.py:250-259` 는 **같은** `sqlite3.OperationalError` 를 `RETRY` 로 분류하고 그
test 이름이 `test_a_transient_sqlite_error_retries_instead_of_holding` 이다. 한 저장소에서 같은
예외가 두 정책을 받는다.

`sqlite3.OperationalError` 는 `database is locked` 뿐 아니라 `disk I/O error`,
`database or disk is full`, `readonly database` 를 포함한다. store busy timeout 은 5초다.

round 10 `C-1` 이 "선언된 회복은 dead letter + operator hold" 라고 명시해 그대로 따른 결과다.
그 지시 자체가 transient 와 terminal 을 구분하지 않았다. **review 지시를 그대로 구현한 것이
새 결함을 만든 사례다.**

### R-3 — 영구 무응답 (두 lens 독립 확인)

contract lens 는 코드 경로로, failure lens 는 실행으로 같은 결론에 도달했다.

```
ATTEMPTS [(RETRY,'INGRESS_DECISION_UNAVAILABLE'), (RETRY,'INGRESS_DECISION_UNAVAILABLE'),
           None, None, None, None]
FINAL dead_letter 2 INGRESS_DECISION_UNAVAILABLE
FEEDBACK []
STRANDED ['CMD-...']
```

`ingress.py:285-288` 이 `claim_next` 안에서 `retry_wait AND attempts >= max_attempts` 를
`dead_letter` 로 옮긴다. claim 조건은 `attempts < max_attempts` 라 그 row 를 다시 claim 하지
않는다. `_with_feedback` 이 영영 돌지 않는다.

reviewer 는 HTTP 200 ack 을 받고 그 뒤 ephemeral 도 Result Card 도 못 받는다.

계약이 침묵의 근거로 쓴 문장은 "the next attempt decides" 다. 소진된 command 에는 다음 attempt
가 없다. `unavailable` 은 D-034 로 recovery hold 전용이 되어 여기서 쓸 수 없다. **T007 이 이
종점을 덮을 수 있던 유일한 mapping 을 없앴다.**

`ingress.stranded()` 가 operator 에게는 보인다. reviewer 에게는 아무것도 안 간다.

### R-4 / R-5 — wave 4 가 만든 계약 결함

`R-4` — `test_every_safe_outcome_value_has_a_producer` 가 producer 를 한 번도 보지 않는다.
`produced` 를 enum 자신에서 hardcoded 문자열로 걸러 만들고 좌우변을 비교한다. 동어반복이다.
`ingress_worker.py:389` 의 `return SafeInteractionOutcome.UNAVAILABLE` 을 지워도 통과한다.

계약 `interaction-feedback.md:31-33` 이 "Every listed outcome has a producer" 를 새로 선언했고
`T007` evidence AC-01 이 "신규 test 가 이를 고정한다" 고 적었으나 **고정하지 않는다.**

`R-5` — FR-024 는 `denied, expired, stale, already-completed` 넷을 적는다. 코드가 만드는 safe
outcome 은 다섯이다. `unavailable` 은 실제로 사용자에게 문자열을 보낸다
(`slack_http.py:533`). `T007` evidence AC-02 가 "일치한다" 고 적었으나 4 대 5 다.

두 건 다 T007 의 목적(3자 정렬)을 스스로 어겼다.

### R-7 ~ R-9 — 남은 회귀 방어 공백

| # | mutation | 결과 |
|---|---|---|
| M39 | `review_cards.py:634` 에서 `payload_json` 성분만 제거 | **SURVIVED** |
| M40 | `events.py:1247` 에서 audit `actor_id` 성분 제거 | **SURVIVED** |
| M41 | `review_cards.py:226` outbox cardinality `!= 1` → `< 1` | **SURVIVED** |
| M26 | `migrations.py:4193` history version 연속성 제거 | **SURVIVED** |
| M28 | `migrations.py:4950` `store_kind` metadata 검사 제거 | **SURVIVED** |

**M39 는 T006 evidence 의 판단을 뒤집는다.** T006 은 이 대조 전체를 "immutability trigger 로
보호돼 도달 불가" 로 분류했다. regression lens 가 성분을 하나씩 나눠 보고 반박했다 — `row[1]`
검사는 저장된 digest column 과 event payload 의 digest 를 비교할 뿐이고, **`row[2]` 가 그
digest 의 preimage 인지는 아무도 확인하지 않는다.** redundant 가 아니다.

M26 은 history 에 구멍이 있어도(1,2,4) `verify` 가 4를 반환한다. migration 3 은 영영 적용되지
않는데 store 는 version 4 로 보고한다.

## Corrections To Earlier Records

round 11 이 앞선 기록 셋을 정정한다.

**round 10 `C-12` (P0) → Advisory.** 확정. 세 mutation 대조가 결론이다.

```
[M16] SURVIVED  # raw_token in idempotency_key 만 제거
[M17] KILLED    # _contains_persisted_secret 만 제거
[M18] KILLED    # 둘 다 제거
```

raw token 은 `secrets.token_hex(16)` 이라 항상 32자 lowercase hex 이고
`_contains_persisted_secret` 은 정확히 그 형태의 substring 을 전부 뽑아 persisted hash 와
대조한다. 이번 호출의 token 은 이미 저장돼 있으므로 반드시 걸린다. credential 이 durable
column 으로 새는 결과가 성립하지 않는다.

**round 10 `C-13` (P1) → Advisory.** 확정. 두 절단이 같은 200자 bound 를 건다. 한쪽만 지우면
SC-006 이 유지되고, 둘을 동시에 지워야 깨지며 그때 test 가 잡는다. regression lens 가 역방향도
확인했다 — `_text_object` 절단을 지워도 모든 field 가 상류에서 이미 bound 된다.

**`T007` evidence 의 도달 가능성 판정은 틀렸다.** `result_identity_exceeds_body_limit` 를
"구조적 하한이 없다" 고 적었으나 **도달 불가**다. `project_limit < 4` 에 닿으려면 revision 자릿수
합이 146 이상이어야 하는데, 두 값은 sqlite `INTEGER` (8byte) 라 각각 최대 19자리, 합 38 →
`project_limit = 111` 이 하한이다. column type 이 하한을 준다.

**`T006` evidence 의 "미지원 status 는 이미 test 가 있다" 는 근거가 없다.** `test_slack_cards.py`
에 `render_result` 를 미지원 status 로 부르는 test 는 없다. 다만 `render_result` 의 유일한
호출자가 `slack_presentation_payload:273` 에서 먼저 걸러내므로 그 guard 는 도달 불가이고,
`DecisionProjectionPayload.proposal_status` 의 pattern 도 같은 역할을 한다. 등급은 Advisory 다.

**`T006` evidence 의 "무결성 대조는 전부 도달 불가" 는 부분적으로 틀렸다.** 위 M39 참조.

## Verification Evidence

| Check | Result |
|---|---|
| Full pytest | 1202 passed, 4 deselected |
| Ruff check | exit 0 |
| Ruff format check | exit 0, 135 files |
| mypy | exit 0, 102 source files |
| Schema check | exit 0 |
| Vault knowledge lint | exit 0 |
| `amplai-foundry verify --root .` | 7/7 PASS |
| Task manifest validator | 8/8 PASS |
| `git diff --check` | PASS |

live Slack E2E 는 실행하지 않았다. 실행하지 않은 검증을 PASS 로 세지 않는다.

## Method

| Lens | 방법 |
|---|---|
| Contract | spec FR/SC, contract 3종, data-model, manifest scope 를 구현과 대조. T007 정렬을 가장 세게 심문 |
| Failure / Recovery | T004 fix 를 실물 dispatcher 로 재검증. credential 유출, 부분 실패·crash 복구, idempotency, 취소·만료, fail-closed 를 repro 로 확인 |
| Regression | 전용 격리 복제본(단독 사용, baseline `1202 passed`)에서 mutation 43종. 전 mutation 을 line index 로 적용. SURVIVED 는 full suite green 으로만 판정. 매 mutation 후 복원·검증 |

**round 10 의 방법론 결함을 고쳤다.** 격리 복제본을 regression lens 전용으로 새로 만들었고 다른
작업이 접근하지 않았다. 시작·종료 모두 source 와 byte-identical 임을 확인했다. journal 은
mutation 1건마다 즉시 append 했다.

세 lens 모두에 "evidence 파일을 믿지 말고 직접 재검증하라" 를, regression lens 에는 "P0 로
매기기 전에 다른 guard 가 결과를 덮는지 먼저 증명하라" 를 명시했다. 후자가 round 10 의
과대평가 두 건을 닫았다.

## Could Not Verify

- **round 10 survivor 24건 중 21건만 식별 가능하다.** 나머지 3건은 round 10 기록에 mutation 이
  명시돼 있지 않아 대응시키지 못했다
- **round 10 M10 (`review_fallback_exceeds_limit`)** 은 round 11 에서 재실행하지 않았다.
  `A-7` 에서 도달 불가로 닫혀 있으나 독립 확인은 없다
- **`BaseExceptionGroup` 의 실제 발생 빈도.** `R-1` repro 는 `_call` 을 대체해 만들었다.
  결함 자체는 도달 경로와 무관하게 실측됐다
- **`R-2` 의 hold 를 운영자가 out-of-band 로 복구하는 절차가 문서에 있는지** 확인하지 않았다
- **동시성 하의 rowcount guard** 는 코드·schema·transaction mode 논증까지다. 병렬 writer 실험은
  하지 않았다
- **`read_history` 경로의 `_call` 밖 BaseException** 은 repro 를 만들지 않았다. `R-1` 과 같은
  형태일 수 있다

## Gate Decision

**PASS 하지 않는다.** P1 3건, Blocking-P2 6건이 남는다.
`APPROVALS.md` 와 `DECISIONS.md` 에 Package 5 gate 를 기록하지 않는다.

P0 는 0이다. round 10 대비 실질적인 전진이다. 남은 P1 셋 중 둘(`R-1`, `R-2`)은 wave 4 가 만든
것이거나 wave 4 가 놓친 것이다.
