# MGC-012 Package 3 Gate — 2026-08-07

## Decision

PASS

## Evidence

- Implementation and review record: `5b2024b`
- Gate commit: 이 문서의 commit
- Tests: `898/898 PASS`
- Canonical verification: `7/7 PASS` at `5b2024b`
- Knowledge lint: `0 errors, 0 warnings, 56 notes`
- Review: wave 4 three-lens round 2 blocker 0 at `5b2024b`

Package 3 는 wave 4개로 나뉘고 wave 마다 3 lens review 를 돌렸다. wave 1~3 의 gate 는
각 review checkpoint 에 있고, 이 문서는 Package 3 전체를 닫는다.

## Waves

| Wave | Task | Review | 결과 |
|---|---|---|---|
| 1 | T001 transport Protocol·error 분류 | round 4 에서 blocker 0 | PASS |
| 2 | T002 send path·marker·receipt | round 3 에서 blocker 0 | PASS |
| 3 | T003 bounded fail-closed reconcile | round 2 에서 blocker 0 | PASS |
| 4 | T004 순서·supersession / T005 crash·실패 행렬 | round 2 에서 blocker 0 | PASS |

## Wave 4 Review Rounds

| Round | contract | failure-recovery | regression | 고유 blocker | 결과 |
|---|---|---|---|---|---|
| 1 | BP2 4 | P1 1 / BP2 1 | 0 | 6 | 전부 해소 |
| 2 | BP2 5 | 0 | 0 | 5 | 전부 해소 |

round 2 의 5건 중 둘은 round 1 **수정이 만든** 새 결함이다. 나머지 셋은 부분 폐쇄와
잔여다. 자세한 내역은 `CHECKPOINTS/MGC-012-package-3-wave-4-review-2026-08-07.md` 다.

가장 컸던 것은 round 2 의 NEW-1 이다. round 1 이 post-send guard 의 경계를 `attempts`
단독으로 적었는데 거짓이다 — `last_error_code == 'OUTBOX_LEASE_EXPIRED'` 와의 AND 다
(`events.py:2844`-`2847`). probe 로 반증하고 AC-10 과 test 로 반대쪽을 고정했다.

## Mutation Evidence

wave 4 regression lens 가 격리 `git worktree` 에서 16종을 돌렸다. **16종 사망, survivor 0.**

post-send guard 는 조건을 절반씩 나눠도 각각 다른 test 에 잡힌다 — 한쪽만 지워도 살아남는
구멍이 없다. `operator_hold` gate 와 앞 sequence gate 는 wave 4 이전까지 어떤 test 도
죽이지 못했고, 이번에 teeth 를 줬다.

## Delivered Scope

Package 3 는 **production code 를 `slack_projection.py` 하나만 추가했고**, 기존 파일은
`projections.py` 의 사전 검증 예외 종류만 바꿨다 (D-022). wave 4 는 production code 를
한 줄도 안 바꿨다.

- `SlackProjectionDestination` — `ProjectionDestination` 의 두 번째 구현
- 주입 transport Protocol (send·read). 실제 HTTP 는 Package 4
- message marker read-back 으로 판정하는 `reconcile()`. remote 가 진실 원천이다
- fail-closed 실패 분류 — transport 층은 재시도, 미지의 Slack code 는 terminal
- 순차 전달·경쟁 dispatcher 직렬화·supersession·`delivered_sequence` 추적
- crash recovery 와 실패 행렬. 지워진 Card 는 조회 범위와 guard 로 세 갈래

dependency 는 `pydantic`·`pyyaml`·`typer` 셋을 유지했다. schema 변경 없다.

## Scope Expansion

- D-025 — T005 의 acceptance 정정 둘과 신설 셋. 사용자 승인을 받았다 (선택지 셋 중 첫째).
  AC-02 원문은 D-023 이전 판이었고 AC-07 은 이 repo 에 없는 state 를 가리켰다

## Accepted Advisory

수용하고 기록만 한다. 근거는 wave 4 review checkpoint 에 있다.

- `max_attempts=1` 구성에서는 post 를 한 번도 안 한 event 도 post-send guard 에 걸려
  영구 hold 가 된다. 이번 변경 이전부터 있던 동작이고 fail-closed 선택이다
- synthetic hold gate test 는 production 이 못 만드는 상태를 못박는다. 그 gate 가 유일한
  killer 를 잃으면 즉시 무방비로 돌아가기 때문에 둔다
- page **사이**의 순서는 무방비다. cursor 를 우리가 만들지 않아 destination 에서 못 막는다
- `SLACK_PROJECTION_MARKER_DIGEST_MISMATCH`·`SLACK_PROJECTION_METADATA_UNREADABLE` 은
  dispatcher 층 code 문자열이 미고정이다. 메커니즘은 search-cap test 가 증명했다

## Process Violation

wave 3 review 가 "mutation 을 돌리는 lens 와 코드를 읽는 lens 를 동시에 돌리면 안 된다"
고 기록했는데 wave 4 는 round 1·2 모두 셋을 동시에 돌렸다. round 2 의 NEW-4 (재현 안 된
1회 실패) 가 그 결과일 가능성이 크다. **wave 5 부터 regression lens 는 단독으로 돌리고
mutation 은 격리 worktree 에서만 한다.**

## Deferred

- `OQ-003` — Slack message metadata 크기 상한 미확인. marker 에 payload 본문을 안 넣어
  회피 중이고 실측은 Package 4
- lease 가 `reconcile` 도중 만료되면 terminal 판정이 DLQ·hold 어디에도 안 남는다.
  `events.py` 를 건드려야 해서 별도 item
- taskify validator 가 `done` task 에 `completed_at` 을 요구하는데 T002·T003 이 그 값을
  안 채워 실패한다. wave 4 는 T004·T005 만 채웠다. 별도 item
- D-014 destination granularity, R-007 `Retry-After` 주입, R-008 `chat.update` — 전부
  별도 item

## Next

Package 4 — Slack reference E2E, activation isolation, closure review. `/taskify` 로
task manifest 를 만드는 것이 다음 단계다. Slack test workspace 구성과 credential 경로는
**아직 확인 안 됐다** (D-019 항목 8).
