# Messenger Governance Closure V3 Context Pack

## Resume State

- Workstream: `messenger-governance-closure-v3`
- Current: **Package 5 wave 5** — round 11의 P1 3건과 Blocking-P2 6건을 닫는다
- Active feature pointer: `.specify/feature.json` → `specs/003-slack-proposal-card`
- Last gate: Wave 6R `PASS` for T008/T009/T011 only (`APR-017`, `D-032`).
  **Package 5 gate는 아직 없다.** round 2–8 not approved, round 9 not assessed,
  round 10 FAIL, round 11 FAIL
- Package 5 task: `MGC-012-P5-T001`~`T008` **여덟 개 전부 `done`**
- Latest measurement: full pytest `1202 passed, 4 deselected`, `amplai-foundry verify` 7/7,
  Ruff check/format·mypy·schema check·Vault lint exit 0, manifest validator 8/8,
  `git diff --check` PASS
- Worktree: branch `mgc-012-package-3-wave-4`에 커밋했다. **커밋은 gate 통과를 뜻하지 않는다.**
  round 11은 FAIL이고 Package 5 gate는 없다. 커밋은 세션 인계용 저장일 뿐이다
- 커밋해도 review target aggregate는 그대로다. `git hash-object`는 commit 여부와 무관하게
  파일 내용만 본다

## Next Action — 다음 세션은 여기서 시작한다

```text
/grill-me
```

대상은 **`R-2`와 `R-3` 둘뿐이다.** 나머지 7건은 심문할 것이 없다 — 무엇을 할지 이미 확정됐다.

`R-2`와 `R-3`은 서로 얽힌다. `R-2`에서 재시도를 늘리면 `R-3`의 "소진되면 무엇을 보여주나"가
더 중요해지고, `R-2`를 terminal로 두면 `R-3`의 무응답 구간이 짧아진다. 한쪽 답이 다른 쪽 답을
바꾼다. 질문 두 개로 끝날 일이 아니라서 grilling이다.

그 뒤 흐름은 아래 **Wave 5 Plan**이다.

## Round 11 Findings — wave 5의 범위

기록: `specs/003-slack-proposal-card/evidence/3lens-review-round-11.md`

| ID | 등급 | 대상 | 내용 |
|---|---|---|---|
| `R-1` | P1 | `slack_http.py:596-603` | round 10 `C-1`의 **두 번째 사본**이 안 고쳐졌다. bare `BaseException`이 dispatcher 밖으로 샌다 |
| `R-2` | P1 | `slack_projection.py:899` | 일시적 store 실패가 재시도 예산을 쓰지 않고 destination 전체를 되돌릴 수 없이 멈춘다 |
| `R-3` | P1 | `ingress.py:282-301` | ingress 재시도 소진이 영구 무응답이다. 계약이 침묵의 근거로 쓴 전제가 코드에서 거짓 |
| `R-4` | Blocking-P2 | `tests/test_slack_ack_boundary.py:1723-1738` | producer 계약을 고정한다는 test가 동어반복이다 |
| `R-5` | Blocking-P2 | `spec.md:120` FR-024 | 공개 outcome 5개 중 4개만 열거한다. `unavailable`이 빠졌다 |
| `R-6` | Blocking-P2 | `slack_projection.py:1002,1012`, `slack_http.py:591,602` | `generator_exit` 분기가 두 파일 모두 test 0건 |
| `R-7` | Blocking-P2 | `review_cards.py:634`, `events.py:1247` | 무결성 대조의 `payload_json`·`actor_id` 성분이 무방비 |
| `R-8` | Blocking-P2 | `review_cards.py:226` | outbox cardinality를 약화해도 안 잡힌다 |
| `R-9` | Blocking-P2 | `migrations.py:4193,4950` | migration history version 연속성과 `store_kind` 검사가 무방비 |

**`R-2`와 `R-3`만 설계 판단이 필요하다.** 나머지 7건은 기계적이다.

`R-2` 선택지: 재시도 예산을 쓰게 한다 / terminal 유지하되 hold를 푸는 governed 경로를 만든다 /
그대로 두고 문서에만 적는다. 두 번째를 고르면 **새 기능이라 `/speckit-specify`부터 다시 탄다.**

`R-3` 선택지: 소진된 명령에 종결 outcome을 준다(**D-034의 `unavailable` 축소를 되돌리는 일이다**) /
침묵을 유지하되 계약을 사실대로 고친다 / operator 전용으로 둔다(`ingress.stranded()`는 이미 있다).

## Wave 5 Plan

```text
1. /grill-me           — R-2, R-3, 둘의 상호작용
2. /speckit-clarify    — spec 층위 답을 spec.md 에 기록 (필수, D-037)
3. DECISIONS.md        — D-038 기록
4. /taskify            — 9건을 MGC-012-P5-T009~ 로 분해
5. /speckit-analyze    — 필수 (D-036). CRITICAL/HIGH 있으면 정지
6. /speckit-implement
7. round 12 freeze     — D-035 구성대로
8. three-lens review
```

`/speckit-specify`와 `/speckit-plan`은 건너뛴다. 새 기능이 아니라 기존 spec 안의 결함 수정이고
spec 변경은 `R-5` 한 줄뿐이다. 단 `R-2`를 governed 경로 신설로 정하면 이 판단이 뒤집힌다.

## Wave 4 회고 — 같은 실수를 반복하지 않는다

round 11의 P1 셋 중 둘과 Blocking-P2 둘이 wave 4가 만든 것이다. `CURRENT_ITEM.md`의 stop rule로
옮겼고 요지는 넷이다.

1. **결함을 고칠 때 같은 형태가 저장소에 몇 개 있는지 먼저 센다.** `R-1`이 이 규칙 부재로 생겼다.
   `slack_projection.py`의 bare `BaseException`을 고치면서 `slack_http.py`의 같은 코드를 놓쳤고,
   negative verification도 고친 사본만 확인했다
2. **review 지시를 그대로 구현하기 전에 그것이 다른 축을 무너뜨리는지 본다.** `R-2`가 그 사례다.
   round 10 `C-1`이 "dead letter + operator hold"를 선언했고 그대로 따랐는데, 그 지시가
   transient와 terminal을 구분하지 않았다
3. **test가 무엇을 고정하는지 스스로 증명한다.** 겨냥한 guard를 실제로 깨뜨려 실패를 확인하지
   않은 test는 evidence에 "고정한다"고 적지 않는다. `R-4`가 동어반복이었다
4. **계약·spec을 코드에 맞출 때 양쪽 집합의 크기를 센다.** `R-5`가 4 대 5 불일치를 남기고
   일치했다고 기록한 사례다

## Process Decisions Landed This Session

| ID | 내용 |
|---|---|
| `D-033` | `/speckit-implement` 앞의 사람 승인 제거. Pre-Implement Procedure와 구현 후 review가 대신한다 |
| `D-034` | safe outcome 계약·spec을 구현에 맞춘다. `completed` 제거, `unavailable`을 recovery hold 전용으로 |
| `D-035` | frozen target 구성 재정의. per-task manifest YAML을 넣고 `index.yaml`·`tasks.md`·`evidence/**`를 뺀다. **round 12부터 적용** |
| `D-036` | `/speckit-analyze`를 필수로. `/taskify` 뒤, `/speckit-implement` 앞. CRITICAL/HIGH면 정지 |
| `D-037` | `/speckit-clarify`를 필수로. `/speckit-specify` 뒤, `/speckit-plan` 앞 |

`D-036`·`D-037`의 공통 잔여 위험: **engine이 `speckit.analyze`·`speckit.clarify`를 실제로
dispatch하는지 확인하지 못했다.** 실패하면 step을 지우지 말고 `AGENTS.md`의 절차를 손으로 돌린다.

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
