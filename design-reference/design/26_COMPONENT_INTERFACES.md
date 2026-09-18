# 26. 구현 경계와 인터페이스

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 구현 단위의 규칙

`contracts/component-interfaces.json`에 application service의 책임·입출력·의존 포트·제안 경로를 고정했다. 함수 시그니처는 **설계 의사코드**이며 구현 파일을 생성한 것이 아니다. facade가 모든 작업을 다시 품는 monolith가 되지 않도록 storage/authority/effect/verification ownership을 지킨다.

Domain types는 `src/amplai_foundry/runtime/contracts/` 등 vendorneutral 계층에 두고, vendor event parser는 `agent_drivers/<vendor>/`에 둔다. provider SDK exception은 normalized typed error로만 application 계층에 올라온다. 일반 orchestration code가 JSON 문자열을 임의 조작해 승인·상태를 건너뛰지 못하게 한다.

## 2. Error와 result

expected condition은 typed Result(outcome,reason,refs), programming fault는 exception + fail-closed incident로 구분한다. NO_WORK는 오류가 아니다. HOLD는 재시도 가능한 시스템 에러와 다르다. success response에는 committed object ref와 row_version/cursor를 포함한다. 모든 asynchronous start는 accepted handle이고 완료 결과는 evidence/verdict를 통해 별도로 조회한다.

## 3. 구현 시 필수 method contract

각 public application method는 입력 schema, authenticated ActorContext, allowed state, expected revision, mandatory gates, transaction boundary, emitted events, outbox effect, idempotency semantics, failure rollback, telemetry, test IDs를 docstring 또는 service spec에 표기한다. 이 열 가지가 없는 handler는 완료로 인정하지 않는다.

endpoint payload의 actor/status/grant 발급정보를 server-owned model과 혼합하지 않는다. Command DTO는 caller-writable fields만 받고 Domain Record는 서버가 생성한다. JSONSchema와 Pydantic 간 mapping은 one-way generator 또는 자동 equivalence test를 선택한다. 수동으로 두 schema를 유지하면 CI가 diff를 잡아야 한다.

## 4. 기존 CLI와의 접합

`scripts/amplai_runtime.py`와 portable runtime 기능을 하나의 application service로 수렴한다. CP 접속 불가 fallback은 명시적 local profile로만 선택하며 자동으로 독립 상태 원장을 만들어 동일 Work를 실행하지 않는다. legacy CLI는 v2 payload를 adapter로 변환하여 `legacy_imported` provenance를 보존한다.

`/work`는 입력→GoalService command, `/design`은 mode=design command다. host entry 파일에는 복잡한 governance implementation을 넣지 않고 사용법/최소 보호 경계/필수 문서 링크를 둔다. agent memory나 host resume 정보는 canonical status와 혼동하지 않는다.

## 5. Pseudocode의 해석

문서의 직선 화살표는 병렬·eventual consistency·async 실패를 생략한 설명이다. 실제 order/guard는 state machine과 transaction spec이 우선한다. '검증 후 실행'의 검증은 capability/authority/contract 사전검증이며 '실행 후 검증'은 실제 outcome 검증이다. 두 의미를 구분한다.
