# MGC-012 Package 2 Review Round 1 — 2026-07-31

## Decision

NOT GATED — round 1 blocker는 모두 해소했으나 수정본 re-review는 미실행이다.

## Scope

- Implementation: `0179fc9`
- Fixes: `23bc421`
- Reviewers: Contract, Evidence, Ops

## Round 1 Result

| Reviewer | Reported blockers |
|---|---|
| Contract | 1 |
| Evidence | 4 |
| Ops | 6 |

중복 제거 후 고유 blocker 8건이다.

## Resolved

| ID | Finding | Resolution |
|---|---|---|
| C1 | `GovernanceEventError`가 worker를 탈출해 command를 `leased`로 방치 | `process`가 `GovernanceEventError`와 잔여 `Exception`을 봉쇄한다 |
| C2 | `_TERMINAL_CODES`에 `PROJECT_ACCESS_DENIED`, `PROVIDER_INSTALLATION_INVALID`, `LEGACY_*` 6종 누락 | 세 소스에서 전수 확인해 보강했다 |
| C3 | finalize와 claim 경로의 store error가 worker loop을 종료 | `FINALIZE_FAILED`, `CLAIM_FAILED` outcome으로 봉쇄한다 |
| T1 | retry 경로 증거 0 | `test_transient_decision_failure_keeps_the_retry_budget` |
| T2 | replay key 도출 미검증 | `test_two_distinct_interactions_do_not_share_a_replay_result` |
| S1 | A9 주장이 증거를 초과 | D-012로 범위를 좁혀 기록했다 |
| S2 | Slack 재시도 전제 미확인 | 공식 문서로 반증하고 D-013에 기록, test 이름을 정정했다 |
| S3 | stranded command 관측 수단 부재 | `IngressService.stranded()` |

## Mutation Evidence

수정 후 두 mutation을 실제로 실행해 test가 잡는 것을 확인했다.

- `idempotency_key` → 상수: 2 failed
- retry → hold: 2 failed

Round 1에서는 두 mutation 모두 24 test를 통과했다.

## Discovered

같은 Slack channel의 두 번째 proposal decision은 `OUTBOX_SOURCE_REVISION_CONFLICT`로
실패한다. Package 2는 봉쇄와 관측만 하고 destination granularity 수정은 Package 3
범위다. D-014에 기록했다.

## Verification

- Tests: `685/685 PASS`
- Canonical verification: `7/7 PASS`

## Deferred

- Activation Record의 timeout과 backlog threshold 확정 — MGC-015
- SPEC "unfinished ingress" startup check — MGC-016
- `src/` 전역 logging 부재 — workstream 범위 밖

## Next

수정본에 대한 Contract·Evidence·Ops re-review를 실행한 뒤 gate를 판정한다.
