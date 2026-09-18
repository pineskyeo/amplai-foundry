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
