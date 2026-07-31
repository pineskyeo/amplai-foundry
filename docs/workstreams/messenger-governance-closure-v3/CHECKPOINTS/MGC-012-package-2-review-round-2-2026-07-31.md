# MGC-012 Package 2 Review Round 2 — 2026-07-31

## Decision

NOT GATED — round 2 blocker는 모두 해소했으나 round 2 수정본 re-review는 미실행이다.

## Scope

- Implementation: `0179fc9`
- Round 1 fixes: `23bc421`
- Round 2 fixes: `362cc4b`
- Reviewers: contract, failure-recovery, regression — `AGENTS.md` Review Before Gate

## Round 1 Fix Verification

Round 2 reviewer가 독립적으로 확인한 결과다.

| 항목 | 판정 |
|---|---|
| `GovernanceEventError` 봉쇄 | VERIFIED — except 절 순서까지 검증, `process` 탈출 없음 |
| store error 봉쇄 | VERIFIED — `complete()` 반복 실패 실측, loop 생존 |
| `_TERMINAL_CODES` 완성 | PARTIALLY — 세 소스는 완전, 새 소스 누락 |
| `stranded()` | PARTIALLY — 술어 범위 부족 |
| `_COMMAND_COLUMNS` refactor | CLEAN — 20 column 순서 프로그램 대조 |
| round 1 mutation 2건 | 재현 확인, 각 2 failed |

## Round 2 Result

| Reviewer | Reported blockers |
|---|---|
| contract | 1 |
| failure-recovery | 3 |
| regression | 2 |

중복 제거 후 고유 blocker 5건이다.

## Resolved

| ID | Finding | Resolution |
|---|---|---|
| R1 | `stranded()`가 retry 소진 command를 놓쳐 worker 정지 시 빈 결과 반환 | 술어에 `retry_wait AND attempts >= max_attempts` 추가 |
| R2 | `GovernanceEventError` code가 전부 미분류, audit integrity 실패가 5회 재시도 | D-016 — terminal 기본값 + 재시도 allowlist 3종 |
| R3 | `except Exception` fail-closed 절에 test 증거 0 | `test_unclassified_failure_fails_closed_into_retry` |
| R4 | `stranded()`의 `dead_letter` 분기에 test 증거 0 | `test_retry_exhausted_command_is_stranded_before_the_sweep` |
| R5 | replay lookup이 Authority 생성보다 뒤라 SPEC Replay Precedence 위반 | D-015 — 순서 교정, replay를 durable ingress identity에 결합 |

## Mutation Evidence

Round 2에서 살아남은 mutation과 R5 되돌리기를 실행해 각각 의도한 test가 잡는 것을 확인했다.

| Mutation | 잡은 test |
|---|---|
| `except Exception` 제거 | `test_unclassified_failure_fails_closed_into_retry` |
| `stranded()`에서 dead_letter/retry_wait 제외 | `test_retry_exhausted_command_is_stranded_before_the_sweep` |
| event code 전부 retryable | `test_audit_integrity_failure_holds_instead_of_burning_retries` |
| replay 순서 되돌리기 | `test_committed_decision_replays_after_the_actor_loses_permission` |

## Scope Expansion

R5 수정은 `decisions.py`의 `decide_ingress_in_transaction`을 바꾼다. MGC-004/006 코드이며
package diff 밖이다. 이 순서 결함은 `a560843`부터 존재했으나 worker가 없어 도달 불가였고
Package 2가 처음 도달 가능하게 만들었다. 직접 `decide()` 경로는 건드리지 않았다.
`test_decisions.py`, `test_authority.py`, `test_ingress.py` 회귀 없음을 확인했다.

## Verification

- Tests: `689/689 PASS`
- Canonical verification: `7/7 PASS`

## Open Advisory

- `_error_code`의 code 추출 분기 미검증 — 모든 test가 `.code` 없는 error로만 구동한다
- `_TERMINAL_CODES` 23개 중 20개가 개별 test 없음, retryable 쪽은 test 1개에 의존
- `process_next`가 빈 `worker_id`에 `ValueError`를 던진다 — docstring의 "never raise"와 불일치
- `FINALIZE_FAILED`가 원래 denial code를 버린다
- `CLAIM_FAILED`에 backoff나 bound가 없다 — caller loop 계약으로 남는다
- `dead_letter`/`recovery_hold`에서 나오는 requeue 경로가 없다

## Deferred

- Activation Record의 timeout과 backlog threshold — MGC-015
- SPEC "unfinished ingress" startup check — MGC-016
- `src/` 전역 logging 부재 — workstream 범위 밖

## Next

Round 2 수정본에 대한 contract·failure-recovery·regression re-review를 실행한 뒤 gate를
판정한다. `decisions.py` 변경이 포함되므로 regression lens가 특히 필요하다.
