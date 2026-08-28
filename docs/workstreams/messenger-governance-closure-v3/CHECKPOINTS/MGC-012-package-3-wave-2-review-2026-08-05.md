# MGC-012 Package 3 Wave 2 Review — 2026-08-05

대상은 MGC-012-T002 (`SlackProjectionDestination.send`) 다. reviewer 셋을 관점별로 돌렸다 —
contract, failure/recovery, regression. 이 문서는 근거 기록이고 승인 주체가 아니다 (D-004).

## Round 1

### Gate-Blocking 판정과 처리

| # | lens | 등급 | 지적 | 처리 |
|---|---|---|---|---|
| 1 | regression | P1 | `send()` 가 marker 를 통째로 빼먹어도 106개 test 가 전부 통과한다. mutation 으로 확인했다 (`marker={}` → 106 passed) | `test_send_passes_the_configured_channel_payload_and_marker` 가 세 인자를 전부 대조한다 |
| 2 | contract / regression | Blocking-P2 / P1 | `test_destination_satisfies_the_projection_destination_protocol` 이 거짓을 주장하며 항상 통과한다. `reconcile` 이 없어 Protocol 을 만족하지 않고, annotation 은 runtime 에 무의미하며 mypy 는 `tests/` 를 안 본다 | 구성원을 직접 대조하는 `test_destination_implements_the_protocol_members_t002_owns` 로 교체. `reconcile` 부재를 명시적으로 고정해 T003 이 붙으면 뒤집힌다 |
| 3 | contract / failure | Blocking-P2 / P1 | 생성자가 아무것도 검증하지 않는다. `max_attempts=0` 이면 첫 transient 실패가 C-3.1 을 타 되돌릴 수 없는 hold 를 만든다. dispatcher config 일치는 검증 못 해도 `ge=1` 하한은 config 없이 검사된다 | `__init__` 이 `destination_ref`·`channel` 공백과 `max_history_pages < 1`·`max_attempts < 1` 을 `ValueError` 로 막는다 |
| 4 | contract | P1 | `data-model.md` 가 receipt 를 `SlackSendResult.channel` 로 만든다고 쓰고 있어 새 C-2.1 과 충돌한다. T003 이 그것을 읽고 reconcile 을 구현하면 `OUTBOX_DELIVERY_RESULT_CONFLICT` 가 난다 | `data-model.md` 를 C-2.1 로 맞췄다 |
| 5 | failure | Blocking-P2 | D-021 의 논거("마지막 attempt 는 어차피 dead letter 라 state 전이가 안 바뀐다")를 실제 dispatcher 로 검사하는 test 가 없다. 틀렸을 때의 결과가 되돌릴 수 없는 hold 다 | 실물 `OutboxDispatcher` + SQLite store 로 도는 test 4개 추가. attempts 목록, 최종 state, DLQ row, hold row 를 고정한다. 대조군(`max_attempts` 를 크게 준 경우)이 `OUTBOX_DELIVERY_FAILED` 를 남기는 것까지 함께 고정해 차이가 error_code 하나뿐임을 보인다 |

### 반영한 Advisory

- `build_slack_marker` 를 `try` 밖으로 뺐다. 안에 있으면 우리 결함이 `transport_...` 로 기록돼 transport 구현자 탓이 된다 (failure #5)
- `_rewrap` 이 builtin 이 아닌 예외에 module 을 붙인다. Package 4 가 HTTP client 를 넣으면 `ConnectError` 같은 이름이 여러 module 에서 나와 한 문자열로 뭉친다 (failure #7)
- `_raise_for_failure` 가 terminal 판정을 `raise_for_slack_failure` 에 위임한다. 두 벌이던 분류 규칙이 하나가 됐고 T001 test 가 도는 쪽이 죽은 copy 가 아니게 됐다 (regression A2)
- `exhausted_cause_suffix` docstring 이 `transport_ConnectionError` 라고 썼으나 실제 저장값은 소문자다. operator 가 grep 해도 안 나온다 (regression A5)
- 자기 자신과 비교하던 `test_receipt_is_stable_for_one_message` 를 지웠다. `_receipt` 를 `"WRONG"` 으로 바꿔도 통과했다. C-2.3 의 나머지 절반은 T003 몫임을 주석으로 남겼다 (contract A-3, regression A3)
- digest 대조 test 의 `is not None` 을 receipt 값 비교로 바꿨다. `send` 는 `-> str` 이라 그 단언은 언제나 참이었다 (regression A4)
- `attempts` 증가가 조건부(`CASE WHEN attempts < ?`)라는 것을 C-3.1 과 D-021 항목 1 에 반영했다. 결론은 그대로다 — 증가하지 않는 재claim 경로는 `deliver_next` 가 `send()` 앞에서 가로챈다 (contract A-1)
- C-2.1 결과표에 C-3.1 예외를 적었다 (contract A-4)
- T002 구간의 AC 주석에 task id 를 붙였다. T001 의 AC-01 과 번호가 겹쳤다 (contract A-5)
- manifest `allowed_paths` 에 D-021 이 연 문서 경로를 넣었다 (contract A-6)

### 기록만 하고 안 고친 Advisory

- **lease 만료 시 D-021 보장이 무효**가 된다 (failure #8). `post_message` 가 lease 보다 오래 끌면 `fail()` 이 `_require_lease` 에서 터져 판정이 통째로 버려진다. 결과는 fail-closed 라 옳지만(고아 Card 가능성이 Slack 원인보다 우선한다) D-021 은 best-effort 이고 C-1 의 강제 불가능한 조항에 의존한다. T003 runbook 에 적을 것
- `MemoryError` 가 5번 재시도된다 (failure #6). 결과는 같은 DLQ 이고 좁히면 다른 위험이 커진다
- `events.py:NNNN` 인용 8곳이 line 번호에 묶여 있다 (regression A7). 지금은 전부 정확하다. 자동 검사 수단이 없다
- `max_attempts` 불일치가 양방향으로 손실을 낸다 (failure #4). D-021 과 C-2 가 이미 수용한 대가로 적어 뒀다

## Round 1 의 마지막 P1 — D-022 로 닫았다

**pre-send 검증 실패가 원인 없이 5번 재시도된다** (failure lens P1 #1).

`OUTBOX_DESTINATION_MISMATCH` 와 `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` 는 `GovernanceEventError`
로 나간다. 그것은 `OutboxReconcileError` 의 **부모**라 (`events.py:24`-`35`)
`deliver_next` 의 `except OutboxReconcileError` (`events.py:2856`)가 못 잡고 generic
handler 로 떨어져 `OUTBOX_DELIVERY_FAILED` 상수만 남는다. reviewer 가 실물 dispatcher 로
확인했다.

두 조건은 event row 의 불변 column 에서 나오므로 재시도가 확정적으로 무의미하다.
결과적으로 payload 손상(무결성 사건)과 destination 배선 버그가 평범한 전달 실패와 같은
문자열로 남는다. D-021 이 방금 닫은 결함과 같은 종류다.

2026-08-05 사용자가 선택지 셋 중 **두 destination 을 같이 바꾸는 쪽**을 골랐다. D-022 로
기록했다. 두 검증 실패를 `OutboxReconcileError` 로 던진다. `projections.py` 를 열어야 해서
`forbidden_paths` 에서 뺐다 (`scope_boundary_must_expand` 를 사용자 승인으로 해소). `events.py`
는 열지 않았다 — 새 예외 class 없이 기존 `OutboxReconcileError` 를 쓰므로 dispatcher 계약이
그대로다. `except GovernanceEventError` 로 잡던 기존 호출자는 자식 class 도 잡으므로 영향이
없다.

test 는 셋을 넣었다. 두 destination 각각의 예외 종류·code 대조와, 실물 dispatcher 로
payload 무결성 실패가 attempts=1 에 dead letter + hold 로 끝나는 것.

## Round 2

round 1 의 수정 자체가 production code 를 바꿨으므로 3 lens 를 다시 돌렸다. **P0 0건, P1
0건.** Blocking-P2 3건은 전부 contract lens 가 냈고 처리했다.

| # | lens | 등급 | 지적 | 처리 |
|---|---|---|---|---|
| 1 | contract | Blocking-P2 | round 1 의 D-022 수정이 `projections.py` 에 4줄 주석을 넣어 그 파일을 가리키는 인용 12곳이 전부 밀렸다. `projections.py:165` 는 `_payload_digest` 를 가리키지 않게 됐다. T002 manifest 의 `implementation.guidance` 두 줄이 그 anchor 를 쓴다 | 11곳을 `+4` 로 고쳤다. `events.py` 인용은 전부 그대로 정확함을 reviewer 가 재확인했다 |
| 2 | contract | Blocking-P2 | `allowed_paths` 가 실제 diff 를 못 덮는다. `tasks.md` 와 이 checkpoint 파일이 빠졌는데 후자는 이미 `evidence_paths` 에 들어 있었다 | spec 계열 전부와 checkpoint 를 넣었다 |
| 3 | contract | Blocking-P2 | 생성자 검증이 계약에 없는 production 동작이다. test 는 있는데 C-2 에도 AC 에도 근거가 없다 — Completion Gate 규칙의 역방향이다 | C-2 에 거부 조건 넷을 표로 적고 AC-10 을 추가했다 |

### 반영한 Advisory

- **생성자가 공백을 검사만 하고 정규화하지 않았다** (failure lens). `channel=" C123 "` 이 통과해 그대로 Slack 에 나가면 `channel_not_found` 다 — allowlist 밖이라 terminal 이고 hold 는 되돌릴 수 없다. `_receipt` 도 그 값을 써서 reconcile 과 갈린다. `.strip()` 해서 저장하고 test 를 넣었다
- C-2.1 결과표가 "마지막 attempt 는 C-3.1" 이라고만 적어 terminal 이 예외라는 것을 못 담았다. 행을 둘로 쪼개고 C-3.1 본문에도 명시했다. 코드는 처음부터 맞았다 — `_raise_for_failure` 가 `and classify(...) is RETRYABLE` 로 단락한다
- AC-01·AC-02 문구에 예외 종류를 넣었다. D-022 로 바뀐 부분이 AC-09 에만 있었다
- quickstart 9·10 행과 spec.md edge case 둘에 "재시도 없이 DLQ + hold" 를 적었다
- plan.md 의 `projections.py # 수정 없음` 을 고쳤다
- 성공 경로 integration test 의 `metadata is not None` 을 실물 event 로 만든 marker 대조로 바꿨다. 빈 dict 도 통과하던 단언이다
- D-022 에 `APR-002` 참조를 넣었다. reviewer 가 workstream 문서 전체를 훑어 `YamlProjectionDestination.send` 의 예외 종류를 규정한 기존 기록이 없음을 확인했다 — `supersedes` 대상이 없고 이 Decision 이 최초 규정이다

### 기록만 하고 안 고친 Advisory

- **`import test_governance_events` 는 pytest 의 `prepend` import mode 에 의존한다** (regression A1). `--import-mode=importlib` 에서 collection 이 깨지고 `tests/__init__.py` 가 생기면 파일 전체가 못 돈다. CI 는 맨 `python -m pytest` 라 지금은 안 맞는다. 고치려면 `pyproject.toml` 에 `pythonpath = ["tests"]` 를 넣어야 하는데 그 파일은 `forbidden_paths` 다. 후속 task 감이다
- **`YAML_PROJECTION_DIVERGED` 가 한 파일 안에서 비대칭이다** (contract·failure 양쪽). `projections.py:49` 는 `OutboxReconcileError`, `:70` 은 `GovernanceEventError` 다. 같은 divergence 가 발견 위치에 따라 즉시 hold 와 5회 재시도로 갈린다. D-022 의 논거가 그대로 적용되지만 AC-09 가 지목한 두 code 밖이다. T003 으로 넘긴다
- **`max_attempts` 과소 설정이 남은 attempt 를 버린다** (failure lens 1). reviewer 가 실물로 재현했다 — config 5 / destination 2 면 3회를 안 쓰고 hold 가 된다. D-021 이 수용한 대가이고 round 1 의 `>= 1` 하한은 그 하위 집합만 막는다. 완전히 닫으려면 생성자가 `OutboxConfig` 를 받아야 한다. Package 4 몫이다
- `_rewrap` 이 module 의 `.` 을 먼저 `_` 로 바꿔 `persisted_code_suffix` 의 digest 보호를 우회한다 (failure lens 3). 적대적으로 지은 class 이름이 필요해 실사용 위험은 없다
- `test_without_the_exhaustion_rule_the_cause_is_lost` 는 C-3.1 을 지워도 실패하지 않는다. 대조군이라 의도한 것이고 C-3.1 의 coverage 로 세면 안 된다 (regression A4)
- test 가 다른 test module 의 private helper 를 쓴다 (regression A7). 중복 fixture 보다 낫다고 판단했다. 이름이 바뀌면 조용히 깨진다
- `events.py:2240` 이 `OUTBOX_PAYLOAD_INTEGRITY_FAILURE` 를 여전히 `GovernanceEventError` 로 던진다. store 무결성 검증 경로라 `deliver_next` 와 무관하다

### Mutation 결과

regression lens 가 13개 mutation 을 넣어 전부 잡히는 것을 확인했다. round 1 에서 뚫렸던
marker 누락(M3)이 이제 잡히고, D-021 의 "재시도를 안 잃는다" 주장도 `attempts >=
max_attempts - 1` mutation(M11)으로 실물 dispatcher test 가 잡는다. `projections.py` 변경은
mutation 을 되돌리면 새 test 하나만 실패한다 — 이전에는 coverage 가 0 이었다.

## Round 3

round 2 처리에서 production code 가 한 줄 더 바뀌어 (`.strip()`) delta 만 다시 돌렸다.
**세 lens 전부 P0·P1·Blocking-P2 0건.**

검증한 것.

- contract lens 가 `projections.py` 인용 13곳과 `events.py` 인용 30곳을 **전부 열어** 대조했다. 전부 맞다. round 2 가 안 밀린 `data-model.md:40` (`projections.py:36`) 을 그대로 둔 것도 옳다 — 삽입 위치가 53줄이라 36 은 안 밀렸다
- `.strip()` 이 D-018 항목 1 (형식을 만들거나 해석하지 않는다) 을 어기지 않는다. 정규화지 파싱이 아니다. C-2.3 은 오히려 **강해진다** — 저장 시점 한 곳에서 정규화하므로 send 와 reconcile 이 같은 값을 본다
- 되돌아가는 위험이 없다. `destination_ref` 는 `events.py:1442`·`1515`·`2446` 에서 enum + hex digest 로 만들어져 공백을 가질 수 없다. 즉 DB row 쪽에 padding 이 있어 stripping 이 claim 을 깨는 경우가 없다
- 이미 delivered 인 event 의 receipt 가 갈려 `OUTBOX_DELIVERY_RESULT_CONFLICT` 가 나는 경로도 없다. `claim_next` 가 `pending`·`retry_wait` 만 고르므로 delivered row 는 다시 claim 되지 않는다
- regression lens 가 mutation 18개 (round 3 신규 4 + round 2 재실행 14) 를 넣어 **전부 잡히는 것**과 round 2 대비 회귀가 없음을 확인했다. 작업 tree 는 SHA-256 대조로 원복을 증명했다
- `taskify_to_tasks_md.py` 재생성 결과가 tree 와 byte-identical 이고, manifest 6개가 전부 parse 된다. 변경/미추적 15경로가 `allowed_paths` 와 정확히 일치한다

### 반영한 Advisory

- class docstring 이 `destination_ref` 를 "그대로 받는다" 로 적어 `.strip()` 과 어긋났다. 문구를 고쳤다
- 성공 경로 test 의 marker 단언이 양변에 같은 함수를 써서 marker **내용**은 안 잡힌다 (mutation 으로 확인 — `payload_digest` 를 빼도 통과). 내용을 고정하는 것은 `test_marker_carries_the_four_identity_fields` 하나뿐이므로 지우지 말라는 주석을 달았다. 전달 여부는 이 단언이 잡는다
- checkpoint 자신의 round 2 서술에서 line 번호 하나를 고쳤다

### 기록만 하고 안 고친 Advisory

- **`.strip()` 은 zero-width 문자와 BOM 을 못 잡는다** (failure lens). `​`, `﻿`, `‎` 는 `isspace()` 가 False 라 blank 검사도 통과하고 그대로 Slack 에 나가 `channel_not_found` → terminal → 되돌릴 수 없는 hold 가 된다. UTF-8-BOM YAML 이나 Slack UI 복사가 경로다. **round 2 이전에도 있던 구멍이고 이 delta 가 넓히지 않았다.** 완전히 닫으려면 다듬는 대신 형식으로 거부해야 한다 — `channel` 은 `^[CGD][A-Z0-9]+$`, `destination_ref` 는 `^provider:[a-z]+:[0-9a-f]{64}$`. C-2 거부표를 바꾸는 계약 수정이라 T003/Package 4 로 넘긴다. `max_attempts` 를 `OutboxConfig` 로 받는 건과 함께 처리한다
- round 1·2 의 나머지 미해결 advisory 는 이 delta 로 등급이 바뀌지 않았다

## Commands

- `python -m pytest` — 829 passed
- `python -m ruff check .` — All checks passed
- `python -m ruff format --check .` — 128 files already formatted
- `python -m mypy` — Success, 99 source files
- `amplai-foundry lint vault` — 0 errors, 0 warnings, 56 notes

## Gate

**PASS.** 2026-08-05, MGC-012-T002, wave 2.

- review 3 lens 를 3라운드 돌렸다. 마지막 라운드에서 P0·P1·Blocking-P2 가 0건이다
- round 3 에서 처리한 것은 docstring·주석·checkpoint 문구뿐이다. production code 는 안
  바뀌었으므로 4라운드를 돌리지 않는다. round 1·2 는 처리 과정에서 code 가 바뀌어 다음
  라운드를 돌렸다
- Advisory 미해결 항목은 위 각 라운드에 남겼다. T003 과 Package 4 가 받는다

명령 결과는 위 Commands 절이다. 전부 실행했다.

Decision 은 D-021 (retryable 소진 시 원인 보존) 과 D-022 (사전 검증 실패는 unreconcilable)
둘이다. 이 문서는 근거이고 승인 주체가 아니다 (D-004).
