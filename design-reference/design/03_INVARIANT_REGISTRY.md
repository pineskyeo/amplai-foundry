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
