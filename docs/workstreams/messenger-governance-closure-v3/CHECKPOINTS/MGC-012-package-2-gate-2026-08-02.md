# MGC-012 Package 2 Gate — 2026-08-02

## Decision

PASS

## Evidence

- Implementation: `0179fc9`
- Review fixes: `23bc421`, `362cc4b`, `5146428`, `f3f7a98`, `7501385`
- Tests: `705/705 PASS`
- Canonical verification: `7/7 PASS`
- Review: round 4 contract·failure-recovery·regression blocker 0

## Review Rounds

| Round | Reported blockers | 고유 blocker | 결과 |
|---|---|---|---|
| 1 | Contract 1 / Evidence 4 / Ops 6 | 8 | 전부 해소 |
| 2 | contract 1 / failure-recovery 3 / regression 2 | 5 | 전부 해소 |
| 3 | contract 2 / failure-recovery 1 / regression 1 | 3 | 전부 해소 |
| 4 | 0 / 0 / 0 | 0 | PASS |

Round 4 이후 non-blocking 증거 공백 3건과 advisory 3건을 추가로 닫았다. `7501385`.

## Mutation Evidence

Reviewer가 4개 round 동안 살아남는다고 보고한 mutation은 전부 test가 잡는다. 각 수정 후
직접 실행해 확인했다.

| Mutation | 잡은 test |
|---|---|
| `idempotency_key` → 상수 | `test_two_distinct_interactions_do_not_share_a_replay_result` |
| retry → hold | `test_transient_decision_failure_keeps_the_retry_budget` |
| `except Exception` 제거 | `test_unclassified_failure_fails_closed_into_retry` |
| event code 전부 retryable | `test_audit_integrity_failure_holds_instead_of_burning_retries` |
| replay 순서 되돌리기 | `test_committed_decision_replays_after_the_actor_loses_permission` |
| `_replay_ingress_result` guard 3종 각각 제거 | `test_a_foreign_result_under_the_same_key_is_a_conflict` |
| `stranded()` retry_wait 과다 보고 | `test_a_command_with_retry_budget_left_is_not_stranded` |
| `stranded()` leased clause 제거 | `test_an_expired_lease_on_the_last_attempt_is_stranded` |
| `stranded()` live lease 과다 보고 | `test_a_live_lease_on_the_last_attempt_is_not_stranded` |
| `_is_corruption` 상시 True | `test_a_transient_sqlite_error_retries_instead_of_holding` |
| `_is_corruption` 상시 False | `test_store_corruption_holds_instead_of_retrying` |
| `_is_corruption` primary code 비교 | `test_extended_corruption_codes_also_hold` |
| `committed_decision` 무검증 | `test_committed_decision_ignores_a_foreign_result` |
| `result_for` blank key 미검증 | `test_result_for_rejects_a_blank_idempotency_key` |
| `process_next` worker_id 미검증 | `test_process_next_rejects_a_blank_worker_id_before_touching_ingress` |
| `LEASE_LOST` code 되돌리기 | `test_lease_loss_during_finalize_keeps_the_denial_code` |

## Delivered Scope

- Bounded synchronous ack — `BoundedIngressAck`, response mapping, budget invariant
- Background decision handoff — `IngressDecisionWorker`, terminal/retryable 분류
- Operator recovery surface — `IngressService.stranded()`, `committed_decision()`
- Replay precedence 교정 — `decisions.py`

## Scope Expansion

`decisions.py`의 `decide_ingress_in_transaction` 순서 수정은 MGC-004/006 코드다. 결함은
`a560843`부터 존재했으나 worker가 없어 도달 불가였고 Package 2가 처음 도달 가능하게
만들었다. 직접 `decide()` 경로는 변경하지 않았고 회귀 없음을 확인했다.

## Accepted Advisory

- `CLAIM_FAILED`에 backoff나 bound가 없다. Caller loop 계약으로 남긴다
- `stranded()`가 store 손상 시 raise 한다. Operator read의 fail-closed 계약은 없다
- operator `IngressService`와 worker fleet의 `max_attempts`·clock이 다르면 pre-sweep
  window에서 보고가 어긋난다. MGC-015 Activation Record 배선 제약이다

## Deferred

- Activation Record의 timeout과 backlog threshold — MGC-015
- `dead_letter`에서 나오는 requeue 경로와 SPEC "unfinished ingress" startup check — MGC-016
- provider outbox destination granularity — Package 3, D-014
- `src/` 전역 logging 부재 — workstream 범위 밖

## Next

Package 3 — ordered Slack message projection, retry and recovery
