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
  전부 not approved, round 9는 not assessed(lens 미실행 + freeze 후 drift), round 10–13은
  전부 FAIL이다. wave 7이 round 13 blocker 9건을 닫았으나 **round 14 review는 아직 안
  돌렸다. Package 5 gate는 없다.** 상세는 아래 Next 절
- Repository Verification: PASS — **1301 passed/4 deselected**, all seven verify stages,
  Ruff check/format, mypy, knowledge lint, manifest validator 20/20, diff check PASS
  (2026-08-17, wave 7 종료 시점)
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

**round 14 three-lens review 는 FAIL 이다. gate 를 열지 않았다.**
기록은 `specs/003-slack-proposal-card/evidence/3lens-review-round-14.md` 다.
target aggregate `48f79c28051b7798c37d6442fabe367fe05cb24cf39edf1805936c0e5102bbab`.

| Lens | P0 | P1 | Blocking-P2 | Advisory |
|---|---:|---:|---:|---:|
| Contract | 0 | 0 | 3 | 6 |
| Failure / Recovery | 0 | 1 | 2 | 3 |
| Regression | 1 | 1 | 1 | 2 |
| **합계(중복 제거)** | **1** | **2** | **5** | **11** |

round 13 대비 P0 0 → **1**, Blocking-P2 7 → 5.

**round 13 blocker 9건의 동작 수정 자체는 사실로 확인됐다** — mutation 11종 전부 재현·killed,
죽은 test 수까지 정확했다. `F-1` 은 12/12 leak 0 이고 positive control 로 측정 유효성도
확인됐다. `PBC-3` 은 wave 7 이 옳고 round 13 이 틀렸음이 확정됐다.

**그런데 그 과정에서 P0 을 만들었다.**

round 13 blocker 9건 전부에 대응했다. 기록은 `specs/003-slack-proposal-card/evidence/`
아래 `MGC-012-P5-T015`~`T019.md` 다.

| id | 등급 | task | 결과 |
|---|---|---|---|
| `F-1` | P1 | T015 | 네 진입점 12 경우 전부 leak 0. mutation 4종 killed |
| `P1-1` | P1 | T016 | 재현 후 `D-042` 로 수정. mutation 7종 killed |
| `CT-1`·`CT-2` | Blocking-P2 x2 | T017 | `D-041` 로 spec·계약을 종점 셋으로. 코드 변경 없음 |
| `CT-3` | Blocking-P2 | T019 | wave 6 항목 backfill, `T011` scope 정정, ID 충돌 셋 정정 |
| `F-2` | Blocking-P2 | T015 | `_send` guard 를 가리던 `post_message` 사본 제거 후 killed |
| `F-3` | Blocking-P2 | T016 | durable `last_error_code` 를 보는 test 로 killed |
| `F-4` | Blocking-P2 | T018 | 블록 1 kill rate 3/9 → 9/9 |

측정: full pytest **1301 passed, 4 deselected** (round 13 의 1274 대비 +27),
`verify` 7/7, manifest validator **20/20**, Ruff check/format·mypy·`git diff --check` PASS.

### Wave 8 대상 — blocker 8건

| id | 등급 | 내용 |
|---|---|---|
| `P0-1` | **P0** | `slack_http.py:604` 의 `body = None` 을 지워도 1301건이 전부 통과한다. `body` 는 button credential 을 든 직렬화 payload 다. `request = None` 은 고정됐고 **바로 다음 줄**이 무방비다 |
| `P1-1` | **P1** | `GovernanceFilesystemError`(RuntimeError)가 잡는 집합 밖이다. `store.connect()` 가 연결마다 filesystem guard 를 두 번 부른다. round 13 `P1-1` 의 피해가 그대로 재현된다 |
| `P1-2` | **P1** | `..._traceback[post_message]` 가 구조상 통과한다. 형제 셋은 mutation 을 죽이는데 하나가 무력이다 |
| `BP2-1` | Blocking-P2 | `stranded()` 에 사람이 부를 수 있는 진입점이 없다. `D-042` 의 보상 통제가 실재하지 않는다 |
| `BP2-2` | Blocking-P2 | 같은 불완전한 `except` 집합이 `_transition`·`claim_next` 에도 있다 |
| `BP2-3` | Blocking-P2 | `spec.md:102` 에 종점 규칙의 네 번째 사본이 살아 FR-026 과 모순된다 |
| `BP2-4` | Blocking-P2 | 종점 3 의 조건을 Decision 없이 좁혔다. 사용자 원답변이 옳았다 |
| `BP2-5` | Blocking-P2 | T018/T020 의 블록·성분 수가 틀렸다. 8블록인데 9로 적었다 |

### 다섯 번째 라운드, 같은 패턴

**형제를 세지 않는다.** 이번엔 세 축에서 동시에 나왔고 세 lens 가 각각 독립으로 짚었다.

| round | 고친 것 | 놓친 것 |
|---|---|---|
| 11 `R-1` | `slack_projection` 사본 | `slack_http` 사본 |
| 12 `F-2` | `post_message` | 나머지 세 진입점 |
| 12 `RL-3`~`RL-5` | 지목받은 성분 3개 | 같은 `if` 의 나머지 2개 |
| 13 `F-1` | `BaseException` arm | `SlackTransportError` arm |
| **14 `P0-1`** | **`request = None`** | **바로 다음 줄 `body = None`** |
| **14 `BP2-2`** | **`try` 밖 store 접근** | **`try` 안 `except` 의 형제 둘** |
| **14 `BP2-3`** | **종점 규칙 사본 셋** | **`spec.md:102` 의 네 번째** |

### 내 evidence 주장 여섯이 틀렸다

1. T017 AC-07 "사본 전수" — `spec.md:102`·`:103` 을 놓쳤다. sweep 이 `exhaust`/`retry budget`
   으로 grep 했는데 그 줄은 "runs out of retries" 다
2. T017 AC-07 "plan/research 에 enum 사본 없다" — 둘 다 있다
3. T016 AC-09 예외 type 일반화 — filesystem guard 를 세지 않았다
4. T016 AC-08 형제 표 — `try` 밖만 물었고 `try` 안 `except` 의 충분성은 안 물었다
5. T018 블록 수 — 8인데 9로 적었다. `L1712` 는 10성분인데 9로 적었다
6. `/speckit-analyze` 가 `spec.md` 파일 내 모순을 놓쳤다

**세 라운드 연속 수치를 틀렸다** — round 12 "21종을 19종", 13 "12종을 9종", 14 "8블록을 9블록".

### Review 판정 중 틀린 것 둘을 정정했다

- **`PBC-3` 은 오판이다.** review 는 그 test 를 "동어반복" 으로 보고 삭제를 지시했다. 성분을
  갈라 재니 두 test 가 **서로 다른 성분**을 잡는다. 지웠으면 `decision is not None` 이
  무방비가 되고 `None.replayed` 로 worker 가 죽는다. 지우지 않고 이름·docstring 만 고쳤다.
  그리고 같은 `if` 의 세 번째 성분은 **아무도 잡지 않았다** — review 도 세지 않았다.
- **`F-4` 는 규모를 크게 축소해 서술했다.** 감사 대조 블록의 형제가 8개 더 있고 전부 같은
  함수 `reconcile_connection()` 안이다. 성분 69개 중 **66개가 무방비**였다. `F-4` 가 지목한
  둘은 그 66 중 둘이다.

### 명시적으로 넘긴 것

`MGC-012-P5-T020` — 나머지 8블록 60 성분. 사용자 결정으로 이번 wave 에서 뺐고 wave 13 에
배치했다. **조용히 줄인 것이 아니다** — 수치와 위치가 T018 evidence, T020 manifest,
`index.yaml` 세 곳에 있다.

### 새 Decision 둘

- `D-041` — 종점 셋을 코드에서 지우는 대신 계약에 적는다. 실측이 코드가 옳다고 정했다
- `D-042` — 결정 장부를 읽지 못하면 침묵한다. 모르는 채 보낸 통지는 되돌릴 수 없다

### 열린 질문

`OQ-001` — bot token 이 traceback 에 남는 것을 금지하는 FR 이 `spec.md` 에 없다. 근거는
`contracts/review-card-lifecycle.md` 뿐이다. FR 신설은 계약 변경이라 Decision 이 필요하고
이번 wave 밖이다. `blocking: false`.

### 다음

```text
round 14 three-lens review — 대상은 T015~T019
```

review 전에 target 을 새로 얼린다. round 13 이후 tree 가 바뀌었다.

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
