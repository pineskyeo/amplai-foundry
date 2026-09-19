# 01 · V3 제품 범위와 결정

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 제품 정의

**AMPLAI V3 — Intent to Verified Work**. 사람은 의도·금지사항·가치 판단을 제공한다. AMPLAI는 접근 가능한 사실로 목표를 구체화하고, 검증 가능한 계약을 만들고, 필요한 작업을 조합하고, 증거가 갖춰진 결과만 완료로 인정한다. 그 실행 경험으로 harness 변경안을 만들되, 변경 제안과 승인·적용 권한을 분리한다.

흐름은 `Intent → Discovery/Goal Resolver → Goal Contract → WorkGraph → Adaptive execution → Evidence/Verdict → Verified outcome → Evolution proposal`이다. 질문·중단·실패·모호함도 정상 결과다. 목표가 불가능하거나 검증할 수 없으면 agent 수·반복 횟수를 늘려 덮지 않는다.

## 2. 이번 납품과 다음 구현의 경계

이번 패키지는 **설계 문서, 규범 계약, 예시 데이터, 테스트 명세, 이전 지도, 구현 작업 목록, 역할 프롬프트**다. AMPLAI 소스를 고치거나 installer를 실행하거나 production을 변경하지 않았다. JSON Schema 검증과 링크·참조·커버리지 검사는 설계 패키지 검증이지 V3 runtime의 동작 테스트가 아니다.

전체 기능을 V3 하나의 목표 아키텍처로 설계한다. 구현 backlog의 단계는 선후 의존성이지 V2 패치를 거쳐 가거나 Meta-harness를 다음 버전으로 미루자는 뜻이 아니다. 실제 설치 전환은 검증·백업·권한 확인 때문에 staging/canary/cutover 순서를 따른다. “한 번에 V3”와 “전 fleet에 동시에 무검증 덮어쓰기”는 다르다.

## 3. 지켜야 할 기존 자산

Foundry의 project identity, immutable Source, Proposal/Decision/Apply 분리, provenance, approved snapshot, 감사·outbox를 보존한다. 기존 knowledge object의 ID와 supersedes 관계를 일괄 재발급하지 않는다. `/work`와 `/design`만 일상 public entry로 남긴다. Claude/Codex/OpenCode는 실행 host이고 AMPLAI는 그 위의 도메인 지식·정책·작업 제어다. Worker는 Canonical Memory, 승인 ledger, production credential을 직접 소유하지 않는다.

Platform과 Kit는 독립 제품/release train을 유지한다. V3는 아키텍처 세대명이다. 제안 릴리스 목표는 Platform `3.0.0`, Kit `3.0.0`, Protocol `3.0`, capability pack별 독립 SemVer다. 같은 숫자라고 상호 의존성을 암묵적으로 만들지 않는다. ReleaseSet manifest가 실제 호환 조합을 pin한다.

## 4. 이번 재조사로 보정한 앞선 제안

- `AstraDriver`는 만들지 않는다. Astra는 model이다. `CodexCliDriver`, 선택 `CodexAppServerDriver`, `OpenAIResponsesDriver`, `ClaudeCodeDriver`, `OpenCodeDriver`, `ManagedAgentDriver`와 `ModelProfile`을 직교시킨다. [R01–R05, R22–R27]
- `target_app_hint` 필수 입력은 없애지만, **안전하게 resolve된 target app 필수 조건은 유지**한다. LLM의 자신감만으로 잘못된 repo를 선택하지 않는다.
- WorkGraph를 DAG로 제한하는 것은 AMPLAI macro execution의 선택이다. 일반적인 agent graph에는 cycle이 있다. 각 Work 내부의 loop, 재계획 revision, retry는 명시적으로 남긴다. [R12]
- handoff 파일 부재는 optional installer 항목일 수 있다. 증거가 부족하면 drift 후보이지 확정 결함이 아니다. 반대로 kit VERSION/문서와 gardening enum 차이는 실제 정적 불일치로 기록한다.
- legacy governance는 살아 있는 승인·복구 경로에서 import된다. 이름 때문에 제거하지 않는다. 안전 기능을 옮기고 동등성 테스트를 통과한 뒤 archive/remove한다.
- 모델이 강해져도 sandbox, authorization, verify, audit는 제거 대상이 아니다. 줄일 대상은 중복 prompt와 의미 없는 고정 planning 단계다.

## 5. V3 필수 범위

Goal Resolver와 Contract Critic, versioned WorkGraph와 replan, Direct/Loop/Deliberative strategy 선택, durable scheduler·lease·fencing·recovery, session/steering ledger, provider-neutral drivers와 모델 route, Tool/Secret Broker, capability pack registry, knowledge readiness·context resolver·ontology ports, deterministic+semantic+human verifier, visual/document QA pack, Eval Observatory, harness federation 및 proposal-driven evolution, immutable ReleaseSet·distribution·migration·rollback까지 포함한다.

외부 서비스 adapter는 계약과 qualification fixture가 필수다. 사용 권한/사내 정책이 없는 cloud connector는 `unqualified/disabled` 상태로 완성된 경계를 제공한다. 이는 핵심 기능 구현을 stub으로 남기는 것과 다르다. 실제 cloud 작동을 시험하지 않고 “연동 완료”라 쓰지 않는다.

## 6. 명시적 비목표

모델 자체 학습/가중치 수정, 무인 production 제어, 승인 없는 Git push/merge/deploy, 범용 microservice 플랫폼, 모든 domain을 중앙 ontology로 합치기, 모든 코드의 embedding index, 자체 graph DB/queue/identity provider 제작은 하지 않는다. 기존 Cortex/Synapse 제품 기능을 이번 재설계 명목으로 바꾸지 않는다. Frontend·Docify를 Core에 흡수하지 않고 pack 계약과 verifier profile로 연결한다.

## 7. 성공의 정의

동일한 intent+facts+policy snapshot에서 canonical contract 검증 및 graph compilation은 재현 가능해야 한다. LLM 초안의 동일 byte 재현은 요구하지 않는다. 각 결과에 사용한 입력·모델·driver·harness·verifier revision이 추적되어야 한다. 동시 worker·서버 중단·중복 이벤트·취소·권한 철회·상위 계약 변경에도 stale 결과가 적용되지 않아야 한다. 실제 비용 절감률은 baseline 측정 전 약속하지 않는다.
