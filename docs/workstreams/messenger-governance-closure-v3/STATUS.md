# Messenger Governance Closure V3 Status

## Summary

| Field | Value |
|---|---|
| Workstream | `messenger-governance-closure-v3` |
| Status | in-progress |
| Current Item | `MGC-012` |
| Completed | `11/16` |
| Baseline | `e78858cba73c70307385458ad37fe2c41e34173b` |
| Spec | `AMP-SPEC-MGC-003` |

## Gate

- Planning: PASS
- Implementation: MGC-001–011 PASS / MGC-012 ACTIVE
- Subagent Review: MGC-012 Package 1 PASS at `ef35201` — P0/P1/Blocking-P2 0
- Subagent Review: MGC-012 Package 2 — round 4 three-lens blocker 0 at `f3f7a98`,
  post-gate delta regression review blocker 0 at `2dcf663`. Round 1–3에서 고유
  blocker 16건, round 5에서 1건을 해소했다
- Subagent Review: MGC-012 Package 3 PASS at `5b2024b` — wave 1–4 전부 blocker 0.
  wave 4는 round 1에서 고유 blocker 6건, round 2에서 5건을 해소했다. round 2의 5건 중
  둘은 round 1 수정이 만든 새 결함이다. mutation 16종 survivor 0
- Subagent Review: MGC-012 Package 4 wave 5 PASS at `dfbcd15` — 2 라운드. round 1에서
  고유 blocker 8건(P0 1건 포함), round 2에서 2건을 해소했다. round 2의 둘은 round 1
  수정이 미완이었음을 드러낸 test gap이다. mutation 37종 중 survivor 3은 전부 닫았다
- Subagent Review: MGC-012 Package 4 Wave 6R PASS — T008/T009/T011. Final contract와
  failure/recovery lens는 P0/P1/Blocking-P2/Advisory 0, isolated regression lens는 mutation
  10종 killed/0 survived다. T010/T012/T013은 review·gate 범위 밖이고 BLOCKED를 유지한다
- Subagent Review: MGC-012 Package 5 (`specs/003-slack-proposal-card`) — **미완**. round 2–8은
  전부 not approved, round 9는 not assessed(lens 미실행 + freeze 후 drift), round 10 FAIL,
  round 11 FAIL이다. **Package 5 gate는 아직 없다.** 상세는 아래 Package 5 절
- Repository Verification: PASS — **1202 passed/4 deselected**, all seven verify stages,
  Ruff check/format, mypy, knowledge lint, manifest validator 8/8, diff check PASS
- Frozen source/test evidence:
  `0009be7bc702705f776ee057bb717d607db0168e11c8d86feb3623e428c1f20f`
- Git state: branch `mgc-012-package-3-wave-4`에 커밋했다. **커밋은 gate 통과를 뜻하지
  않는다.** Package 5는 round 11 FAIL이고 gate가 없다. Wave 6R PASS도 commit claim이 아니다

## Package 5 — Slack Proposal Cards

Wave 6R 뒤 `.specify/feature.json`이 `specs/003-slack-proposal-card`로 옮겨갔다. Package 5는
`MGC-012-P5-T001`(Result Card), `T002`(governed Review Card), `T003`(signed interaction feedback
및 E2E) 셋이고 manifest status는 모두 `done`이다. `T003` evidence는 live Slack E2E 2 passed를
포함한다.

구현은 끝났고 **review가 안 끝났다**. 사실 관계는 다음과 같다.

- `evidence/review-target.txt`는 round 9를 `1190d6c4…`로 얼렸다. 그 뒤
  `governance/decisions.py`, `events.py`, `review_cards.py`, `slack_cards.py` 네 파일이
  **test 변경 없이** 수정됐다. round 9 blob은 object store에 쓰인 적이 없어 diff를 복원할 수
  없다. round 9는 assessed로 세지 않는다
- 세 `evidence/MGC-012-P5-T00*.md`는 drift 이전에 작성됐다. 그 command 결과는 지금 없는 tree를
  가리키므로 round 10에서 다시 돌린다
- drift된 tree는 `amplai-foundry verify`의 ruff stage에서 실패했다. `ruff check --fix`가
  `slack_cards.py`의 import block(I001)을, `ruff format`이 `review_cards.py`의 호출 하나를
  고쳤다. 둘 다 동작을 바꾸지 않는다
- 수리 후 재측정: full pytest `1174 passed, 4 deselected`, `amplai-foundry verify` 7/7 PASS,
  Ruff check/format·mypy·schema check·Vault lint(0 errors) exit 0, P5 manifest validator 3/3 PASS,
  `git diff --check` PASS
- round 10 aggregate: `89c44faaf9c637227d834c6d09a39389fb5dbb76205bfdf338655416cf9994e0`

### Round 10 결과와 Convergence Wave

round 10은 **FAIL**이다. `specs/003-slack-proposal-card/evidence/3lens-review.md`가 기록이다.

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 1 | 6 |
| Failure / Recovery | 0 | 1 | 2 | 3 |
| Regression | 7 | 10 | 7 | 0 |

mutation 36종 중 12 killed / 24 survived. kill rate 33%다. `slack_cards.py`는 14종 중 4종만,
`events.py`는 6종 중 0종만 killed다.

**실제 동작 결함은 하나뿐이다** (`C-1`). 나머지는 전부 회귀 방어 부재다. 코드는 오늘 맞게
동작하지만 그 정확성을 붙잡는 test가 없다. 이 구분을 task에도 유지했다.

`/speckit-converge` → `/taskify`로 잔여 작업을 회수해 wave 4~5를 만들었다.

| Task | 성격 | status |
|---|---|---|
| `MGC-012-P5-T004` | send 경로 예외 분류 수정. 유일한 코드 결함 | `ready` |
| `MGC-012-P5-T005` | P0 다섯의 test 공백 | `ready` |
| `MGC-012-P5-T006` | P1·Blocking-P2 test 공백 | `ready` |
| `MGC-012-P5-T007` | D-034 계약·spec 정렬 | `ready` |
| `MGC-012-P5-T008` | 미측정 증거 회수 | `blocked` (T005·T006 대기) |

T001~T003은 `done`·ID·evidence를 그대로 보존했고 renumber하지 않았다. manifest validator 8/8
PASS. `tasks.md`는 재생성했다.

round 10 target은 닫혔다. 이후 `index.yaml`과 `tasks.md` 둘만 바뀌어 aggregate가
`60549622c50e1161b3e32ede8ac147baa5ac2af4e3a5b7a6b89fda382b2516b8`이 됐다. source·test·spec·
contract·data-model은 불변이다. 이 drift는 `review-target.txt`에 기록했다. **round 11은 새로
얼린다.** round 9를 무효로 만든 것이 바로 기록 없는 freeze 후 변경이었다.

### Wave 4 구현과 Round 11 결과

wave 4(`T004`~`T007`)를 구현하고 `T008`까지 닫았다. 여덟 task 전부 `done`이다.
full pytest는 `1174 → 1202 passed`(신규 28건)다.

round 11 aggregate: `36e3923f66599997f2e4eb56d535a63276b7d6b8f8ee93a1bf3d555cdb7ec247`.
기록은 `evidence/3lens-review-round-11.md`와 `evidence/mutation-round-11.md`다.

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 3 | 6 |
| Failure / Recovery | 0 | 3 | 1 | 3 |
| Regression | 0 | 0 | 5 | 9 |

**round 11도 FAIL이다.** 다만 round 10 대비 실질 전진이다.

- round 10의 **P0 5건이 하나도 재현되지 않았다.** 전부 KILLED이거나 2차 guard가 결과를 막는
  것이 실험으로 증명됐다
- mutation kill rate `33% → 67%`(43종 중 29 killed). `slack_cards.py`는 4/14 → 11/14,
  `events.py`는 0/6 → 4/5, `slack_http.py`는 5/5
- convergence wave가 죽였다고 주장한 **15개 site 전부**를 regression lens가 evidence를 믿지
  않고 직접 재현했다. 주장은 사실이었다

남은 P1 셋 중 둘은 wave 4가 만들었거나 놓친 것이다.

- `R-1` — round 10 `C-1`의 **두 번째 사본**이 `slack_http.py:596-603`에 그대로 있다. T004의
  negative verification이 `slack_projection.py` 사본만 확인했다
- `R-2` — T004가 반대편으로 치우쳤다. 일시적 `sqlite3.OperationalError`가 재시도 예산을 쓰지
  않고 destination 전체를 되돌릴 수 없이 멈춘다. 같은 저장소가 `ingress_worker.py`에서는 같은
  예외를 `RETRY`로 분류한다
- `R-3` — ingress 재시도 소진이 영구 무응답이다. contract와 failure 두 lens가 독립 확인했다.
  T007이 이 종점을 덮을 수 있던 유일한 mapping(`unavailable`)을 없앴다

round 11이 앞선 기록 넷을 정정했다. round 10 `C-12`(P0)와 `C-13`(P1)은 과대평가로 확정,
`T007` evidence의 도달 가능성 판정은 오류, `T006` evidence의 "무결성 대조는 전부 도달 불가"는
부분 오류다. 근거는 각 evidence 파일에 있다.

## Next

**Package 5 wave 5**를 다음 세션에서 `/grill-me`로 시작한다. 대상은 round 11의 `R-2`와 `R-3`
둘뿐이고 나머지 7건은 심문할 것이 없다. resume 절차 전체는 `CONTEXT_PACK.md`에 있다.

Package 5 task 여덟 개는 전부 `done`이지만 **gate는 열지 않았다.** round 11이 FAIL이고
P1 3건 / Blocking-P2 6건이 남아 있다. `APPROVALS.md`와 `DECISIONS.md`에 Package 5 gate를
기록하지 않았다.

Package 4의 `MGC-012-T010`·`T013`·`T012`는 그 다음이다. Package 5 review가 닫히기 전에는
Package 4 구현을 재개하지 않는다.

이번 세션에 절차 결정 셋을 확정했다 — D-035(frozen target 구성), D-036(`/speckit-analyze`
필수), D-037(`/speckit-clarify` 필수). 셋 다 `AGENTS.md`·`workflow.yml`·
`.specify/memory/constitution.md`에 반영했다.
### Package 4 복귀 조건

`MGC-012 — Slack Reference Adapter` Package 4 Wave 6R은 T008 readback 자가검사,
T009 E2E harness, T011 Provider 격리까지 PASS했다. Package 4와 MGC-012는 아직 ACTIVE다.

활성 feature pointer는 `specs/003-slack-proposal-card`이므로 `/speckit-*`를 부르면 002가 아니라
003을 잡는다. Package 4로 돌아갈 때 pointer를 먼저 옮긴다.

다음 구현을 자동 시작하지 않는다. T010은 test channel/app invite와 네 Slack 설정값,
T012는 T010·T013 완료와 Python 3.12 clean-clone 환경, T013은 production entrypoint,
official recovery contract, governed recovery item, narrow lifecycle schema에 대한 사용자 승인
기록이 필요하다. 모두 `blocked`를 유지한다.

wave 5 이전에 "아직 없다"고 적었던 셋은 전부 닫혔다. Slack workspace와 app은 만들어졌고
(`auth.test` 확인, scope가 `chat:write`·`channels:history`로 H-1.1과 일치), credential
경로는 T007이 만들었고(`AMPLAI_SLACK_BOT_TOKEN`, `AMPLAI_SLACK_SIGNING_SECRET`),
`conversations.history`의 OAuth scope는 research R-009가 공식 문서로 고정했다.

T009가 H-4.1의 channel ID(`C...`)와 app ID(`A...`) 환경변수 계약을 닫았다.

T010의 E-3/E-4 전제는 그 뒤 바뀌었다. repository 밖 credential 파일이 존재하고(5개 key:
bot token, signing secret, app ID, channel ID, reviewer user ID) Package 5의 `T003`이 그 파일을
`source`해서 live Slack E2E 2건을 통과시켰다. 즉 test channel과 app invite는 실물로 확인된
것으로 보인다. 다만 이것은 P5 evidence의 기록이고 T010 자신의 acceptance로 검증한 것이
아니다. **T010 재개 시 그 범위가 아직 유효한지 먼저 재검토한다.** 값은 이 문서에 쓰지 않는다.

D-014의 provider outbox destination granularity는 Package 4에도 넣지 않는다.
`destination_ref` 형식을 바꾸면 `reconcile_connection`이 기존 durable row를 digest
불일치로 거부하므로 evidence migration이 필요하고, 그건 별도 item이다.

다음 세션은 먼저 workstream을 resume하고, 이어서 아래 command로 blocker 하나만 선택한다.

```text
$pinesky-workstream-next docs/workstreams/messenger-governance-closure-v3
```
