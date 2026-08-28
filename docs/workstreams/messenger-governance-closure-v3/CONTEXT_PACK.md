# Messenger Governance Closure V3 Context Pack

## Resume State

- Workstream: `messenger-governance-closure-v3`
- Current: **Package 5 wave 7** — round 13 이 FAIL 이다. blocker 9건(P1 2건)을 닫는다
- Active feature pointer: `.specify/feature.json` → `specs/003-slack-proposal-card`
- Package 5 task: `MGC-012-P5-T001`~`T014` 전부 `done`. **wave 6·7 은 manifest 가 없다 —
  `CT-3` 이 그것을 blocker 로 잡았다**
- Gate 이력: round 2–8 not approved, round 9 not assessed, round 10 FAIL, round 11 FAIL,
  round 12 FAIL, **round 13 FAIL**. **Package 5 gate 는 아직 없다**
- Latest measurement: full pytest `1274 passed, 4 deselected`, `verify` 7/7,
  Ruff·mypy exit 0, manifest validator 14/14, `git diff --check` PASS
- Worktree: branch `mgc-012-package-3-wave-4`. **커밋하지 않았다**

## Next Action — 다음 세션은 여기서 시작한다

**`/grill-me`.** 설계 판단이 **둘**이고 나머지 일곱은 기계적이다.

1. **`CT-1`** — `D-039` 가 만든 세 번째 종점(결정 기록됨 → 침묵)이 FR-026 의
   `MUST produce the unavailable safe outcome` 을 어긴다. **spec 을 고칠지, 코드를 고칠지**가
   판단이다. `C-2` 사건의 거울상이라 이번엔 Decision 을 먼저 받아야 한다
2. **`P1-1`** — `committed_decision()` 읽기가 실패할 때 무엇을 할지. 지금은 예외가 worker
   밖으로 나가 command 가 `leased` 로 남는다

그 뒤 **반드시** `/speckit-clarify` → `D-041` → **`/taskify`** → validator → `tasks.md` 생성 →
`/speckit-analyze` → `/speckit-implement` → round 14 freeze → three-lens.

**`/taskify` 를 건너뛰지 않는다.** `CT-3` 이 wave 6 의 절차 생략을 Blocking-P2 로 잡았다.

## Round 13 Blockers — 9건

기록: `specs/003-slack-proposal-card/evidence/3lens-review-round-13.md`

| id | 등급 | 위치 | 내용 |
|---|---|---|---|
| `F-1` | **P1** | `slack_http.py:445`, `:471` | **bot token 이 traceback 에 샌다.** `read_history`·`delete_message` 가 `SlackTransportError` 를 sanitize 하지 않는다. real urllib 로 실측됨 |
| `P1-1` | **P1** | `ingress_worker.py:444` | `committed_decision()` 읽기가 `try` 밖. 실패하면 예외가 새고 command 가 `leased` 로 남는다. **실패 모드가 상관돼 있다** — 재시도를 만든 조건이 이 읽기도 실패시킨다 |
| `CT-1` | Blocking-P2 | `spec.md:133` | FR-026 위반, Decision 없음 |
| `CT-2` | Blocking-P2 | `contracts/interaction-feedback.md:33` | 계약은 종점 둘, 코드는 셋 |
| `CT-3` | Blocking-P2 | `index.yaml` | wave 6 에 manifest 가 없고 `T011` scope 를 무효화했다 |
| `F-2` | Blocking-P2 | `slack_http.py:544` | `_clear_exception_frames` 무방비 |
| `F-3` | Blocking-P2 | `ingress_worker.py:446` | 승격된 hold 의 원인 code 무방비 |
| `F-4` | Blocking-P2 | `events.py:1261`, `:1262` | 감사 대조 형제 성분 둘 무방비 |

## 세 가지 반복되는 실패 양식

**다음 wave 는 이것을 먼저 읽는다.**

1. **형제 위치를 안 센다 — 네 라운드 연속.** round 11 `R-1`, 12 `F-2`, 12 `RL-3~5`,
   13 `F-1`. 규칙은 이미 적어놨다: **정의 사본 + 호출 지점 + 같은 검사의 모든 성분.**
   wave 6 이 그 규칙을 적어놓고 스스로 어겼다
2. **test 가 구조상 통과한다 — 세 라운드 연속.** wave 6 의 token 누출 test 는 fixture 가
   `del request` 로 검사 대상을 먼저 지운다. **test 를 쓸 때 fixture 가 대상을 없애는지 본다.**
   mutation 이 살아남으면 코드보다 test 를 먼저 의심한다
3. **한 방향을 고치며 반대를 만든다.** round 10 `C-1` → `R-2` → `F-1`(12) → `P1-1`(13).
   **review 지시를 그대로 구현하기 전에 다른 축이 무너지는지 본다**

## Do Not Report As New Findings

`D-038` 항목 1(operator hold 해제 없음), 항목 6(lease 만료 침묵), 항목 4
(`_clear_exception_frames` 4사본). round 12·13 의 Advisory 는
`3lens-review-round-13.md` 의 Remaining Advisory 표에 있다.

## Process Decisions So Far

| ID | 내용 |
|---|---|
| `D-033` | `/speckit-implement` 앞 사람 승인 제거 |
| `D-034` | safe outcome 계약을 구현에 맞춤 |
| `D-035` | frozen target 구성 |
| `D-036` | `/speckit-analyze` 필수 |
| `D-037` | `/speckit-clarify` 필수 |
| `D-038` | round 11 `R-1`·`R-2`·`R-3` 판단 6개 |
| `D-039` | 결정이 기록된 소진은 침묵 |
| `D-040` | FR-027 이 재시도 분류에서 FR-021 에 우선 (안 B) |

## Deferred Phase — Package 4

- Item: `MGC-012-T010` — real Slack workspace reference E2E
- Downstream order: `T010 → T013 → T012`
- Secret rule: token/signing secret을 출력하거나 repository 문서에 남기지 않는다
- **Package 5 review가 닫히기 전에는 Package 4 구현을 재개하지 않는다**
- E-3/E-4 재검토 필요: repository 밖 credential 파일이 5개 key로 존재하고 Package 5 `T003`이
  그것으로 live Slack E2E 2건을 통과시켰다. 이는 P5 evidence의 기록이지 T010 acceptance가 아니다

## Durable Evidence

- `CURRENT_ITEM.md`, `STATUS.md`
- `DECISIONS.md` D-031~D-037
- `APPROVALS.md` APR-016/APR-017
- `CHECKPOINTS/MGC-012-package-4-wave-6r-gate-2026-08-12.md`
- `specs/003-slack-proposal-card/evidence/`
  - `review-target.txt` — round 11 target, round 2–10 supersede, round 10·11 closure,
    round 11 header correction
  - `3lens-review.md` — round 10 (FAIL)
  - `3lens-review-round-11.md` — round 11 (FAIL)
  - `mutation-round-11.md` — mutation 43종 journal
  - `MGC-012-P5-T00{1..7}.md` — per-task evidence
- External `SPEC.md` expected SHA-256:
  `e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3`

## Aggregate History

| Round | Aggregate | 결과 |
|---|---|---|
| 9 | `1190d6c4…` | not assessed. lens 미실행 + freeze 후 drift, blob 복원 불가 |
| 10 | `89c44faa…` | FAIL. closure 후 `index.yaml`·`tasks.md` drift → `60549622…` |
| 11 | `36e3923f…` | FAIL. closure 후 같은 두 파일 drift → `bb1fa49c…` |
| 12 | 미정 | wave 5 완료 후 D-035 구성으로 새로 얼린다 |

round 10과 11의 drift는 둘 다 task bookkeeping이 frozen set 안에 쓴 것이고 source·test·spec·
contract는 불변이었다. 둘 다 `review-target.txt`에 기록했다. D-035가 이 구조를 닫는다.
