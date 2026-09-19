# 02 · 목표 아키텍처와 책임 경계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 시작 형태: 모듈식 단일 Control Plane + 분리된 Worker

첫 V3 운영 형태는 Python `>=3.11`의 modular monolith Control Plane 한 인스턴스, local-disk transactional store, 별도 sandbox Worker들이다. 원격 Worker가 여러 개여도 동일 서비스 API에 접속하므로 중앙 DB 파일을 공유하지 않는다. 별도 queue broker, graph DB, Redis, Kubernetes는 필수 구성요소가 아니다. 이 구조는 설계 선택이며 처리량 수치는 qualification에서 정한다. [R30]

```text
Human / Hermes / CLI
          │ authenticated request
          ▼
┌──────────── AMPLAI Platform ──────────────────────────────────┐
│ Intake → Scope Resolver → Goal Compiler ↔ Contract Critic     │
│                         │                                    │
│                   Contract Registry                          │
│                         ▼                                    │
│ WorkGraph compiler → Policy admission → Runtime scheduler     │
│                         │                                    │
│ Governance/Authority ← guarded effects → Tool/Secret Broker   │
│ Knowledge/Context   ← snapshot refs → Session/Run stores       │
│ Verifier service → Eval Observatory → Evolution proposals     │
│ Release registry → staged promotion / rollback                │
└─────────────────────────┬────────────────────────────────────┘
                          │ leased ExecutionEnvelope
             ┌────────────┼─────────────────┐
             ▼            ▼                 ▼
        Local Worker   Remote Worker    Managed provider adapter
        Driver+model   Driver+model     qualified/policy allowed
        sandbox        sandbox          evidence export required
             └──────── evidence/receipts ────────────┘
```

## 2. Control Plane의 소유권

`goals`는 의도와 계약 revision, `workgraph`는 immutable work definitions와 graph revision, `runtime`은 scheduling·admission·budget·lease·fencing·상태전이를 소유한다. `sessions`는 provider ID와 무관한 durable chronology를 소유한다. `verification`은 trusted verdict를, `evaluation`은 실험 결과를, `evolution`은 harness 제안과 promotion plan을 소유한다. 기존 `governance`가 인간 승인과 적용 권한의 유일 authority다.

Worker가 “done”이라고 말하는 것은 `candidate_completed` 관측이다. Worker는 Run을 성공으로 확정하거나 Work dependency를 풀거나 canonical release를 바꾸지 못한다. Scheduler는 LLM을 호출해 목표를 발명하지 않는다. Planner를 실행시켜 제안을 받는 것은 가능하지만, Scheduler가 그 제안을 검증 없이 승인하는 것은 불가능하다.

## 3. Platform과 Kit 경계 재정의

기존 Kit에 있던 durable Work 제어 의미론을 Platform runtime으로 승격한다. Kit는 **portable client + capability packs + host entry + repo binding + non-destructive installer**로 경량화한다. 실제 코드를 중복 배포하는 방식 대신 설치된 compatible runtime에 연결한다. 독립 앱에서 offline 작업할 때는 같은 Platform runtime package를 developer host에서 local service mode로 띄운다. 두 번째 의미론의 “경량 supervisor”를 새로 만들지 않는다.

`.ai-team/`은 사용자에게 익숙한 공식 agent configuration 공간으로 보존한다. `.amplai/`는 mutable local state/cache/workspaces다. `.agents/skills/`는 생성된 host-facing surface로 만들고 pack 정본과 양방향 편집하지 않는다. host mirror는 symlink 또는 generated copy이며 원본 digest를 기록한다.

## 4. Core ports와 구현 선택

| Port | 기본 구현 | 금지/제약 |
|---|---|---|
| AuthorityPort | 기존 Governance service adapter | LLM self-approval, client-supplied actor 신뢰 금지 |
| RuntimeStore | local SQLite WAL + transaction/outbox | NFS DB, multi-host file locking 금지 |
| ArtifactStore | content-addressed local files | worker가 승인 결과와 artifact bytes 교체 금지 |
| CanonicalMemoryPort | 기존 Markdown repository + provenance | 검색 index가 canonical을 덮어쓰기 금지 |
| ContextSearchPort | scoped lexical/symbol/typed relations | cross-project 검색 무제한 금지 |
| AgentDriver | CLI/API/qualified ACP adapters | 모델별 if 분기를 scheduler에 넣지 않기 |
| SandboxDriver | disposable workspace/container/approved VM | worktree만으로 security isolation 주장 금지 |
| ToolBroker | typed allowlisted tools + effect ledger | raw secret 반환, unrestricted shell proxy 금지 |
| EvalStore | local tables + artifact refs | audit의 대체로 OTel 사용 금지 |

동일 process 배치는 구현 편의일 뿐 포트를 통한 권한 경계를 생략하는 이유가 아니다. 특히 generated code가 실행되는 process/user와 승인 credential을 가진 service user를 분리한다. [R09, R20, R24]

## 5. Graph 세 가지를 혼동하지 않는다

WorkGraph는 “이 목표를 어떤 독립 작업과 dependency로 수행하나”다. KnowledgeGraph는 “용어·규칙·증거·결정이 무엇과 관련되나”다. HarnessComposition은 driver/packs/policy/verifier/model profile의 버전 조합이다. 세 객체 사이 참조는 가능하지만 하나의 만능 graph schema로 통합하지 않는다.

Macro graph는 한 revision 안에서 DAG다. 재작업은 node의 새 run 또는 새 graph revision이다. inner loop는 cyclic이고, human wait는 이벤트 기반 정지 상태다. “Graph Engine이 없어서 workflow가 부족하다”는 식의 framework 추가 대신 이 계약을 우선한다. [R12–R13]

## 6. 목표 기술 경로

현재 Pydantic/Typer 구조와 엄격한 type/lint 원칙을 살린다. contract JSON Schema가 wire 정본이고, domain model이 같은 규격인지 CI에서 비교한다. Python 모델/JSON/YAML/prompt에 enum과 policy를 각각 수작업 복제하지 않는다. 인프라 변경은 ADR로 trade-off와 회귀시험을 붙인다. 최초 SQLite backend와 향후 PostgreSQL backend는 같은 Store conformance suite를 공유하되, 검증 전 둘 다 지원한다고 선언하지 않는다.

## 7. 확장에 대한 정석

Multi-agent는 여러 bounded worker의 운영이지 agent에게 관리자 권한을 늘리는 일이 아니다. Sandbox·모델 route·tool set·출력 접근권은 parent scope의 부분집합이다. nested delegation depth와 global concurrency/budget을 중앙에서 집계한다. 공급자 자체 subagent 실행을 관측할 수 없다면 strict mode에서 금지하거나 vendor-managed opaque boundary로 구분해 보증 수준을 낮춘다.
