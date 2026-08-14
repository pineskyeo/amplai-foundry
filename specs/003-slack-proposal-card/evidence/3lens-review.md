# MGC-012-P5 Three-Lens Review — Round 10

## Target

| Field | Value |
|---|---|
| Feature | `MGC-012-P5` — Slack Proposal Cards (`specs/003-slack-proposal-card`) |
| Round | 10 |
| Frozen at | 2026-08-13 Asia/Seoul |
| Aggregate SHA-256 | `89c44faaf9c637227d834c6d09a39389fb5dbb76205bfdf338655416cf9994e0` |
| Files in target | 36 |
| Manifest | `evidence/review-target.txt` |

세 lens 모두 시작 시점에 aggregate 를 독립 재계산해 일치를 확인했다. contract lens 는 종료
시점에도 재확인했다. review 종료 후 재계산도 같은 값이다.

## Verdict

**FAIL. gate 를 열지 않는다.**

| Lens | P0 | P1 | Blocking-P2 | Advisory | Result |
|---|---:|---:|---:|---:|---|
| Contract | 0 | 0 | 1 | 6 | FAIL |
| Failure / Recovery | 0 | 1 | 2 | 3 | FAIL |
| Regression | 7 | 10 | 7 | 0 | FAIL |

Regression 은 mutation 36종 실행 / 12 killed / **24 survived**다. kill rate 는 33% 다.

drift 된 네 파일 중 `slack_cards.py` 가 최악이다 — 14종 중 4종만 killed. `events.py` 는 6종 중
0종 killed 다. 반대로 `slack_projection.py` 는 3/3, `review_cards.py` 의 revocation·유일성 경로는
3/3 killed 다. 즉 전체가 무방비인 것이 아니라 **렌더 경계와 payload 검증 계층에 구멍이 몰려 있다**.

세 lens 의 지적은 상당 부분 겹친다. 아래 `Consolidated Findings` 가 중복을 제거한 목록이고
그것이 수정 범위의 기준이다. 위 표는 lens 별 원본 집계다.

`CLAUDE.md` Review Before Gate 규칙에 따라 P0/P1/Blocking-P2 가 하나라도 있으면 gate 는 닫힌다.

## The Single Most Important Distinction

발견 중 **실제 동작 결함은 하나뿐이다** (`C-1`). 나머지는 전부 **회귀 방어의 부재**다. 즉 코드는
오늘 맞게 동작하지만 그 정확성을 붙잡는 test 가 없다.

이 구분을 보고와 수정에 그대로 유지한다. "test 가 없다"를 "동작이 틀렸다"로 올려 읽으면 수정
범위가 잘못 커지고, 반대로 "동작은 맞다"를 이유로 넘기면 다음 변경에서 조용히 깨진다.

## Round 9 Invalidation

이 round 10 은 round 9 를 대체한다. round 9 는 assessed 로 세지 않는다.

- round 9 target 은 `1190d6c460fadd585f29769eca263cae82969f5e713e7c331d98a2b72b5e6ad4` 로
  얼렸으나 lens 가 하나도 돌지 않았고 `3lens-review.md` 가 없었다
- freeze 뒤 `governance/decisions.py`, `governance/events.py`, `governance/review_cards.py`,
  `governance/slack_cards.py` 네 파일이 **test 변경 없이** 수정됐다
- round 9 blob 은 `git hash-object` 로 계산만 하고 object store 에 쓰지 않아 `git cat-file` 로
  조회되지 않는다. **diff 는 복원 불가다**
- 세 lens 전부 이 항목을 Could Not Verify 에 올렸다. "drift 가 무엇을 바꿨는가" 는 이 review 로
  확인되지 않는다

발견의 분포가 이 사실과 정합한다. 확정된 finding 의 대부분이 drift 된 네 파일에 몰려 있고, 그
성격이 일관되게 "코드는 있는데 test 가 없다"다.

## Pre-Review Repair

drift 된 tree 는 `amplai-foundry verify` 의 ruff stage 에서 실패했다. round 10 freeze 전에
기계적 수리 두 건만 적용했다.

- `ruff check --fix` — `slack_cards.py` import block 정렬 (I001). `import traceback` 이 정렬 밖에
  붙어 있었다
- `ruff format` — `review_cards.py` 의 호출 하나 재포맷

둘 다 동작을 바꾸지 않는다.

## Verification Evidence

수리 후 재측정이다. drift 이전에 쓰인 `evidence/MGC-012-P5-T00*.md` 의 수치는 지금 없는 tree 를
가리키므로 근거로 쓰지 않는다.

| Check | Result |
|---|---|
| Full pytest | 1174 passed, 4 deselected |
| Ruff check | exit 0 |
| Ruff format check | exit 0, 135 files |
| mypy | exit 0, 102 source files |
| Schema check | exit 0, schemas are current |
| Vault knowledge lint | exit 0, 0 errors / 0 warnings / 56 notes |
| `amplai-foundry verify --root .` | 7/7 PASS |
| P5 task manifest validator | 3/3 PASS |
| `git diff --check` | PASS |

live Slack E2E (`-m slack_e2e`) 는 이번 review 에서 실행하지 않았다. 실제 workspace 에 보이는
message 를 남기고 review 범위 밖이다. 실행하지 않은 검증을 PASS 로 세지 않는다.

## Consolidated Findings

중복을 제거한 목록이다. `lens` column 은 같은 사실에 도달한 lens 를 적는다.

| ID | 등급 | 대상 | 내용 | lens |
|---|---|---|---|---|
| `C-1` | P1 | `slack_projection.py:881-893,988-1006` | 평범한 Exception 이 process interruption 으로 오분류돼 bare `BaseException` 이 dispatcher 밖으로 샌다. **유일한 실제 동작 결함** | failure |
| `C-2` | **P0** | `slack_cards.py:204,219,220` | `_validate_action_set` 의 fail-closed 가 fail-open 으로 바뀌어도 아무도 안 잡는다. 취소·소비된 action set 렌더, 다른 reviewer·다른 channel 에 묶인 token 노출 | contract, failure, regression |
| `C-2b` | P1 | `slack_cards.py:46,206,209,221,305` | 나머지 renderer 거부 5종이 보호되지 않는다 | contract, failure, regression |
| `C-3` | **P0** | `slack_cards.py:322-333` | `render_review` credential scrub 경계가 보호되지 않는다. scrub 을 죽이면 raw ActionToken 이 traceback frame 에 실제로 노출된다 | contract, failure, regression |
| `C-4` | P1 | `events.py:119` | non-Slack review channel 거부가 보호되지 않는다 | contract, regression |
| `C-5` | **P0** | `decisions.py:411` | token 만료 경계가 보호되지 않는다. 정확히 만료 시각의 결정이 durable 하게 커밋될 수 있다 | regression |
| `C-6` | P1 | `review_cards.py:340` | replay fingerprint binding (`IDEMPOTENCY_CONFLICT`) 이 보호되지 않는다 | regression |
| `C-7` | Blocking-P2 | `ingress_worker.py:366-385`, `slack_http.py:529` | `completed` safe outcome 이 계약에 선언됐으나 어느 경로에서도 생성되지 않는다. artifact 3자 불일치 | contract |
| `C-8` | P1 | `events.py:126-129` | `ReviewProjectionPayload` invariant 2종이 보호되지 않는다 | contract, regression |
| `C-9` | Blocking-P2 | `events.py:1225,1241,1247` | review-card integrity 대조 성분 3종이 보호되지 않는다 | contract, regression |
| `C-10` | **P0** | `review_cards.py:634` | `prepare()` 가 token 3장을 발급하기 직전의 digest 무결성 검사가 보호되지 않는다. 검증 안 된 payload 에 raw credential 을 발급하게 된다 | regression |
| `C-11` | Blocking-P2 | `slack_cards.py:156,173`, `review_cards.py:226`, `decisions.py:458` | bound 경계 flip 과 cardinality guard 가 보호되지 않는다 | contract, regression |
| `C-12` | **P0** | `decisions.py:771` | 같은 호출의 raw ActionToken 이 `idempotency_key` 에 섞여 durable column 으로 들어가는 것을 막는 guard 가 보호되지 않는다 | regression |
| `C-13` | P1 | `slack_cards.py:197` | `_review_body` 최종 200자 절단이 보호되지 않는다 | regression |

### C-1 — 유일한 실제 동작 결함

`src/amplai_foundry/governance/slack_projection.py:881-893`, `:988-996`, `:999-1006`

Review outbox event 배달 중 `ReviewActionSetService.prepare()` 가
`sqlite3.OperationalError("database is locked")` 를 던지면 — SQLite 동시 writer 에서 흔한
실패다 — `except (ReviewCardError, SlackCardRenderingError, ValueError)` (`:878`) 에 안 걸리고
`except BaseException` (`:881`) 으로 간다. `_interruption_kind` 가 `"exception"` 을 돌려주고
(`:996`) `_raise_sanitized_interruption` 이 bare `BaseException` 을 던진다 (`:1006`).

`deliver_next` 는 `except OutboxReconcileError` 와 `except Exception` 만 잡는다
(`events.py:3237`, `:3243`). `BaseException` 은 둘 다 통과해 dispatcher 밖으로 나간다.
`fail()` 이 불리지 않아 durable 기록이 남지 않는다.

failure lens 의 실주행 관측:

```
escaped deliver_next: BaseException | Slack Review Card delivery interrupted.
state: leased  attempts: 1  last_error_code: None
dead letters: 0
holds: 0
```

renderer 가 `TypeError` 를 던지는 경우도 같다.

선언된 회복은 `OutboxReconcileError(render_failure_code)` → dead letter + operator hold 다
(`:893`, `events.py:3237-3242`). class 하나만 다른 실패가 dead letter 도 hold 도 만들지 않아
operator 에게 신호가 가지 않는다. `except Exception` 으로 도는 worker loop 도 이 예외를 못 잡아
worker 가 죽는다. `contracts/review-card-lifecycle.md` 의 crash matrix
"Process interruption during render or post … preserve the interruption class" 를 어긴다.
`sqlite3.OperationalError` 는 interruption 이 아니다.

**같은 파일의 cleanup 경로는 이미 옳게 나눠져 있다.** `_abandon_review_closed` 는
`cleanup_failure_kind != "exception"` 일 때만 interruption 을 재던지고 `"exception"` 은
`OutboxReconcileError("REVIEW_ACTION_SET_CLEANUP_FAILED")` 로 닫는다 (`:983-986`). send 경로에만
그 분기가 없다. 설계 판단이 아니라 누락이다.

fail-open 은 아니다. lease 만료 sweep 이 `last_error_code='OUTBOX_LEASE_EXPIRED'` 를 채우므로
(`events.py:2966-2974`) 재claim 시 reconcile 이 먼저 돈다. 중복 Card 는 나지 않는다. 그래서 P0 가
아니라 P1 이다.

### C-2 — renderer 거부 7종이 전부 unheld

세 lens 가 서로 다른 방법으로 같은 결론에 도달했다. contract lens 는 line-level trace 로 미실행
statement 를 열거했고, failure lens 는 test 참조 부재를 추적했고, regression lens 는 실제로
깨뜨려 확인했다.

mutation 결과가 결정적이다.

| # | mutation | 결과 |
|---|---|---|
| M01 | result status identity: `rejected` 가 approved 제목으로 렌더 | KILLED |
| M02 | `unsupported_result_status` fail-closed guard 삭제 | **SURVIVED** |
| M03 | `action_set_state_mismatch` guard 무력화 | **SURVIVED** |
| M04 | `action_set_count_mismatch` 약화 (`!= 3` → `< 1`) | **SURVIVED** |
| M05 | `action_set_action_mismatch` 약화 (정확한 집합 → 비어있지 않음) | **SURVIVED** |
| M06 | binding 검사에서 reviewer actor 성분 제거 | **SURVIVED** |
| M07 | binding 검사에서 bound channel 성분 제거 | **SURVIVED** |
| M08 | binding 검사에서 `expires_at` 성분 제거 | **SURVIVED** |

`_validate_action_set` 은 Card 를 그리기 직전 token 세 장이 그 payload 의 snapshot·actor·channel
·expiry 에 묶였는지 확인하는 마지막 fail-closed 관문이다. M06~M08 은 그 성분을 하나씩 빼도 전
suite 가 초록임을 보인다. 다른 reviewer 나 다른 channel 에 묶인 token 이 Card 에 실려도 test 가
침묵한다.

계약 `contracts/slack-card-payloads.md:104-108` 이 이 거부를 명시적으로 요구한다.

> Renderer rejects unknown status, malformed operation summary, non-Slack review channel, wrong
> action set, mismatched snapshot, and overlong unbounded input.

M01 이 KILLED 인 것이 대조군이다. 같은 파일에서 test 가 있는 부분은 정상적으로 잡는다. 즉
`slack_cards.py` 가 통째로 무방비인 것이 아니라 `_validate_action_set` 에 구멍이 몰려 있다.

`tests/test_slack_cards.py` 에 `render_review` test 가 0개다. test 7개 전부
`render_result` / `slack_presentation_payload` 다. `tests/test_review_cards.py` 의 `render_review`
호출 5곳 (`:1272`, `:1300`, `:1317`, `:1334`, `:1360`) 은 전부 happy path 다.

### C-3 — scrub 경계 (P0)

`slack_cards.py:322` 의 `_clear_exception_frames` 를 즉시 `return` 하는 no-op 으로 바꿔도 전
suite 가 초록이다 (M13 SURVIVED).

**현재 코드는 옳다.** failure lens 가 직접 주입 실험으로 확인했다 — `card` 가 완성된 뒤
KeyboardInterrupt 를 주입하면 traceback frame 이 `['<module>']` 로 줄고 raw credential 이 보이지
않는다. chained `__cause__` / `__context__` 도 loop 가 따라간다.

**그 정확성을 지키는 test 가 없다.** 기존 scrub test 전부
(`test_every_review_failure_boundary_scrubs_raw_action_credentials`,
`test_pytest_showlocals_output_contains_no_raw_action_credential`) 가 `render_cleanup` mode 에서
`FailingReviewRenderer` (`tests/test_review_cards.py:670-672`) 를 쓴다. 이 stub 은 `render_review`
를 통째로 override 하므로 실제 scrub 경로가 한 번도 실행되지 않는다.

**no-op 화가 실제로 credential 을 노출시키는 것을 직접 확인했다.** real `render_review` 를
구동하고 raw token 세 장을 sentinel 값으로 바꾼 뒤, `card` dict 가 완성된 뒤에 raise 되는
경로(`slack_cards.py:157`)로 실패를 냈다. sentinel 이 성공 경로의 card 에 실제로 들어가는 것을
먼저 확인했다.

| 조건 | traceback frames | raw credential 노출 |
|---|---|---|
| M13 적용 (scrub no-op) | `['drive', 'render_review', '_render_review']` | **True** — `_render_review` frame 의 local `card` |
| 미변형 | `['drive']` | False |

즉 `_clear_exception_frames` 는 관측 가능한 보안 효과를 갖고, 그것을 붙잡는 test 가 하나도 없다.

`pytest --showlocals` 로도 실측했다. mutation 상태에서 pytest 는 `_render_review` frame 과 `card`
local 을 출력하지만, saferepr 이 값을 잘라 이 배치에서는 sentinel 문자열까지는 찍히지 않았다.
미변형에서는 `_render_review` frame 자체가 없어 `card` line 도 나오지 않는다. truncation 없이
`f_locals` 를 읽는 소비자 — logger, crash reporter, frame 순회 코드 — 는 sentinel 을 그대로 얻는다.
**pytest 한정으로는 노출이 truncation 에 우연히 의존한다.**

taxonomy 상 "credential 을 유출시키는 mutation 의 survivor" 는 P0 다. 다만 이것은 **회귀 방어
부재의 P0** 이지 현재 유출이 아니다. 오늘 코드는 옳다. 수정은 코드가 아니라 test 다.

### C-4 ~ C-6 — 그 밖의 unheld 계약 동작

| # | mutation | 결과 |
|---|---|---|
| M15 | `ReviewProjectionPayload` 의 non-Slack channel 거부 무력화 | **SURVIVED** |
| M21 | token 만료 경계 `processed_at >= expires_at` → `>` | **SURVIVED** |
| M24 | token-consume rowcount guard `!= 1` → `< 0` | **SURVIVED** |
| M26 | replay fingerprint binding (`IDEMPOTENCY_CONFLICT`) 무력화 | **SURVIVED** |
| M22 | decision consume 시 sibling action-token 취소 제거 | KILLED |
| M23 | `ACTION_CHANNEL_MISMATCH` binding guard 무력화 | KILLED |
| M25 | `REVIEW_CARD_ALREADY_REQUESTED` one-snapshot 유일성 guard 제거 | KILLED |
| M27 | `prepare()` 의 이전 generation 취소 제거 | KILLED |
| M29 | `REVIEW_CARD_CHANNEL_MISMATCH` guard 무력화 | KILLED |
| M30 | marker build 실패 시 action-set abandon 제거 | KILLED |
| M31 | `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` guard 무력화 | KILLED |

**M21 은 다른 두 lens 가 찾지 못한 것이다.** 만료 판정을 `>=` 에서 `>` 로 바꾸면 정확히 만료
시각에 도착한 click 이 유효한 것으로 통과하는데 아무 test 도 잡지 않는다. 경계값 하나만 어긋나는
결함이라 정적 읽기로는 드러나지 않는다. mutation 만이 찾을 수 있는 종류다.

**M24 도 주의가 필요하다.** `< 0` 은 사실상 항상 거짓이므로 guard 가 죽는다. rowcount 가 1 이
아닌 consume 이 성공으로 통과한다. 현재 schema 제약이 그 상황을 막는지는 이 review 에서 확인하지
않았다.

M22·M23·M25·M27·M29·M30·M31 이 KILLED 인 것은 governance 핵심 경로에 실효 test 가 있다는 뜻이다.
전체가 무방비인 상황은 아니다.

### C-7 — `completed` safe outcome 이 생성되지 않는다

`src/amplai_foundry/governance/ingress_worker.py:366-385`, `src/amplai_foundry/governance/slack_http.py:529`

계약 `contracts/interaction-feedback.md:16-23` 의 mapping table:

> | Safe Outcome | Internal Source |
> | `completed` | first successful decision |
> | `unavailable` | retry/hold without a more specific public result |

spec `spec.md:120` FR-024 는 "safe reviewer feedback for **accepted**, denied, expired, stale, and
already-completed actions" 를 요구한다.

`_safe_outcome` 은 `WorkerOutcome.COMPLETED` 이면서 `replayed` 가 아닌 경우 — 즉 바로 그
first successful decision — `:373-374` 에서 `return None` 한다. `RETRY` 도 같은 줄에서 `None` 이다.
repo 전체에서 `SafeInteractionOutcome.COMPLETED` 를 만드는 곳이 없다.
`_SAFE_INTERACTION_MESSAGES[SafeInteractionOutcome.COMPLETED] = "The Proposal decision completed."`
는 도달 불가 문자열이다.

같은 계약의 `Delivery` 절 (`interaction-feedback.md:30-31`) 은 성공 경로를 Result Card 로
재배치한다. 그래서 governed state 는 안전하고 FR-013 은 구현·검증돼 있다
(`tests/test_review_cards.py:1351-1424`).

문제는 artifact 3자가 어긋난 채 남는다는 것이다. `completed` enum 값과 그 message 가 producer
없이 API 에 남아 있어, 다음 사람이 "이미 보내고 있다"로 읽거나 producer 를 붙여 이중 통지를
만든다.

**이것은 계약·spec·코드 중 무엇을 맞출지의 결정 사항이다. review 가 임의로 정하지 않는다.**

### C-8 ~ C-11 — 보호되지 않는 내부 invariant

| # | mutation | 결과 |
|---|---|---|
| M16 | `operation_count == sum(operation_counts)` invariant 무력화 | **SURVIVED** |
| M17 | `remaining_operation_count == operation_count - len(titles)` invariant 무력화 | **SURVIVED** |
| M18 | `REVIEW_CARD_ROOT_MISMATCH` 비교에서 `payload_json` 성분 제거 | **SURVIVED** |
| M20 | `REVIEW_CARD_AUDIT_MISMATCH` 비교에서 `actor_id` 성분 제거 | **SURVIVED** |
| M28 | `_command_for_event` 의 command/payload digest 무결성 검사 무력화 | **SURVIVED** |
| M11 | `result_identity_exceeds_body_limit` 경계 `< 4` → `< 2` | **SURVIVED** |
| M19 | review-card audit cardinality `len(audits) != 1` → `< 1` | **SURVIVED** |
| M32 | `review_cards.py:226` outbox cardinality `len(outbox) != 1` → `< 0` | **SURVIVED** |
| M09 | `_bounded_project_label` namespace truncation 제거 | KILLED |
| M12 | `_truncate` 의 truncation 전면 제거 | KILLED |
| M14 | `_freeze` 가 MappingProxyType 대신 mutable dict 반환 | KILLED |

M16·M17 은 `data-model.md:28-33` 이 선언한 invariant 다. M18·M20·M28 은 같은 문서
`Integrity Checks` 절이 선언한 검사다. 선언돼 있으나 FR/SC 는 아니라 Blocking-P2 로 둔다.

M11 은 도달 가능성을 확인하지 않았다. 도달 불가 dead branch 로 밝혀지면 Advisory 로 내리고 코드를
지우는 쪽이 맞다.

M19 는 driver 가 anchor 비유일(파일 내 8곳)로 건너뛴 것을 재실행한 결과다. 정확한 guard 는
`events.py:1241` 의 `if len(audits) != 1:` 이고 다음 줄이
`raise GovernanceEventError("REVIEW_CARD_AUDIT_MISMATCH")` 다. 앞뒤 7줄로 anchor 를 늘려 유일성을
확인한 뒤 적용했다. `len(audits) == 0` 은 변형체에서도 여전히 raise 하므로 남은 gap 은 **audit
event 가 2개 이상인 경우**다. 그 상태를 만드는 test 가 없다.

M35 는 `review_cards.py:226` 의
`if len(outbox) != 1: raise ReviewCardError("REVIEW_CARD_OUTBOX_INVALID")` 를 `< 0` 으로 바꾼
것이다. M19 와 같은 성격의 cardinality guard 이고 마찬가지로 SURVIVED 다.

### C-10 / C-12 — 새로 드러난 credential 경계 두 곳

`review_cards.py:634` 는 `prepare()` 가 token 세 장을 발급하기 **직전**의 fail-closed 지점이다.

```python
if str(row[1]) != digest or str(row[2]) != canonical or event.payload_digest != digest:
    raise ReviewCardError("REVIEW_CARD_ROOT_MISMATCH")
```

이 검사를 통째로 죽여도 full suite 가 초록이다. `grep -rn "REVIEW_CARD_ROOT_MISMATCH" tests/`
결과가 **0건**이다. 검증되지 않은 payload 에 raw credential 을 발급하게 되는 경로가 회귀로부터
전혀 보호되지 않는다.

`decisions.py:771` 은 raw credential 이 durable column 으로 새는 것을 막는 마지막 검사다.

```python
if raw_token in idempotency_key:
    raise DecisionError("IDEMPOTENCY_CONFLICT")
```

이 guard 를 지워도 아무 test 가 안 잡는다. 지우면 이번 호출의 raw token 이 섞인
`idempotency_key` 가 통과해 `governance_decision_results.idempotency_key` 로 영구 저장된다.

관련 test 는 있지만 **다른 경우를 덮는다.** `tests/test_governance_events.py:462` 는
`idempotency_key=f"prefix-{sibling.raw_token}-suffix"` 로 **sibling** token 의 raw 값을 넣는다.
이 test 는 M33(`_contains_persisted_secret` 무력화)을 죽였다. 그러나 **같은 token 의 raw 값**을
넣는 경우는 어떤 test 도 덮지 않는다.

이 둘은 앞선 두 lens 가 찾지 못했다. 경계값과 우회 경로라 정적 읽기로 드러나지 않는다.

## Advisory

| ID | 대상 | 내용 | lens |
|---|---|---|---|
| `A-1` | `data-model.md:37-57` | Review Card Command column 목록이 17개인데 schema 31 은 18개다. `provider_installation_ref` 누락. `verify_schema` 가 잡으므로 silent 는 아니다 | contract |
| `A-2` | `contracts/review-card-lifecycle.md:13-20` | "request channel equal to the reviewer-bound Slack channel" 전제에 대응하는 durable binding 이 없다. 실질 보호는 one-channel-scope 제약과 배달 시점 검사로 존재한다. 계약 문구가 실제보다 강하다 | contract |
| `A-3` | `data-model.md:92-93` | `ChannelRef.message_id` 를 "when present" 로 적지만 `models.py:73` 은 항상 필수다. 권한 판정에서 무시된다는 본문 자체는 지켜진다 | contract |
| `A-4` | `slack_projection.py:860` | Review event 판별이 `"reviewer_actor_id" in event.payload` 라는 key sniffing 이다. 계약이 판별 방식을 지정하지 않아 위반은 아니다 | contract |
| `A-5` | `spec.md:140` SC-006 | rendered Card 의 200자 bound 가 default 4 operation 한 곳에서만 확인된다. 극단 입력 실측에서 구현은 bound 를 지킨다. test 가 고정하지 않는다 | contract |
| `A-6` | `slack_projection.py:956-963` | retryable code 로 마지막 attempt 가 소진되면 dead letter + hold 는 나지만 그 generation 이 24시간 `issued` 로 남는다. raw credential 은 memory-only 라 이미 사라져 보안 문제는 아니다 | failure |
| `A-7` | `slack_cards.py:155-157` | `review_fallback_exceeds_limit` 는 `ProposalId` 가 22자 고정이라 도달 불가능한 dead branch 다. M10 SURVIVED 는 test 공백이 아니라 도달 불가로 설명된다 | contract, regression |
| `A-8` | `ingress_worker.py:373-374` | `unavailable` safe outcome 이 `RETRY` 에서 나가지 않는다. 계약이 "may call" 이라 의무는 아니다. ingress retry 가 소진되면 사용자는 끝까지 message 를 못 받는다 | failure |

## Could Not Verify

- **round 9 → round 10 diff.** blob 이 object store 에 없어 복원 불가다. 네 파일에서 무엇이
  바뀌었는지 확인할 수 없다. 세 lens 가 각자 대체 방법으로 접근했고 그 결과가 위 finding 이다
- **M24 의 실현 가능성.** rowcount 가 1 이 아닌 consume 이 현재 schema 제약 아래 실제로 발생할 수
  있는지 확인하지 않았다
- **M11 의 도달 가능성.** `result_identity_exceeds_body_limit` 가 실제 입력으로 도달 가능한지
  확인하지 않았다. `A-7` 의 `review_fallback_exceeds_limit` 처럼 dead branch 일 수 있다
- **`migrations.py` 와 `slack_http.py` 는 mutate 하지 않았다.** 예산을 drift 된 네 파일에 몰았다.
  두 파일의 회귀 방어 수준은 이 review 로 측정되지 않았다
- **batch 2 survivor 의 재현.** M32~M36 은 위 `Method Defect` 의 오염 구간에서 나왔다. `C-10`,
  `C-12` 는 실제 repo `grep` 으로 별도 확인했으나 M32·M35·M36 은 깨끗한 복제본에서 재실행하지
  않았다
- **격리 복제본에서 시작하지 않은 pytest process 가 한 번 관찰됐다.** 원인은 모른다. source 를
  쓰지 않으므로 판정에 영향이 없다고 보지만 확인하지 않았다
- **`python -O` 환경.** `slack_projection.py:964` 와 `review_cards.py:545` 의 `assert` 는 `-O`
  에서 제거된다. 이 repo 가 `-O` 로 도는 증거를 찾지 못해 위험으로 올리지 않았다
- **FR-023 / SC-007 실 workspace 검증.** `-m slack_e2e` 를 실행하지 않았다. 코드상으로는
  production 경로를 그대로 타지만 action set 은 fake 가 공급하는 합성 credential 이다
- **FR-012 의 3초 ack.** `BoundedIngressAck` 은 review target 밖 파일이다. budget 상한 고정까지만
  확인했다

## Method

| Lens | 방법 |
|---|---|
| Contract | spec FR-001~025 / SC-001~008, contract 3종, data-model, task manifest scope 를 구현과 대조. line-level trace 로 미실행 statement 열거 |
| Failure / Recovery | credential 유출, 부분 실패·crash 복구, idempotency, 취소·만료·staleness, 순서·hold, fail-closed 기본값을 실주행 repro 로 확인 |
| Regression | 격리 복제본(전용 venv, baseline `1174 passed, 4 deselected`)에서 mutation 36종. targeted 로 먼저 돌리고 green 이면 full suite 로 재확인한 뒤 SURVIVED 판정. 매 mutation 후 복원·검증 |

mutation 적용은 anchor 문자열이 파일 내 **정확히 1회** 매칭될 때만 하고, 아니면 건너뛴 뒤 그
사실을 기록한다. 엉뚱한 위치를 고쳐 오판정하는 것을 막는다. M19 가 그렇게 건너뛰어졌고 유일
anchor 로 재실행했다.

regression 격리는 실제 repo 와 완전히 분리된 복제본에서 수행했다. 종료 후 복제본이 원본과 byte
단위로 동일함과 실제 repo aggregate 불변을 확인했다.

### Method Defect — 격리 복제본의 동시 접근

**후반부에 두 작업이 같은 격리 복제본을 동시에 썼다.** regression 의 batch 2 와 M19/M13 후속
검증이 겹쳤다. 관측된 증상은 둘이다.

- M19 재실행 시 `events.py:1241` 이 이미 `< 1` 상태였다 (다른 쪽이 적용 중)
- `review_cards.py:226` 의 M35 가 적용된 채 남아 있었다

발견 즉시 복원하고 `diff -r` 로 복제본이 원본과 완전히 같음을, full suite 가
`1174 passed, 4 deselected` 임을 확인했다. 실제 repo 는 aggregate `89c44faa…` 불변으로 영향이
없다.

**판정에 미치는 영향을 과소평가하지 않는다.** SURVIVED 판정은 오염에 대해 보수적이다 — mutation
이 하나 더 걸려 있으면 test 가 깨질 가능성이 높아지지 배제될 가능성이 낮아지지 않는다. 따라서
"full suite green" 관측은 유효하다. 반면 **KILLED 판정은 원칙적으로 다른 mutation 때문일 수
있다.** 다만 각 KILLED 는 실패 test 이름이 그 mutation 의 의미와 대응한다.

batch 2 결과(M32~M36)는 이 오염 구간에서 나왔다. 그중 `C-10`(M28)과 `C-12`(M34)는 별도로 실제
repo 에서 test 부재를 `grep` 으로 직접 확인했으므로 오염과 무관하게 성립한다. 나머지 batch 2
survivor 는 재실행으로 확인하는 편이 낫다.

원인은 orchestration 이다. 격리 복제본 하나를 두 작업이 공유하지 않도록 분리했어야 했다.

## Gate Decision

**PASS 하지 않는다.** P0 5건, P1 다수, Blocking-P2 다수가 존재한다. `APPROVALS.md` 와
`DECISIONS.md` 에 Package 5 gate 를 기록하지 않는다.

P0 는 전부 현재 유출·오작동이 아니라 회귀 방어의 부재다. 코드 수정 대상은 `C-1` 하나이고 나머지는
test 다. `C-7` 은 계약·spec·코드 중 무엇을 맞출지의 결정 사항이며 review 가 정하지 않는다.
