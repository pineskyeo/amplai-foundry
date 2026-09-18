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
