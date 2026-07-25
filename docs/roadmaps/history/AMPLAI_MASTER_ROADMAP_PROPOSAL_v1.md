# AMPLAI Master Roadmap Proposal v1

> 상태: Review Proposal  
> 기준 저장소: `amplai-foundry-main` 0.1.0  
> 목적: 프로젝트 단위의 지식·온톨로지·에이전트 실행·평가·메타루프를 하나의 확장 가능한 AMPLAI 구조로 정렬한다.

---

## 1. 결론

AMPLAI는 단순한 메모리 저장소나 범용 Agent 앱이 아니라 다음 다섯 계층으로 발전해야 한다.

1. **Foundry** — 검토된 지식과 온톨로지를 만들고 변경을 통제한다.
2. **Context Runtime** — 프로젝트에 필요한 지식·온톨로지·사실·도구를 질문별 Context Packet으로 조립한다.
3. **Agent Harness** — 외부 Agent와 내부 Agent의 도구, 권한, Loop, 종료 조건을 통제한다.
4. **Observatory** — 실행 이벤트, 평가, 실패 패턴과 개선 효과를 측정한다.
5. **Meta-loop** — 관찰된 실패를 지식·온톨로지·Skill·Policy·Harness 개선 Proposal로 전환하고 검증한다.

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

- 프로젝트 Manifest와 Project Pack 계약
- 다중 프로젝트 Identity와 Import
- Source 수집·해시·중복 판별
- Memory Candidate, Proposal, Review, Apply
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

### 3.1 Namespace가 있지만 실제 Identity는 전역 ID로 동작한다

현재 `MarkdownMemoryRepository`는 `memory.id`만 key로 사용한다. 따라서 서로 다른 프로젝트가 각각 `CON-0001`을 갖는 정상적인 상황을 지원할 수 없다.

```text
현재
get("CON-0001")

목표
get(MemoryRef(namespace="org/pinesky/project/cortex", id="CON-0001"))
```

다중 프로젝트 전에 `(namespace, id)`를 Canonical Identity로 확정해야 한다.

### 3.2 Memory Relation과 Ontology Relation을 섞으면 안 된다

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

### 3.3 모든 것을 MemoryObject로 확장하면 안 된다

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

---

## 4. 목표 아키텍처

```text
                         ┌─────────────────────────────┐
                         │ Human / External Agents     │
                         │ Claude / Codex / GLM / UI   │
                         └──────────────┬──────────────┘
                                        │
                              Tool/API/MCP Gateway
                                        │
┌───────────────────────────────────────▼───────────────────────────────────────┐
│                              AMPLAI Agent Harness                            │
│ Role · Tool Policy · Permission · Loop · Stop Gate · Approval               │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │ Context Request
┌───────────────────────────────▼───────────────────────────────────────────────┐
│                              Context Runtime                                 │
│ Scope Resolution · Retrieval · Ontology Slice · Token Budget · Evidence     │
└──────────────┬──────────────────┬────────────────────┬────────────────────────┘
               │                  │                    │
      Canonical Memory      Ontology/Profile      Project Facts
       Markdown/Git       Turtle/SHACL/Registry   Mapping/Graph View
               │                  │                    │
               └──────────────────┴─────────┬──────────┘
                                           │
┌──────────────────────────────────────────▼────────────────────────────────────┐
│                                 Foundry                                      │
│ Source → Candidate → Proposal → Review → Apply → Canonical Assets            │
└──────────────────────────────────────────┬────────────────────────────────────┘
                                           │
                               Change / Execution Evidence
                                           │
┌──────────────────────────────────────────▼────────────────────────────────────┐
│                               Observatory                                    │
│ Run Events · Tool Calls · Evaluations · Failure Taxonomy · Replay            │
└──────────────────────────────────────────┬────────────────────────────────────┘
                                           │
┌──────────────────────────────────────────▼────────────────────────────────────┐
│                                Meta-loop                                     │
│ Pattern Detection → Hypothesis → Experiment → Proposal → Review → Promotion  │
└───────────────────────────────────────────────────────────────────────────────┘
```

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

### 8.1 공통 Agent 역할

| 역할 | 책임 |
|---|---|
| Client Partner | 사용자의 자연어 요구를 내부 목표와 작업 계약으로 변환 |
| Router | 프로젝트·도메인·도구·모델 선택 |
| Curator | Source에서 Candidate와 중복·충돌 후보 생성 |
| Planner | 목표, 제약, 성공 기준과 작업 순서 수립 |
| Implementer | 코드·설정·문서 변경 수행 |
| Reviewer | 결과, 테스트, 근거와 위험 검토 |
| Governor | 권한·정책·승인·배포 Gate 판단 |
| Evaluator | 결과를 결정론적·모델 기반 평가로 점수화 |
| Meta-loop Analyst | 반복 실패와 개선 기회를 Proposal로 전환 |

초기에는 이 역할을 모두 독립 Agent로 만들지 않는다. 역할 계약을 먼저 정의하고 한 개 External Agent가 여러 역할을 순차 수행하도록 시작한다.

### 8.2 개발 Agent Vertical Slice

```text
Issue 또는 자연어 요구
→ Project Context 구성
→ 관련 Decision·Architecture·Ontology 조회
→ 구현 계획
→ Code 변경
→ Test/Lint/Build
→ Reviewer 평가
→ 필요 시 Knowledge Candidate 생성
→ Governor 승인 대상 분리
```

### 8.3 관리 Agent Vertical Slice

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

AMPLAI의 자기 발전은 **무제한 자기 수정**이 아니라 다음의 통제된 개선 Loop다.

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

# 10. 우선순위 로드맵

## Phase 0 — Foundry Baseline 고정

**상태:** 현재 구현됨, 보강 필요

**목표**
- 현재 계약을 회귀 기준으로 고정한다.
- 기존 26개 note와 lint를 첫 Canonical Corpus로 유지한다.

**구현 항목**
- CI에서 pytest, Ruff, mypy, vault lint 실행
- 현재 `docs/ROADMAP.md`를 이 Proposal로 바로 덮어쓰지 않고 review 후 supersede
- Version과 migration policy 문서화

**완료 Gate**
- 깨끗한 clone에서 한 명령으로 모든 검증 통과
- 현재 Memory Contract의 Golden Fixture 고정

---

## Phase 1 — Project Pack, Identity, Multi-project Local Workspace

**가장 먼저 구현할 단계**

**목표**
- 프로젝트를 독립적으로 이동·복제·검증할 수 있는 단위로 만든다.
- Domain과 Project의 namespace 및 import 경계를 먼저 고정한다.

**핵심 산출물**
- `ProjectManifest`
- `MemoryRef(namespace, id)`
- `ProjectPackRepository`
- Local Workspace Resolver
- `domain.lock.yaml`
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

**완료 Gate**
- AMPLAI와 Cortex 두 Project Pack을 같은 Workspace에서 동시에 lint·조회
- Runtime/Index를 삭제해도 Canonical Pack만으로 재구성

---

## Phase 2 — Source Ingestion, Candidate, Proposal, Review/Apply

**목표**
- Agent가 지식을 발견하되 공식 지식을 직접 수정하지 못하게 한다.

**핵심 모델**

```text
SourceRecord
MemoryCandidate
ChangeProposal
ReviewDecision
ApplyPlan
ChangeSet
```

**구현 항목**
- Source SHA-256, origin, collected_at, classification
- 중복 Source 탐지
- Candidate 중복·충돌 탐지
- Proposal diff와 영향 대상
- 승인·거절·수정 요청
- optimistic revision check
- 승인된 Apply만 Canonical Repository 변경

**완료 Gate**
- Source 한 건에서 Candidate를 만들고, Proposal 검토 후 Canonical Note로 반영
- 승인 없이 Canonical File이 바뀌는 경로가 없음
- Supersede 변경이 양쪽 Note에 원자적으로 적용

---

## Phase 3 — Ontology Kernel과 Domain Registry

**목표**
- 확장 가능한 Domain Package의 공통 계약을 구현한다.

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

---

## Phase 4 — DC Test E2E Ontology Vertical Slice

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

## Phase 5 — Derived Search와 Context Runtime

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

## Phase 6 — External Agent Gateway와 Harness Contract

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
- Tool 호출과 근거를 재현 가능하게 기록
- 금지된 write/action이 Policy Gate에서 차단

---

## Phase 7 — Development Agent Vertical Slice

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
→ Knowledge Candidate 제안
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
- 작업 종료 후 지식 Proposal은 만들지만 자동 반영하지 않음

---

## Phase 8 — Management Agent Vertical Slice

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

## Phase 9 — Run/Event Store와 Evaluation Observatory

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
- Ontology competency questions
- Task correctness
- Code test/build quality
- Policy compliance
- Citation/provenance completeness
- Latency/cost/token
- Human acceptance and correction

**완료 Gate**
- 모든 Agent Run에 결과, Tool Trace, 평가와 실패 분류 존재
- 과거 Run을 새 Harness/Prompt로 Replay 가능
- Project별 Scorecard와 회귀 기준선 존재

---

## Phase 10 — Proposal-driven Meta-loop

**목표**
- AMPLAI가 자신의 실패를 관찰하고 개선안을 검증해 제안한다.

**첫 Meta-loop Use Case**

```text
반복되는 Context 누락 실패 탐지
→ 원인: Ontology relation 또는 retrieval rule 누락
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

## Phase 11 — Central Memory Server, Multi-user, Hybrid Security

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

1. 전체 반도체 산업 온톨로지를 먼저 만드는 것
2. 모든 Note와 로그를 하나의 거대 Graph DB에 넣는 것
3. Vector DB를 기본 검색으로 도입하는 것
4. 대화 전체를 자동으로 장기 Memory로 저장하는 것
5. Agent가 Canonical Memory나 Ontology를 직접 수정하게 하는 것
6. 자체 범용 Agent Runtime을 External Agent 연동보다 먼저 만드는 것
7. 중앙 서버와 권한 시스템을 Local Project Pack보다 먼저 만드는 것
8. 모든 역할을 독립 Multi-Agent로 한꺼번에 분리하는 것
9. Raw 측정 데이터 전체를 Knowledge Graph에 복제하는 것
10. Meta-loop를 자동 배포 시스템으로 오해하는 것

---

## 12. 가장 먼저 착수할 설계 패키지

다음 설계·구현 대상은 **Phase 1: Project Pack v1**이다.

### 제안할 Canonical Knowledge

```text
CON — Project Pack은 프로젝트 지식과 실행 계약의 이동 단위다
PRI — Canonical과 Derived Runtime State를 분리한다
PRI — Domain은 import하고 Project는 profile로 확장한다
DEC — Memory Identity는 namespace + local ID를 사용한다
DEC — Project-local `.amplai/`를 첫 Pack layout으로 사용한다
ARC — Workspace, Project Pack, Domain Registry의 경계
QUE — 공유 Domain Package의 배포 방식은 무엇인가
EXP — AMPLAI와 Cortex 두 Pack을 같은 Workspace에서 로딩한다
```

### 첫 구현 순서

1. `ProjectManifest`, `DomainImport`, `MemoryRef` Pydantic model
2. `MemoryRepository`를 qualified reference 기반으로 변경
3. `WorkspaceRepository`와 Project Resolver 추가
4. `.amplai/project.yaml` schema와 linter 추가
5. 기존 `vault/projects/amplai`를 Project Pack fixture로 migration
6. Cortex 빈 Project Pack fixture 추가
7. multi-project duplicate local ID test
8. import cycle, broken import, namespace leak test
9. `amplai project validate/list/show` CLI
10. 기존 CLI backward compatibility 결정

### 첫 단계가 끝난 후에야 Ontology 설계를 시작해야 하는 이유

- Ontology Module이 어떤 Project와 Scope에 속하는지 먼저 정해야 한다.
- ID와 Import 규칙이 없으면 Domain이 늘어날수록 migration 비용이 커진다.
- Project-specific 지식과 공유 Domain 지식의 승격 경계가 선행돼야 한다.
- Meta-loop의 Proposal 대상과 영향 범위를 Project/Domain 단위로 계산할 수 있어야 한다.

---

## 13. 최종 제품 정의

AMPLAI의 목표 상태는 다음 문장으로 정의한다.

> **AMPLAI는 프로젝트별 Canonical Memory와 확장 가능한 Domain Ontology를 기반으로, 필요한 Context와 Tool을 Agent에게 제공하고, 실행 결과를 평가하여 검토 가능한 개선 Proposal을 반복 생성하는 Project Intelligence Runtime이다.**

그리고 AMPLAI의 세 가지 공학 축은 다음 계층에 대응한다.

```text
Harness Engineering
  Project Pack + Context Runtime + Tool/Policy Gateway

Loop Engineering
  Agent Harness + Evaluation + Exit Gate + Replay

Meta-loop Engineering
  Failure Pattern + Experiment + Proposal + Review + Promotion/Rollback
```

따라서 개발 순서는 다음 한 줄로 고정한다.

```text
Project Boundary
→ Governed Knowledge Change
→ Ontology Kernel
→ DC Test Vertical Slice
→ Context Runtime
→ External Agent Harness
→ Development/Management Agents
→ Evaluation Observatory
→ Meta-loop
→ Central/Hybrid Platform
```
