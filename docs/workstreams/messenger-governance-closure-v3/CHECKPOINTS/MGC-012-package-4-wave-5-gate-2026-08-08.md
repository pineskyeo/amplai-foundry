# MGC-012 Package 4 Wave 5 Gate — 2026-08-08

## Decision

PASS

## Evidence

- Implementation: `0e8e9bb`
- Review round 1 fixes: `ec3b8fa`
- Review round 2 fixes: `dfbcd15`
- Gate commit: 이 문서의 commit
- Tests: `987/987 PASS`
- Canonical verification: `7/7 PASS` at `dfbcd15`
- Knowledge lint: `0 errors, 0 warnings, 56 notes`
- Review: wave 5 three-lens 2 라운드. 종료 시점 blocker 0.
  근거는 `CHECKPOINTS/MGC-012-package-4-wave-5-review-2026-08-08.md` 다.

wave 5 는 Package 4 의 첫 wave 다. Package 3 가 wave 1~4 였고 번호를 이어 쓴다
(D-019 항목 3).

## Tasks

| Task | 내용 | 상태 |
|---|---|---|
| MGC-012-T006 | stdlib 기반 실제 Slack HTTP transport | done |
| MGC-012-T007 | credential 경로와 비노출 | done |

## Review Rounds

| Round | contract | failure-recovery | regression | 고유 blocker | 결과 |
|---|---|---|---|---|---|
| 1 | P1 3 / BP2 2 | P0 1 / P1 2 / BP2 1 | (미실행) | 8 | 전부 해소 at `ec3b8fa` |
| 2 | — | — | BP2 2 / Adv 4 | 2 | 전부 해소 at `dfbcd15` |

round 1 은 읽기만 하는 두 lens 를 동시에 돌렸고, mutation 을 돌리는 regression lens 는
round 2 에서 **단독으로** 돌렸다. wave 3·4 가 기록한 process 규칙을 이번에는 지켰다.

round 2 의 둘은 live bug 가 아니라 test gap 이다. 다만 하나는 round 1 의 수정이
**미완**이었음을 드러냈다 — 예외 유출 fix 가 try 안쪽만 덮고 바깥 형제 경로를 안 덮었다.

## Mutation Evidence

round 2 regression lens 가 37개를 돌렸다. 1건은 equivalent mutant 로 판명해 제외하고
유효 36건 중 **33 killed, 3 survived** 였다. survivor 셋은 전부 이 gate 전에 닫았고 각각
mutation 으로 죽는 것을 확인했다.

round 1 fix 여덟 중 일곱은 그 자리에서 teeth 가 확인됐다. 나머지 하나
(`add_unredirected_header`) 가 survivor 였고 round 2 에서 닫혔다.

기존 898 test 는 약화되지 않았다. wave 5 두 commit 이 건드린 파일은 신규 2개뿐이고,
`slack_projection.py` 를 mutate 해 기존 test 가 여전히 죽는 것을 8종으로 확인했다.

## Delivered Scope

- `HttpSlackTransport` — `SlackTransport` Protocol 의 실제 HTTP 구현. runtime dependency
  셋을 유지했다 (stdlib `urllib`)
- 모든 실패를 `SlackTransportError` 로 감싸는 경로. body 생성과 `Request` 구성까지 포함
- `ok: false` 를 Slack 봉투의 판별자로 쓰는 error code 추출
- monotonic deadline 을 가진 응답 읽기와 크기 상한
- redirect 거부 + `add_unredirected_header` 이중 방어
- 생성자가 강제하는 호출 예산 검증
- credential 경로 — 환경변수 둘, 한 지점 읽기, 부분 구성은 오류

## Accepted Advisory

- **`classify_slack_failure` 의 "규칙 순서가 우선순위" 서술은 동작으로 관측되지 않는다.**
  매칭되는 세 규칙이 전부 `RETRYABLE` 을 반환하므로 순열이 전부 같은 동작이다. 429 검사를
  뒤로 내린 mutation 은 equivalent mutant 다. 결함이 아니고, 다음 reviewer 가 같은 것을
  쫓지 않도록 기록한다.
- **fake server 는 Slack 의 실제 수용 여부를 증명하지 않는다.** content type 선택(R-016)과
  `metadata` 를 JSON body 로 보내는 형태는 문서 근거일 뿐이다. 실측은 T010 이다.

## Process Finding

**이 repo 에서 `git worktree` 격리는 기본으로 안 된다.** `.venv` 의 editable install `.pth`
가 main tree 를 가리켜 worktree 의 pytest 가 main tree source 를 import 한다. round 2
lens 의 첫 30회 측정이 그것 때문에 무효였다 (21/30 실패). 전용 venv 를 만들거나
`PYTHONPATH` 를 worktree 의 `src` 로 세워야 한다.

**wave 5 부터의 review 규칙에 이 줄을 더한다.** 기존 규칙 — "regression lens 는 단독으로
돌리고 mutation 은 격리 worktree 에서만 한다" — 은 유지한다.

## Real Slack Setup

quickstart A 절을 사용자가 진행했고 `auth.test` 로 확인했다.

| 항목 | 결과 |
|---|---|
| token | `ok: true` |
| workspace | `amplai` (`T0BNPT96BFD`), enterprise install 아님 |
| granted scopes | `channels:history, chat:write` |

H-1.1 과 정확히 일치한다. `chat:write.public` 이 없다.

**확인 못 한 것 둘.** app 이 internal 인지 (API 로 안 나온다), 채널 생성·초대 (channel ID
미확보).

## Deferred

- **T010 이 쓸 값 둘이 계약에 없다** — channel ID (`C...`) 와 app ID (`A...`). `auth.test`
  가 주는 `bot_id` 는 `app_id` 와 다른 값이라 대체할 수 없다. `reconcile` 이 `app_id` 로
  남의 message 를 배제하므로 필수다. T009 또는 T010 이 H-4.1 에 변수를 더한다
- `OQ-003` metadata 크기 상한, `OQ-005` marker 의 workspace 전체 노출, `OQ-006` T011 이
  T005 AC-07 과 다른지 — 전부 이월
- `R-012` 배포 시 rate limit cliff, `lease-expiry-during-reconcile`, `page-ordering` —
  전부 이월

## Next

wave 6 — T008 (readback 자가검사), T009 (E2E harness), T011 (Provider 격리). 셋 다 ready
이고 network 없이 돈다. 같은 test file 을 쓰므로 순차로 돈다.
