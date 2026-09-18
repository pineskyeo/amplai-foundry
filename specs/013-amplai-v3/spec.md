# Feature Specification: AMPLAI V3 RC-01 — Integration & Migration

**Feature Branch**: `feat/013-amplai-v3`

**Created**: 2026-09-18

**Status**: Ready for implementation (design approved externally; this Work integrates and completes it)

**Input**: 사용자 지시 "위 amplai V3 컨셉대로 실제로 만들어달라" + `AMPLAI_V3_DEV_03_799a17d8.zip` (DEV-03 development snapshot) + `AMPLAI_V3_Design_2026-09-15.zip` (승인 설계, `design-reference/` 에 무변경 동봉)

## 정본과 이 문서의 관계

V3 의 요구·설계·계약·테스트 명세의 정본은 `design-reference/` 다.

| 정본 | 내용 |
|---|---|
| `design-reference/design/01..33_*.md` | 33 개 설계 문서 |
| `design-reference/contracts/schemas/*.schema.json` | 30 개 JSON Schema 2020-12 (wire 정본) |
| `design-reference/contracts/{invariant-registry,gate-matrix,semantic-validators,state-machines,storage-model}.json` | 불변조건·gate·검증·상태·저장 키 |
| `design-reference/implementation/tasks.yaml` | 62 task DAG (wave 0~5) |
| `design-reference/implementation/requirements-traceability.json` | 35 REQ ↔ task ↔ test |
| `design-reference/eval/test-catalog.json` | 112 test (given/when/expected) |

이 spec 은 그것을 다시 쓰지 않는다. **이 Work 가 무엇을 하고 무엇을 하지 않는지**만 고정한다.

## 현재 상태 (사실)

DEV-03 (3.0.0.dev3, 2026-09-16) 은 62 task 중 42 개를 부분 구현했고 20 개는 미착수다. 자세한 상태와 로컬 재현 결과는 `dev03-delivery-evidence.json` 에 있다.

- V3 자동시험 477 통과, 관련 regression 132 통과, 전체 suite 2971 통과 (2026-09-18 로컬 재현)
- 35 REQ 중 fully_verified 0, 112 scenario 중 fully_qualified 0 — 자동시험 수와 규범 인수는 다른 축이다
- 품질 게이트: DEV-03 소스는 ruff 3500 / mypy 1223 오류를 들여왔고 HEAD 는 셋 다 0 이다
- 외부 자격 인증 9 건 (`EXTERNAL_QUALIFICATION_PENDING`) 은 credential/접근 권한 없이는 닫을 수 없다

## User Scenarios & Testing

### User Story 1 — DEV-03 가 이 저장소의 게이트를 통과한다 (Priority: P1)

개발자가 `python3 .ai-team/verifiers/run.py --profile v2` 를 실행하면 PASS 다. DEV-03 가 도입한 477 test 와 기존 2971 test 는 모두 유지된다.

**Independent Test**: v2 profile PASS + junit test 수 비감소.

**Acceptance Scenarios**:
1. **Given** feat/013-amplai-v3 HEAD, **When** `run.py --profile v2`, **Then** VERIFIER: PASS (ruff/ruff-format/mypy/pytest/schema/vault-lint/project-pack/kit-seal/doctor 전부).
2. **Given** 같은 HEAD, **When** `pytest tests/v3`, **Then** 477 passed 이상, skip/xfail 로 줄이지 않음.

### User Story 2 — Storage 원자 경계가 실제로 닫힌다 (Priority: P1, V3-007)

Runtime Store 가 `design/09 §4` 의 `BEGIN IMMEDIATE → CAS → event(seq+1) → outbox → idempotency digest → COMMIT` 경계를 제공한다.

**Acceptance Scenarios**: T-003, T-004, T-099~T-106 (`eval/test-catalog.json`).

### User Story 3 — Knowledge runtime 이 Foundry 8 kind 를 보존한 채 readiness 와 repo facts 를 제공한다 (Priority: P1, V3-012/013/014)

**Acceptance Scenarios**: T-015~T-020, T-073~T-078.

### User Story 4 — Capability pack 이 signed registry 와 conformance profile 로 설치·호출·검증된다 (Priority: P2, V3-034~039)

**Acceptance Scenarios**: T-073~T-078, T-092~T-098, T-057~T-063 (pack 별 interface/validation profile). 모든 domain pack 엔진 완성을 주장하지 않는다 (`design/13 §3`).

### User Story 5 — Hermes 가 authority 없이 intake/status 만 한다 (Priority: P2, V3-049)

**Acceptance Scenarios**: T-001~T-014, T-087~T-091.

### User Story 6 — 배포·이관이 owned path 와 authority 를 보존한다 (Priority: P2, V3-050/051/052/054)

**Acceptance Scenarios**: T-092~T-106, T-111, T-112.

### User Story 7 — end-to-end 와 actual-render 가 로컬에서 재현된다 (Priority: P3, V3-057/059)

**Acceptance Scenarios**: T-021~T-030, T-064~T-072, T-107~T-112, T-057~T-063.

### User Story 8 — 종결·cutover 는 fail-closed 다 (Priority: P3, V3-060/061/062)

**Acceptance Scenarios**: T-092~T-098, T-107~T-112. V3-060 실제 삭제와 V3-062 cutover 는 human gate 없이는 HOLD 다.

## Requirements

FR 은 `design-reference/implementation/requirements-traceability.json` 의 REQ-01~REQ-35 를 그대로 쓴다. 이 Work 는 새 REQ 를 만들지 않는다. 설계와 구현이 충돌하면 `DESIGN_CHANGE_PROPOSAL.md` 에 사유·영향·대안·test 를 기록하고 유리한 쪽을 임의로 고르지 않는다 (`design-reference/contracts/README.md`).

## Non-goals

`work-contract.json` 의 `non_goals` 가 정본이다. 요약: live provider 자격, 물리 격리, 모델 학습, V2 API 파괴, V3 FINAL 선언, push/merge/release.

## Success Criteria

- SC-1: v2 verifier PASS (GATE-001)
- SC-2: 20 개 미착수 task 각각에 대해 test-catalog 의 대응 T-id 가 positive + negative case 로 `tests/v3` 또는 `tests/e2e` 에 존재하고 통과
- SC-3: `TASK_TRACE` 상태가 `inherited_or_not_started_not_qualified` 인 task 가 0 개
- SC-4: 외부 자격 9 건은 `disabled/unqualified` 경계로 명시되고 stub 을 PASS 로 세지 않음
- SC-5: 세 독립 reviewer (contract / failure-recovery / regression) 에 P0·P1·Blocking-P2 없음
