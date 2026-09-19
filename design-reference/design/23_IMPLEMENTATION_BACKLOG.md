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
