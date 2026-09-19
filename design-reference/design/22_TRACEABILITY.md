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
