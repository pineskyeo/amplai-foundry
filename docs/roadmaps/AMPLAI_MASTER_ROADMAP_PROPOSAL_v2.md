# AMPLAI Master Roadmap Proposal v2

> 상태: Approved
> 기준 저장소: `amplai-foundry-main` 0.1.0  
> 갱신일: 2026-07-25  
> 승인일: 2026-07-26
> 승인자: user
> 목적: 프로젝트 단위의 지식·온톨로지·에이전트 실행·평가·메타루프를 하나의 확장 가능한 AMPLAI 구조로 정렬한다.

## v2 핵심 변경

v1은 `Project Pack`을 첫 구현 대상으로 두었으나, 실제 사용자 경험 관점에서는 사용자가 매번 저장 위치·지식 유형·ID·관계를 지시해야 하는 문제가 있었다. v2는 이를 바로잡아 다음 원칙을 최우선으로 확정한다.

> **사용자는 의미와 목적만 말하고, AMPLAI가 프로젝트 선택, 원문 보존, 지식 분류, 중복·충돌 분석, 관계 생성, Proposal 작성, 안전 변경 적용과 검증을 수행한다.**

이에 따라 기존 Phase 1과 Phase 2를 다음처럼 재구성한다.

```text
Phase 1A — Minimal Project Identity
Phase 1B — Knowledge Intake & Steward
Phase 1C — Full Project Pack v1
```

`Knowledge Intake & Steward`는 단순 편의 기능이 아니라 AMPLAI가 지식 시스템으로서 의미를 갖기 위한 첫 번째 필수 Vertical Slice다.

---

## 1. 결론

AMPLAI는 단순한 메모리 저장소나 범용 Agent 앱이 아니라 다음 여섯 계층으로 발전해야 한다.

1. **Client Partner** — 사용자의 자연어 의도를 프로젝트 목표와 변경 의도로 해석한다.
2. **Knowledge Steward / Foundry** — 입력을 Source로 보존하고, 지식 후보·중복·충돌·관계를 분석해 통제된 Proposal로 전환한다.
3. **Context Runtime** — 프로젝트에 필요한 지식·온톨로지·사실·도구를 질문별 Context Packet으로 조립한다.
4. **Agent Harness** — 외부 Agent와 내부 Agent의 도구, 권한, Loop, 종료 조건을 통제한다.
5. **Observatory** — 실행 이벤트, 평가, 실패 패턴과 개선 효과를 측정한다.
6. **Meta-loop** — 관찰된 실패를 지식·온톨로지·Skill·Policy·Harness 개선 Proposal로 전환하고 검증한다.

이 전체를 운반하고 배포하는 기본 단위는 **AMPLAI Project Pack**으로 정의한다.

```text
AMPLAI Project Pack
├── Project Manifest
├── Canonical Memory
├── Ontology Profile
├── Project Facts and Mappings
├── Policies and Skills
├── Agent Profiles
├── Evaluation Suites
└── Derived Runtime State     # Git 원본이 아니며 재생성 가능
```

프로젝트는 하나의 도메인 트리만 상속하지 않는다. 여러 독립적인 도메인 모듈을 조합한다.

```text
Cortex Project
├── foundation
├── software-engineering
├── work-management
├── semiconductor-core
├── test-core
├── electrical-test
├── dc-test
└── cortex-application-profile
```

AMPLAI의 사용자 인터페이스는 저장 구조 중심이 아니라 **의도 중심**이어야 한다.

```text
사용자 입력
“이 로드맵을 AMPLAI의 공식 계획에 반영해줘.”

AMPLAI 내부 처리
프로젝트 판별
→ Source 보존
→ 문서 유형 판별
→ 지식 후보 추출
→ 기존 지식 비교
→ Roadmap/Tracker 갱신안 생성
→ Proposal 및 승인 항목 분리
→ Lint/Test
```

사용자는 어느 폴더에 저장할지, 어떤 ID를 사용할지, Concept인지 Decision인지 지시하지 않는다.

---

## 2. 현재 AMPLAI Foundry의 위치

현재 저장소는 **Phase 0: Canonical Memory Foundation**으로 평가한다.

### 이미 잘 만들어진 기반

- 저장 방식과 독립된 `MemoryObject`
- 공식 장기 지식과 실행 메모리의 분리
- Markdown + Git을 현재 공식 원본으로 사용
- 읽기 전용 `MemoryRepository` Port
- lifecycle, provenance, relation, link integrity lint
- 공식 지식 변경의 자동 승인 금지
- 검색 DB와 Vector Index를 파생 데이터로 취급
- External Agent Mode 우선
- Modular Monolith 우선
- Harness, Loop, Meta-loop 개념의 초기 정의

### 현재 코드에서 확인된 구현 범위

- 26개 Canonical Memory Note
- `source`, `concept`, `principle`, `decision`, `question`, `architecture`, `experiment`, `map`
- `lint`, `stats`, `show` CLI
- Markdown Repository Adapter
- 테스트 19개 통과 (`PYTHONPATH=src` 기준)
- Vault lint 0 error, 0 warning

### 아직 없는 핵심 기능

- 사용자의 의도를 기록 작업으로 변환하는 Client Partner 계약
- 프로젝트 자동 선택과 최소 Project Identity
- Source 수집·해시·중복 판별
- 입력 문서 유형 및 지식 유형 자동 분류
- 기존 Canonical Knowledge와 중복·충돌·대체 비교
- Memory Candidate, Proposal, Review, Apply
- 안전 변경 자동 적용과 위험 변경 승인 분리
- Roadmap/Tracker 자동 갱신
- 프로젝트 Manifest와 완전한 Project Pack 계약
- 다중 프로젝트 Import와 Domain Lock
- 정식 Ontology Module과 Domain Registry
- Project Fact 및 Knowledge Graph Instance
- 시스템 DB·파일 Mapping
- Search 및 Context Assembly
- Agent Tool Gateway와 MCP
- Run/Event Store와 평가
- 개발 Agent와 관리 Agent
- Meta-loop 실행 파이프라인
- 중앙 서버, 접근권한, Hybrid Model Routing

---

## 3. 현재 모델에서 먼저 고쳐야 할 구조적 한계

### 3.1 사용자가 저장 구조를 지시해야 하는 인터페이스는 실패한 인터페이스다

다음 요청은 AMPLAI의 목표 사용자 경험이 아니다.

```text
이 파일을 00-sources에 넣고,
Concept 2개와 Decision 3개를 만들고,
Map과 Tracker를 갱신해줘.
```

목표 사용자 경험은 다음처럼 단순해야 한다.

```text
이 로드맵을 AMPLAI에 반영해줘.
이번 작업 결과를 Cortex 프로젝트 지식으로 정리해줘.
이 결정을 공식 기준으로 채택해줘.
```

AMPLAI가 내부 저장 구조를 알고 있어야 하며, 사용자는 업무 의도와 승인 판단에만 집중해야 한다.

### 3.2 Namespace가 있지만 실제 Identity는 전역 ID로 동작한다

현재 `MarkdownMemoryRepository`는 `memory.id`만 key로 사용한다. 따라서 서로 다른 프로젝트가 각각 `CON-0001`을 갖는 정상적인 상황을 지원할 수 없다.

```text
현재
get("CON-0001")

목표
get(MemoryRef(namespace="org/pinesky/project/cortex", id="CON-0001"))
```

다중 프로젝트 전체를 구현하기 전이라도 Source와 Proposal의 귀속을 결정하기 위한 최소 `(namespace, id)` 계약은 먼저 필요하다.

### 3.3 Memory Relation과 Ontology Relation을 섞으면 안 된다

현재 relation은 지식 문서 사이의 관계다.

```text
supports
implements
derived_from
supersedes
```

도메인 온톨로지의 관계는 업무 세계의 관계다.

```text
LotRun executesLot Lot
TestExecution usesRecipe RecipeVersion
Observation generatedBy ActionExecution
```

두 관계는 목적과 검증 규칙이 다르므로 별도 모델과 Repository로 유지한다.

### 3.4 모든 것을 MemoryObject로 확장하면 안 된다

다음 객체는 각각 다른 lifecycle과 저장 특성을 가진다.

| 자산 | 의미 | Canonical 여부 | 권장 저장 경계 |
|---|---|---:|---|
| MemoryObject | 개념·결정·원칙·설명 | 예 | Markdown/Git |
| OntologyModule | 클래스·관계·제약 | 예 | Turtle/SHACL + Manifest |
| ProjectFact | 실제 Part·Run·Equipment 관계 | 일부 | Fact Repository / Graph Index |
| SourceArtifact | 원문·파일·대화·SOP | 증거 | Object/File Store + Metadata |
| Skill/Policy | 수행 절차·권한·Interlock | 예 | Versioned Files |
| RuntimeEvent | Run·Step·Tool Call·Observation | 아니오 | Event Store |
| Evaluation | 결과 점수·실패 원인 | 아니오 | Evaluation Store |
| Proposal | 공식 자산 변경 요청 | 검토 중 | Proposal Repository |
| RoadmapState | 단계·상태·완료 조건 | 운영 상태 | Plan/Tracker Repository |

### 3.5 Knowledge Steward는 LLM 프롬프트 하나가 아니라 통제된 Application Service다

Knowledge Steward는 LLM이 자유롭게 파일을 생성하는 기능으로 구현하지 않는다. 다음 결정론적 구성요소와 모델 기반 분류를 조합한다.

```text
Intent Resolver
Project Resolver
Source Registrar
Artifact Classifier
Candidate Extractor
Deduplication/Conflict Analyzer
Relation Builder
Roadmap Updater
Proposal Builder
Risk Classifier
Policy Gate
Validation Runner
```

LLM은 의미 추출과 후보 제안에 사용하고, ID 생성·저장 위치·정책·검증·Apply는 Application Service가 책임진다.

---

## 4. 목표 아키텍처와 사용자 경험 계약

```text
┌───────────────────────────────────────────────────────────────────────────────┐
│ User                                                                          │
│ “이 로드맵을 반영해줘” · “이번 결과를 Cortex 지식으로 정리해줘”              │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Client Partner                                                                │
│ Intent · Target Project · Authority · Expected Outcome 해석                   │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Knowledge Steward                                                             │
│ Ingest · Classify · Compare · Relate · Propose · Track · Validate             │
└──────────────┬───────────────────────────────┬────────────────────────────────┘
               │                               │
               ▼                               ▼
┌──────────────────────────────┐   ┌────────────────────────────────────────────┐
│ Governor / Policy Gate       │   │ Context Runtime                            │
│ Risk · Impact · Approval     │   │ Existing Knowledge/Ontology/Facts 조회     │
└──────────────┬───────────────┘   └───────────────────┬────────────────────────┘
               └───────────────────────┬───────────────┘
                                       ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Foundry                                                                       │
│ Source → Candidate → Proposal → Review/Auto-apply → Canonical/Operational     │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                 ▼
       Canonical Memory      Ontology/Profile   Project/Roadmap State
        Markdown/Git        Turtle/SHACL        Facts/Tracker/Plan
                                   │
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Agent Harness                                                                 │
│ Tool Policy · Permission · Loop · Stop Gate · Approval                        │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ Observatory → Meta-loop                                                       │
│ Run · Eval · Failure Pattern → Hypothesis → Experiment → Proposal             │
└───────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Intent-driven Intake 계약

최소 입력 계약은 다음과 같다.

```yaml
intent_request:
  instruction: "이 로드맵을 AMPLAI의 공식 계획에 반영해줘"
  artifacts:
    - AMPLAI_MASTER_ROADMAP.md
  project_hint: amplai       # 생략 가능, 자동 판별
```

AMPLAI의 출력은 저장 명령이 아니라 의미 있는 변경 요약이어야 한다.

```text
자동 처리
- Source 원본 등록
- Roadmap 문서와 Tracker 변경안 생성
- 기존 지식과 중복 3건 연결
- 신규 Candidate 7건 생성
- Lint/Test 통과

자동 적용
- Source 등록
- Tracker 상태 변경
- 기존 Map 관계 추가

승인 필요
- 새 Principle 1건
- Architecture Decision 2건
- 기존 Decision supersede 1건
```

### 4.2 변경 위험도와 승인 정책

| 등급 | 예 | 기본 처리 |
|---|---|---|
| 낮음 | Source 등록, 파생 Index, 명백한 Map 링크, Tracker 상태 | 정책 통과 시 자동 적용 |
| 중간 | Concept/Question/Experiment Candidate, 비파괴 문서 보완 | Candidate/Proposal 생성, 묶음 검토 가능 |
| 높음 | Principle, Decision, Architecture, Supersede, Ontology 의미 변경 | 사용자 승인 필수 |
| 제한 | 권한·배포·보안 Policy, Production Action, Breaking Change | Governor 및 별도 승인 필수 |

자동 적용은 공식 의미를 바꾸지 않는 변경으로 제한한다. Canonical Decision과 Ontology의 의미는 항상 검토 가능한 Proposal을 거친다.

---
## 5. Project Pack 표준 구조

초기에는 각 소프트웨어 프로젝트 안에 `.amplai/`를 두는 구성이 가장 이동성이 좋다.

```text
cortex/
├── src/
├── tests/
├── AGENTS.md
└── .amplai/
    ├── project.yaml
    ├── domain.lock.yaml
    │
    ├── memory/
    │   ├── sources/
    │   ├── concepts/
    │   ├── principles/
    │   ├── decisions/
    │   ├── architecture/
    │   ├── questions/
    │   ├── experiments/
    │   └── maps/
    │
    ├── ontology/
    │   ├── profile.yaml
    │   ├── project-extension.ttl
    │   └── project-shapes.ttl
    │
    ├── facts/
    │   ├── curated/
    │   └── examples/
    │
    ├── mappings/
    │   ├── source-code.yaml
    │   ├── database.yaml
    │   ├── artifacts.yaml
    │   └── external-systems.yaml
    │
    ├── skills/
    ├── policies/
    ├── agents/
    ├── evals/
    └── runtime/                 # gitignore
        ├── index/
        ├── events/
        ├── candidates/
        └── cache/
```

공유 Domain Package는 별도 Registry에서 관리한다.

```text
domains/
├── foundation/
├── software-engineering/
├── work-management/
├── semiconductor-core/
├── test-core/
├── electrical-test/
└── dc-test/
```

### project.yaml 예시

```yaml
schema_version: 1
project_id: cortex
namespace: org/pinesky/project/cortex
name: Cortex ATE Middleware

imports:
  - module: foundation
    version: 1.x
  - module: software-engineering
    version: 1.x
  - module: work-management
    version: 1.x
  - module: semiconductor-core
    version: 0.1.x
  - module: test-core
    version: 0.1.x
  - module: electrical-test
    version: 0.1.x
  - module: dc-test
    version: 0.1.x

profiles:
  - cortex-application-profile

canonical:
  memory: .amplai/memory
  ontology: .amplai/ontology
  skills: .amplai/skills
  policies: .amplai/policies
  evals: .amplai/evals

runtime:
  root: .amplai/runtime
```

### Project Pack의 이동 원칙

- Git에 들어가는 것은 Canonical Asset과 Manifest다.
- Index, Embedding, Cache, Run Event는 재생성하거나 별도 동기화한다.
- Secret과 Credential은 Pack에 저장하지 않는다.
- `domain.lock.yaml`로 Domain Package 버전을 고정한다.
- 다른 PC나 사내 환경에서 clone 후 `amplai project rebuild`로 파생 상태를 재생성한다.

---

## 6. 온톨로지 확장 모델

### 6.1 Domain Package는 트리가 아니라 Import Graph다

```text
foundation
├── measurement-common
├── equipment-common
├── software-engineering
└── work-management

semiconductor-core
└── test-core
    └── electrical-test
        └── dc-test

cortex-application-profile
├── dc-test
├── equipment-common
├── software-engineering
└── work-management
```

### 6.2 각 Ontology Module의 표준 파일

```text
domains/dc-test/
├── module.yaml             # ID, version, owner, imports, exports
├── ontology.ttl            # Class와 Property
├── shapes.ttl              # 필수값과 제약
├── vocabulary.ttl          # SKOS 용어·동의어·분류
├── competency-questions.md # 이 모듈이 답해야 하는 질문
├── examples/
│   ├── valid.trig
│   └── invalid.trig
└── tests/
```

### 6.3 공유 Domain으로 승격하는 절차

```text
Project에서 발견
→ Project-local Candidate
→ 둘 이상의 Project/Use Case에서 의미 동일성 검증
→ Domain Change Proposal
→ 영향도 및 호환성 검토
→ Domain Package 새 버전
→ Project Lock 갱신
```

프로젝트에서 처음 발견된 개념을 바로 `semiconductor-core`에 올리지 않는다. 재사용성이 입증되기 전에는 Project Profile 또는 하위 Domain에 둔다.

### 6.4 Ontology와 Memory의 연결

- Ontology는 형식적 의미와 제약의 원본이다.
- Memory Note는 그 의미를 선택한 이유, 경계, 사례와 반례를 설명한다.
- `OntologyTerm`은 관련 `MemoryRef`를 가질 수 있다.
- 두 자산을 복제하지 않고 역할을 분리한다.

```text
ontology:TestExecution
  formal meaning → ontology.ttl
  required fields → shapes.ttl
  rationale → ARC/CON/DEC MemoryObject
  examples → examples/*.trig
```

---

## 7. Agent가 사용하는 Project Context

전체 Vault나 전체 Ontology를 매번 모델에 넣지 않는다. Context Runtime이 질문별로 작은 **Context Packet**을 만든다.

```yaml
context_packet:
  project: cortex
  request_intent: diagnose_execution_failure
  scope:
    domains: [test-core, dc-test, cortex-profile]
    systems: [cortex, ate]
  ontology_slice:
    classes: [ActionExecution, PMIContext, Equipment]
    relations: [requires, executedOn, produced]
  canonical_memory:
    - ref: org/pinesky/project/cortex#DEC-0012
    - ref: org/pinesky/project/cortex#ARC-0007
  facts:
    - run_id: prod-...
  evidence:
    - artifact: production/actions/pmi.jsonl
  tools:
    - cortex.read_run
    - cortex.read_artifact
  policy:
    mode: read_only
```

### Context와 Memory의 경계 결정

- **Foundry/Memory Layer**: Canonical Asset 조회, provenance, lifecycle, scope를 소유한다.
- **Context Runtime**: Query 해석, ranking, token budget, ontology slice, evidence packaging을 소유한다.
- **Agent Harness**: Context Packet을 사용해 Loop와 Tool 실행을 소유한다.

이는 현재 열린 `Context와 Memory의 정확한 module 경계` 질문에 대한 권장 결정이다.

---

## 8. Agent 확장 방향

### 8.1 공통 역할

| 역할 | 책임 |
|---|---|
| Client Partner | 사용자의 자연어 요구를 내부 목표와 의도 계약으로 변환 |
| Knowledge Steward | Source 등록, 분류, 후보 추출, 중복·충돌 분석, 관계·Proposal·Tracker 생성 |
| Router | 프로젝트·도메인·도구·모델 선택 |
| Planner | 목표, 제약, 성공 기준과 작업 순서 수립 |
| Implementer | 코드·설정·문서 변경 수행 |
| Reviewer | 결과, 테스트, 근거와 위험 검토 |
| Governor | 권한·정책·위험도·승인·배포 Gate 판단 |
| Evaluator | 결과를 결정론적·모델 기반 평가로 점수화 |
| Meta-loop Analyst | 반복 실패와 개선 기회를 Proposal로 전환 |

초기에는 이 역할을 모두 독립 Agent로 만들지 않는다. 역할 계약과 Application Service를 먼저 정의하고 한 개 External Agent가 여러 역할을 순차 수행하도록 시작한다.

Knowledge Steward는 기존 `Curator` 역할을 포함하지만 범위가 더 넓다. 단순 후보 추출뿐 아니라 프로젝트 귀속, 기존 지식 비교, Roadmap/Tracker 갱신, 위험도 분류와 검증까지 책임진다.

### 8.2 Knowledge Intake Vertical Slice

첫 번째 실제 사용자 Workflow는 다음과 같다.

```text
사용자
“이 로드맵을 AMPLAI에 반영해줘.”
       │
       ▼
Client Partner
intent=adopt_roadmap, project=amplai, authority=user
       │
       ▼
Knowledge Steward
Source 보존
→ roadmap 유형 판별
→ Phase/Goal/Decision/Question 후보 추출
→ 기존 Memory와 중복·충돌 비교
→ Roadmap과 Tracker 변경안 생성
→ Proposal 구성
       │
       ▼
Governor
안전 변경 자동 적용
+ 의미 변경 승인 요청
       │
       ▼
Lint/Test와 변경 요약
```

이 Vertical Slice의 성공 기준은 사용자가 저장 위치·ID·지식 유형·관계 이름을 한 번도 지정하지 않는 것이다.

### 8.3 개발 Agent Vertical Slice

```text
Issue 또는 자연어 요구
→ Project Context 구성
→ 관련 Decision·Architecture·Ontology 조회
→ 구현 계획
→ Code 변경
→ Test/Lint/Build
→ Reviewer 평가
→ Knowledge Steward가 작업 결과에서 Candidate 생성
→ Governor 승인 대상 분리
```

### 8.4 관리 Agent Vertical Slice

관리 Agent용 공통 도메인 `work-management`를 별도로 둔다.

```text
Goal
Workstream
Task
Owner
Status
Dependency
Risk
Decision
Milestone
Evidence
```

```text
팀 메신저 대화
→ 업무 상태 Candidate 추출
→ 기존 Task와 중복·변경 비교
→ Jira/업무 시스템 반영 Proposal
→ 사용자 또는 Governor 승인
→ 일정·리스크·결정 요약
```

대화의 모든 문장을 Canonical Memory로 저장하지 않는다. 업무 상태는 Operational Store에 두고, 장기적으로 재사용할 Decision·Principle·Architecture만 Foundry로 승격한다.

---

## 9. Meta-loop의 안전한 형태

AMPLAI의 자기 발전은 **무제한 자기 수정**이 아니라 다음의 통제된 개선 Loop다. Meta-loop가 만든 개선안도 Knowledge Steward의 Proposal 형식과 Governor Gate를 동일하게 사용한다.

```text
1. Observe
   Run, Step, Tool Call, 결과, 비용, 실패를 기록한다.

2. Evaluate
   Task Eval, Retrieval Eval, Policy Eval, Domain Eval로 점수화한다.

3. Diagnose
   반복 실패가 지식 누락, Ontology 오류, Tool 부족, Prompt 문제,
   Policy 문제 또는 Model 선택 문제인지 분류한다.

4. Propose
   개선안을 Memory/Ontology/Skill/Policy/Harness Proposal로 만든다.

5. Experiment
   과거 Run Replay 또는 Sandbox에서 변경 전후를 비교한다.

6. Review
   Governor와 사람이 근거, 영향, 위험을 검토한다.

7. Apply
   승인된 변경만 Branch/PR 또는 Review Apply를 통해 반영한다.

8. Verify and Promote
   회귀 Eval을 통과하면 활성화하고 실패하면 Rollback한다.
```

### Meta-loop가 변경할 수 있는 대상

- Retrieval rule과 Context Packet 구성
- Prompt와 Skill
- Tool 선택 정책
- Harness 종료 조건과 Retry 정책
- Ontology 정의와 Constraint
- Canonical Memory
- Evaluation Dataset과 Threshold
- Model Routing Policy

### 절대 자동 승격하지 않는 대상

- 공식 Decision
- 안전·배포·권한 Policy
- Production 실행 권한
- Domain Ontology의 Breaking Change
- Source가 불명확한 Canonical Knowledge

---

## 10. 우선순위 로드맵

## Phase 0 — Foundry Baseline 고정

**상태:** 현재 구현됨, 보강 필요

**목표**
- 현재 계약을 회귀 기준으로 고정한다.
- 기존 26개 Note와 lint를 첫 Canonical Corpus로 유지한다.

**구현 항목**
- CI에서 pytest, Ruff, mypy, vault lint 실행
- Version과 migration policy 문서화
- Intent-driven Intake 구현 전후를 비교할 Golden Fixture 준비

**완료 Gate**
- 깨끗한 clone에서 한 명령으로 모든 검증 통과
- 현재 Memory Contract의 Golden Fixture 고정

---

## Phase 1A — Minimal Project Identity와 Intent Boundary

**목표**
- 입력된 Source·Candidate·Proposal이 어느 프로젝트에 속하는지 안전하게 결정할 최소 경계를 만든다.
- 전체 Project Pack보다 먼저 Knowledge Intake가 의존하는 Identity만 구현한다.

**핵심 산출물**

```text
ProjectRef
MemoryRef(namespace, local_id)
IntentRequest
AuthorityContext
ProjectResolver
```

**구현 항목**
- `(namespace, local_id)` qualified identity
- 기본 프로젝트와 명시적 project hint 지원
- 모호한 프로젝트 판별 시 안전한 보류 상태
- cross-project reference 최소 검증
- 기존 단일 프로젝트 CLI 호환 계층

**완료 Gate**
- AMPLAI와 Cortex가 각각 같은 local ID를 사용 가능
- 입력 파일과 대화가 잘못된 프로젝트에 자동 반영되지 않음
- 프로젝트를 확정하지 못하면 Canonical 변경 없이 검토 대상으로 보류

---

## Phase 1B — Knowledge Intake & Steward Vertical Slice

**최우선 사용자 가치 단계**

**목표**
- 사용자가 저장 구조를 설명하지 않고도 자연어 의도와 파일만으로 지식 반영 절차를 시작하게 한다.
- Agent가 공식 지식을 직접 수정하지 않으면서 Source부터 Proposal과 안전 변경 적용까지 수행한다.

**첫 사용자 시나리오**

```text
입력
“이 로드맵을 AMPLAI의 공식 계획에 반영해줘.”
+ roadmap.md

출력
Source 등록
Roadmap/Tracker 변경안
Canonical Candidate
중복·충돌 분석
자동 적용된 안전 변경
승인 필요한 의미 변경
Lint/Test 결과
```

**핵심 모델**

```text
IntentRequest
SourceRecord
ArtifactClassification
KnowledgeCandidate
ComparisonResult
ChangeProposal
ReviewDecision
ApplyPlan
ChangeSet
RoadmapUpdate
ValidationReport
```

**Knowledge Steward 구성요소**

```text
Intent Resolver
Project Resolver
Source Registrar
Artifact Classifier
Candidate Extractor
Deduplication/Conflict Analyzer
Relation Builder
Roadmap Updater
Proposal Builder
Risk Classifier
Policy Gate
Validation Runner
```

**구현 항목**
- Source SHA-256, origin, collected_at, authority 기록
- roadmap, architecture proposal, meeting note, incident, SOP, implementation result 분류
- Concept/Principle/Decision/Question/Experiment/Architecture 후보 추출
- 기존 Note와 신규·중복·보완·충돌·대체 후보 비교
- Roadmap Phase와 Tracker 갱신안 생성
- 변경 위험도 분류와 정책 기반 자동 적용
- optimistic revision check
- 승인된 Apply만 Canonical Repository 변경
- Supersede 변경의 원자적 적용

**자동 적용 범위**
- Source 원본과 메타데이터 등록
- 공식 의미를 바꾸지 않는 Map 관계
- Tracker의 진행 상태와 파생 Index
- 정책상 안전하다고 명시된 비파괴 변경

**승인 필수 범위**
- Principle, Decision, Architecture
- 기존 Canonical Note의 의미 변경과 Supersede
- Ontology 의미 및 Constraint 변경
- 권한·보안·Production Policy

**완료 Gate**
- 사용자는 `반영해줘`라는 의도와 입력물만 제공
- 저장 위치, ID, 지식 유형, 관계를 사용자에게 요구하지 않음
- Source 한 건에서 Candidate, 비교, Proposal, Review, Apply까지 재현 가능
- 승인 없이 Authoritative Canonical Meaning이 바뀌는 경로가 없음
- 같은 입력의 재처리에서 중복 Note가 생성되지 않음

---

## Phase 1C — Full Project Pack v1과 Multi-project Local Workspace

**목표**
- 프로젝트를 독립적으로 이동·복제·검증할 수 있는 완전한 단위로 만든다.
- Knowledge Steward가 만든 자산, 정책, 평가와 Runtime 경계를 Project Pack으로 묶는다.

**핵심 산출물**
- `ProjectManifest`
- `ProjectPackRepository`
- Local Workspace Resolver
- `DomainImport`
- `domain.lock.yaml`
- Canonical/Derived Runtime 경계
- 기존 Vault Migration

**CLI 후보**

```text
amplai project init
amplai project validate
amplai project list
amplai project pack
amplai project rebuild
```

**필수 테스트**
- 두 프로젝트가 각각 `CON-0001`을 가져도 충돌하지 않는다.
- 잘못된 cross-project reference를 탐지한다.
- import cycle과 잠금 버전 불일치를 탐지한다.
- 다른 경로에 clone해도 동일 Project Pack을 읽는다.
- Runtime/Index를 삭제해도 Canonical Pack으로 재구성한다.

**완료 Gate**
- AMPLAI와 Cortex 두 Project Pack을 같은 Workspace에서 동시에 lint·조회
- Project Pack 이동 후 Knowledge Intake와 Proposal 이력이 유지
- Runtime/Index를 삭제해도 Canonical Pack만으로 재구성

---

## Phase 2 — Ontology Kernel과 Domain Registry

**목표**
- 확장 가능한 Domain Package의 공통 계약을 구현한다.
- Knowledge Steward가 입력에서 발견한 용어를 기존 Ontology와 비교하고 Project-local Candidate로 제안할 수 있게 한다.

**핵심 모델**

```text
OntologyModule
OntologyVersion
OntologyTerm
OntologyProperty
ConstraintShape
VocabularyConcept
OntologyProfile
DomainImport
SystemMapping
```

**구현 항목**
- `module.yaml` schema
- Turtle/SHACL parser와 validator
- Import graph와 semantic version lock
- URI/Term 중복 검사
- Deprecation/Supersede/Breaking change 분류
- Competency Question test runner
- Ontology Registry CLI
- Ontology Candidate와 Domain Change Proposal

**CLI 후보**

```text
amplai ontology lint
amplai ontology graph
amplai ontology explain <term>
amplai ontology test <module>
amplai ontology impact <change>
```

**완료 Gate**
- Foundation, Test Core, DC Test의 최소 3개 Module import 성공
- Valid/Invalid example이 SHACL Gate를 정확히 통과·실패
- Module 변경의 영향 Project를 조회 가능
- 신규 용어가 검토 없이 공유 Domain으로 승격되지 않음

---

## Phase 3 — DC Test E2E Ontology Vertical Slice

**목표**
- 전체 반도체 온톨로지가 아니라 실제 업무 한 줄을 끝까지 연결한다.

**첫 범위**

```text
Part + ProcessStep
→ Approved Condition
→ RecipeVersion
→ TestPlanArtifact
→ ExecutionPlan
→ LotRun / WaferRun / TestAttempt
→ ActionExecution
→ Observation
→ CalculatedResult
→ Judgment
→ RetestDecision
```

**구현할 Domain Module**

```text
foundation
semiconductor-core
test-core
electrical-test
dc-test
equipment-common
```

**Application Profile**

```text
recipe-studio-profile
synapse-profile
cortex-profile
ate-profile
```

**실제 데이터 작업**
- Golden Trace 1건 선정
- 각 시스템 권위 정보 정의
- DB, JSONL, TPL/LIM, RES/ROOT field mapping
- 계획값과 실제 적용값 분리
- Attempt와 Selected Result 분리

**완료 Gate**
- 한 결과에서 최초 Condition과 승인 근거까지 역추적
- Recipe/Plan/Run/Result 중 끊긴 link를 자동 보고
- Part–Step–PPID 불일치와 Retest context 누락 검증

---

## Phase 4 — Derived Search와 Context Runtime

**목표**
- Agent가 전체 지식을 읽지 않고 필요한 의미와 증거만 받게 한다.

**우선 구현 순서**
1. Structured filter
2. SQLite FTS5
3. Graph adjacency/path query
4. Context Packet assembler
5. 대표 Query Evaluation
6. Vector Search는 기준 미달이 확인될 때만 추가

**핵심 모델**

```text
ContextRequest
Scope
RetrievalHit
EvidenceRef
OntologySlice
ToolHint
ContextPacket
```

**완료 Gate**
- 프로젝트와 권한 범위를 벗어난 결과가 섞이지 않음
- 모든 Canonical Claim에 MemoryRef/SourceRef 포함
- 대표 질의에서 Keyword+Graph 기준선 측정
- 같은 입력으로 결정론적 Context Packet 재현 가능

---

## Phase 5 — External Agent Gateway와 Harness Contract

**목표**
- 자체 범용 Agent Runtime보다 Claude, Codex, 사내 GLM이 AMPLAI를 사용하게 한다.

**Read Capability**

```text
project.describe
memory.get
memory.search
ontology.resolve
ontology.explain
graph.trace
source.read
policy.check
context.build
```

**Controlled Write Capability**

```text
intake.submit
candidate.create
proposal.create
review.submit
```

Canonical write와 Production action은 직접 노출하지 않는다.

**구현 항목**
- CLI/API 공통 Application Service
- MCP Adapter
- Tool catalog와 permission scope
- Read-only DB connector
- Tool call audit
- 모델별 adapter와 data classification hook

**완료 Gate**
- Codex/Claude/GLM 중 두 실행기가 동일 Project Pack을 사용
- 단순 자연어 요청으로 Knowledge Steward Pipeline 호출
- Tool 호출과 근거를 재현 가능하게 기록
- 금지된 write/action이 Policy Gate에서 차단

---

## Phase 6 — Development Agent Vertical Slice

**목표**
- Domain Knowledge가 실제 개발 품질을 높이는지 검증한다.

**첫 Workflow**

```text
요구 해석
→ 관련 Architecture/Decision/Ontology 검색
→ 변경 영향 분석
→ 구현
→ Build/Test/Lint
→ Reviewer
→ 결과와 근거 제공
→ Knowledge Steward가 Candidate/Proposal 생성
```

**Harness 기능**
- 프로젝트별 AGENTS/CLAUDE/CODEX 지침 생성
- Command whitelist
- DB read-only control
- 위험 작업 Governor 승인
- 검증 실패 시 제한된 Retry Loop

**완료 Gate**
- 대표 Cortex 변경 과제 세트에서 기존 Agent 단독 대비 결과 비교
- 결정 위반과 Domain Contract 위반을 실행 전에 탐지
- 작업 종료 후 사용자는 저장 방법을 지시하지 않고 지식 반영 결과만 검토

---

## Phase 7 — Management Agent Vertical Slice

**목표**
- 팀 대화를 업무 상태로 정리하고 Jira/업무 관리와 연결한다.

**구현 항목**
- `work-management` Ontology Module
- Messenger Ingestion Adapter
- Task/Decision/Risk Candidate extraction
- Existing work item deduplication
- Jira Proposal/Apply Adapter
- 일정과 의존성 검사
- 주간 상태·위험·결정 요약

**완료 Gate**
- 대화 근거가 없는 Task를 생성하지 않음
- 기존 Task와 중복 등록을 방지
- 외부 시스템 write는 명시적 승인 후 수행
- 중요한 Decision만 Canonical Memory Proposal로 승격

---

## Phase 8 — Run/Event Store와 Evaluation Observatory

**목표**
- Meta-loop가 추측이 아니라 측정값에 기반하도록 한다.

**핵심 모델**

```text
AgentRun
RunStep
ToolCall
Observation
ArtifactRef
EvaluationCase
EvaluationResult
FailureCategory
Feedback
ReplaySet
```

**평가 층**
- Retrieval relevance
- Intake classification accuracy
- Deduplication/conflict detection accuracy
- Proposal acceptance/correction rate
- Ontology competency questions
- Task correctness
- Code test/build quality
- Policy compliance
- Citation/provenance completeness
- Latency/cost/token
- Human acceptance and correction

**완료 Gate**
- 모든 Agent Run에 결과, Tool Trace, 평가와 실패 분류 존재
- Knowledge Intake 오분류와 사용자 수정 이력을 평가 데이터로 축적
- 과거 Run을 새 Harness/Prompt로 Replay 가능
- Project별 Scorecard와 회귀 기준선 존재

---

## Phase 9 — Proposal-driven Meta-loop

**목표**
- AMPLAI가 자신의 실패를 관찰하고 개선안을 검증해 제안한다.

**첫 Meta-loop Use Case**

```text
반복되는 Knowledge Intake 오분류 또는 Context 누락 탐지
→ 원인: 분류 규칙, Ontology relation, retrieval rule 누락
→ 개선 Proposal 생성
→ 과거 실패 Run Replay
→ 성공률과 부작용 비교
→ Governor Review
→ 승인 적용
→ 회귀 Eval
```

**구현 항목**
- Failure aggregation
- Improvement hypothesis generator
- Experiment runner
- Change impact analyzer
- Promotion/Rollback workflow
- Knowledge GC와 stale/conflict detection

**완료 Gate**
- 실제 반복 실패 한 종류가 전체 Meta-loop를 통해 감소
- 개선 전후 Eval 증거와 변경 Provenance 존재
- 실패한 개선은 자동 Rollback 또는 비활성화
- Canonical Knowledge/Ontology는 항상 Review Gate 유지

---

## Phase 10 — Central Memory Server, Multi-user, Hybrid Security

**목표**
- 로컬 Project Pack 모델을 유지하면서 조직 단위 공유와 권한을 추가한다.

**구현 항목**
- PostgreSQL Canonical Adapter 또는 Git-backed service
- Project/Domain Registry Server
- ACL, Role, Audit
- Local cache와 offline sync
- Conflict-aware proposal merge
- Source classification과 redaction
- On-prem GLM / Cloud Model routing
- Secret store와 credential boundary
- 필요가 입증된 경우 RDF Store/Graph DB

**완료 Gate**
- 로컬 Pack과 중앙 Server가 같은 Domain Contract를 사용
- 프로젝트 간 정보 유출 방지
- Cloud 전송 금지 데이터가 정책에 따라 사내 Model로만 라우팅
- 중앙 장애 시 읽기 가능한 Local Pack 유지

---

## 11. 지금 하지 말아야 할 것

1. 사용자에게 매번 폴더, ID, 지식 유형과 관계를 지정하게 하는 것
2. LLM이 검증 없이 Canonical 파일을 직접 생성·수정하게 하는 것
3. 전체 반도체 산업 온톨로지를 먼저 만드는 것
4. 모든 Note와 로그를 하나의 거대 Graph DB에 넣는 것
5. Vector DB를 기본 검색으로 도입하는 것
6. 대화 전체를 자동으로 장기 Memory로 저장하는 것
7. Agent가 Canonical Memory나 Ontology를 직접 수정하게 하는 것
8. 자체 범용 Agent Runtime을 External Agent 연동보다 먼저 만드는 것
9. 중앙 서버와 권한 시스템을 Local Project Pack보다 먼저 만드는 것
10. 모든 역할을 독립 Multi-Agent로 한꺼번에 분리하는 것
11. Raw 측정 데이터 전체를 Knowledge Graph에 복제하는 것
12. Meta-loop를 자동 배포 시스템으로 오해하는 것
13. 분류 확신이 낮은 입력을 임의의 프로젝트나 Canonical 유형으로 강제 저장하는 것

---

## 12. 가장 먼저 착수할 설계·구현 패키지

다음 작업은 `Full Project Pack` 전체가 아니라 **Phase 1A + Phase 1B의 최소 Vertical Slice**다.

### 12.1 목표 사용자 경험

```text
사용자:
“이 로드맵을 AMPLAI의 공식 계획에 반영해줘.”

AMPLAI:
- 입력 프로젝트와 문서 의도를 판별
- 원문 Source 보존
- 기존 Roadmap/Memory와 비교
- 변경 Proposal 생성
- 안전 변경 적용
- 승인 필요한 항목만 제시
- Lint/Test 수행
```

사용자는 저장 구조를 알 필요가 없어야 한다.

### 12.2 제안할 Canonical Knowledge

```text
CON — Knowledge Steward는 지식 입력을 통제된 변경 Proposal로 변환한다
CON — Intent Request는 저장 명령이 아니라 업무 목적을 표현한다
PRI — 사용자는 의미를 제공하고 AMPLAI는 저장 구조를 책임진다
PRI — 자동 적용은 공식 의미를 바꾸지 않는 변경으로 제한한다
PRI — 분류 불확실성은 임의 저장보다 보류를 우선한다
DEC — Knowledge Intake를 Full Project Pack보다 먼저 구현한다
DEC — Knowledge Steward는 LLM과 결정론적 Application Service의 조합이다
ARC — Client Partner, Knowledge Steward, Governor, Foundry의 경계
QUE — 프로젝트 자동 판별의 최소 신뢰도와 보류 기준은 무엇인가
QUE — Canonical Candidate의 묶음 승인 UX는 어떻게 구성할 것인가
EXP — Roadmap 한 건을 자연어 요청만으로 반영하는 E2E 실험
```

### 12.3 첫 구현 순서

1. `ProjectRef`, `MemoryRef`, `IntentRequest`, `AuthorityContext` 모델
2. 최소 `ProjectResolver`와 안전한 unresolved 상태
3. `SourceRecord`와 SHA-256 중복 등록 방지
4. `ArtifactClassification`과 roadmap 분류
5. `KnowledgeCandidate` 공통 모델
6. 기존 Memory와 exact/semantic 중복·충돌 비교
7. `ChangeProposal`, `ApplyPlan`, `ReviewDecision`
8. Roadmap/Tracker Update Adapter
9. Risk Classifier와 Auto-apply Policy
10. Validation Runner와 변경 요약 Report
11. 첫 E2E Fixture: `이 로드맵을 반영해줘`
12. 동일 입력 재처리, 오분류, 프로젝트 불명확, 승인 거부 테스트

### 12.4 첫 단계 완료 정의

```text
입력물과 “반영해줘”라는 의도만으로 전체 Pipeline 실행
사용자가 저장 위치나 유형을 지정하지 않음
같은 입력을 두 번 처리해도 중복 Canonical Note 없음
높은 위험의 의미 변경은 승인 없이 적용되지 않음
불명확한 프로젝트/분류는 안전하게 보류
변경과 검증 결과를 사람이 이해하는 요약으로 제공
```

### 12.5 이후 Full Project Pack으로 확장하는 이유

- Intake가 먼저 동작해야 Project Pack의 저장 계약이 실제 사용자 흐름으로 검증된다.
- Source, Candidate, Proposal, Tracker의 실제 요구를 확인한 뒤 Pack 구조를 고정해야 과설계를 줄인다.
- 완전한 Project Pack은 Knowledge Steward의 산출물을 이동·복제·버전 고정하는 단위로 자연스럽게 확장된다.

---

## 13. 최종 제품 정의

AMPLAI의 목표 상태는 다음 문장으로 정의한다.

> **AMPLAI는 사용자의 자연어 의도와 업무 증거를 프로젝트별 Canonical Memory와 확장 가능한 Domain Ontology로 안전하게 축적하고, 필요한 Context와 Tool을 Agent에게 제공하며, 실행 결과를 평가해 검토 가능한 개선 Proposal을 반복 생성하는 Project Intelligence Runtime이다.**

사용자는 다음만 제공한다.

```text
목표
업무 의도
원문 또는 증거
승인이 필요한 핵심 판단
```

AMPLAI는 다음을 책임진다.

```text
프로젝트와 도메인 판별
Source 보존
지식과 온톨로지 후보 추출
중복·충돌·영향 분석
적절한 저장 구조와 관계 결정
Proposal·Review·Apply
Context 구성과 Agent 실행
평가와 Meta-loop 개선
```

AMPLAI의 세 가지 공학 축은 다음 계층에 대응한다.

```text
Harness Engineering
  Intent Contract + Project Pack + Context Runtime + Tool/Policy Gateway

Loop Engineering
  Knowledge Intake + Agent Harness + Evaluation + Exit Gate + Replay

Meta-loop Engineering
  Failure Pattern + Experiment + Proposal + Review + Promotion/Rollback
```

따라서 개발 순서는 다음 한 줄로 갱신한다.

```text
Minimal Project Identity
→ Knowledge Intake & Steward
→ Full Project Pack
→ Ontology Kernel
→ DC Test Vertical Slice
→ Context Runtime
→ External Agent Harness
→ Development/Management Agents
→ Evaluation Observatory
→ Meta-loop
→ Central/Hybrid Platform
```
