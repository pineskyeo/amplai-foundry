# AMPLAI V3 Full Design — 읽기용 통합본

2026-09-15 · 설계만 / 구현 아님. 정본은 design/*와 contracts/*다. 이 통합본은 자동 생성된 투영이며 독립 편집하지 않는다.

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


---

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


---

# 03. 불변조건 정본

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님

정본은 `contracts/invariant-registry.json`이며 이 문서는 그 읽기용 투영이다. 아래 안전 불변조건은 waiver 불가다. 기능 acceptance의 제한적 waiver와 혼동하지 않는다.

## INV-01 · Scope isolation

모든 조회·명령·artifact·event·ref는 authenticated tenant/project 범위에 속해야 한다.

**Owner:** AuthorityService · **강제:** API/repository/broker

**실패:** `SCOPE_DENIED` · **증거:** cross-scope request와 내부 FK/lookup 결과



## INV-02 · No authority from language

LLM·문서·tool output·role 이름은 승인·권한을 발급하지 않는다.

**Owner:** AuthorityService · **강제:** grant issuance/effect admission

**실패:** `UNAUTHORIZED_AUTHORITY` · **증거:** actor binding와 issuer audit



## INV-03 · Immutable definitions

사용 중 contract/graph/context/pack/release 내용은 덮어쓰지 않고 새 revision을 만든다.

**Owner:** DefinitionStore · **강제:** create/update/activate

**실패:** `IMMUTABLE_OBJECT` · **증거:** old/new digest와 revision event



## INV-04 · Verified target

hint 생략은 허용해도 registry 기반 target 확정 전 write 실행은 금지한다.

**Owner:** GoalService · **강제:** resolution/admission

**실패:** `AMBIGUOUS_TARGET` · **증거:** resolution facts와 registry refs



## INV-05 · Acceptance binding

mandatory acceptance마다 고정된 verifier·evidence·success rule이 있어야 한다.

**Owner:** ContractCompiler · **강제:** contract freeze/verify

**실패:** `ACCEPTANCE_UNBOUND` · **증거:** criterion→verification binding



## INV-06 · Design is not implementation

mode=design은 설계 artifact만 생성하며 제품 구현·배포 effect를 dispatch하지 않는다.

**Owner:** RuntimeService · **강제:** strategy/capability/effect

**실패:** `DESIGN_SCOPE_VIOLATION` · **증거:** effective capabilities와 produced artifacts



## INV-07 · Graph correctness

WorkGraph는 acyclic·scope-safe·typed·coverage complete이며 unsafe any-success join을 금지한다.

**Owner:** GraphCompiler · **강제:** compile/activate/replan

**실패:** `GRAPH_INVALID` · **증거:** deterministic semantic validation report



## INV-08 · Policy intersection

유효 capability는 요청·사용자·정책·sandbox·qualified driver의 교집합이며 deny 우선이다.

**Owner:** PolicyService · **강제:** admission/dispatch

**실패:** `CAPABILITY_DENIED` · **증거:** evaluated policy snapshot와 effective subset



## INV-09 · Revision-bound approval

승인/grant는 대상·action·revision/digest·expiry·generation에 bind하며 변경/취소 후 재사용하지 않는다.

**Owner:** AuthorityService · **강제:** admission/pre-effect/promote

**실패:** `GRANT_STALE` · **증거:** decision/grant/consume/revocation receipts



## INV-10 · Fenced lease

현재 lease/epoch/fencing 없는 worker 결과·heartbeat·effect는 거부한다.

**Owner:** RuntimeService · **강제:** worker ingress/broker

**실패:** `STALE_LEASE` · **증거:** lease row/version와 accepted event



## INV-11 · Bounded root budget

모든 child/native delegation/repair/eval usage는 root 예약에 계상하며 unknown을 0으로 취급하지 않는다.

**Owner:** BudgetService · **강제:** admission/continuation/usage close

**실패:** `BUDGET_EXHAUSTED` · **증거:** reservation journal와 usage reconciliation



## INV-12 · No blind effect retry

외부 effect timeout은 미적용 증거가 아니다. UNKNOWN은 reconcile 전에 재실행하지 않는다.

**Owner:** EffectService · **강제:** dispatch/retry/recovery

**실패:** `UNKNOWN_EFFECT` · **증거:** effect receipts와 reconciliation evidence



## INV-13 · Transactional state events

aggregate update·version·event·outbox·idempotency 응답은 같은 로컬 transaction으로 일관된다.

**Owner:** RuntimeStore · **강제:** command commit

**실패:** `STATE_INTEGRITY_ERROR` · **증거:** fault injection snapshots와 event sequences



## INV-14 · Trusted artifact verdict

실제 artifact digest·환경·검증기·현재 계약에 bind한 trusted verdict만 성공에 사용한다.

**Owner:** VerifierService · **강제:** evidence intake/global verify

**실패:** `UNTRUSTED_VERDICT` · **증거:** artifact recompute와 verifier attestation



## INV-15 · No evaluator gaming

builder/meta proposer는 자신을 평가할 보호된 기준·golden·holdout을 동시에 바꿔 통과할 수 없다.

**Owner:** EvaluationService · **강제:** diff guard/experiment/promote

**실패:** `EVAL_TAMPERING` · **증거:** protected corpus diff와 independent review



## INV-16 · Governing context complete

실행 context는 현재 권한 경계·active invariants/decisions·contract를 포함하며 모순/누락을 숨기지 않는다.

**Owner:** ContextService · **강제:** assembly/resume/pre-effect

**실패:** `CONTEXT_NOT_READY` · **증거:** governing set manifest와 freshness report



## INV-17 · Domain-kit separation

앱 domain/build/production 동작은 Kit/host/tool runtime import에 의존하지 않는다.

**Owner:** ArchitectureVerifier · **강제:** build/release/uninstall

**실패:** `DOMAIN_COUPLING` · **증거:** import graph와 kit-absent build



## INV-18 · Protected canonical governance

발견·요약·meta proposal은 기존 Source→Proposal→Decision→Apply 경로 밖에서 canonical을 쓰지 않는다.

**Owner:** FoundryGovernance · **강제:** knowledge publish/migration

**실패:** `CANONICAL_WRITE_DENIED` · **증거:** governed publish receipts



## INV-19 · Cancellation truth

취소 ACK·provider 접수는 실제 도구 종료/외부 rollback 완료를 의미하지 않는다.

**Owner:** RuntimeService · **강제:** cancel/status/recovery

**실패:** `CANCELLATION_UNRESOLVED` · **증거:** child process/effect reconciliation



## INV-20 · Data containment

허용된 data classification/egress/secret scope 밖으로 모델·도구가 데이터를 전송하지 않는다.

**Owner:** SandboxBroker · **강제:** context/provider/tool ingress

**실패:** `DATA_POLICY_DENIED` · **증거:** redaction/egress/secret broker audit



## INV-21 · Qualified capabilities only

문서 주장·probe·검증·Run별 권한은 별도이며 미검증/unsupported 기능은 안전한 대체 또는 HOLD다.

**Owner:** DriverRegistry · **강제:** selection/probe/release

**실패:** `CAPABILITY_UNQUALIFIED` · **증거:** versioned conformance report



## INV-22 · Safe meta evolution

meta 변경은 freeze된 독립 eval과 승인된 promote/rollback을 거치며 자체 승인·보호정책 약화는 불가다.

**Owner:** EvolutionService · **강제:** proposal/eval/promote

**실패:** `META_PROMOTION_DENIED` · **증거:** experiment/approval/release chain



## INV-23 · Scoped idempotency

같은 scope/key+같은 payload는 같은 결과, 다른 payload는 conflict이며 중복 effect를 생성하지 않는다.

**Owner:** CommandService · **강제:** API/inbox/outbox/effect

**실패:** `IDEMPOTENCY_CONFLICT` · **증거:** dedupe records와 original response



## INV-24 · Non-destructive migration

삭제/덮어쓰기는 owned path+expected digest+backup+live-ref 검사+승인+검증 뒤에만 가능하다.

**Owner:** MigrationService · **강제:** install/archive/delete

**실패:** `MIGRATION_CONFLICT` · **증거:** dry-run/action/restore receipts



## INV-25 · Preserve evidence meaning

imported legacy evidence·waiver·inconclusive·not_run을 trusted pass로 재명명하지 않는다.

**Owner:** EvidenceService · **강제:** migration/report/verify

**실패:** `EVIDENCE_CLASSIFICATION_ERROR` · **증거:** provenance와 outcome counts



## INV-26 · Local SQLite boundary

live SQLite WAL은 로컬 filesystem/단일 활성 CP에서만 쓰며 cross-DB/remote effect atomicity를 주장하지 않는다.

**Owner:** StorageService · **강제:** startup/config/transaction

**실패:** `UNSUPPORTED_STORAGE_TOPOLOGY` · **증거:** topology validation와 ownership lock



## INV-27 · Pinned supply chain

pack/driver/model config/release는 exact digest·version·qualification으로 고정하고 latest 자동변경을 금지한다.

**Owner:** ReleaseService · **강제:** install/dispatch/promote

**실패:** `RELEASE_INTEGRITY_ERROR` · **증거:** release set/signature/provenance



## INV-28 · Fail closed on uncertainty

필수 authority·state·ref·evidence 검증이 불확정이면 HOLD/INCONCLUSIVE이며 성공으로 추정하지 않는다.

**Owner:** GateEngine · **강제:** all mandatory gates

**실패:** `REQUIRED_GATE_UNKNOWN` · **증거:** typed gate results와 hold reason


---

# 04. Gate Matrix

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님

정본은 `contracts/gate-matrix.json`.

| Gate | 시점 / Owner | 필요한 증거 | 관련 불변조건 |
|---|---|---|---|
| G-01 Authenticated scope | 모든 command/worker/API ingress / AuthorityService | 검증된 ActorContext와 scope; idempotency payload binding | INV-01, INV-02, INV-23 |
| G-02 Intent and target resolution | resolution 완료 및 execution admission / GoalService | registry target, data class, source-backed facts | INV-04, INV-20 |
| G-03 Goal contract and critic | contract freeze/activate / ContractCompiler | acceptance binding, non-goals, protected constraints, critic findings closure | INV-03, INV-05, INV-06 |
| G-04 Knowledge readiness | context compile/resume / ContextService | 8 readiness 항목, mandatory governing refs, conflict resolution | INV-16, INV-18 |
| G-05 WorkGraph compile | graph create/activate/replan / GraphCompiler | cycle/type/ref/coverage/resource checks | INV-03, INV-07 |
| G-06 Authority and capability admission | worker dispatch/steering activation / PolicyService | current grant, policy intersection, qualified environment | INV-08, INV-09, INV-20, INV-21 |
| G-07 Environment and release integrity | startup/dispatch/install / ReleaseService | pinned release, driver/sandbox probes, workspace fingerprint | INV-17, INV-21, INV-26, INV-27 |
| G-08 Root budget and resources | claim/child spawn/continuation / BudgetService | atomic reservation, lease, write resource exclusivity | INV-10, INV-11 |
| G-09 Lease and freshness | worker event/heartbeat/resume / RuntimeService | exact revision+epoch+lease; governing context unchanged | INV-03, INV-09, INV-10, INV-16 |
| G-10 Tool effect preflight | broker dispatch 및 재전송 / EffectService | grant consume, effect key binding, egress/args policy | INV-08, INV-09, INV-10, INV-12, INV-20, INV-23 |
| G-11 Evidence admission | artifact/evidence registry commit / EvidenceService | 실제 bytes hash, provenance, verifier identity, size/media/secret checks | INV-14, INV-15, INV-25 |
| G-12 Work verification | work verify/repair decision / VerifierService | mandatory node acceptance, frozen regression, exact artifact | INV-05, INV-14, INV-15, INV-25 |
| G-13 Global convergence | goal verified 전 / GoalVerifier | integration/doc freshness/no pending effects/questions + coverage | INV-05, INV-14, INV-16, INV-19, INV-25 |
| G-14 Canonical publish and human authority | knowledge/apply/release external action / FoundryGovernance | Decision/ApplyGrant와 exact subject digest | INV-02, INV-09, INV-18 |
| G-15 Pack install and app separation | pack/kit install update uninstall / Installer | owned path/hash/deps/trust/kit-absent build | INV-17, INV-24, INV-27 |
| G-16 Experiment protection and freeze | experiment approved/start / EvaluationService | baseline/candidate/corpus/verifier/budget/analysis/holdout 고정 | INV-15, INV-22, INV-25 |
| G-17 Offline eval decision | canary 후보 승인 전 / EvaluationService | valid metrics, safety tests, uncertainty, independent evaluation | INV-14, INV-15, INV-22, INV-25 |
| G-18 Canary admission and abort | canary start/iteration / EvolutionService | opt-in eligible class+budget+stop rules+fallback | INV-08, INV-09, INV-11, INV-12, INV-22 |
| G-19 Promotion and rollback authority | active release pointer CAS / ReleaseService | exact grant+eval report+expected active+rollback compatibility | INV-03, INV-09, INV-22, INV-27 |
| G-20 Migration and deletion | dry-run/apply/delete/archive / MigrationService | baseline/hash/owner/refs/backup/approval/restore tests | INV-09, INV-18, INV-24, INV-25 |
| G-21 Recovery and reconciliation | crash/startup/cancel resume / RecoveryService | new epoch, inbox/outbox idempotency, external outcome classified | INV-10, INV-12, INV-13, INV-19, INV-23, INV-26 |
| G-22 Protected surface change | policy/verifier/authority diff review / Governor | independent review + 기존 validator + separate benchmark qualification | INV-02, INV-15, INV-18, INV-22 |
| G-23 Protocol compatibility | handshake/adapter upgrade/event ingestion / ProtocolService | supported major/schema/versioned provider decoder | INV-01, INV-21, INV-23, INV-27 |
| G-24 Final release acceptance | V3 release enable / ReleaseService | requirements→tests→evidence coverage; fault/security/migration/meta rollback evidence | INV-14, INV-17, INV-22, INV-24, INV-25, INV-27, INV-28 |

모든 strategy/risk에서 해당 생명주기 boundary에 도달하면 gate가 적용된다. tiny work가 meta experiment gate를 실행한다는 뜻은 아니다. 어떤 boundary를 실행하면서 그 gate를 tiny/fast라는 이유로 생략할 수 없다는 뜻이다. unknown 결과는 hold다. N/A는 정해진 applicability rule과 reason을 기록한다.


---

# 05 · Intent → Goal Contract 상세 설계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 입력 계약

`IntentEnvelope`는 raw user text, actor/session channel reference, tenant/project hints, attachment refs, requested mode(`design|work`), received_at, locale를 담는다. tenant/actor는 서버 인증에서 결정하며 payload의 힌트로 덮어쓰지 않는다. 사용자가 repo·폴더·artifact ID를 모두 지정할 필요가 없다. 단, 자료로 resolve할 수 없는 중요한 의미를 모델이 지어내서는 안 된다.

Goal Resolver는 다음 순서로 작업한다. 동일 사용자에게 이미 확인된 질문을 반복하지 않도록 question ledger를 조회한다.

1. **Scope resolution**: authorized project registry → 명시적 project binding → app registry/현재 repo → 최근 승인된 관련 Decision → 내용 기반 후보. 각 후보마다 evidence와 exclusion reason을 기록한다.
2. **Fact discovery**: 변경될 behavior, 현재 tests, active invariants, public boundaries, historical incidents, available verifier를 읽는다. raw source는 untrusted evidence로 분리한다.
3. **Intent interpretation**: `explicit_user`, `repository_fact`, `approved_decision`, `proposed_assumption` 출처를 field 단위로 붙인다. “빠르게”, “안정적으로”, “예쁘게”를 관찰 가능한 결과로 나눈다.
4. **Goal Contract draft**: 목적, scope/exclusions, non-goals, acceptance, constraints, unknowns, authority, budget, evidence requirement, affected app refs를 작성한다.
5. **Contract Critic**: 범위 과잉·모호한 acceptance·검증 수단 부재·금지조건 누락·자기채점 유도·승인 필요 여부를 검토한다.
6. **Deterministic validation**: schema, references, authorization, completeness, conflicting governing facts, available verifier binding을 검사한다.
7. `ready`, `awaiting_decision`, `discovering`, `blocked` 중 하나로 귀결한다. implementation dispatch는 ready 이후만 가능하다.

## 2. 자동으로 정해도 되는 것과 질문해야 하는 것

명시 binding과 authorized app registry가 일치하면 앱 선택을 자동 resolve할 수 있다. 문서 링크·ID·기본 파일 위치·test command 발견은 질문하지 않는다. ambiguous app 두 개가 같은 수준으로 지지되면 후보·근거·영향을 보여 주고 물어본다. 모델 confidence 0.9 같은 숫자는 권한 증거가 아니다.

업무 범위 확대, public API break, production 사용, canonical ontology 의미 변화, 데이터 삭제, 미측정 성능 목표의 임의 수치화는 질문/승인 대상이다. “reference-state를 완성해줘”라는 요청으로 실제 장비 측정을 실행할 수는 없다. 대신 현재 미완료 contract와 tests를 조사하고 누락 목표 초안을 제안한다.

질문은 가능한 한 한 번에 묶는다. `question_id, decision_type, choices, recommended_choice, consequence, blocks_node_ids, expires_at`를 기록한다. 답이 필요 없는 read-only discovery는 계속 가능하나 승인 기다리는 효과 실행은 불가능하다. 기본 timeout은 자동 동의가 아니라 HOLD다.

## 3. Contract의 규범 필드

`goal-contract.schema.json`이 field 정본이다. acceptance 항목은 고유 ID, 의미, `facet(functional|safety|performance|visual|ux|documentation)`, 검증 방식, verifier refs, evidence types, 중요도, 관찰 대상과 필요 시 threshold를 가진다. 모든 mandatory acceptance가 최소 하나의 실행 가능한 verifier 또는 명시적 human review에 연결되어야 한다. semantic scorer만으로 safety acceptance를 충족시키지 않는다.

Budget은 비용·시간·도구호출·모델호출·iteration·parallelism의 상한을 각각 가진다. `unknown usage`를 0으로 계산하지 않는다. Contract에는 설계 default를 찍되, 실제 실행은 사용자가/정책이 허용한 예산의 교집합을 적용한다.

Immutable definition과 mutable lifecycle을 분리한다. `contract_id/revision`은 정본 식별이며 content digest는 전체 canonical definition을 대상으로 계산한다. 서명·승인·상태는 별도 records다. contract 안의 문자열을 바꾸고 같은 revision/digest를 유지할 수 없다.

## 4. Acceptance를 만드는 절차

먼저 현재 행동을 재현할 test 또는 관측 근거를 찾는다. 실패 사례가 있으면 해당 defect를 기준으로 `Given/When/Then`을 작성한다. 아직 unknown이면 discovery acceptance를 먼저 둔다. 존재하지 않는 성능 baseline과 “0 bug”를 만들어 넣지 않는다.

예: “문서가 깨지지 않게”는 (a) 지정 renderer에서 SVG/font load 실패 없음, (b) 페이지 밖 요소·겹침 없음, (c) 원문 section coverage 충족, (d) 지정 예제의 사용자 검토 승인으로 나눈다. 예: “안정적 runtime”은 crash replay/no duplicate publish/stale worker fence/approval revoke 거부처럼 측정 가능한 시나리오로 바꾼다.

Planner가 기능 목록을 과도하게 확대하면 critic은 `SCOPE_EXPANSION`을 반환한다. 범위 추가는 contract revision+필요한 승인으로만 가능하다. Planner/evaluator 협상은 제품 실험에서 유효했던 방법이지만 무한 논쟁이나 완벽성 보증으로 채택하지 않는다. AMPLAI 기본 협상 예산은 최대 2 round다. 해결 안 되면 사람에게 남은 차이를 보여 준다. [R08]

## 5. Goal과 Work의 관계

Goal Contract는 결과의 정의다. WorkGraph는 실행 제안이다. 한 Goal에 여러 graph revision이 있을 수 있다. Graph가 바뀌어도 Goal acceptance를 조용히 줄이지 않는다. Goal이 너무 크면 새 lower-level Work contracts를 생성하되 parent acceptance coverage 표를 만든다. 로컬 task 성공이 전체 Goal 성공을 보장하지 않으므로 마지막 integration/global verification은 별도로 둔다.

`design` mode의 결과는 설계 artifact와 review evidence다. 실행 계획에 구현 node를 기록할 수는 있지만 지금 그 node를 dispatch하면 안 된다. `work` mode로의 전환은 새 사용자 요청/승인과 admission을 요구한다.

## 6. 실패와 재진입

Source 접근 불가 → `DISCOVERY_SOURCE_UNAVAILABLE`; 권한 미확정 → `SCOPE_UNRESOLVED`; 결정 충돌 → `KNOWLEDGE_CONFLICT`; verifier 없음 → `ACCEPTANCE_UNVERIFIABLE`; budget 불가 → `BUDGET_UNAUTHORIZED`. 해결되면 과거 초안을 삭제하지 않고 새 revision을 만든다. resume 시 governing source hash/authorization epoch/working tree가 바뀌었는지 재확인한다.

## 7. 인수 조건

애매한 요청을 5개 정도의 무관한 기능으로 불리는 planner는 실패다. 같은 이름의 Cortex app이 두 namespace에 있어도 scope가 섞이면 실패다. 테스트 파일을 지워 PASS를 만드는 acceptance 설계는 실패다. mandatory criterion 미결합·충돌 자료 생략·미승인 가정 적용·design mode 구현 dispatch를 모두 negative fixture로 막는다.


---

# 06 · WorkGraph IR, compiler, 재계획

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 그래프 정의

WorkGraph는 Goal Contract의 immutable execution IR다. `graph_id/revision, contract_ref, nodes, join_policy, created_from, replan_reason, compiler_version`을 가진다. 각 node는 목적, target app, strategy, `depends_on`, typed `consumes/produces`, acceptance IDs, verifier profile, resource claims, required capabilities, effects category를 가진다. node ID는 graph 내 유일하고 revision 변경에도 의미가 동일한 node만 ID를 재사용한다.

`depends_on`만 execution ordering을 만든다. `blocks`라는 역방향 중복 edge는 저장하지 않고 projection으로 계산한다. KnowledgeGraph 관계를 runtime dependency로 자동 복사하지 않는다. 모든 consumed artifact는 외부 immutable input ref이거나 dependency closure에 있는 producer의 output slot을 가리켜야 한다.

## 2. Compiler 절차와 deterministic validation

LLM은 GraphDraft만 만든다. compiler는 JSON schema 확인 후 node ID·scope·contract digest·pack/verifier existence·input/output type compatibility·artifact producer uniqueness·acceptance coverage·acyclicity·capability/budget feasibility를 검증한다. Kahn/topological ordering은 node ID로 stable tie-break한다. compiler가 semantic plan을 발명하지 않는다. 부적합 계획은 구조화 오류로 planner에게 되돌린다.

모든 edge는 `reason`이 있어야 한다. “항상 design 다음 code” 같은 의식적 순서가 아니라 실제 산출물 dependency 또는 안전 gate가 있는지를 설명한다. 독립 작업만 병렬화한다. 같은 파일을 수정하는 node는 `repo-write:<repo_ref>` 또는 더 작은 검증된 exclusive resource claim을 공유하므로 한 번에 실행하지 못한다.

## 3. Loop와 graph의 경계

`direct`는 한 번 구현 후 verifier 실행이다. `bounded_loop`는 동일 Work의 구현·검증·repair를 예산 안에서 반복한다. `deliberative`는 불확실성이 큰 경우 planner/builder/evaluator 역할을 분리한다. cross-app은 새로운 strategy 이름이 아니라 여러 node의 graph다. Discovery는 read-only node다. 반복 횟수를 node 수로 펼쳐 무한 graph를 만들지 않는다.

WorkGraph를 DAG로 제한하는 것은 종료·검증·운영 단순화를 위한 선택이다. 일반적인 graph/agent runtime은 cycle을 포함한다. [R12] 외부 cross-change dependency는 V3에서 typed artifact/import reference로 명시하고 project permission과 revision pin을 요구한다. 무제한 Work 간 숨은 wait cycle은 금지한다.

## 4. Join과 global verification

기본 join은 `all_required`. 필수 node가 실패/취소/unknown effect면 Goal은 성공할 수 없다. 선택 node는 contract에서 optional로 선언되어 있어야 하고, skip 사유를 남긴다. `any_success` fan-out는 read-only 대안 탐색에만 허용한다. 가장 빨리 성공한 구현을 production에 적용하는 경쟁 실행은 금지한다.

모든 각 앱 unit test가 성공해도 cross-app interface integration node가 통과하지 않으면 verified가 아니다. integration node는 producer artifact digest·contract digest·환경 fingerprint를 pin한다. `DONE`은 supplier message가 아니라 trusted accepted verdict를 기반으로 한다.

## 5. Replan: 새 revision 생성과 activation barrier

재계획 trigger는 새로운 governing fact, contract steering, 반복 실패, resource 불가, 의존 산출물 invalidation이다. Planner는 기존 graph base digest를 참조한 candidate를 제출한다. 기존 graph를 in-place 편집하지 않는다.

Activation transaction은 다음을 수행한다. (a) current graph revision이 expected와 일치, (b) 새 graph validation PASS, (c) 필요한 승인 valid, (d) 변경 node와 downstream closure 계산, (e) 해당 old dispatch 중지·lease epoch 증가, (f) new active revision 교체, (g) 이벤트/outbox append. 이 transaction 밖의 외부 effect는 effect ledger와 reconciliation 대상이다.

Old running worker는 즉시 새 revision에 소속되지 않는다. 허용된 read-only compute를 종료할 수 있으나 old artifact는 quarantine/stale로 귀속한다. reuse 가능성을 중앙이 별도로 판정한다. 강한 새로운 금지조건이면 ToolBroker grant를 즉시 revoke하고 sandbox cancel을 요청한다.

## 6. 결과 재사용 규칙

재사용하려면 node definition digest, transitive input artifact hashes, contract acceptance 관련 subset digest, verifier version, policy/authorization relevant epoch, environment fingerprint, knowledge pin이 일치해야 한다. 모두 일치해도 deploy/publish receipt는 compute 결과처럼 reuse하지 않는다. 호환성을 추정하는 LLM 문장만으로 evidence를 승격하지 않는다.

## 7. Resource와 bounded fan-out

기본 최대 node 64, per-Goal active worker 4, delegation depth 2는 initial design defaults이며 `policy/defaults.json`에서 관리한다. 모델 지능 점수에 따라 무제한 늘리지 않는다. fan-out는 parent reservation에서 child budget을 차감한다. 여러 child가 독립 reserve를 만들어 총량을 초과하면 admission 오류다.

## 8. 인수 조건

cycle/self-edge/missing producer/cross-tenant edge/duplicate node/coverage gap를 deterministic하게 거부해야 한다. 재계획 직후 old worker heartbeat·result·publish 세 경로를 모두 fence해야 한다. unrelated node 결과는 보존 가능하지만 영향 node 결과는 무효화해야 한다. graph 전체 완료 후 contract revision 변경 시 새 Goal 검증이 필요한 상태를 정확히 보여야 한다.

## 10. 결정적 compile과 named output 보완

Compiler는 사전에 저장된 CompileRequest의 ID/revision/created_at를 사용하고 now/random을 호출하지 않는다. 같은 frozen 입력에 대한 결정성을 뜻한다. consumes에는 from_node뿐 아니라 output_name을 명시하여 producer의 어느 output인지 확정한다. immutable hash reference cycle과 WorkOutput publishing 규칙은 `design/33_IDENTITY_REFERENCE_LIFECYCLE.md`를 따른다.


---

# 07 · Adaptive Runtime와 deterministic scheduler

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 적응과 강제의 분리

Strategy 선택은 비용·난이도·불확실성에 대한 제안이다. 권한, 격리, evidence, 최대 budget, mandatory verify, 감사는 strategy와 무관하게 강제한다. Tiny change도 최소 contract와 verify receipt를 가진다. entry 문서나 권한 policy처럼 짧아도 위험한 변경을 tiny로 낮추지 않는다.

선택 기준은 path-based risk floor → contract risk → semantic uncertainty → available capabilities → budget envelope → qualification 결과 순이다. model routing은 평가 결과를 참고할 수 있지만 사내 데이터 분류보다 우선할 수 없다. 자동 위험 하향은 금지한다.

| 조건 | 기본 strategy | 생략 가능 | 생략 불가 |
|---|---|---|---|
| 작고 국소적이며 deterministic test 존재 | direct | 별도 planner/taskify | scope, authority, actual verify |
| 일반 기능/버그 | bounded_loop | 장황한 중복 계획 | env/context/evidence/freshness |
| 요구·설계 불확실성이 큼 | deliberative 또는 discovery | 불필요한 구현 착수 | unknown 해결/contract gate |
| cross-app | graph of bounded nodes | node 내부 고정 stage | integration/global verify |
| production/high-risk | 위 strategy + 강화 gate | 편의성 자동 publish | exact approval+containment |

## 2. Scheduler tick

한 tick은 pending inbox를 dedupe 처리하고, 만료 lease를 reconcile하고, cancellation/revocation/replan을 먼저 처리한다. 그 다음 ready graph nodes를 dependency 상태·resource·budget·authority·driver qualification 순으로 검사한다. claim은 transaction에서 `expected_revision`과 `lease_generation`을 증가시킨다. claim record와 run allocation과 outbox dispatch는 같은 RuntimeStore transaction이다.

Network/LLM 호출은 DB transaction 안에서 실행하지 않는다. dispatch outbox는 at-least-once다. Worker는 `dispatch_id/run_id`로 dedupe하여 같은 run에 두 개 process를 만들지 않는다. receipt를 잃었을 때 새 run을 무조건 만드는 것이 아니라 기존 session/run status를 조회한다.

## 3. Lease와 fencing

Lease는 우선 작업 소유권이지 security credential이 아니다. fencing generation은 work resource별 monotonic integer다. heartbeat는 current run/worker/token/generation을 모두 확인한다. expired lease의 token으로 checkpoint, completion, ToolBroker effect 실행을 허용하지 않는다. TTL 비교는 server UTC가 기준이고 worker clock을 신뢰하지 않는다.

기본 lease TTL 120s, heartbeat 30s, dispatch acknowledgement timeout 30s는 제안 default다. 실제 workload/네트워크에서 fault injection으로 결정한다. client가 요청한 임의 TTL이 policy 최대를 초과하면 거부한다. TTL 만료로 외부 effect가 자동 undo되지는 않는다.

## 4. 동시성·공정성

Tenant → project → Goal → app/resource의 hierarchical quota를 적용한다. 한 Goal의 막대한 fan-out가 다른 프로젝트를 starvation시키지 않도록 round-robin/aging을 둔다. 우선순위 tie-break는 enqueue sequence다. exclusive resource lock은 lease generation과 결합한다. 워커는 공유 working tree가 아니라 immutable base에서 생성한 독립 worktree/sandbox를 사용한다.

기본 concurrency 1 per repo write scope, read-only 최대 4는 측정 전 conservative default다. 같은 파일을 다른 worktree에서 수정해도 merge conflict와 semantic conflict가 있으므로 integration queue에서 다시 verify한다. Git worktree isolation과 OS security isolation은 다른 속성이다.

## 5. 예산 reservation

BudgetLedger는 root Goal에 cost, model_calls, tool_calls, wall_time, repair, active slots의 `limit/reserved/consumed/released`를 관리한다. Reserve 성공 후에만 dispatch한다. exact cost가 없으면 provider rate-card pin과 상한 추정으로 reserve하며 최종 usage를 받지 못한 금액은 `unknown`으로 보존한다. token 0으로 집계하지 않는다.

Wall clock은 active execution과 human wait를 별도 집계한다. deadline 자체는 기다림을 포함한다. user 답변을 기다리는 run은 compute slot과 write lock을 checkpoint 후 반환한다. 무응답이면 승인 상태로 바뀌지 않는다.

## 6. Repair와 실패 분류

implementation defect는 같은 node 새 attempt, task boundary 문제는 graph replan, requirement conflict는 Goal revision, environment 문제는 BLOCKED다. 동일 failure signature가 기본 3회면 같은 repair를 중단한다. 전체 attempt 제한과 같은 signature 제한을 별도로 적용한다. signature는 normalized error class+verifier ID+relevant stack/code path로 만들며 raw secret/log를 포함하지 않는다.

Backoff는 exponential+jitter+cap. retry 가능한 read-only/transient failures와 불명확한 external effect를 구분한다. 429/timeout 모두 같은 처리로 반복하면 중복 부작용을 만들 수 있다. UNKNOWN_EFFECT는 reconciliation 전 retry하지 않는다.

## 7. 취소와 종료

Cancel 수신 즉시 새 dispatch·새 grant·새 effect를 막는다. 실행 중 작업에는 cancel 요청을 보내고 graceful deadline 후 sandbox/process group을 종료한다. 외부 작업의 실제 종료 확인이 없으면 `cancel_requested`와 `effect_pending`을 표시하고 성공 종료처럼 보고하지 않는다. provider에게 interrupt를 보냈다는 사실이 runtime 종료 증거는 아니다.

## 8. 인수 조건

동시에 두 scheduler tick이 실행돼도 하나의 node에 current lease가 둘 생기면 안 된다. DB commit 직후 dispatch 실패·ack 전 crash·heartbeat 지연·clock skew·budget reserve 충돌·우선순위 starvation·cancel race·provider quota stop을 시험한다. 초기 release는 single active scheduler지만 단일 process의 실수로 병렬 tick이 발생해도 데이터 invariant가 보호되어야 한다.


---

# 08 · 상태 모델, 중단과 복구

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. Immutable definition과 mutable state

Contract/Graph/Pack/ReleaseSet 정의는 content-addressed immutable object다. lifecycle은 revisioned aggregate record다. WorkDefinition을 바꿔 과거 Run이 무엇을 했는지 다시 해석하지 않는다. 한 Work는 여러 Run을 가질 수 있고, Run은 한 definition revision에만 속한다. 한 Run은 한 번의 scheduled Work attempt다. 내부 모델/tool step은 Run에 속하지만 verifier-triggered repair는 이전 Run을 종료하고 새 Run을 만든다. transport retry는 새 semantic effect를 만들지 않는다. 실패 run을 성공 상태로 다시 살리지 않는다.

Normative transition table은 `contracts/state-machines.json`이다. 모든 command는 expected_revision을 요구하며, 모든 상태 변화는 동일 transaction의 event와 함께 기록된다. event는 관측/감사 및 projections를 위한 것이며, 최초 V3는 전체 DB를 event replay만으로 재구성하는 event-sourcing framework를 요구하지 않는다. audit event와 backup 둘 다 필요하다.

## 2. 상태별 의미

Goal: `draft → discovering/awaiting_decision → ready → active → verifying → verified`. blocked는 해결될 수 있는 제약, failed는 해당 실행 경로 실패, cancelled는 요청 취소다. Contract가 바뀌면 activate_new_revision 명령으로 execution epoch를 새로 만들고 관련 완료 표시는 재검증 대상으로 바뀐다. 기존 terminal revision과 verdict는 그대로 보존한다.

Work: `pending → ready → leased → running → verifying → succeeded`. `blocked`, `awaiting_human`, `failed`, `cancelled`, `superseded` 분기가 있다. pending은 dependency를 기다림, blocked는 해결해야 할 사유가 있음이다. 둘을 같은 상태로 숨기지 않는다.

Run: `created → running → pausing/paused → verifying → succeeded|failed|cancelled|lost|unknown_effect`. provider session은 별개라 expired session을 재생성해 같은 Work를 retry할 수 있다. `unknown_effect`는 외부 상태 확인을 요구하며 성공도 실패도 아니다.

## 3. Crash point별 복구

| Crash 시점 | durable 사실 | 복구 |
|---|---|---|
| admission 전 | Intent만 있음 | compile/admission 재시작, effect 없음 |
| claim commit 후 dispatch 전 | lease+run+outbox | 같은 dispatch_id 재전송 |
| worker spawn 후 ack 전 | worker journal 또는 provider session id | exact run 조회, 중복 spawn 방지 |
| tool effect 예약 후 실행 전 | effect PREPARED | policy 재검사 후 동일 effect_id 실행 |
| remote 실행 후 receipt 저장 전 | outcome 불확정 | UNKNOWN_EFFECT, remote receipt/status 조회 |
| evidence blob 저장 후 index 전 | orphan blob | hash 검증 후 attach 또는 retention GC |
| verdict 저장 후 event 전 | 같은 transaction이면 둘 다/둘 다 아님 | transaction replay, 개별 수작업 상태 수정 금지 |
| 승인 후 graph revision 변경 | 구 revision 승인 | digest mismatch로 실행 차단 |
| release active pointer 교체 후 crash | journal+pointer | registry reconcile, 같은 revision으로 원자적 완료 확인 |

## 4. Checkpoint

checkpoint는 contract/graph/node/run refs, last accepted event sequence, worktree base+diff artifact hash, context bundle, pending tool calls, steering inbox cursor, budget snapshot, driver continuation info, sandbox recipe를 가진다. provider opaque reasoning state는 export되지 않아도 된다. AMPLAI는 비공개 chain-of-thought 수집을 요구하지 않는다.

복구 시 governing instruction/active Decision/authorization changes를 먼저 확인한다. 과거 context summary만 믿고 실행하지 않는다. provider가 session resume를 지원하지 않으면 현재 contract+checkpoint+evidence digest로 새 session을 만들되 new provider session binding을 append한다. “이어 실행”과 “동일 provider session 재개”를 구분해서 기록한다.

## 5. 외부 효과의 상태

저장 enum은 lowercase다. Effect states는 `prepared → dispatched → applied|not_applied|unknown`이고 `unknown → reconciled`에는 resolved_outcome과 근거가 필요하다. 문서에서 대문자 표기는 같은 상태의 설명용 강조다. 자체 DB state 전이는 atomic하지만 HTTP/SSH/Git/파일 서버의 effect와 DB commit은 atomic하지 않다. 외부가 idempotency key를 지원하면 같은 key로 조회/재시도한다. 지원하지 않으면 식별 가능한 target revision/receipt를 먼저 읽고 reconcile한다. 검증 불가하면 human reconciliation이다. exactly-once 실행을 주장하지 않는다.

## 6. 부정확한 성공을 금지하는 규칙

프로세스 exit 0, 모델의 완료 문구, HTTP 202/204, projection의 DONE, 이전 revision의 tests PASS는 각각 완료 증거가 아니다. success를 확정하려면 현재 definition의 mandatory acceptance coverage, 환경과 evidence hash, effective authority, docs freshness, required independent review가 모두 맞아야 한다.

## 7. 인수 조건

모든 transition에 happy-path뿐 아니라 wrong state, stale revision, unauthorized actor, replayed request, out-of-order event를 넣는다. paused run이 새 instruction을 수신했는데 context에 반영하지 않고 resume되는 경우를 잡는다. crash 복구 이후 transcript를 전부 재실행해서 tool effect가 중복 발생하는 구현은 실패다.


---

# 09. 저장소·트랜잭션·정합성

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 저장의 책임을 분리한다

**파일이 모두 SSOT인 구조도, SQLite 한 곳에 모든 의미를 넣는 구조도 채택하지 않는다.** 객체별 소유자를 하나로 고정한다. Git은 승인된 지식·정책·정의의 배포 가능한 정본을 관리하고, Authority Store는 현재 유효한 승인·grant·revocation의 권한 원장이며, Runtime Store는 작업 상태·lease·예산·이벤트 원장이다. Git에 복제한 실행 요약은 projection이지 실행을 재개하는 기준이 아니다.

| 객체 | 정본 / 쓰기 주체 | 사본·투영의 역할 |
|---|---|---|
| source/knowledge/decision content | Foundry canonical Git + 기존 governance publish 경로 | ContextBundle이 digest로 고정하여 읽음 |
| 승인·ApplyGrant·revocation | Authority Store / AuthorityService | 감사용 export는 권한 부여 불가 |
| GoalContract·WorkGraph revision | content-addressed immutable blob + Runtime aggregate pointer | Git export는 설계·리뷰용 |
| goal/work/run/lease/reservation | Runtime Store / RuntimeService | status UI, Hermes, SSE는 read projection |
| artifact·verifier result | CAS + ArtifactRegistry | 첨부·HTML은 참조만 사용 |
| 집계 metric | Observatory projection | RunRecord와 원시 event에서 재생성 가능 |
| model/pack/harness definition | 서명된 ReleaseSet이 고정한 정의 | 설치된 host surface는 생성물 |

한 DB에 구현할 수 있더라도 논리 소유권은 유지한다. 기존 Authority DB와 신규 Runtime DB의 **서로 다른 트랜잭션을 원자적이라고 부르지 않는다**. [R30]

## 2. V3 기본 배치

단일 활성 control plane, 단일 로컬 Runtime SQLite, 로컬 CAS, 로컬 authority 연결로 시작한다. WAL/foreign_keys/busy_timeout을 명시하고 트랜잭션은 짧게 유지한다. **NAS/NFS/SMB 파일을 SQLite live DB로 사용하지 않는다.** 원격 worker는 HTTPS/API로 접속한다. 다중 writer control plane 및 PostgreSQL backend는 포트 수준의 확장점이며 별도 conformance 없이는 지원으로 표시하지 않는다. CP 프로세스 이중 기동은 exclusive owner lock + DB owner epoch로 차단한다. 다중 호스트 자동 leader election은 초기 필수 범위가 아니다. [R30]

DB 작업 중 LLM, 외부 HTTP, Git push, 파일 렌더링을 실행하지 않는다. 그것들은 Effect/Job으로 분리한다. SQLite schema migration은 transactional 가능한 단계와 CAS/file 단계가 분리된다. 저장소를 여는 서비스는 schema major 불일치를 read-write로 열지 않는다.

## 3. 논리 테이블과 키

전체 정의는 `contracts/storage-model.json`을 정본으로 사용한다. 모든 project 데이터 PK/FK에 `tenant_id, project_id`를 포함한다. ID가 우연히 전역 고유해도 scope 검증을 생략하지 않는다.

| 그룹 | 테이블 | 필수 제약·인덱스 |
|---|---|---|
| intake | intents, resolutions, questions | actor binding; `(scope, external_message_id)` dedupe |
| 정의 | goal_revisions, graph_revisions, artifact_refs | revision immutable; digest unique; active pointer CAS |
| 실행 | goals, works, runs, leases | row_version CAS; goal+node unique; active lease partial unique |
| 자원 | reservations, resource_claims | root budget sum; write resource unique; release idempotent |
| 변경 | steering_events, replan_requests | client event id unique; accepted/applied revision 별도 |
| 메시지 | runtime_events, inbox, outbox | aggregate seq unique; global cursor monotonic; delivery dedupe |
| 결과 | effects, evidence, verdicts | effect idempotency unique; artifact/verifier/subject digest 고정 |
| 평가 | eval_experiments, eval_runs, promotions | frozen manifests; protected holdout refs; promotion CAS |
| 보안 | grant_refs, revocation_watermarks | 참조만 저장; 최종 권한은 AuthorityService가 결정 |

인덱스: ready scheduling `(scope,status,not_before,priority,created_at)`, run recovery `(status,last_heartbeat_at)`, outbox `(delivery_status,next_attempt_at)`, event `(scope,cursor)`, root budget `(scope,goal_id,state)`. 모든 pagination은 snapshot/cursor 기반이며 tenant 경계를 넘는 global cursor는 외부로 반환하지 않는다.

## 4. 명령 처리의 원자 경계

```text
Authenticate -> authorize command -> validate syntactic input
BEGIN IMMEDIATE
  load current aggregate + row_version
  verify immutable refs + state + generation
  reserve budget/resource if needed
  update aggregate using CAS
  append aggregate event(seq = old_seq + 1)
  append outbox item, if external dispatch is required
  persist idempotency response digest
COMMIT
return committed response
```

중복 idempotency key + 같은 payload digest는 원래 결과를 반환한다. 같은 key + 다른 payload는 `IDEMPOTENCY_CONFLICT`. TTL 만료는 기록을 삭제해 위험한 write를 재허용하는 근거가 아니다. 일반 조회성 command dedupe와 irreversible effect receipt의 보존 기간을 분리한다.

외부 consumer는 inbox에 `(producer_id,event_id)`를 먼저 기록한 트랜잭션과 상태 반영을 함께 commit한다. ACK 전 crash하면 다시 전달되어도 중복 적용하지 않는다. delivery 자체는 **at-least-once**이며, exactly-once effect는 외부 시스템의 idempotency/reconciliation 지원이 있는 범위에만 한정된다.

## 5. Authority와 Runtime 사이

실행 admission은 authoritative grant를 조회하고 정확한 scope/action/contract_digest/graph_revision/expiry/policy_version을 대조한다. runtime에 grant reference를 저장해도 권한이 영구 고정되지 않는다. **부작용 직전에 Broker가 다시 조회**한다. 조회 불가·revocation watermark gap은 write HOLD다. read-only는 policy가 허용하는 cached lease 범위만 허용하며 오프라인에서 신규 권한을 생성하지 않는다.

grant revoke 직후 이미 전송된 외부 effect를 완전히 되돌릴 수 있다고 약속하지 않는다. `dispatched_before_revocation`, `unknown`, reconciliation 결과를 구분한다. 승인 취소 이후 새 side effect가 발생하지 않도록 중앙 Broker와 epoch fencing을 사용하며, 외부 시스템이 요청을 수신한 뒤의 결과는 Effect protocol로 추적한다.

## 6. CAS와 artifact commit

객체는 임시 파일에 기록 -> hash 계산 -> 허용 크기·형식·secret scan -> 같은 filesystem atomic rename -> registry commit 순서다. registry보다 파일이 먼저 생기면 orphan은 안전하게 quarantine 가능하다. 파일보다 registry가 먼저 보이는 상태를 만들지 않는다. upload는 content-type/extension을 신뢰하지 않고 media validator로 확인한다. 경로가 아니라 ArtifactRef를 교환한다. 경로 탈출·symlink·archive bomb 차단은 ingestion boundary 책임이다.

digest는 `sha256:<64 lowercase hex>`; JSON hash는 RFC 8785 JCS canonical bytes를 사용한다. `sort_keys=True`만으로 JCS라고 하지 않는다. 정수 범위는 I-JSON 안전 범위, NaN/Infinity 금지, timestamp는 UTC `Z` 정규형. digest field 자기 자신과 transport metadata는 hash 대상에서 제외하며 각 schema의 `digest_scope`에 고정한다. [R31]

## 7. 백업·복원·삭제

DB consistent backup API + CAS referenced object manifest + release pins + authority checkpoint를 함께 보관한다. live WAL 파일을 무작정 복사하지 않는다. 복원은 새로운 owner epoch로 시작하고 이전 worker lease를 전부 무효화한다. 외부 effect receipts와 grant revocation 상태를 reconcile하기 전 write 실행을 재개하지 않는다.

삭제는 mark -> reference scan -> retention/legal hold check -> approval -> tombstone -> physical purge 순서다. active contract/context/release/experiment가 참조한 digest는 GC 제외다. 최신 N개 보존만으로 evidence를 삭제하지 않는다. 민감정보 삭제 의무가 생기면 원문 삭제·redacted replacement·tombstone·감사 기록을 함께 처리하며 hash 존재만으로 원문 복원이 가능하다고 주장하지 않는다.


---

# 10. 권한·격리·신뢰 경계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 보안 모델

LLM은 planning과 제안을 담당하지만 authority는 아니다. 사용자 메시지, repo README, 검색 결과, 외부 MCP 응답, golden artifact에는 모두 악성·오염 지시가 섞일 수 있다. **instruction처럼 보이는 data를 policy로 승격하지 않는다.** 실제 안전 경계는 AuthN/AuthZ + sandbox + credential broker + effect broker + immutable audit다. sandbox만으로 데이터 유출·잘못된 승인을 모두 해결한다고 보지 않는다. [R11,R20,R24]

신뢰 등급: `control`(배포·승인된 정책), `canonical`(governance로 승인된 도메인 지식), `observed`(도구로 관측한 사실), `untrusted`(외부·사용자 제공 데이터). canonical도 실행 권한을 갖지는 않는다. 모델 prompt에는 data/control 분리를 표시하지만 enforcement는 코드에서 수행한다.

## 2. Actor와 역할

| 역할 | 가능 | 불가능 |
|---|---|---|
| user/requester | 의도 제출, 정책 범위 내 steer/cancel, 본인 승인 요청 확인 | 자신에게 없는 production 권한 위임 |
| planner/critic | resolution·contract·graph 초안, 질문 | 승인 원장 수정, worker credential 발급 |
| worker | 발급받은 제한 capability로 sandbox 내 작업 | canonical write, 타 project data 조회 |
| verifier | 고정된 verifier policy로 결과 판정 | acceptance·골든·정책을 동시에 변경 |
| governor/authorized approver | 정확히 지정된 변경 승인·거절 | 다른 tenant 권한 자동 획득 |
| meta proposer | harness 변경 후보와 실험 계획 제안 | holdout 열람, 자기 변경 자동 promote |
| release service | 검증된 승인과 release set으로 배포 | 검증 실패를 성공으로 변환 |

단일 사람이 여러 역할을 가질 수 있어도 high-risk 승인자는 정책으로 분리 가능해야 한다. 시스템 역할 이름은 서비스 계정의 실제 권한과 매핑되어야 하며 prompt의 `you are governor`는 권한이 아니다.

## 3. Capability 계산

`effective = requested ∩ project_policy ∩ actor_grant ∩ sandbox_supported ∩ driver_qualified`.

정책은 namespace/project/app/repo/read-write roots/egress host+port/tool verbs/secret handles/max budget/production action을 포함한다. union으로 권한을 합치지 않는다. deny가 allow보다 우선한다. capability 미지원이면 넓은 shell로 우회하지 않고 `CAPABILITY_UNSUPPORTED`다. 신규 target 자동 발견은 registry 조회 권한일 뿐 그 target의 write 허용이 아니다.

`ExecutionGrant`는 scope, subject contract digest, graph revision, allowed action, artifact/environment bounds, expiry, max uses, policy version, generation, issuer를 묶는다. 민감 action은 **일회용 effect key**로 bind한다. payload나 tool arg가 바뀌면 재승인 또는 재평가가 필요하다. 실행 중 risk 상승은 자동 하향 조정 없이 escalation한다.

## 4. 승인 화면과 TOCTOU

사용자는 “승인” 버튼만 보지 않는다. 대상 repo/app, 현재→변경, 검증 근거, irreversible 여부, 필요한 credential 범위, rollback 조건, 실제 effect 종류, 예상 상한 비용을 본다. 사람의 선택은 authenticated actor + current revision + digest에 기록한다. 메시지에 “승인함”이라는 문자열이 있거나 이모지가 달렸다는 이유만으로 승인하지 않는다.

승인 이후 contract/graph/artifact/policy 중 binding이 변경되면 기존 승인은 stale다. 권한 체크와 전송 사이 race는 중앙 broker에서 grant lease를 소비하고 effect PREPARED를 원자 기록한 뒤 제어한다. 외부 call의 물리적 도착과 revoke를 완전히 원자화할 수 없는 한계는 명시한다. strict 업무는 외부 endpoint도 fencing/generation을 검증해야 한다.

## 5. Worker containment

기본 worker는 별도 unprivileged UID 또는 container/VM, isolated worktree, read-only policy mount, writable workspace allowlist, no host Docker socket, no home credential mounts, egress deny-by-default다. package 설치는 사전 승인 mirror/lockfile만 이용한다. host filesystem sandbox가 없는 드라이버는 capability를 낮추거나 isolated VM 안에서 실행한다. 격리 기능을 off하고 같은 risk로 표시하지 않는다.

production credential은 agent text/context/env에 장기 노출하지 않는다. broker가 짧은 scoped token 또는 exact server-side operation을 수행한다. shell에서 직접 production network 접근 가능한 worker를 “broker enforced”라고 표시하면 안 된다. 로컬모델/온프레미스 기본 profile은 외부 provider 전송 금지이며 cloud 전환은 데이터 분류·정책·명시 승인을 통과해야 한다.

## 6. Pack·MCP·remote input

pack은 실행 코드 및 지시문을 포함하는 supply-chain 단위다. registry 서명·digest·publisher trust·permissions·dependency closure를 확인한 후 설치한다. `SKILL.md` 자체는 sandbox가 아니다. [R17,R18]

MCP server별 audience/credential을 분리하고 token passthrough를 금지한다. OAuth approval/redirect/origin 정책은 SDK 문서와 서버 설정으로 강제한다. tool list가 변경되면 capability snapshot이 바뀐 것으로 보고 requalification한다. MCP task ID는 원격 추적 ID이지 AMPLAI 승인이나 완료 증거가 아니다. [R20,R21]

## 7. 정책 변경과 감사

권한 policy·verifier·holdout·approval code는 protected surface다. 일반 /work·meta proposal이 수정 초안을 만들 수는 있지만 독립 리뷰와 기존 validator에 의한 검증 없이 적용할 수 없다. evaluator를 고쳐 green을 만드는 경우 기존 acceptance 변경 건과 분리하여 명시적 계약 변경으로 처리한다.

감사 event에는 actor/service identity, causal command, scope, old/new revision, grant ref, effect key, result classification을 남긴다. raw prompts, secrets, private chain-of-thought는 기본 저장하지 않는다. reasoning 대신 짧은 decision rationale와 근거 참조를 저장한다. 보안 관련 evidence는 삭제·redact 이벤트 자체를 감사한다.

## 8. 필수 보안 부정 테스트

cross-project IDOR; forged actor in JSON; stale approval; revoked grant; replayed effect; tool alias permission bypass; symlink escape; archive traversal; malicious README instruction; pack signing mismatch; evaluator self-change; leaked token; inaccessible authority; old worker epoch; network redirect to disallowed host. 각각 `eval/test-catalog.json`에 독립 test ID로 추적한다.


---

# 11. AgentDriver·Session·Model·Sandbox

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 네 가지를 혼합하지 않는다

`AgentDriver`는 호출/이벤트/세션 transport, `ModelProfile`은 모델 ID와 제공자·추론 설정, `SessionStore`는 재개에 필요한 상태, `SandboxDriver`는 격리된 실행 환경이다. **AstraDriver는 만들지 않는다.** Astra 모델은 Codex/Responses 등 실제 드라이버 위의 ModelProfile로 선택한다. 한 provider에 강한 기능이 있다고 모든 host에서 동작한다고 가정하지 않는다. [R01,R02,R09]

AMPLAI는 model 자체의 compaction/subagent/async tool 기능을 중복 구현하지 않는다. 대신 사용 가능 여부·범위·실패 의미·usage를 관측하고, provider가 보장하지 않는 durable ledger·권한·budget·recoverability를 관리한다.

## 2. Driver port

| method | 입력 | 반환 / 조건 |
|---|---|---|
| `probe` | binary/API version, environment ref | declared + observed capabilities; side effect 없는 probe |
| `prepare` | ExecutionEnvelope, sandbox lease | driver session ref; no work started |
| `start` | prepared ref, immutable prompt/context refs | accepted handle + event cursor; success 의미 아님 |
| `poll/subscribe` | exact run handle, after cursor | normalized event batch; dedupe 가능한 event ID |
| `steer` | SteeringEvent, expected native turn | accepted/unsupported/stale; 적용 여부 별도 |
| `pause/cancel` | run handle, reason | requested/acknowledged; process/tool 종료 확인 별도 |
| `checkpoint` | run handle | resumable opaque ref or unsupported; credentials 미포함 |
| `resume` | exact session + compatible versions | resumed/new_session_required/stale |
| `collect` | stopped handle | artifacts/usage/errors; authority 결정하지 않음 |
| `destroy` | prepared/run handle | idempotent resource teardown; effect rollback 아님 |

driver는 `invoke shell arbitrary bypass` 같은 별도 escape hatch를 갖지 않는다. normalized errors는 auth/unavailable/rate_limit/context_limit/capability/session_stale/cancelled/unknown_effect/provider_error로 분류한다. provider stderr 전문을 사용자에게 내보내기 전 redact한다.

## 3. Qualification matrix

| 드라이버 | V3 포지션 | 필수 자격시험 |
|---|---|---|
| ClaudeCode CLI | 기본 로컬/격리 worker 후보 | explicit session resume, JSON parsing, allowed tools, process-tree stop, usage unknown 처리 |
| Codex CLI | 기본 로컬/격리 worker 후보 | exact run/session correlation, JSONL fragmentation, nonzero/empty output, sandbox policy |
| Codex App Server | **실험적 opt-in** | version-generated schema, authentication/stdio boundary, turn fencing, disconnect recovery |
| OpenCode Server | 선택 profile | password/auth explicitly on, async 204 + event correlation, SSE recovery, permissions |
| OpenAI Responses | 선택 API profile | original call_id pairing, async pending tools, WebSocket reconnect, budget across continuations |
| Managed Agents API | 선택 managed profile | region/data/credential policy, durable handle, authority boundary, SDK change qualification |
| 온프레미스 API | 데이터 제한 profile | protocol subset, usage/provenance, unavailable capabilities fail closed |

2026-09-15에 확인한 Codex app-server 문서는 experimental/production unsupported 경고가 있으므로 이것을 V3 기본 production transport로 고정하지 않는다. OpenCode async endpoint의 204는 접수이며 완료가 아니다. managed API는 최신 공급 옵션일 뿐 Foundry를 대체할 이유가 아니다. [R03,R22,R25]

`declared`(문서/설정), `observed`(현재 endpoint probe), `qualified`(AMPLAI conformance 통과), `granted`(이 Run에 정책 허용)를 따로 저장한다. 드라이버 버전이 바뀌면 이전 qualification을 그대로 재사용하지 않는다. 버전·OS·sandbox·모델 조합의 재검증 범위는 release compatibility matrix가 정한다.

## 4. Async와 native subagent

외부 tool 요청은 `amplai_effect_id ↔ provider_call_id ↔ native_job_id`를 일대일 또는 명시된 parent-child 관계로 저장한다. 결과가 다른 run/session에 섞이면 reject한다. 모델이 tool pending 중 다른 일을 해도 AMPLAI resource/budget reservation은 root goal에 유지한다. tool result 재전송은 provider 문서의 protocol 허용 범위에 맞춰 dedupe한다. [R04]

native subagent는 자체 권한자가 아니라 parent execution capability의 subset을 받는다. parent가 완료됐다고 child process가 자동 정리됐다고 가정하지 않는다. 드라이버가 child usage/취소/범위 제어를 제공하지 못하면 그 risk profile에서는 native delegation을 비활성화하고 AMPLAI가 명시적 Work로 분리한다. 동시성 제한은 AMPLAI+native 합계가 정책 상한을 넘지 않아야 한다.

## 5. Steering 전달

native steer 지원 시 expected turn ID와 event ID를 묶는다. ACK=수신, APPLIED=모델/세션이 적용 확인, EFFECTIVE=새 revision으로 뒤의 effect admission이 적용된 상태를 분리한다. provider queue disconnect 손실 가능성을 고려해 AMPLAI ledger에 먼저 commit하고 미확인 event를 reconcile한다. duplicate steering이 contract를 두 번 수정하지 않도록 idempotency를 둔다. [R05,R22]

native 미지원이면 safe checkpoint에서 pause -> revision 재평가 -> 새 세션/새 prompt로 resume한다. run 중 stdin에 텍스트를 임의 삽입하여 '지원'했다고 하지 않는다. 이미 시작한 tool 취소·rollback은 provider steering과 별도다.

## 6. Session과 모델 교체

resume은 exact opaque session ID와 driver binary/API version, model compatibility, sandbox snapshot digest, context manifest, contract/graph revision을 대조한다. CLI의 '가장 최근 대화 계속' 옵션은 동시 작업 환경에서 사용하지 않는다. raw nested JSON 어디서나 `session_id`를 찾아서 붙이는 heuristic을 제거하고 version-specific typed decoder로 바꾼다. [R23]

모델 교체는 checkpoint에서만 기본 허용한다. 기존 usage·실패·evidence는 유지한다. reasoning cache/세션 format은 provider별이라 portable이라 주장하지 않는다. resume 불가능하면 canonical facts + approved decisions + artifacts + failed attempts 요약으로 새 session을 만든다. 비공개 사고과정의 복제를 요구하지 않는다.

## 7. 지원 제한의 표기

`unsupported`를 false-success로 바꾸지 않는다. README와 UI에 tested matrix만 '지원'으로 표시한다. 실제 설치된 provider release는 구현 시 다시 pin/probe해야 하며 이 설계 ZIP은 어떤 SaaS credential이나 현재 계정의 사용 가능성을 검증한 결과가 아니다.


---

# 12. Knowledge Runtime·Context·Ontology

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 현재 Foundry 모델을 유지한다

기존 8 memory kind(source/concept/principle/decision/question/architecture/experiment/map)와 Source→Proposal→Decision→Apply를 유지한다. stable knowledge/decision/work/evidence는 **검색·수명주기 view**이지 기존 객체를 네 개의 새 DB로 강제 이관하는 명분이 아니다. Decision의 승인 기록을 일반 메모리 요약으로 대체하지 않는다.

| view | 의미 | 정본 / 승격 |
|---|---|---|
| stable/domain | 승인된 용어·경계·불변조건·현재 동작 | Foundry canonical; governance를 거쳐 변경 |
| decision | 승인·거절·superseded·rationale | Authority와 canonical Decision의 링크 |
| work/session | 계획·시도·질문·중간 상태 | Runtime; 사실/권한 아님 |
| evidence/experience | 관측·verifier·실험·회귀 | CAS/RunRecord; 조건부 근거, 자동 규칙화 금지 |

## 2. Knowledge Readiness는 Goal 작성 이전부터 사용

현재 8개 readiness 항목을 유지한다: terminology, current behavior, boundary, invariants, SSOT, contradictions, acceptance, verifier. Goal Resolver는 각 항목을 `ready/missing/conflicting/not_applicable`과 source refs로 기록한다. 누락을 모두 '새 질문'으로 만들지 않고 registry→canonical→repo facts→evidence 순으로 탐색한다. acceptance 또는 authority에 영향을 주는 unresolved conflict는 실행을 막는다.

`not_applicable`에는 reason과 verifier rule이 필요하다. 언어모델 confidence 숫자 하나로 readiness를 통과시키지 않는다. “개발자가 보통 이렇게 한다”는 외부 지식이 이 프로젝트의 승인된 invariant를 덮어쓰지 못한다.

## 3. ContextBundle

bundle은 긴 복사본 하나가 아니라 **필수 core + 주소/버전/digest를 가진 map + 제한된 필요한 excerpts**다. 반드시 포함: 정확한 contract·graph node·authority bounds·현재 active decisions·app binding·invariants·acceptance/verifier plan. 추가 지식은 progressive disclosure한다. 핵심 보호 정보를 단순 pointer만 남겨 모델이 읽지 않고 작업하도록 허용하지 않는다. [R06,R17]

ref에는 scope/kind/id/revision/digest/trust/freshness/superseded_by/source timestamp/excerpt range가 붙는다. `ContextBundle` hash가 같아도 외부 URL의 현재 내용이 같다고 보지 않는다. 외부 자료는 ingest하여 snapshot ref로 고정하거나 unresolved external dependency로 표시한다. active invariant discovery의 completeness marker가 없으면 context 조립을 완료했다고 하지 않는다.

## 4. Context budget와 검색

고정 core 최소 크기, selected excerpt budget, reserve for tool results를 둔다. 오래된 summary만 읽고 원본 decision을 생략하지 않는다. 우선순위는 권한/보호 invariant > 현재 계약 > 현재 코드 사실 > 관련 canonical > 경험/외부 팁이다. user preference는 domain invariant가 아니며 적용 scope를 명확히 한다.

기본 resolver는 deterministic scope filters + registries + file/symbol/path 검색 + metadata joins다. lexical/semantic retrieval은 후보 탐색 플러그인이다. vector DB를 넣지 않는다는 것은 semantic search를 영구 금지한다는 뜻이 아니다. retrieval recall·staleness·privacy·maintenance가 baseline보다 좋아졌다는 eval 뒤에만 추가한다.

## 5. Ontology의 최소 kernel

DomainConcept, Relation, Constraint, Mapping을 지원하는 registry interface를 둔다. WorkGraph는 실행 관계이고 KnowledgeGraph는 의미·사실 관계이므로 저장 키와 API를 혼합하지 않는다. 반도체 DC의 장비/계측기/핀/물리 상태 owner·ABI 규칙은 `semiconductor-dc` pack이 공급한다. AMPLAI core는 PCMU/Cortex 전용 필드를 가지지 않는다.

RDF/TTL/SHACL 사용이 필요한 domain pack은 기존 canonical data와 source provenance를 보존한다. 안정 baseline은 W3C SHACL Recommendation이며 2026-08-28 SHACL 1.2는 Working Draft로 별도 experimental profile이다. Neo4j/graph DB는 필수 사항이 아니다. [R33,R34]

## 6. 발견→승격→폐기

worker discovery는 `Observation` 또는 기존 proposal intake로 보낸다. 증거 출처·대상 버전·관측 환경·재현 조건·반례를 보관하고 기존 지식과 중복/모순을 검사한다. 반복해서 관측됐다는 이유로 원칙이 자동 승격되지 않는다. Knowledge Governor가 기존 경로로 승인한다.

superseded 정보는 검색 기본 후보에서 낮추되 과거 Run 재현에는 읽을 수 있어야 한다. source 삭제와 supersede는 다르다. canonical change가 활성 run의 protected invariant를 바꾸면 별도 steering/revalidation event를 생성하고 하위 effect를 HOLD한다. 모든 외부 변경마다 불필요한 전체 replan을 하지 않고 affected refs dependency로 범위를 줄인다.

## 7. Hot/cold와 gardening

active/canonical definitions는 기본 context 표면에, raw evidence/history는 archive index에 둔다. `specs/012...` 대량 로그는 즉시 삭제하지 않고 referenced manifest→archive migration→link rewrite→backlink test→approved cleanup 순으로 이동한다. full repository gardening은 독립 Work이며 일반 구현 뒤의 gardening은 report-only다. secret-containing runtime state는 Git/export/LLM context에 유입하지 않는다.


---

# 13. Skill·Capability Pack·App Binding

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 사용자 표면

일상 interface는 `/work`와 `/design` 두 개로 고정한다. Codex 등 host 문법은 adapter가 제공한다. `/work`는 의도를 검증된 결과까지 진행하며 `/design`은 설계 artifact까지만 생성한다. review/debug/research/plan/taskify/analyze/converge는 내부 capability다. 사용자에게 내부 stage 호출 순서를 외우게 하지 않는다.

SpecKit은 지우기 전에 pack으로 내린다. 동작이 중복된 grill-me/grilling은 design question policy와 비교해 필요한 questioning 규칙만 통합한다. eli12처럼 AMPLAI 실행 원리와 관계없는 표현 utility는 기본 설치에서 제외하되 사용자가 선택한 utility pack에 둘 수 있다. 과거 승인·결정 파일은 skill 정리와 함께 삭제하지 않는다.

## 2. Pack 단위

pack에는 ID/version/publisher/digest/signature/runtime protocol range/dependencies/entry capabilities/required permissions/model capability requirements/context refs/verifier profiles/eval cases/license/migration notes가 있다. schema는 `capability-pack.schema.json`. dependency cycle·version conflict·unknown publisher·elevated permission 요구는 설치 전 실패다. pack은 path 문자열만으로 실행 도구를 등록하지 못한다. tool adapter는 정책 registry에 존재해야 한다.

pack 내부 권장 구성은 `PACK.json`, `skills/`, `context/`, `verifiers/definitions/`, `eval/cases/`, `templates/`. 실행 파일을 포함하는 경우 코드 리뷰·sandbox allowlist·release attestation이 추가된다. remote instruction 다운로드로 install grant를 우회하지 않는다. [R17,R18,R36]

## 3. V3 기본 pack 경계

| pack | 역할 | core로 가져오지 않을 것 |
|---|---|---|
| spec | 요구·설계·task decomposition·critic rubrics | 모든 work에 긴 9단계 mandatory pipeline |
| software | build/test/lint/ABI/compatibility profiles | 특정 언어·repo 경로 하드코딩 |
| frontend | functional UX·accessibility·responsive·visual checks | 미감 점수만으로 자동 product approval |
| documents | structural/render/visual/readability evidence | Core에 DOCX/PDF renderer 의존성 |
| research | primary-source retrieval·date/provenance·uncertainty | 검색 결과를 canonical fact로 자동 승격 |
| ontology | semantic validation·mapping·contradictions | graph DB 강제 |
| semiconductor-dc | DC domain/context/safety/compatibility constraints | core에서 tester/production 직접 제어 |

이 설계에는 모두의 **interface와 validation profile**을 포함한다. 모든 domain pack의 실제 엔진 구현이 끝났다는 뜻이 아니다. V3 acceptance는 pack 설치/권한/호출/결과 수집 contract conformance와 명시된 대표 사례로 정의한다.

## 4. AppBinding

registry에 app_id, canonical repo identity, allowed roots, owner, environments, required invariants, target verifier profiles, data classification, write policy를 등록한다. 여러 이름(alias)이 같은 app을 가리킬 수 있으나 대상 registry ID는 하나다. repo URL·경로는 입력에서 직접 실행 cwd가 되지 않는다. 표준화된 repo identity + signed/local authorized binding으로 resolve한다.

앱 프로젝트는 kit uninstall 이후에도 build/test/domain data를 사용할 수 있어야 한다. `.ai-team`는 개발도구 구성, `.amplai`는 임시/영속 실행상태(권한 분리), `.agents`는 generated skill surface다. 앱 source가 이 경로를 import하거나 production binary가 AMPLAI control plane에 의존하면 architecture gate 실패다.

## 5. 설치·갱신·해제

Foundry distribution이 signed ReleaseSet으로 pack+kit+protocol compatibility를 고정한다. installer는 owned paths만 변경하며 `expected_old_digest`가 달라진 파일은 conflict report 후 중지한다. app-local override는 별도 namespace에 두고 upstream 파일에 섞지 않는다. required:false marker가 없다는 이유만으로 corruption이라 판정하지 않는다.

삭제는 `owned=true`, current digest matches, no protected references, verified replacement/retirement decision, backup/rollback available일 때만 실행 가능하다. symlink/mirror는 host마다 확인한다. installation receipt에는 실제 설치 digest와 generated surface inventory를 남긴다. 자동 plugin install 또는 자동 permission escalation은 금지한다.


---

# 14. Verifier·Evidence·문서/UI 품질

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 완료의 의미

worker exit0, 모델의 '완료', PR 존재, screenshot 존재, evaluator의 자연어 칭찬은 각각 Done이 아니다. **해당 계약 revision의 모든 mandatory acceptance에 대해 고정된 평가 방식으로 얻은 Evidence와 Verdict가 있어야 한다.** 결과는 `pass/fail/inconclusive/waived`; waiver는 authorized human decision이고 pass와 구분한다. 보안·authority 불변조건은 waived 불가다.

`VerificationPlan`은 acceptance_id → verifier_id@version → subject artifact selector → required evidence → environment → threshold → independent reviewer requirement를 연결한다. threshold를 implementation 후 맞춰 낮추지 않는다. plan 변경은 계약 변경이다. [R10]

## 2. 증거와 신뢰

Evidence에는 producer, actual artifact digest, tool/version/environment, started/finished, exit code, observation/result refs, subject contract/graph revision, redaction 상태를 넣는다. artifact digest는 agent가 텍스트로 선언하는 값만 믿지 않고 ArtifactService가 재계산한다. VerifierService의 attestation 또는 authenticated server-side execution receipt가 판정의 출처를 증명한다. 서명 자체는 내용의 의미적 정확성을 보증하지 않는다.

code verifier는 change diff뿐 아니라 intended build tree의 정확한 revision을 실행한다. tool command는 VerifierProfile allowlist에서 선택하고 worker가 `/bin/true`를 테스트로 바꾸지 못한다. test changes는 product code changes와 함께 review하되 기존 protected regression corpus는 별도 신뢰 경계에 둔다. 테스트를 삭제해서 green을 만든 경우 coverage/diff guard가 잡는다.

## 3. Verification 층

| 층 | 확인 | 불가능/부족한 것 |
|---|---|---|
| schema/semantic | 구조·참조·범위·타입·불변조건 | 실제 제품 동작 증명 아님 |
| deterministic | unit/integration/build/lint/ABI/negative tests | UX/미감/도메인 만족 전체 증명 아님 |
| artifact inspection | 실제 rendered/file/system output | screenshot만으로 기능 성공 아님 |
| model-assisted critique | 누락·명료성·일관성·루브릭 평가 | 독립된 객관 truth 아님 |
| human acceptance | 중요 의도·취향·배포 승인 | 자동 안전 gate를 무시할 권한은 없음 |

모든 Work에 모든 층을 실행하지 않는다. risk/asset type에 따라 profile을 선택하되 mandatory invariant는 공통이다. Planner→Builder→Evaluator는 uncertainty가 높은 일에 쓰며 작은 deterministic 수정에 세 agent를 강제하지 않는다. [R08]

## 4. Frontend pack 상세

입력은 사용자 흐름, reference direction, design tokens, target devices, accessibility target, non-goals, example content다. 단계는 reference/constraints 확인→작은 visual direction 초안→구조/interaction 구현→실제 브라우저 render→functional+accessibility+responsive 확인→visual critique→bounded repair다.

필수 evidence: 대표 page의 실제 screenshot(정해진 viewport), interaction trace, console/network errors, overflow/clipping/overlap report, keyboard flow, empty/loading/error state, text density/readability 관측. golden baseline은 사람이 승인한 revision이며 agent가 baseline을 갱신하여 visual diff를 없애면 실패다. Playwright screenshot 비교는 재현 환경의 변화 탐지이며 **'아름답다'를 증명하지 않는다**. font/browser/OS/animation/timestamp를 고정한다. [R35]

subjective acceptance는 예: “운영자가 lot 상태·다음 조치를 1화면에서 식별할 수 있다”처럼 task-based rubric으로 쓴다. 단일 aesthetics 9/10을 요구하지 않는다. 사람이 최소한 direction과 최종 중요 UX를 확인하도록 optional human gate를 바인딩한다. visual judge가 fail을 발견하면 좌표/영역/대상 요소/재현 viewport/이유를 구조화한다.

## 5. Documents pack 상세

문서 타입별 renderer를 선택하고 exact version/font availability를 검사한다. DOCX/PPTX/PDF/SVG/HTML 원본뿐 아니라 **최종 렌더링 결과**를 검수한다. 구조 검사(페이지·슬라이드 수, 제목 계층, 테이블 셀, 링크)와 시각 검사(글리프 누락, clipping, 겹침, 읽기 순서, 여백, 대비)를 나눈다. 모든 페이지 thumbnail→flagged page full render→중요 페이지 상세 확인 경로를 둔다.

SVG는 viewBox/size/font/text anchors/clip path/filter/external asset을 검사한다. embedding target별 지원 차이를 고려해 필요 시 PNG fallback을 쓰되 의미 정보와 원본을 보존한다. 폰트가 없으면 임의 대체 후 success하지 않고 지정 대체 정책 또는 HOLD다. font 파일의 무단 배포를 금지한다. raster fallback은 해상도·선명도·접근성 비용을 기록한다.

golden document ingestion은 license/source provenance 확인→content와 style 분리→sanitized style tokens/layout examples→human-approved baseline 순서다. 개인/회사 민감문서를 외부 모델로 자동 보내지 않는다. 사용자에게 보일 글꼴·글자·이미지 깨짐을 reproducible acceptance로 만든다.

## 6. Repair와 convergence

repair는 failure finding IDs를 입력으로 받아 수정한 artifact와 해결 evidence를 제출한다. 동일 failure signature가 반복되거나 수정이 다른 mandatory acceptance를 깨면 bounded budget 내 재시도 후 HOLD한다. pass에 가까워 보인다는 모델 판단만으로 반복 수를 무한 연장하지 않는다.

최종 global verifier는 node별 pass뿐 아니라 integration acceptance·cross-app compatibility·doc freshness·pending effects·open human questions를 확인한다. 서로 다른 branch에서 각각 pass한 결과가 합쳐진 뒤도 pass인지 별도 검사한다. 검증 중 code/contract가 바뀌면 그 verdict는 active revision에 사용할 수 없다.

## 7. 기존 doctrine 보존

Doc freshness와 gardening을 통합하면서 `repository_gardening` enum drift를 고친다. 일반 작업 후 incremental gardening은 report-only; 대량 삭제/이동/format은 scope가 선언된 독립 Work로 제한한다. 테스트가 아직 구현되지 않은 상태는 `not_run`으로 표시하며 '명세 있음'을 '검증 완료'로 집계하지 않는다.

## 8. mandatory와 waiver의 정확한 종료 규칙

`goal.verified`는 현재 계약의 모든 mandatory acceptance가 trusted PASS일 때만 가능하다. optional acceptance의 승인된 waiver는 별도로 기록한다. mandatory criterion을 면제해야 하는 합법적 변경은 승인된 새 contract revision에서 scope/acceptance를 바꾸고 재검증한다. waived verdict를 PASS로 세거나 protected invariant를 면제하지 않는다.


---

# 15. Eval Observatory·추적·측정

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 시작부터 관측한다

Observatory를 메타하네스 직전의 부가 dashboard로 두지 않는다. 첫 V3 Work부터 RunRecord·구조화 event·artifact/verdict 연결을 기록한다. 이것은 후속 자동 최적화의 전제이며, 화면보다 신뢰할 수 있는 데이터 계약이 먼저다. trace가 비어 있는데 성공률만 표시하지 않는다. [R10,R15]

RunRecord는 scope/root_goal/contract_digest/graph_digest/node/run/parent run/model/driver/sandbox/harness composition/context bundle/skills/tools/usage/latencies/failure signatures/human interventions/acceptance coverage/verdict/effect refs를 담는다. private chain-of-thought 대신 **관측 가능한 action, 짧은 판단 이유, 근거 ref**를 남긴다.

## 2. 이벤트와 trace 구조

trace root는 goal attempt, span은 resolver/compiler/dispatch/tool/verifier/human gate/replan/meta experiment다. 실행 event seq는 Runtime Store가 부여하고 OTel timestamp에 의존하여 authoritative order를 추론하지 않는다. wall time과 duration을 구분하며 duration은 monotonic clock으로 계산한다. delayed event는 event_at와 ingested_at를 모두 갖는다.

OpenTelemetry exporter는 내부 RunRecord의 projection이다. GenAI semantic conventions의 별도 저장소 이동/버전을 고려해 mapping version을 pin한다. provider-specific attribute를 무분별하게 internal schema에 퍼뜨리지 않는다. OTel collector 장애 시 실행 권한이 바뀌지 않으며 locally bounded spool 후 drop accounting을 남긴다. 중요한 authority/effect event는 telemetry drop 대상이 아니다. [R28,R29]

## 3. Metric 정의

| metric | 분자 / 분모·범위 | 오해 방지 |
|---|---|---|
| verified goal rate | 모든 mandatory acceptance 충족 goal / 종료된 eligible goal | 취소·blocked·inconclusive를 숨기지 않고 별도 표시 |
| first-attempt verified rate | repair 없이 verified / eligible | 쉬운 업무 증가와 구분해 class별 stratify |
| acceptance coverage | trusted verdict가 있는 mandatory criteria / 전체 mandatory | waived는 pass 아님 |
| rework | verifier failure 후 재실행 effort | 모델 token만이 사람 effort는 아님 |
| human intervention | 질문/승인/steer unique event와 active wait | 자가보고 생산성 지표로 대체하지 않음 |
| lead time | intent 접수→verified; queue/compute/human wait 분해 | background 대기와 실제 작업량 구분 |
| cost | provider 확정 usage 또는 bounded estimate/unknown | 미보고 usage를 0원으로 하지 않음 |
| reliability | lost lease/duplicate suppressed/unknown effect/recovery latency | retry로 감춘 실패도 집계 |
| context quality | stale refs/conflicts/missing invariant incidents | token 수 감소만 최적화하지 않음 |
| safety | denied/revoked/escaped/secret incidents | 0건 관측≠위험 0 |

보고는 model/driver/pack/harness version, task class, risk, repo, time window로 slice한다. 적은 표본에는 count와 uncertainty를 함께 표시한다. 모든 대상에 하나의 성공점수를 적용하지 않는다.

## 4. Dataset 관리

기존 회귀 tests, 실제 실패를 sanitized/replayable case로 만든 corpus, domain goldens, adversarial/safety cases를 분리한다. corpus version, license, privacy class, expected outcome, verifier version, task difficulty, environment fingerprint를 manifest로 고정한다. 신규 사건이 holdout에 들어갈 때 이미 proposer에게 노출된 정보는 contamination 표시한다.

train/development/validation/holdout 역할을 명시한다. meta proposer는 development failure digest만 보고 hidden holdout 원문·expected outputs에 접근하지 않는다. case를 삭제하거나 난이도를 낮추는 변경은 benchmark governance로 별도 review한다. 전부 공개된 로컬 프로젝트에서는 진짜 숨긴 holdout이 없을 수 있으므로 'independent holdout'이라고 잘못 표기하지 않는다.

## 5. Eval 실행 모드

`static`: schema/permission/config analysis. `replay`: immutable input + tool observations로 순수 로직 재현. `sandbox_rerun`: 실제 모델/도구를 새 격리 환경에서 실행. `shadow`: 실사용 입력으로 결과만 비교하고 external effects 금지. `canary`: 명시적으로 허용된 일부 업무에서 신규 composition 실행. 이 다섯 모드를 결과에서 구분한다. 기록된 trajectory를 재생했다고 live 성공률이 증명되는 것은 아니다.

external system을 수반하는 test는 simulator/contract stub→staging integration 순서로 검증하며 production mutation을 eval용으로 사용하지 않는다. secrets·customer source는 export 전에 policy scrub을 적용한다. synthetic fixture에는 실제 operation/승인 효력이 없다는 표시를 둔다.

## 6. 통계와 판정

각 실험은 objective, primary endpoint, baseline/candidate, task sampling, strata, paired design, repeat policy, non-inferiority margin, safety stop, cost ceiling, analysis rule을 **실행 전에 고정**한다. 항상 n=30 또는 임의의 95% 숫자로 통과시키지 않는다. baseline variance와 허용 오차에 맞춰 표본·반복을 정하고 예산 부족 시 `inconclusive`로 끝낼 수 있다.

paired task comparisons와 반복 실행을 사용하되 동일 seed가 모델의 결정성을 보장하지 않는다는 점을 기록한다. 많은 candidate 중 최고만 골라 같은 holdout을 반복 사용하지 않는다. confirmatory test와 탐색적 비교를 분리한다. sequential canary 중간 확인은 사전 정의 stop rule 또는 적절한 error control을 따른다. 배포 결정은 통계 점수만이 아니라 보호 gate와 authority를 모두 요구한다.


---

# 16. Meta-Harness 전체 설계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 두 책임을 나눈다

V3 Meta-Harness는 (A) **Federation & Composition**: 다양한 driver/model/pack/sandbox를 조합·추적·운영하는 층과 (B) **Evidence-driven Evolution**: 관측을 바탕으로 조합·규칙 변경을 제안하고 검증·배포하는 층으로 나눈다. 두 층 모두 Runtime의 권한·effect protocol을 우회하지 않는다. 'meta'는 무제한 최상위 agent를 뜻하지 않는다.

연구의 Meta-Harness/AHE는 반복 측정으로 harness 자체를 개선하는 가능성을 보여주는 근거다. benchmark 성능 증가를 AMPLAI 효과로 옮겨 적거나 자동 자기변경을 production에 바로 적용하는 근거로 쓰지 않는다. [R14,R15]

## 2. Composition Registry

`HarnessComposition`은 versioned immutable release object다. model profile·driver profile·sandbox profile·capability packs·prompt bundle·router policy·context policy·verification profile·budget policy·protocol compatibility를 정확한 digest로 고정한다. native subagent count 같은 optional 설정도 포함한다. 'latest' symbolic reference를 실행 중 해석하지 않는다.

Selection은 project/data/risk/required capabilities로 후보를 **먼저 필터링**한 뒤 task-class baseline policy로 선택한다. 모델 추천 agent는 후보를 제안할 수 있지만 허용되지 않은 외부 provider나 unsupported transport를 선택할 수 없다. dispatch 이후 composition은 고정되며 변경은 checkpoint + new attempt/steering revision이다. 자동 online bandit·강화학습 routing은 초기 기본값이 아니다. 충분한 관측과 안전한 실험 protocol 없이 live 최적화를 켜지 않는다.

## 3. 변경 가능 surface

| class | 예 | 제안/적용 정책 |
|---|---|---|
| A: low-risk composition | task별 prompt excerpt, approved pack 조합, bounded planning strategy | offline eval→low-risk canary→정책 내 승인된 release |
| B: behavior/runtime | routing threshold, context retrieval, repair heuristic, driver mapping | 독립 code review·fault/security regression·human promote |
| C: protected control | authority, verifier core, budget enforcement, signing, holdout | 일반 meta-autopromote 금지; 별도 governed engineering Work |
| D: forbidden action | 자기 approval 발행, evidence 조작, secret 유출, production 무단 access | schema/permission에서 reject |

검증기를 개선하는 합법적 작업도 가능하다. 다만 후보 harness 개선 실험과 동시에 evaluator를 바꾸어 효과를 비교하지 않는다. evaluator 변경은 별도 baseline qualification이고 이전 결과와 동등 비교 가능성을 검증해야 한다.

## 4. Evolution pipeline과 상태

```text
Observation cluster
 -> ChangeProposal(draft)
 -> screened
 -> experiment_approved
 -> offline_running
 -> offline_evaluated(pass | fail | inconclusive)
 -> canary_approved
 -> canary_running
 -> promotion_pending
 -> promoted | rejected | rolled_back
```

각 이동은 `contracts/state-machines.json`의 guards를 따른다. `fail/inconclusive`에서 promote로 바로 이동하지 않는다. 조기 중단은 aborted와 reason을 남긴다. MetaProposer는 development observations와 allowed surfaces만 받는다. proposal은 hypothesis, observed failure refs, exact candidate diff, expected benefit, expected risk, affected classes, protected-surface scan, evaluation plan, rollback plan을 반드시 담는다.

## 5. 실험 생성과 독립성

ExperimentService가 baseline/candidate/corpus/verifier/environment/analysis/budget를 freeze한다. LLM judge를 쓸 경우 model/prompt/rubric도 고정하고 reliability를 deterministic/human reference로 교정한다. 후보 generator와 judge가 같은 모델이라도 shared prompt/context로 답을 미리 넘기지 않는다. high-risk는 독립 human review를 요구한다. 개발자가 구현과 test를 같이 쓰더라도 보호된 regression 결과를 덮어쓸 수 없다.

model API alias가 provider에서 바뀌거나 tool version이 drift하면 실험을 `environment_drifted`로 표시한다. 비용 절감을 주장하면서 모델 버전도 바꿨다면 harness-only 효과가 아니라 composition 효과라고 보고한다. replay와 sandbox rerun의 결과를 같은 성공률 분모에 섞지 않는다.

## 6. Replay·shadow·canary

Replay는 CP state machine/selector/context assembly와 recorded inputs의 동작 비교에 적합하다. 모델을 다시 불러 출력을 생성하는 것은 rerun이며 stochastic하다. 과거 외부 write는 sandbox simulator receipt로 대체하고 실제 재호출하지 않는다. shadow는 side effect zero profile로 dispatch한다.

canary admission은 project opt-in, eligible low-risk classes, small bounded workload fraction/count, maximum spend, concurrency, abort signals, baseline fallback이 고정돼 있어야 한다. **fraction/count 값은 조직 정책에서 승인해야 하며 이 패키지는 임의의 운영 비율을 확정하지 않는다.** canary와 baseline이 같은 workspace에 동시에 write하지 않도록 별도 resources를 사용한다.

자동 abort: policy/security incident, unknown effect 발생, verifier tampering, persistent contract coverage drop, predetermined cost/reliability threshold 초과. 통계적으로 효과가 나빠도 사람이 기다리라고 할 수 있는 것과 무조건 abort할 safety event를 구분한다.

## 7. Promote 원자성

승인자는 exact candidate release digest, eval report digest, allowed target set, expiry를 bind한 PromotionGrant를 발급한다. ReleaseService가 현재 active release의 expected revision을 CAS하고 새 작업 admission pointer를 변경한다. 이미 실행 중인 Run은 원래 composition으로 끝내거나 명시적 checkpoint 전환을 따른다. running process의 prompt/pack 파일을 덮어써 바꾸지 않는다.

배포는 release set 단위이며 tool/pack/model/protocol 호환성을 함께 pin한다. mixed fleet은 capability negotiation으로 지원된 조합만 받는다. 부분 실패 시 기존 active version은 유지하거나 target별 quarantine하고 전체 성공으로 보고하지 않는다.

## 8. Rollback과 kill switch

rollback은 active composition pointer를 직전 검증 release로 돌리고 신규 admission을 차단한다. 완료된 외부 effect를 자동 취소하는 동작이 아니다. 활성 canary는 cancellation→effect reconciliation→resource cleanup을 거친다. schema/storage 호환성 문제가 생기면 db snapshot 복구·migration decision이 별도로 필요하다. release rollback과 data rollback을 같은 버튼으로 숨기지 않는다.

kill switch의 owner는 Authority/Operations이며 MetaProposer가 해제할 수 없다. source·config·package가 훼손된 경우 마지막 정상 release로 전환하되 revoked credential/승인이 되살아나지 않도록 최신 authority 상태를 사용한다.

## 9. Meta-loop 자신의 budget

root experiment budget 아래 후보 생성·judge·rerun·canary를 모두 계상한다. “평가를 더 하면 이길 수 있다”는 이유로 무제한 반복하지 않는다. 동일 hypothesis가 반복 실패하면 cooldown과 인간 검토를 요구한다. proposal 수/동시 experiments/holdout reuse cap은 정책으로 둔다. Harness evolution도 gate가 있는 bounded work다.

## 10. V3 완료 기준

최소 1개의 실제 안전한 개선 후보가 fake simulator가 아닌 V3 실행 pipeline을 통과하여 immutable experiment, baseline comparison, guarded canary, explicit promote, rollback drill 증거를 만들어야 한다. 효과가 없으면 **reject/inconclusive가 정확히 동작하는 것**도 필수 시험이다. 이번 ZIP은 이 기준의 설계이며 그 실험을 실행한 결과가 아니다.


---

# 17. Intent·질문·중간지시·운영 UX

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 사람에게 목표 작성 부담을 넘기지 않는다

사용자는 자연어로 의도를 말한다. 시스템은 확인된 프로젝트 사실을 바탕으로 목표·경계·검증 방법 초안을 만들고, 다음 세 가지를 읽기 쉬운 표현으로 보여준다: **무엇이 달라지는가 / 무엇을 건드리지 않는가 / 무엇을 보면 끝이라고 할 수 있는가**. 불확실한 부분은 조용히 채우지 않고 assumption label을 붙인다.

승인이 불필요한 tiny deterministic 변경은 정책이 허용하는 auto-accept contract 경로로 진행 가능하다. 자동 accept 여부는 기존 actor 권한·risk·불변조건으로 판단하고 '대답 없으면 동의'가 아니다. 비용·대상·production 접근·데이터 외부 전송은 추측으로 넓히지 않는다.

## 2. 질문 원장

Question은 question_id, goal/contract ref, issue kind, evidence already checked, options/tradeoffs, safe default(if any), blocks capabilities, assigned actor, asked_at, status를 가진다. 이미 답한 질문은 answer의 applicability revision을 확인하여 재사용한다. 질문 여러 개를 사용자에게 던지기 전에 repo/registry로 해결할 수 있는지 확인한다.

허용 kind: business_intent, authority, ambiguous_target, conflicting_constraint, subjective_direction, destructive_change. 파일에서 확인할 수 있는 경로·빌드 명령·기존 규칙은 discovery 대상이다. unresolved question과 관련 없는 read-only discovery는 계속할 수 있지만 해당 권한이 필요한 work를 시작하지 않는다. deadline은 expiry/escalation이지 승인이다.

## 3. Steering 상태

사용자 중간 요청은 envelope로 먼저 저장한다. `received → validated → queued → applied | rejected | superseded`와 `effective_contract_revision`을 별도로 둔다. provider ACK만 오면 “전달됨”이지 “변경 반영 완료”가 아니다. 중간지시는 cancel/pause/resume/priority_change/constraint_add/acceptance_change/new_evidence로 분류한다. [R05]

priority 변경은 authority/계약을 바꾸지 않으면 scheduler metadata만 바꾼다. acceptance/target/non-goals/required effects가 바뀌면 contract new revision + critic + graph replan이 필요하다. 새 계약이 활성화되면 old lease는 더 이상 새 effect를 시작할 수 없다. minor presentation preference도 artifact 재검증이 필요할 수 있으므로 subject digest를 다시 bind한다.

## 4. Cancel의 정확한 언어

UI 단계: “취소 요청 접수”→“새 작업 차단”→“실행 중 도구 종료 확인”→“외부 변경 결과 확인”→“취소 완료/확인 필요”. pending external effect가 있으면 '완전히 취소됨'으로 표시하지 않는다. child worker가 남거나 remote ACK가 없으면 `unknown_effect` 또는 `cancellation_pending`을 노출한다.

## 5. Hermes와 UI boundary

Hermes/Slack/Telegram은 IntakeAdapter + notification/projection이다. 사용자 identity가 app 계정과 연결됐는지 검증한 후 authority service에 command를 전달한다. conversation text에 들어있는 repo ID/role/approval token을 신뢰하지 않는다. webhook 재전송은 event idempotency로 제어한다. 메시지 링크는 status projection이고 원본 Decision/Evidence ref로 drill down한다.

초기 V3는 CLI + API + 읽기 가능한 status projection으로 충분하다. 별도 웹 대시보드가 Core Runtime 필수 의존성이 되지 않게 한다. 향후 UI가 생겨도 동일 command/query API를 사용한다. app-native install/update 버튼은 배포 승인 flow를 우회하지 않는다.

## 6. 상태 표현

사용자 기본 상태: 의도 확인 중 / 확인 필요 / 실행 대기 / 작업 중 / 검증 중 / 완료 / 중단 / 실패 / 외부 결과 확인 필요. 내부 leased/pending/outbox ACK를 그대로 제목으로 노출하지 않는다. 완료 화면은 achieved acceptance, remaining limitations, changed targets, evidence, next required approval를 보여준다. 단순 green score나 'AI 판단 완료' 대신 검증된 사실을 표시한다.


---

# 18. API·Command·Event Protocol

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 계약과 transport

V3 application protocol major는 3, schema release는 3.0.0으로 제안한다. endpoint prefix는 `/api/v3`. 기존 Platform API/CLI는 adapter로 공존하며 implicit reinterpretation하지 않는다. 내부 command는 API·CLI·Hermes에서 동일한 application service로 수렴한다. 외부 요청이 DB/파일을 직접 수정하지 못한다.

HTTP JSON은 UTF-8, strict JSONSchema2020-12 + semantic validation. schema의 `$id`는 offline registry key이며 runtime이 인터넷에서 임의 `$ref`를 받아오지 않는다. 구조화 생성용 provider subset schema와 normative schema를 구분하고 모델 출력은 **최종 normative validator를 반드시 거친다**. [R32]

## 2. 공통 header/조건

`Authorization`은 project/tenant binding을 가진 서버 검증 credential. 클라이언트 JSON의 actor 필드는 attribution일 뿐 실제 ActorContext를 대체하지 않는다. write commands는 `Idempotency-Key`, 변경 가능한 aggregate update는 `If-Match` row version을 사용한다. 서버가 contract digest와 policy epoch를 추가 확인한다. 요청 payload digest와 key binding을 보관한다.

모든 response는 request_id, server_time, schema_version, result 또는 typed error를 가진다. error는 code/message/retryable/retry_after/hold_reason/current_revision/safe_next_action을 제공하며 secret·raw provider prompt를 포함하지 않는다. error 문자열을 parsing해서 상태를 정하지 않는다.

## 3. Endpoint inventory

정본은 `contracts/api-catalog.json`. 경로의 `{goal_id}`도 auth scope 내부에서 조회한다. 다음은 핵심 그룹이다.

| route | 의미 | 성공 의미 |
|---|---|---|
| POST /intents | 의도 접수 | 202 persisted, 실행 승인 아님 |
| POST /intents/{id}/resolve | discovery job 요청 | resolution job 접수 |
| GET /goals/{id} | 현재 goal projection | scope 안의 상태 |
| POST /goals/{id}/contracts | frozen contract 후보 생성 | 생성됨, active 아님 |
| POST /goals/{id}/activate | current contract/graph 활성화 | gates 통과 후 admission 가능 |
| POST /goals/{id}/steering | 중간지시·pause/resume/cancel | ledger 접수; 실제 적용 별도 |
| POST /questions/{id}/answers | 인증된 answer | validation 및 contract 반영 별도 |
| POST /graphs/{id}/revisions | replan draft 등록 | semantic valid draft |
| POST /workers/claim | admission 통과 work claim | fenced lease, run envelope |
| POST /runs/{id}/heartbeat | lease 연장 | exact lease+fence 유효 시만 |
| POST /runs/{id}/events | worker 관측 event | 서버가 허용된 event만 채택 |
| POST /artifacts/init + commit | artifact ingest | registry가 hash/scan 검증 |
| POST /effects | broker effect 준비·요청 | receipt 상태, 완료와 구분 |
| POST /verifications | trusted verifier 실행 요청 | verification job |
| POST /meta/proposals | 후보 제안 | 권한/배포 부여 아님 |
| POST /meta/experiments | 승인된 eval freeze/start | experiment state |
| POST /releases/{id}/promote | exact grant 기반 promote | pointer CAS + rollout receipt |
| GET /events | resumable stream | projection; authoritative command ACK 아님 |

승인/ApplyGrant issuance는 기존 governance API의 adapter를 사용하며 worker-facing endpoint에 자기 grant 발급 기능을 넣지 않는다. 파일 ref는 local absolute path 대신 registry ID+digest이며 artifact content download도 scope/분류를 검증한다.

## 4. Event envelope와 ordering

필드: event_id, protocol_version, scope, aggregate_type/id, aggregate_seq, event_type, producer, causal_command_id, correlation_id, occurred_at, ingested_at, payload, payload_digest. aggregate_seq는 authority 있는 서버만 부여한다. worker가 `goal.verified`/`grant.issued` 같은 server-only event를 제출하면 reject한다.

worker events에는 lease_id, fencing_token, contract_digest, graph_digest, work_attempt가 있어야 한다. state transition은 payload 설명이 아니라 현재 aggregate 상태+guards로 수행한다. 알려지지 않은 event type은 quarantine/unsupported; 조용히 무시하고 success로 처리하지 않는다.

SSE는 `Last-Event-ID` 또는 scoped cursor로 재개한다. retention 밖 cursor는 `CURSOR_EXPIRED` + snapshot endpoint를 반환한다. 여러 aggregate event의 timestamp로 total order를 재구성하지 않는다. client projection이 gap을 발견하면 snapshot+after cursor로 복구한다. WebSocket은 adapter 선택사항이며 기본 runtime durability가 socket 연결에 달려 있지 않다.

## 5. 오류 코드와 재시도

400 VALIDATION_ERROR, 401 AUTHENTICATION_REQUIRED, 403 SCOPE_DENIED/CAPABILITY_DENIED, 404 scoped NOT_FOUND, 409 REVISION_CONFLICT/IDEMPOTENCY_CONFLICT/STALE_LEASE, 412 CONTRACT_STALE/GRANT_STALE, 422 GRAPH_INVALID/ACCEPTANCE_UNBOUND/AMBIGUOUS_TARGET, 429 BUDGET_EXHAUSTED/RATE_LIMITED, 503 AUTHORITY_UNAVAILABLE/PROVIDER_UNAVAILABLE. budget exhausted는 시간이 지나면 자동 해결되는 rate limit과 구분한다.

unknown external outcome은 HTTP retryable=true로 노출하지 않는다. 먼저 `GET effect receipt` 또는 reconcile command를 요구한다. 202/204나 queue ACK는 verified를 뜻하지 않는다.

## 6. 버전 변경

schema minor 확장은 optional field일 때만 호환일 수 있다. strict consumers에는 허용된 version negotiation/adapter가 필요하며 모르는 필드를 무조건 받지 않는다. protocol major unsupported는 handshake 단계에서 fail closed다. breaking state semantics는 migration+conformance+release matrix를 요구한다. vendor protocol은 AMPLAI protocol과 version을 따로 기록한다.


---

# 19. 전면 V3 전환·삭제·롤백

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 단일 V3 목표, 안전한 전환 순서

V3 전체를 이번에 설계하고 다음 구현에서 통합한다. 그렇더라도 권한 원장·실행 상태·앱 설치를 동시에 덮어쓰는 방식은 금지한다. implementation wave는 V3 내부 의존 순서일 뿐 meta-harness/knowledge/visual verification을 다음 버전으로 미루는 범위 축소가 아니다.

| wave | 내용 | 통과 증거 |
|---|---|---|
| M0 | baseline freeze·inventory·기존 regression 확인 | tar/git digest, release facts, migration rehearsal plan |
| M1 | contracts/invariants/gates/storage/authority foundation | schema+state+fault+security tests |
| M2 | goal/context/compiler/runtime/drivers/effect/eval core | end-to-end intent→verified on isolated repo |
| M3 | packs·cross-app·steering·federation·meta evolution | conformance + shadow/canary/rollback drills |
| M4 | app installer·data migration·fleet staging | before/after digests, approval preservation, compatibility |
| M5 | authorized cutover·legacy retirement | final regression, restore, no live refs, deletion grants |

## 2. 기존 데이터 처리

V2 Work는 raw payload+schema version+source digest를 먼저 보존한다. active V2 run은 drain 또는 명시적 cancel/reconcile하고 lease를 V3에 그대로 옮겨 유효하게 만들지 않는다. 완료된 V2 evidence는 `legacy_imported` trust/source 상태로 보관하며 새로운 V3 trusted verdict로 재명명하지 않는다.

기존 approved Decision/ApplyGrant는 그 당시 scope/semantics를 유지한다. 새로운 action·contract binding이 필요하면 기존 승인자가 V3 grant를 새로 발급해야 한다. UUID 문자열을 복사해 권한을 확대하는 migration은 실패다. legacy approvals가 유효한지 판단하는 기존 gate를 대체 구현으로 port하기 전 삭제하지 않는다.

## 3. 코드 재배치

`migration/component-map.csv`와 `migration/source-disposition.csv`를 따르되 disposition은 **설계상 권장 조치이며 실행할 delete list가 아니다**. scripts runtime의 durable Work/lease/scheduler semantics는 Platform module로 옮기고 portable CLI는 service adapter로 얇게 만든다. local isolated mode가 필요한 Kit도 동일 Runtime port를 사용해야 하며 두 벌 state machine을 유지하지 않는다.

legacy_*는 이름 기준 폐기 금지. 실제 import/export/registry/CLI/DB behavior를 추적하고 guard equivalence·negative regression을 검증한다. 기존 shim은 호출 telemetry로 미사용을 확인한 후 sunset한다. canonical paths를 바꿀 때 existing refs/ID는 alias+tombstone으로 보존한다.

## 4. 삭제 승인 절차

삭제 후보마다 path + old digest + owner + reason + replacement + live refs + backup ref + required test IDs를 기록한다. path glob만으로 대량 삭제하지 않는다. computed generated surface와 user-authored file은 구분한다. expected digest가 달라졌으면 사용자의 변경으로 보고 conflict HOLD다.

동일 내용의 upstream payload와 installed mirror를 '중복'이라는 이유로 한쪽 삭제하기 전에 배포 생성 경로를 확인한다. required:false handoff marker absence는 오류로 단정하지 않는다. spec/evidence archive는 hash preserved move→backlink rewrite→reference scan→GC approval 순서다.

## 5. 릴리스·설치 receipt

ReleaseSet에는 Platform/Kit/protocol/schema/pack/driver qualification/OS matrix/migration version/digests를 고정한다. Foundry와 Kit 버전은 각각 독립이며 숫자가 같다고 release 상태가 같은 것은 아니다. 현재 archive의 README candidate2.5.0와 VERSION2.4.0 차이는 구현 전 release truth inventory에서 해소해야 한다.

installer는 plan→dry-run→policy/authority gate→backup→atomic supported steps→verify→receipt를 수행한다. 여러 app은 install receipt별 성공/실패가 나뉜다. 일부만 성공하면 부분 완료로 보고하고 전체 성공으로 표시하지 않는다. app-local overrides는 명확한 overlay 경로로 보존한다.

## 6. 롤백 경계

코드 rollback은 이전 signed release 재설치, runtime rollback은 checkpoint/DB schema 지원 여부, canonical rollback은 새 governed revert commit, 권한 rollback은 **최신 revocation 유지**다. 이전 DB backup으로 권한 취소를 되살려서는 안 된다. 외부 side effect는 compensation/reconciliation 절차이며 일반 release rollback과 별개다.

V3가 새로운 data를 기록한 뒤 V2가 읽을 수 없다면 자동 downgrade 금지다. drain→snapshot→explicit migration rollback→verify→admission resume 절차가 필요하다. 가역하지 않은 변환은 dry-run 보고서에 명시하고 원본을 보존한다.


---

# 20. 운영·배포·호환성·장애 대응

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 배치 profile

`local-dev`: 단일 사용자 CP+격리 worker, explicit project policy. `onprem`: 사내 CP+로컬 CAS+인증된 worker, 외부 egress 제한. `hybrid`: 정책 허용된 데이터에만 cloud provider route. `offline`: pinned packages/local model/캐시된 canonical read, 신규 권한·외부 확인 필요한 write HOLD. `legacy-app-client`: RHEL7/Python3.6 등 앱의 얇은 접속·설치 bootstrap; 현대 CP 전체를 old host에 이식하지 않는다.

CP Python >=3.11은 첨부 Platform의 현재 기준과 맞춘다. dependencies는 lockfile+hash+reproducible build로 pin한다. Python3.6용 기존 Kit 경계를 바꾸려면 host inventory와 사용 owner 승인 후 별도 support matrix에 표시한다. 코드가 Python3.11에서 동작했다는 사실로 RHEL7 support를 주장하지 않는다.

## 2. 운영 설정의 확정과 미확정

기술 baseline defaults는 `contracts/runtime-defaults.json`에 둔다. production authority endpoint, signing identity, approved egress, tenant admins, currency budget, retention/legal constraints, provider account availability는 배포자가 설정해야 하는 **activation prerequisites**다. placeholder가 남아도 read-only design/validate는 가능하지만 write worker enable은 fail closed다. 이 설계가 현재 사내 접근권한을 확인했다고 주장하지 않는다.

## 3. 시작 순서

검증된 release→config/schema checksum→authority reachability/revocation sync→store schema/version→recovery scan→driver probes/qualification→pack verification→scheduler enable→worker admission 순서다. authority unavailable 또는 unresolved unknown effects가 있는 project는 read-only status 제공만 허용할 수 있다. webhook/API listen 전에 auth config를 검증한다.

## 4. 상태·알림

health는 liveness/readiness/authority-ready/scheduler-ready/artifact-ready/driver-available로 분리한다. 프로세스가 살아 있다고 write ready가 아니다. 주요 경보는 outbox backlog, heartbeat loss, unresolved effect age, authority sync gap, disk/CAS pressure, budget overrun reservation mismatch, schema drift, invalid signature, cross-scope deny surge다.

log는 structured JSON, bounded size, secret redaction, correlation ID를 사용한다. event store와 log rotate는 다른 보존 정책이다. default debug logging에 raw prompts/DB tokens를 넣지 않는다. incident bundle export는 scoped/sanitized manifest를 생성한다.

## 5. 장애 runbook

| 장애 | 즉시 조치 | 재개 조건 |
|---|---|---|
| CP crash | owner epoch 교체·lease invalidate·outbox/inbox reconcile | pending effects 분류, duplicate-safe dispatch |
| authority down | 신규 write/effect HOLD | authoritative sync와 expiry 재검증 |
| worker lost | lease 만료·resource quarantine | process/external effects 종료 또는 unknown 기록 |
| CAS missing/corrupt | 관련 goal verify 차단 | backup restore와 digest 일치 |
| provider rate limit | bounded jitter·queue backpressure | budget/time window 내 retry |
| leaked credential | revoke/kill switch·audit | 새 scoped token+원인 격리 |
| disk full | stop admission·protect authority/event consistency | space+integrity check 완료 |
| signing key revoke | 신규 installs/promotions 차단 | 신뢰 가능한 새 release chain 검증 |

SLO는 workload baseline을 수집해 정한다. 임의 99.99% 약속을 하지 않는다. 초기 acceptance는 fault 시 권한·증거·재시도 정합성이 깨지지 않는지와 bounded recovery를 시험하는 것이다.

## 6. Supply chain

release package에는 source commit, build tools, dependency lock hashes, SBOM, test/qualification refs, artifact digests, publisher signature/attestation, migration compatibility를 포함한다. SLSA의 provenance 관점을 활용하되 구현하지 않은 level을 달성했다고 표시하지 않는다. [R36]

pack/driver는 latest auto-update가 아니라 staged qualification→release set pin→authorized rollout이다. provider 문서/SDK 변화 감시는 Research/Operations job으로 제안될 수 있으나 승인 없는 시스템 변경을 수행하지 않는다. 도구 접근권한이 없는 미래 자동화를 이 설계만으로 활성화했다고 주장하지 않는다.


---

# 21. 구현 검증·최종 인수 기준

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 설계 검수와 제품 검증은 다르다

이번 ZIP의 JSON parse/schema/fixture/link/task-DAG 검사는 **설계 패키지가 읽히고 서로 참조되는지**를 확인한다. 제품 unit test, actual provider execution, DB crash recovery, production integration을 실행한 것이 아니다. 모든 `eval/test-catalog.json` 사례는 `specified_not_executed` 상태다. 다음 구현자는 실제 commit·환경·결과·artifact refs를 붙인 후에만 상태를 변경한다.

## 2. 검증 피라미드

| 단계 | 대상 | 요구 증거 |
|---|---|---|
| Contract conformance | schema, typed DTO, semantic refs, JCS | valid/invalid/semantic-negative fixtures + mapping parity |
| Unit/property | transitions, policy intersection, DAG compiler, budgets | 모든 edge/guard와 negative generated cases |
| Fault integration | DB/outbox/inbox/CAS/lease/effect | 각 crash point 전후 state+event+receipt 비교 |
| Driver conformance | exact binary/API/OS/model/sandbox | session/async/steering/cancel/usage/errors의 실제 결과 |
| Product E2E | intent→goal→graph→worker→artifact→verdict | current contract coverage, authentic evidence chain |
| Domain/visual | cross-app compatibility, docs/frontend | 실제 최종 렌더링+기능+사용자 rubric 결과 |
| Meta lifecycle | experiment/shadow/canary/promote/rollback | freeze/holdout/grant/pointer/rollback receipts |
| Migration/operations | active run drain, imports, old client, restore | before/after hashes, no authority upgrade, proven restore |

## 3. V3 필수 대표 인수

(A) target hint 없는 rough intent가 실제 registry와 repo facts를 통해 검증 가능한 계약으로 만들어진다. (B) `/design`은 코드 구현을 dispatch하지 않는다. (C) 서로 다른 두 테스트 앱을 병렬 작업하고 합친 뒤 integration criterion으로 완료를 판정한다. (D) 실행 중 요구 변경에서 old worker result/effect가 current goal을 오염시키지 않는다. (E) timeout 외부 write가 UNKNOWN으로 가고 reconcile 없이 반복되지 않는다. (F) protected policy/평가 데이터는 builder/meta가 통과를 위해 수정할 수 없다. (G) 메타하네스의 pass뿐 아니라 fail/inconclusive/reject/abort/rollback 경로가 동작한다. (H) Kit 제거 후 domain build와 기존 Foundry governance regression이 통과한다.

## 4. 테스트 결과 레코드

필드: test_id, requirement_ids, implementation_commit, release_set_ref, environment_ref, started/finished, status(pass/fail/inconclusive/not_run), actual result artifact, expected assertion details, deviations, reviewer. 테스트 파일 존재·TODO check-box·모델의 완료 설명만으로 pass로 표시하지 않는다. negative case는 실제 denied effect·unchanged state까지 확인해야 한다.

fake driver는 state machine unit test에 유효하지만 실제 Claude/Codex provider conformance를 대체하지 않는다. simulator는 external effect semantics 시험에 유효하지만 사내 API contract integration 검증은 별도로 남는다. unavailable environment는 skip 사유와 support limitation에 기록한다.

## 5. Release blockers

unresolved authority/security gate, unclassified external effect, missing mandatory acceptance, changed evaluator without independent baseline, broken old governance guard, unowned file overwrite, no rollback proof, counterfeit evidence, current-contract hash mismatch는 blocker다. optional driver가 unqualified이면 해당 profile disabled로 출시 범위를 표시할 수 있지만 baseline driver/runtime/meta 핵심은 stub으로 닫지 않는다.

'모든 기능을 설계'한 목표는 full V3 implementation에서 추적한다. 특정 구현 항목을 빼려면 명시적 scope-change Decision이 필요하고 이름만 바꾸어 done 처리하지 않는다.


---

# 22. 누락 방지 추적표

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님

각 요구는 문서·schema·gate·test·task에 연결된다. status는 모두 설계됨/미구현이다. 정본은 `implementation/requirements-traceability.json`.
| 요구 | 설계 | 구현 작업 |
|---|---|---|
| REQ-01 전체 V3 단일 목표와 design-only 경계 | `design/01_SCOPE_AND_DECISIONS.md` | V3-001, V3-016, V3-061 |
| REQ-02 Foundry/Kit 독립과 기존 governance 보존 | `design/02_ARCHITECTURE.md` | V3-002, V3-008, V3-050, V3-051 |
| REQ-03 자연어 의도에서 검증된 target·goal 구성 | `design/05_GOAL_RESOLVER_CONTRACT.md` | V3-011, V3-015, V3-016 |
| REQ-04 질문·가정·부족한 목표 자동 정리 | `design/17_STEERING_HUMAN_UX.md` | V3-013, V3-014, V3-015 |
| REQ-05 불변조건·강제 Gate | `design/03_INVARIANT_REGISTRY.md` | V3-003, V3-004, V3-005, V3-056 |
| REQ-06 기존 DAG 승격·typed WorkGraph | `design/06_WORKGRAPH_REPLAN.md` | V3-017, V3-031 |
| REQ-07 adaptive direct/loop/deliberative/discovery | `design/07_RUNTIME_SCHEDULER.md` | V3-018, V3-019, V3-020, V3-021 |
| REQ-08 lease/fence/crash/cancel 정합성 | `design/08_STATE_RECOVERY.md` | V3-020, V3-023, V3-030, V3-055 |
| REQ-09 transaction/outbox/inbox/idempotency | `design/09_STORAGE_TRANSACTIONS.md` | V3-006, V3-007, V3-010 |
| REQ-10 authority/capability/secret/containment | `design/10_AUTHORITY_SECURITY.md` | V3-008, V3-022, V3-023, V3-056 |
| REQ-11 driver/model/session/sandbox 분리 | `design/11_DRIVERS_SESSIONS.md` | V3-024, V3-025, V3-026 |
| REQ-12 Astra async/steering와 선택 API transport | `design/11_DRIVERS_SESSIONS.md` | V3-027, V3-028, V3-029, V3-030 |
| REQ-13 Knowledge Readiness/context/provenance | `design/12_CONTEXT_KNOWLEDGE_ONTOLOGY.md` | V3-012, V3-013, V3-014 |
| REQ-14 Ontology/KnowledgeGraph vs WorkGraph 분리 | `design/12_CONTEXT_KNOWLEDGE_ONTOLOGY.md` | V3-012, V3-017, V3-039 |
| REQ-15 public work/design와 SpecKit 내부화 | `design/13_CAPABILITY_PACKS.md` | V3-034, V3-035, V3-036 |
| REQ-16 frontend UX/UI/visual render 품질 | `design/14_VERIFICATION_VISUAL_QA.md` | V3-037, V3-059 |
| REQ-17 documents/SVG/fonts/golden lifecycle | `design/14_VERIFICATION_VISUAL_QA.md` | V3-038, V3-059 |
| REQ-18 trusted evidence/verdict/global verify | `design/14_VERIFICATION_VISUAL_QA.md` | V3-009, V3-032, V3-033 |
| REQ-19 Observatory를 V3 초기부터 구축 | `design/15_EVAL_OBSERVATORY.md` | V3-010, V3-040 |
| REQ-20 dataset/holdout/실험 품질 | `design/15_EVAL_OBSERVATORY.md` | V3-041, V3-042, V3-043 |
| REQ-21 Meta federation/composition | `design/16_META_HARNESS.md` | V3-044 |
| REQ-22 Meta hypothesis/diff/protected surfaces | `design/16_META_HARNESS.md` | V3-045, V3-056 |
| REQ-23 Replay/shadow/canary/promote/rollback | `design/16_META_HARNESS.md` | V3-042, V3-043, V3-046, V3-047, V3-058 |
| REQ-24 중간 요구 변경·append-only revision | `design/17_STEERING_HUMAN_UX.md` | V3-030, V3-031, V3-049 |
| REQ-25 API/command/event/SSE | `design/18_API_EVENT_PROTOCOL.md` | V3-010, V3-048 |
| REQ-26 source 기반 삭제·경량화·이전 | `design/19_DISTRIBUTION_MIGRATION.md` | V3-001, V3-050, V3-051, V3-052, V3-060 |
| REQ-27 기존 legacy·승인·증거 보호 | `design/19_DISTRIBUTION_MIGRATION.md` | V3-002, V3-051, V3-060 |
| REQ-28 배포/오프라인/RHEL 기존 client 경계 | `design/20_OPERATIONS_RELEASE.md` | V3-050, V3-054, V3-055, V3-062 |
| REQ-29 검증가능한 구현 백로그·인수 기준 | `design/21_TEST_ACCEPTANCE.md` | V3-056, V3-057, V3-058, V3-059, V3-061 |
| REQ-30 app registry와 non-destructive installer | `design/13_CAPABILITY_PACKS.md` | V3-011, V3-034, V3-050 |
| REQ-31 Hermes가 authority가 아닌 intake/projection | `design/17_STEERING_HUMAN_UX.md` | V3-049 |
| REQ-32 tool effect unknown/idempotency/compensation | `design/32_TOOL_EFFECT_PROTOCOL.md` | V3-022, V3-023, V3-055 |
| REQ-33 통계적 불확실성/효과 과장 방지 | `design/30_EXPERIMENT_STATISTICS.md` | V3-041, V3-042, V3-043 |
| REQ-34 정본·hash·참조 cycle 방지 | `design/33_IDENTITY_REFERENCE_LIFECYCLE.md` | V3-003, V3-004, V3-009 |
| REQ-35 공식 최신자료와 검증된 선택/제외 | `design/01_SCOPE_AND_DECISIONS.md` | V3-001, V3-024, V3-053, V3-061 |


---

# 23. 다음 구현 작업 백로그

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님

정본은 `implementation/tasks.yaml`. 아래 순서는 의존 DAG이며 같은 wave 안에서도 depends_on을 따른다. wave 숫자는 새 제품 버전이 아니다. **62개 작업의 구현을 이미 수행한 것이 아니다.** 각 작업의 산출물·테스트·설계 근거를 해당 YAML에 명시했다.

| 작업 | Wave / Owner | 선행 | 산출물 |
|---|---|---|
| V3-001 Baseline freeze / version truth / original regressions | 0 / Migration Lead | — | Archive+git inventory; candidate/release/support facts; current test report incl failures |
| V3-002 Governance importer map and invariant parity | 0 / Authority Engineer | V3-001 | legacy call graph; ApplyGrant guard parity matrix; protected surfaces manifest |
| V3-003 Normative schema registry and offline JSON validation | 1 / Contract Engineer | V3-001 | 2020-12 registry; provider subset mapping; unknown-field rejection |
| V3-004 Canonical identity/hash/ref resolver | 1 / Contract Engineer | V3-003 | JCS conformance; scope/ref digest validator; acyclic definition ownership |
| V3-005 Gate Engine and semantic validator registry | 1 / Runtime Engineer | V3-002, V3-003, V3-004 | all invariant/gate IDs enforced; strategy applicability; unknown HOLD |
| V3-006 Runtime logical schema migrations | 1 / Storage Engineer | V3-003, V3-004 | scoped tables; FK/CAS/indexes; schema-version admission; local SQLite configuration |
| V3-007 Unit of work, idempotency, inbox/outbox | 1 / Storage Engineer | V3-006 | transactional state/event/outbox; dedupe/conflict; bounded dispatch journal |
| V3-008 Authority adapter and project ActorContext | 1 / Authority Engineer | V3-002, V3-005, V3-007 | current grant evaluate/consume/revoke; no LLM issuer; scope-safe ingress |
| V3-009 Immutable CAS and artifact registry | 1 / Storage Engineer | V3-004, V3-006 | safe staged upload; digest/media/size/secret checks; orphan recovery |
| V3-010 Event/RunRecord spine and scoped projections | 1 / Observability Engineer | V3-007, V3-009 | normalized events; cursor/gap recovery; no server event forgery |
| V3-011 AppBinding registry adapter | 2 / Knowledge Engineer | V3-005, V3-008 | verified aliases/repos/owners; allowlisted environments; scope ambiguity HOLD |
| V3-012 Foundry read/context knowledge adapters | 2 / Knowledge Engineer | V3-002, V3-004, V3-008 | 8 kinds preserved; canonical/decision/work/evidence views; governance protected |
| V3-013 Readiness and context completeness validator | 2 / Knowledge Engineer | V3-011, V3-012 | 8 unique areas; current invariants; contradiction HOLD; progressive core/map |
| V3-014 Repo facts resolver and source provenance | 2 / Knowledge Engineer | V3-011, V3-012, V3-013 | scoped file/symbol search; source versioning; no external implicit authority |
| V3-015 Goal Resolver and question ledger | 2 / Goal Engineer | V3-005, V3-011, V3-013, V3-014 | rough intent resolution; origin-tagged assumptions; question reuse; target verification |
| V3-016 Contract compiler/critic with verifiable acceptance | 2 / Goal Engineer | V3-003, V3-005, V3-015 | criterion/verifier binding; bounded critic; design-only contract; revision freeze |
| V3-017 WorkGraph compiler and typed dataflow | 2 / Graph Engineer | V3-004, V3-005, V3-011, V3-016 | DAG/joins/types/coverage; frozen compiler identity; stable ordering |
| V3-018 State machine implementation from registry | 2 / Runtime Engineer | V3-005, V3-006, V3-007 | all legal transitions/guards; CAS; new Run per repair; terminal history |
| V3-019 Budget reservations and shared resource claims | 2 / Runtime Engineer | V3-007, V3-018 | root accounting; unknown usage bounds; exclusive repo writes; native child budget |
| V3-020 Lease/fencing/owner epoch recovery | 2 / Runtime Engineer | V3-007, V3-018, V3-019 | monotonic fencing; expiry; heartbeat; atomic claim dispatch |
| V3-021 Adaptive strategy and fair scheduler | 2 / Runtime Engineer | V3-016, V3-017, V3-018, V3-019, V3-020 | direct/loop/deliberative/discovery selection; gates never bypass; queue fairness |
| V3-022 Sandbox/Secret/Tool ports and containment | 2 / Security Engineer | V3-008, V3-009, V3-019 | UID/worktree/egress bounds; credential handle broker; data policy |
| V3-023 Effect prepare/dispatch/reconcile protocol | 2 / Runtime Engineer | V3-007, V3-008, V3-020, V3-022 | effect-key binding; authority consumption; UNKNOWN reconciliation; compensations |
| V3-024 Driver interface, model profiles, qualification registry | 2 / Driver Engineer | V3-003, V3-005, V3-010, V3-022 | declared/observed/qualified/granted separation; version exact profiles |
| V3-025 ClaudeCode exact-session driver | 2 / Driver Engineer | V3-020, V3-023, V3-024 | typed decoder; exact resume; process-tree cancel; usage unknown handling |
| V3-026 Codex CLI qualified baseline driver | 2 / Driver Engineer | V3-020, V3-023, V3-024 | JSONL parser; exact session; allowed sandbox; stop/recover conformance |
| V3-027 OpenCode authenticated optional driver | 3 / Driver Engineer | V3-020, V3-023, V3-024 | HTTP/SSE auth; 204 accepted not done; event recovery; permissions |
| V3-028 Responses async/steering optional driver | 3 / Driver Engineer | V3-020, V3-023, V3-024 | call_id ledger; pending tools; WS steer reconcile; continuation budget |
| V3-029 Managed/API experimental driver boundaries | 3 / Driver Engineer | V3-024, V3-028 | disabled-by-default adapters; capability manifests; qualification/unsupported outcomes |
| V3-030 Steering/checkpoint/pause/cancel orchestration | 3 / Runtime Engineer | V3-015, V3-017, V3-020, V3-023, V3-025, V3-026 | durable steering states; native/fallback; effect-aware cancellation; checkpoint restart |
| V3-031 Replan activation and stale-output invalidation | 3 / Graph Engineer | V3-017, V3-020, V3-023, V3-030 | CAS activation; old fence invalidation; conservative artifact reuse |
| V3-032 Trusted verifier runner and evidence attestation | 2 / Verification Engineer | V3-008, V3-009, V3-018, V3-022, V3-023 | fixed verifier profiles; artifact-bound evidence; test/golden guards |
| V3-033 Global acceptance and doc freshness | 3 / Verification Engineer | V3-016, V3-017, V3-021, V3-032 | integration coverage; no pending effects/questions; optional waiver semantics |
| V3-034 Versioned capability pack registry | 3 / Pack Engineer | V3-003, V3-005, V3-008, V3-024, V3-032 | signed pack/dependency/permission/schema registry; app-independent install boundary |
| V3-035 Core work/design host surface and SpecKit internalization | 3 / Pack Engineer | V3-015, V3-016, V3-021, V3-034 | two public entries; embedded question/plan capabilities; grill/eli12 retirement map |
| V3-036 Software/review/debug capability pack | 3 / Pack Engineer | V3-032, V3-034 | build/test/ABI profiles; independent review; failure diagnosis outputs |
| V3-037 Frontend functional/visual/UX pack | 3 / Visual QA Engineer | V3-009, V3-032, V3-034 | pinned browser renders; viewport/interaction/a11y checks; human direction/golden governance |
| V3-038 Document lifecycle/render/golden pack | 3 / Visual QA Engineer | V3-009, V3-032, V3-034 | extract existing docs lifecycle; SVG/fonts/final render checks; sanitized golden ingestion |
| V3-039 Research/ontology/domain pack adapters | 3 / Knowledge Engineer | V3-012, V3-013, V3-014, V3-034 | source-date/provenance; optional SHACL adapter; DC domain context not core coupling |
| V3-040 RunRecord metrics and OTel projection | 2 / Observability Engineer | V3-010, V3-019, V3-024, V3-032 | task-class metrics; usage unknown states; versioned OTel mapping; bounded export spool |
| V3-041 Sanitized corpus and protected holdout registry | 3 / Evaluation Engineer | V3-009, V3-012, V3-032, V3-040 | dev/validation/holdout roles; source/privacy/contamination lineage; protected access |
| V3-042 Frozen experiment runner and replay/rerun separation | 3 / Evaluation Engineer | V3-021, V3-023, V3-032, V3-040, V3-041 | baseline/candidate manifests; simulated effects; paired run recording; budget freeze |
| V3-043 Analysis/report criteria and uncertainty | 3 / Evaluation Engineer | V3-041, V3-042 | predeclared analysis; inconclusive paths; drift and selection-bias reporting |
| V3-044 Composition federation and routing policies | 3 / Meta-Harness Engineer | V3-021, V3-024, V3-034, V3-040 | pinned model/driver/packs/policy selection; data/risk filters; native capability limits |
| V3-045 Harness proposal screening and protected surfaces | 3 / Meta-Harness Engineer | V3-002, V3-005, V3-041, V3-043, V3-044 | hypothesis/diff/risk/rollback proposals; A/B/C classes; no self approval |
| V3-046 Shadow/canary orchestration and kill switch | 3 / Meta-Harness Engineer | V3-023, V3-030, V3-042, V3-043, V3-045 | eligible opt-in traffic; separate sandbox; stop rules; budget; baseline fallback |
| V3-047 ReleaseSet signing and promotion/rollback | 3 / Release Engineer | V3-008, V3-034, V3-043, V3-045, V3-046 | signed component set; report-bound grant; CAS active pointer; in-flight pin; rollback receipts |
| V3-048 Public command DTO/API and event streaming | 3 / API Engineer | V3-007, V3-008, V3-010, V3-015, V3-021, V3-023, V3-030, V3-033, V3-045 | complete OpenAPI from contracts; server-owned fields rejected; scoped SSE/snapshot; error mapping |
| V3-049 Hermes intake/status/approval adapter | 3 / Integration Engineer | V3-008, V3-010, V3-015, V3-030, V3-048 | authenticated identity mapping; deduped intake; read projection; approval via authority only |
| V3-050 Kit installer v3 plan/dry-run/apply/receipt | 4 / Distribution Engineer | V3-001, V3-002, V3-034, V3-035, V3-047 | owned-path compare; optional markers; signatures; override preservation; rollback receipt |
| V3-051 Runtime/contract V2 import without authority upgrade | 4 / Migration Engineer | V3-001, V3-002, V3-003, V3-006, V3-008, V3-009, V3-018, V3-023 | raw original/digest preserved; drain/reconcile active; legacy evidence trust; new grant required |
| V3-052 Evidence hot/cold archive and safe gardening | 4 / Migration Engineer | V3-009, V3-012, V3-013, V3-038, V3-050 | live-ref index; hash-preserving move; aliases/tombstones; report-only default |
| V3-053 Roadmap/manifest/skill truth reconciliation | 4 / Integration Lead | V3-001, V3-035, V3-047, V3-050, V3-051, V3-052 | current implementation/candidate/qualified/deployed states; registry consistency CI |
| V3-054 Local/onprem/hybrid/offline/legacy profiles | 4 / Operations Engineer | V3-008, V3-022, V3-024, V3-040, V3-047, V3-050 | activation prerequisites; no cloud fallback; modern CP vs old client tested matrix |
| V3-055 Backup/restore and effect recovery drills | 4 / Operations Engineer | V3-006, V3-007, V3-008, V3-009, V3-020, V3-023, V3-047, V3-051, V3-054 | consistent DB/CAS backup; new epoch; authority revocation sync; restore evidence |
| V3-056 Security/adversarial and property conformance suite | 4 / Security Reviewer | V3-005, V3-008, V3-022, V3-023, V3-032, V3-041, V3-045, V3-048, V3-054 | all gate branches; IDOR/injection/supplychain/eval abuse; state/fault/property tests |
| V3-057 Cross-app end-to-end goal/steer/replan demo | 4 / Integration Engineer | V3-011, V3-015, V3-016, V3-017, V3-021, V3-025, V3-026, V3-030, V3-031, V3-033, V3-048, V3-054 | rough intent→two sandbox apps→integration evidence; mid-steer old worker fencing |
| V3-058 Complete meta evolution promote/reject/rollback drill | 4 / Meta-Harness Reviewer | V3-041, V3-042, V3-043, V3-044, V3-045, V3-046, V3-047, V3-055, V3-056 | real bounded test pipeline with pass/fail/inconclusive; independent grant; kill/rollback evidence |
| V3-059 Frontend/docs actual-render acceptance suite | 4 / Visual QA Reviewer | V3-037, V3-038, V3-048, V3-054, V3-056 | all final pages/screens rendered and inspected; golden protected; reproducible artifacts |
| V3-060 Legacy retirement candidates and exact deletion proposal | 5 / Migration Lead | V3-002, V3-050, V3-051, V3-052, V3-053, V3-055, V3-056, V3-057, V3-058, V3-059 | call/ref zero or compat adapter; approved digest list; rollback proven; no automatic delete |
| V3-061 Final V3 conformance and requirement evidence closure | 5 / Release Reviewer | V3-053, V3-054, V3-055, V3-056, V3-057, V3-058, V3-059, V3-060 | every REQ/task/test evidence tied; remaining blockers explicit; no design check counted as runtime pass |
| V3-062 Authorized cutover and independent release receipts | 5 / Release Operator | V3-047, V3-049, V3-050, V3-051, V3-054, V3-055, V3-061 | human approved release set; target-by-target status; safe rollback; Platform/Kit separate version truth |


---

# 24. Architecture Decision Records

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


다음 결정은 이번 설계안의 선택이다. 공식 문서가 이 모든 선택을 명령한다고 해석하지 않는다. 실제 deployment constraint가 다르면 ADR revision으로 근거·영향·회귀·승인 범위를 명시한다.

| ADR | 결정과 이유 | 검토 후 제외한 대안 / 비용 |
|---|---|---|
| ADR-01 | Intent→Verified Work를 V3 목표로 정의 | Graph가 Loop의 세대교체라는 branding 제외; topology보다 목표/검증이 우선 |
| ADR-02 | 기존 `amplai_foundry` package와 governance 유지 | 전면 rename은 호환성 비용만 늘림; runtime submodules는 새로 구성 |
| ADR-03 | modular monolith CP + isolated workers | microservices/K8s/Temporal 강제 제외; 추후 요구와 conformance 시 확장 |
| ADR-04 | Macro DAG, micro bounded loop | arbitrary cyclic macro graph는 종료/복구 복잡성; 탐색은 node loop/새 graph revision |
| ADR-05 | frozen contract+typed artifact-bound verification | free-text goal만으로 completion 판단 제외 |
| ADR-06 | hard invariant gates + adaptive strategies | 모든 작업에 긴 pipeline 강제 제외; tiny도 안전 검증은 유지 |
| ADR-07 | user hint optional, verified registry target mandatory | target_app_hint requirement 삭제를 target auth 삭제로 오해하지 않음 |
| ADR-08 | single active CP/local SQLite + outbox/inbox | NAS SQLite 또는 crossDB exactly-once 주장 제외; 단일 노드 한계 공개 |
| ADR-09 | authority ledger와 canonical content/runtime truth 분리 | Git만으로 live revocation/lease 관리하거나 SQLite가 canonical memory 정책을 몰래 대체하지 않음 |
| ADR-10 | capability intersection + brokered effects | prompt-only 권한·무조건 승인 UI 의존 제외 |
| ADR-11 | one Run per scheduled Work attempt | 실패 이력을 덮어쓰거나 재시도를 완료로 감추지 않음 |
| ADR-12 | native async/steering 사용하되 durable ledger 유지 | provider ACK를 applied/cancelled로 오해하지 않음 |
| ADR-13 | model과 driver 분리 | AstraDriver 제거; 모델 교체가 transport 재구현이 되지 않게 함 |
| ADR-14 | qualified CLI baseline, experimental app-server opt-in | 현재 문서 경고가 있는 API를 production 필수로 pin하지 않음 [R22] |
| ADR-15 | two public commands + signed capability packs | public skill proliferation/monolithic document renderer core 제외 |
| ADR-16 | progressive context + mandatory governing core | 모든 문서 prompt dump 또는 pointer만 주고 필수 규칙 누락 제외 [R06] |
| ADR-17 | ontology port와 기존 8 kinds 유지 | graph DB/vector DB 의무화·네 가지 memory DB 강제 이관 제외 |
| ADR-18 | observed action/usage/evidence trace, no private CoT | 비용 큰 원시 대화 무제한 저장 및 민감 reasoning 수집 제외 |
| ADR-19 | Observatory first, Meta evolution gated | 개선을 측정할 baseline 없이 자동 자기변경 활성화 제외 |
| ADR-20 | Federation와 Evolution을 별도 service로 | 만능 supervisor agent가 권한·평가·배포 모두 수행하는 구조 제외 |
| ADR-21 | independent eval/holdout + explicit inconclusive | 임의 점수/표본수로 자동 promote, benchmark leakage 제외 |
| ADR-22 | render-based docs/UI QA + functional/human rubric | source 문법 통과나 screenshot diff만으로 제품 미감/UX 보증 제외 |
| ADR-23 | class C protected changes 별도 governed work | evaluator/authority를 후보가 바꾸어 자기 개선을 증명하는 구조 제외 |
| ADR-24 | one full V3 target, staged migration within it | big-bang overwrite 금지; 단계가 다음 세대로의 미루기는 아님 |
| ADR-25 | preserve legacy guards until proven replacement | 파일명 legacy만으로 삭제 금지; 코드가 많아도 안전 의미 우선 |
| ADR-26 | pack/kit release pin과 non-destructive installer | latest 자동 갱신·사용자 override overwrite 제외 |
| ADR-27 | postfact attestation reverse refs, immutable reference DAG | contract↔plan/qualification hash cycles를 만들지 않음 |
| ADR-28 | provider/cloud use governed by data classification | on-prem 실패 시 unapproved cloud fallback 금지 |

재검토 trigger는 observed bottleneck, unsupported target requirement, actual incident, primary-source status change, audited cost/quality result다. 유행어 변경 자체는 architecture rewrite trigger가 아니다.


---

# 25. 실제 저장소 변경 지도

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. package 이름은 유지

현재 실제 Python package는 `src/amplai_foundry/`다. V3는 제품/아키텍처 세대이며 package를 `amplai`로 무조건 rename하지 않는다. 기존 governance/domain/projects/control_plane/verification 코드를 보호하면서 아래 확장 모듈을 추가한다.

```text
src/amplai_foundry/
  domain/                 existing, no Kit dependency
  governance/             existing authority, preserve guards
  control_plane/          existing API/store + runtime adapter
  projects/               existing registry + AppBinding
  verification/           existing canonical checks + work verdict layer
  runtime/
    contracts/ goals/ graphs/ execution/ budgets/
    effects/ evidence/ storage/ recovery/
  knowledge_runtime/      Foundry read + repo facts + ontology port
  agent_drivers/          typed host/provider adapters
  evaluation/             frozen corpus/experiment/metrics
  meta_harness/           composition + evolution proposals
  distribution/           pinned releases and deployment receipts

packs/                    spec/software/frontend/documents/...
tools/amplai-loop-kit/     portable entries, installer, generated payload
contracts/                normative schemas/registries (versioned)
specs/active/             current implementation definitions
specs/canonical/          approved design summaries
artifacts/evidence/       indexed historical evidence, policy-controlled
```

이는 **제안 목표 구조**이며 지금 생성한 소스 디렉터리가 아니다. 기존 `verification/`를 새 디렉터리로 덮어쓰지 않는다. exact module layout은 기존 imports를 분석한 후 해당 작업의 구현 plan에 고정한다.

## 2. 무조건 삭제하지 않을 자산

legacy_* 현재 importer; Foundry approvals/Decision/ApplyGrant; app override; old canonical ID; referenced evidence; installer baseline/rollback; tests/protected goldens. 삭제 대신 adapter/rename/isolation이 먼저다. 1,669개 metadata inventory를 읽었다는 사실은 모든 파일의 의미 검증을 완료했다는 의미가 아니다.

## 3. 경량화할 표면

18개 일반 Skill entry를 사용자에게 모두 보일 필요는 없다. `/work`,`/design`을 유지하고 internal capability로 내려보낸다. implementation dependency는 줄이되 invariant는 줄이지 않는다. README/AGENTS/CLAUDE는 source of rules가 아닌 progressive map에 가깝게 한다. 규칙 정본은 schema/policy/gate registry이고 entry 파일에는 필수 안전 경계와 위치를 명확히 남긴다.

## 4. 실제 변경 작업서

`migration/component-map.csv`는 source path/prefix, matching file count, action, proposed target, reason, retirement condition을 담는다. `migration/source-disposition.csv`는 normal files 전체의 현재 digest와 분류다. implementer는 해당 작업의 subset을 추출하고 live import/ref scan을 다시 실행한 후 MigrationPlan을 만들며, glob을 그대로 delete command로 변환하지 않는다.


---

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


---

# 27. 대표 시나리오와 실패 분기

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## Scenario A — 이번 사용자 요청과 같은 설계-only

입력: “기존 AMPLAI를 V3로 설계, 구현은 다음 작업.” Intent.mode=design. resolver는 업로드 source와 공식 research를 읽고 app binding과 보호 조건을 붙인다. contract acceptance는 설계 문서/규격/마이그레이션/test traceability/출처를 요구하고 source code modification을 non-goal로 둔다. 그래프는 research/audit 병렬 read → design synthesis → schema/consistency review → design artifact packaging이다.

worker는 output directory에 문서만 만들 수 있다. runtime installer/production broker capability는 주지 않는다. schema 검증을 실행해도 제품 runtime 검증이라고 표시하지 않는다. verifier는 실제 ZIP 목록·refs·구조 검사 결과를 artifact digest로 묶는다. 사용자는 결과 검토 후 다음 구현을 별도로 지시한다.

## Scenario B — 작은 코드 수정

입력: “오류 메시지 오타 수정.” repo facts가 수정 범위와 test를 확인한다. direct strategy + 작은 contract를 만든다. registered sandbox worktree 한 곳에만 write. build/test/doc freshness applicable checks를 실행하고 diff artifact로 증거를 남긴다. 무조건 planner3명·graph10개·태스크 문서20장을 만들지 않는다. 변경 중 ABI/권한 영향이 발견되면 tiny 분류를 유지하지 말고 contract/risk를 재평가한다.

## Scenario C — Cortex/Synapse reference-state cross-app

입력은 예시이며 실제 production 변화 요청이 아니다. resolver가 두 app registry와 interface facts를 찾아 scope·C99/ABI·기존 profile semantics를 계약으로 만든다. 승인된 테스트 환경에 Cortex node와 Synapse node를 병렬 dispatch한다. 두 output이 모두 검증된 뒤 integration node가 consume한다. 작업 도중 Synapse API contract가 바뀌면 Cortex 결과도 영향 분석하여 재검증한다. node test만으로 global pass하지 않는다.

`fixtures/scenarios/cross-app-workgraph.json`은 합성 topology 예시다. 실제 repo·credential·Approval과 연결된 실행 파일이 아니다.

## Scenario D — 목표가 애매한 UX/UI 요청

입력: “페이지 예쁘고 편하게 바꿔.” frontend pack이 주요 사용자 업무·빈/loading/error 상태·참고 방향을 수집한다. 질문은 실제 preference/업무 순서가 필요할 때만 한다. 초기 visual direction을 확인하고 functional tasks+responsive+a11y+render findings를 acceptance로 만든다. 주관적 만족은 human rubric에 남기고 pixel similarity 점수로 대신하지 않는다. 선정한 golden을 builder가 임의 교체하지 못한다.

## Scenario E — 실행 중 방향 변경

사용자가 “기존 API는 유지하고 신규 API는 추가만 해”라고 말한다. steering ledger received→validated. constraint change는 contract v2, graph v2를 만든다. v1 lease는 신규 effects에 무효다. already dispatched effects를 먼저 확인하고 unrelated read-only work는 허용 범위 내 계속한다. native steering 지원 여부와 무관하게 durable requested/applied/effective state는 AMPLAI가 소유한다.

## Scenario F — 외부 쓰기 timeout

worker가 ticket/create 또는 approved publish를 요청한다. broker PREPARED→DISPATCHED 후 연결이 끊긴다. fail로 단정하지 않고 UNKNOWN. 외부 idempotency lookup으로 applied receipt를 찾으면 reconciled(applied). 없음을 확실히 확인할 수 없으면 HOLD. retry loop가 다시 create하지 않는다. user cancel 역시 이미 적용된 action을 없애지 않는다.

## Scenario G — Meta 개선 후보가 실패

Observatory가 특정 task class에서 plan overhead를 관측한다. proposer가 그 class에서 direct strategy를 쓰자는 후보를 만든다. independent experiment가 safety는 pass했지만 quality confidence가 불충분하다고 판정하면 inconclusive. candidate는 promote되지 않는다. 실험 수치를 맞추기 위해 task corpus나 criterion을 바꾸면 새 별도 실험으로 분리한다.

## Scenario H — Meta canary→promote→rollback

동일 frozen corpus/verifier/environment에서 baseline 대비 predeclared 기준을 통과한 composition에 authorized canary grant를 발급한다. eligible sandbox workload에만 적용한다. 통과하면 exact release/report digest에 대한 promotion grant와 expected active CAS로 새 admission pointer를 바꾼다. 회귀 시 kill switch→new admissions stop→active effect reconciliation→previous release pointer 복원. 완료된 side effects나 revoked authority를 되돌리지 않는다.

## Scenario I — V2 migration 충돌

installer가 owned 파일의 expected digest와 현재 digest가 다름을 발견한다. 현재 사용자의 변경을 덮어쓰지 않고 conflict report. optional handoff marker는 missing allowed로 분류한다. legacy_gates는 현재 import chain이 있으므로 replacement parity evidence 전 retirement 거부. 완료된 V2 log는 legacy_imported provenance를 유지한다.

## Scenario J — 온프레미스/offline

사내 classified code를 로컬 모델로 작업하다 provider down. hybrid cloud가 존재하더라도 해당 data policy가 허용하지 않으면 HOLD. read-only reference browsing은 캐시된 canonical digest의 freshness 규칙에 따라 가능하다. 새 grant/production write는 authority 재접속 전 시작하지 않는다. old RHEL app에는 modern CP를 강제 설치하지 않고 검증된 thin client 경로를 사용한다.


---

# 28. 설계 리뷰와 구현 완료 점검

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 구현 시작 전 리뷰 산출물

Review Lead는 `implementation/requirements-traceability.json`과 `contracts/*`를 읽고 각 검토 finding에 severity/blocker, exact file/section, violated invariant, concrete correction, required test를 붙인다. “복잡하다/좋다/최신이다” 같은 인상평은 구현 승인 근거가 아니다. source baseline와 design assumptions가 달라진 경우 어떤 사실이 달랐는지 digest와 함께 기록한다.

필수 확인은 (1) object ownership/transaction/effect boundary, (2) 권한 발급과 revocation/race, (3) contract/graph/context/hash reference closure, (4) lifecycle·cancel·retry semantics, (5) provider qualification/support flags, (6) eval/holdout/promote independence, (7) app-safe migration/deletion/rollback, (8) 실제 acceptance evidence다.

## 2. 자동 검수로 충분하지 않은 부분

JSON Schema는 실제 목적·authority·서명 진위·프로젝트 access·통계·UI 품질을 증명하지 않는다. Markdown link 검사도 내용의 논리적 정확성을 증명하지 않는다. 실제 invariant/gate 코드·fault injection·integration·qualified environments가 필요하다. 이 설계 패키지 검수 보고서에서 해당 항목은 `not_run`으로 남긴다.

## 3. 구현 리뷰의 금지 shortcuts

테스트를 제거해 통과; unknown cost=0; null data를 빈 성공 결과로 대체; latest provider alias drift 무시; permission failure 후 unrestricted shell로 fallback; archive를 안 읽고 obsolete 단정; grant의 revision bind 제거; fake driver만으로 production 지원 표시; meta proposal이 자신의 평가기와 승인자를 변경; 이미 반환된 202를 완료로 보고; 설계 문서를 읽었다는 말로 실제 gate test 생략.

## 4. 수동 설정이 필요한 배포 조건

실제 authority identity/승인자, signing keys, 경로/egress, provider account/region/data-use policy, 비용 예산, retention/legal hold, canary 규모, support matrix는 배포 시 조직 환경으로 채운다. **이것은 설계 누락을 의미하는 TODO가 아니라 환경별 보안 파라미터**다. 값이 없으면 write enable이 막히도록 activation gate를 구현한다. 아무 값이나 합성 fixture에서 가져오지 않는다.

## 5. 문서 변경 규칙

설계 충돌은 `DESIGN_CHANGE_PROPOSAL`로 보고하고 영향 받은 requirement/schema/gate/test/task/ADR를 함께 갱신한다. 구현이 편하다는 이유만으로 규격을 조용히 낮추지 않는다. 승인된 scope change는 버전 기록을 남긴다. source 중 새 사실이 확인되면 진짜 경계를 지키면서 더 단순한 구현을 선택할 수 있다.

## 6. 최종 완료 문구

완료 보고는 구현 commit, qualified release matrix, 실행한 tests 수와 pass/fail/not_run, e2e evidence, migration rehearsal, unresolved limitations, deployment 승인 필요 여부를 포함한다. “100% 완료/최적/완전 안전” 대신 검증된 범위를 말한다. 이 ZIP은 설계 완료 산출물이고 실제 구현 완료 보고가 아니다.


---

# 29. 위협·실패 모델과 통제 매핑

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 보호 자산과 공격 표면

보호 자산은 source code/사내 데이터, canonical knowledge, decisions/grants, release signing keys, credentials, runtime/evidence integrity, budget, production apps다. ingress는 사용자 메시지·repo/doc attachments·tool/MCP responses·driver event stream·pack registry·webhooks·migration archives·eval datasets다.

| 위협 | 경로 | 통제·검증 |
|---|---|---|
| confused deputy | 사용자 project ID/role 위조, 다른 app hint | ActorContext + scoped lookup + grant/action binding; T scope group |
| indirect prompt injection | README/검색결과가 policy 변경 요구 | trust-labelled context + non-LLM policy/broker; security group |
| excessive agency | planner가 추가 scope/production action 수행 | capability intersection + protected non-goals + budget; G-03/06/10 |
| data exfiltration | external provider/tool/redirect/secret env | egress allowlist·classification routing·secret broker·redaction |
| supply chain | unsigned pack/model profile/driver schema drift | digest/signature/qualified release sets; G-07/15 |
| evidence fabrication | agent-declared digest/pass/judge score | server-computed artifacts + frozen verifiers + attestation; G-11/12 |
| eval gaming | holdout leakage, test deletion, criterion weakening | protected surfaces·separate approval·independent corpus; G-16/22 |
| stale authority | revoked grant + old worker + changed contract | pre-effect authoritative check·fence·immutable refs; G-09/10 |
| duplicate effects | outbox replay/timeout/callback duplicate | scoped idempotency+inbox+reconcile; G-10/21 |
| resource exhaustion | graph explosion/recursive subagents/infinite repairs | root budget·max nodes·depth/concurrency caps·stop signatures |
| lost state | CP crash/CAS gap/clock skew/disk full | UnitOfWork·CAS staging·server time·owner epoch·fault drills |
| destructive cleanup | legacy name heuristic/user override overwrite | owned digest/live ref/backup/grant gates; G-20 |

## 2. 공격자와 실수의 구분

모든 문제가 악의적 공격일 필요는 없다. 잘못된 model output, 오래된 문서, 정상 네트워크 timeout도 같은 안전 실패를 유발한다. 통제는 악성/실수 양쪽에 동일하게 적용한다. 사용자 자신이 임시로 policy를 낮추려 해도 조직 권한과 보호 invariant에 따라 필요한 승인/격리를 거쳐야 한다.

## 3. 남는 한계

허용된 tool 자체가 잘못 구현됐거나 external system이 idempotency 의미를 지키지 않으면 AMPLAI 내부 transaction만으로 안전을 보장할 수 없다. native subprocess/agent가 sandbox control을 제대로 상속하는지도 qualification이 필요하다. 단일 host compromise는 해당 host 권한의 위험을 수반한다. 별도 VM/호스트/credential 분리와 감사·revoke 절차로 영향을 줄이며 'sandbox이므로 완전 안전'이라 쓰지 않는다.

모델이 UX rubric을 잘못 평가할 수 있고 독립 model judge도 편향될 수 있다. critical/business acceptance에는 deterministic evidence와 human review를 적절히 혼합한다. **보안 gate와 통계 uncertainty를 숨기지 않고 운영 상태에 노출하는 것**이 이 설계의 요구다.


---

# 30. Harness 실험의 판정 규약

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 실험 단위

task(case)가 primary unit이며 repeated run은 task 내 stochastic variation을 추정한다. baseline/candidate에 동일 task/environment/entry constraints를 배정한다. 여러 agent output을 서로 독립 task sample처럼 세어 표본 수를 부풀리지 않는다. latency는 queue/compute/human wait를 구분하고 비용은 measured/estimated/unknown 비율을 공개한다.

## 2. 사전 규칙

ExperimentPlan은 primary endpoint, quality non-inferiority margin(있다면), benefit endpoint, baseline variance estimate, repeat/sample rationale, failure/aborted/inconclusive treatment, missing data policy, confidence/analysis procedure, sequential stopping, subgroup plan, maximum compute budget을 선언한다. 값을 설정하지 못한 실험은 exploration으로 표시하며 자동 promotion의 confirmatory evidence로 쓰지 않는다.

## 3. 판정 우선순위

1. scope/authority/evidence/safety integrity 위반은 점수와 무관하게 fail/abort다.
2. 환경 drift·오염·missing usage/결과가 비교 가능성을 훼손하면 inconclusive다.
3. 충분한 evidence와 사전 분석 규칙에 따라 quality·benefit·cost 기준을 비교한다.
4. canary와 final promote는 별도의 authority·rollback gate를 요구한다.

사전 non-inferiority를 선택했다면 '차이가 유의하지 않다'를 '같다'라고 바꾸지 않는다. uncertainty interval이 허용 경계를 넘으면 증거 부족이다. 여러 endpoint 중 유리한 것만 사후 primary로 선택하지 않는다.

## 4. 실행 권장안

paired task-level differences, task-level bootstrap 또는 데이터 특성에 맞는 분석법을 구현자가 통계 리뷰와 함께 선택한다. 반복치가 묶인 구조를 보존하고 dependency를 무시하지 않는다. API seed/pin은 재현에 도움을 주지만 완전 결정성을 보장하지 않는다. 연구 논문의 특정 성공률/비용절감률을 AMPLAI의 목표치로 복사하지 않는다.

safe canary 동안 언제나 monitoring할 수 있지만 같은 데이터로 원하는 p-value가 나올 때까지 멈추지 않는 방식은 금지다. 사전 stop rule과 exploratory/confirmatory 구분을 남긴다. 여러 candidate는 development corpus에서 고르고 최종 검증 corpus의 반복 사용을 제한한다.

## 5. 보고 형식

대상 task class/표본 task 수/총 runs/제외 사유/버전/env/metric 정의/baseline와 candidate 결과/불확실성/안전 사건/비용 미보고분/한계/권고를 함께 표시한다. 최종 verdict는 pass/fail/inconclusive/aborted다. tradeoff가 있는 경우 비용 절감만 강조하지 않고 사용자가 승인한 objective와 어떻게 맞는지 설명한다.

이 규약은 특정 통계 수치를 이번 설계에서 계산했다는 뜻이 아니다. 실제 baseline 수집·power/sample planning은 다음 구현의 Evaluation work다.


---

# 31. 환경별 실행과 지원 매트릭스

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 기본 profile

| Profile | Control Plane | Worker/model | 권한·데이터 | V3 검증 요구 |
|---|---|---|---|---|
| local-dev | modern Python≥3.11 local disk | isolated CLI/session | 등록된 project scope, budget | baseline driver + state/fault suite |
| onprem | 사내 modern host, local DB/CAS | local API/approved CLI worker | 기본 external egress deny | internal auth/secret/backup/fleet test |
| hybrid | onprem truth/authority | 승인된 cloud 또는 local route | data classification별 허용; 자동 유출 fallback 금지 | both routes conformance+privacy policy |
| offline | pinned local release + cached canonical | qualified local model only | 신규 grant·외부 verification 필요한 write HOLD | cold start/cache staleness/deny tests |
| legacy-app-client | modern CP 별도 | old host의 얇은 client/installer | 기존 앱 build/runtime 독립 유지 | 실제 OS/Python version별 smoke/uninstall |
| managed optional | 동일 Foundry authority | managed API profile | vendor persistence/region/secret policy 별도 검토 | exact provider capability/exit/effect tests |

## 2. CP/Kit 배포 의존

CP는 core source의 Python>=3.11 기준을 따른다. 앱의 RHEL7/Python3.6 경계와 충돌하면 얇은 client를 유지한다. 앱의 production binary가 AMPLAI session/lib를 import하도록 만들지 않는다. CP가 꺼져도 기존 앱 서비스 동작은 유지돼야 한다. 개발 automation은 HOLD할 수 있지만 production business dependency로 새로 삽입하지 않는다.

## 3. 설정을 provider 기능과 혼동하지 않는다

model이 async를 지원해도 선택 host가 expose하지 않으면 native async capability는 false다. driver가 API상 기능을 선언해도 실제 sandbox/권한/versions 조합이 conformance를 통과하지 않으면 qualified가 아니다. profile activation은 required capabilities⊆qualified∩granted 조건이다.

## 4. 운영 원칙

단일 active scheduler를 기본으로 한다. worker pool 증가와 다중 CP writer는 다른 기능이다. V3는 여러 worker를 제어할 수 있으나 shared SQLite 파일을 여러 host가 여는 HA를 지원하지 않는다. uptime 요구가 강해지면 port-compatible DB/scheduler backend를 별도 검증해 도입한다.

비용·canary 비율·retention·key identities는 template value가 아닌 actual deployment config로 채운다. 검증된 combination만 support matrix에 올리고 미검증 항목은 experimental/disabled/not-tested로 표시한다. 이 ZIP의 합성 fixture는 어떤 환경의 credential/승인/모델 선택값으로도 사용하지 않는다.


---

# 32. Tool Broker·외부 Effect 프로토콜

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 모든 도구가 같은 위험은 아니다

`pure_read`, `sandbox_write`, `external_idempotent_write`, `external_nonidempotent_write`, `production_control`로 분류한다. pure_read도 secret/PII 접근·네트워크 유출 위험이 있으므로 권한 검증은 남는다. sandbox_write는 workspace scope와 quota를 enforce한다. external write는 EffectService를 거쳐 receipt를 남긴다. production_control은 기본 금지이며 도메인 시스템 자체의 검증·승인 경계를 대체하지 않는다.

기존 Cortex/Synapse production business behavior는 AMPLAI control logic로 옮기지 않는다. AMPLAI는 개발·검증·승인된 배포 업무를 돕는 플랫폼이며 tester 제어 경로에 새 자율판단을 삽입하지 않는다.

## 2. ToolRegistry

각 tool definition은 ID/version/input schema/output schema/effect class/required capabilities/allowed endpoints/timeout/max result bytes/idempotency support/reconcile method/compensation policy/redaction/owner를 가진다. command alias나 shell string이 schema·permissions를 우회하지 못한다. shell은 별도 high-granularity sandbox capability이며 production network secrets를 주지 않는다.

MCP/ACP/provider native tool은 transport mapping이다. AMPLAI policy가 실제 해당 tool에 강제되는지 qualification한다. MCP Tasks는 remote async 상태 조회 보조이며 experimental 지원 범위는 별도 capability로 표시한다. [R19,R20,R21]

## 3. Prepare → dispatch → reconcile

1. EffectRequest scope/run/lease/fence/contract revision/tool args digest를 검증한다.
2. authoritative grant와 현재 revocation generation을 확인한다.
3. unique effect_key에 request digest를 bind하고 PREPARED receipt + outbox를 commit한다.
4. dispatcher는 재전송 가능성을 확인하고 exact idempotency key로 call한다.
5. response 관측을 APPLIED/NOT_APPLIED/UNKNOWN으로 분류하고 output evidence를 저장한다.
6. timeout/network disconnect는 NOT_APPLIED가 아니다. query/reconcile을 수행한다.
7. UNKNOWN에서 재호출은 별도 권한과 외부 시스템 의미 검증을 요구한다.

provider의 HTTP 500도 업무 action이 실행된 후 응답만 실패했을 수 있다. `retryable`은 네트워크 에러 문자열이 아니라 tool contract에 따라 결정한다. tool이 idempotency와 조회를 모두 제공하지 않으면 고위험 자동 retry를 금지하고 human reconciliation으로 보낸다.

## 4. Fencing 한계

CP monotonic fencing은 stale worker의 새 broker request를 막는다. worker가 이전에 direct credential로 외부 endpoint에 보낸 요청까지 되돌리지는 못한다. 따라서 credentials/egress를 broker-bound로 만들고 가능한 endpoint도 generation/fence를 검증한다. endpoint가 지원하지 않는 경우 dispatch 시간창의 비원자성·unknown status를 운영상 드러낸다.

## 5. 승인과 결과

grant max-use 소비와 effect PREPARED 기록을 같은 authority service command 또는 transactional equivalent로 처리해야 한다. 분산된 stores이면 issue/consume receipt + idempotent inbox를 사용하고 부분 실패를 reconcile한다. 단순 양쪽 update를 이어 쓰고 atomic이라 부르지 않는다.

compensation은 새로운 승인된 effect다. 원래 실패를 삭제하지 않는다. 예를 들어 잘못 생성된 external ticket을 close하는 것은 ticket이 없었던 상태와 같지 않다. evidence에는 original effect와 compensating effect를 연결한다.

## 6. 반드시 시험할 race

dispatch 직전 revoke, dispatch 직후 timeout, response 저장 전 crash, 같은 effect key 다른 args, stale worker heartbeat 재등장, child tool orphan, external API가 동일 key를 다른 semantics로 처리, callback 타 project 위장, reconcile 결과 뒤늦게 도착, partial batch 일부만 applied. batch는 item receipts와 aggregate partial state를 가지며 all-or-nothing 지원 없는 endpoint에 원자적 batch를 약속하지 않는다.


---

# 33. 객체 identity·해시·참조 수명주기

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 세 가지 식별자를 구분

business ID는 같은 Goal/Work를 계속 가리키는 이름, revision은 immutable definition의 순번, digest는 정확한 내용 bytes를 가리킨다. row_version은 mutable aggregate의 CAS 버전이다. 이 네 개를 서로 대체하지 않는다. scope는 tenant/project로 항상 함께 대조한다. `latest`는 UI projection에서만 해석할 수 있으며 executing contract에는 고정 ref가 필요하다.

## 2. hash 범위

JSON object digest는 `signature` container와 transport-only envelope를 제외한 unsigned content의 JCS bytes다. metadata가 hash 대상인지 schema마다 임의로 달리하지 않는다. top-level definitions의 created_at/id/revision은 내용에 포함되므로 freeze 시 이미 정해져 있어야 한다. artifact digest는 raw bytes. digest 문자열을 object 자기 필드로 넣어 hash하지 않는다.

## 3. Compiler 결정성

ID/created_at/revision은 compile **전에** intake/allocator가 durable CompileRequest에 한 번 부여한다. compiler는 frozen metadata+contract+plan+registry snapshot을 순수 입력으로 받고 실행 중 now/random UUID를 호출하지 않는다. 동일 frozen request에 대한 compile은 같은 output/digest다. 새로운 request에 다른 created_at/ID가 부여되면 digest가 다를 수 있으며 이는 비결정적 compiler 결함이 아니다.

## 4. 참조 순환 금지

immutable identity graph는 backward-reference DAG여야 한다. 특히 다음을 지킨다.

- 초기 Resolution의 candidate_contract_ref는 null. Contract가 그 Resolution을 참조한다. 나중의 Resolution projection/new revision은 Contract를 가리킬 수 있지만 기존 Contract의 resolution_ref를 덮어쓰지 않는다.
- 초기 VerificationPlan/ContextBundle의 contract_ref는 null. Contract가 이 초기 plan/context를 bind한다. Run용 context는 frozen Contract를 참조하는 새 artifact이고 ExecutionEnvelope가 이를 참조한다. 기존 Contract는 새 run context를 backfill하지 않는다.
- HarnessChangeProposal의 experiment_plan_ref는 후보/검증 조건만 가진 **pre-experiment definition**이다. 이후 Frozen EvalExperiment는 proposal_ref를 가질 수 있다. proposal을 수정해 그 EvalExperiment를 다시 hash dependency로 넣지 않는다.
- HarnessComposition.qualification_ref는 이미 존재하는 driver/model/pack 자격 근거 집합이다. 완성 composition에 대한 후속 평가 보고서는 외부 attestation/reverse registry relation으로 연결한다.
- ReleaseSet의 qualification_matrix_ref는 component compatibility 근거다. 최종 release conformance/promotion receipt는 release ref를 향하는 별도 append-only object이며 release 내용을 backfill하지 않는다.

이 규칙은 객체 관계를 graph DB로 옮기면 해결되는 문제가 아니다. hash 정의와 lifecycle 설계의 문제다. `SEM-24`와 identity property tests가 cycle을 잡아야 한다.

## 5. Artifact binding

WorkGraph.produces의 output name은 node 안에서 유일하다. consumes는 local input name, from_node, **output_name**, external_ref, media_type를 가지며 from_node+output_name 또는 external_ref 중 정확히 하나의 source를 선택한다. producer output이 실제 WorkOutput registry에서 current graph/run/verdict와 bind되기 전에는 consumer가 준비되지 않는다.

worker artifact.produced event의 output_name은 declared produces에 있어야 한다. 실제 digest/media를 ArtifactService가 확인한 뒤 trusted verifier가 결과를 판정한다. worker가 임의의 artifact를 consumer input으로 직접 주입하지 못한다. 이름만 같고 hash가 다른 output은 서로 다른 revision의 결과다.

## 6. 시간과 승인

timestamp 형식 검사는 실제 clock/expiry 의미 검사를 대신하지 않는다. server-authoritative time으로 not_before/expiry를 확인한다. worker clock은 audit observation이다. 오래된 approval·context가 구조상 valid JSON이어도 active authority라는 뜻이 아니다. fixture의 synthetic times/keys는 실제 실행에 사용할 수 없다.
