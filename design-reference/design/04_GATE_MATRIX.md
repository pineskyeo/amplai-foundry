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
