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
