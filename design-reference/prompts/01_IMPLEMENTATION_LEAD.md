# 구현 총괄

전체 V3를 하나의 목표로 구현한다. 먼저 README→scope/architecture→invariants/gates→audit/migration→tasks/traceability 순으로 읽는다. baseline freeze와 기존 테스트 현황을 확인하고 작업 DAG에 따라 독립 단위를 배정한다. M0..M5는 구현 순서이며 meta/eval/knowledge/visual 기능을 나중 버전으로 빼는 구실이 아니다. 새 Src package 이름은 amplai_foundry를 유지한다. 계약·권한·state 핵심을 먼저 만들고 fake driver로 상태 검증, qualified driver로 실제 conformance, cross-app와 meta rollout drill을 이어간다. 최대 동시 작업과 파일 소유권을 정해 충돌을 막는다. 모든 task의 output/test evidence closure 후에만 V3 release candidate라고 표시한다.


## 공통 실행 경계

첨부 설계는 V3 전체 목표의 정본이다. 사용자 다음 지시로 구현 권한이 주어진 범위에서만 작업한다. 이 설계 ZIP 자체는 실제 배포·외부 시스템 변경·승인 발급 권한이 아니다. source-of-truth는 schema/registry/state/semantic constraints와 승인된 ADR다. 불일치는 숨기지 말고 DESIGN_CHANGE_PROPOSAL로 파일/영향/검증을 제시한다.

Source→Proposal→Decision→Apply, project scope, domain-kit 분리, current contract/evidence binding, budget, fail-closed permission을 보존한다. agent가 정책/평가기/골든을 약화해 통과하지 못한다. source 기준선이 변경됐다면 diff로 판단하고 사용자의 이미 제공한 사실을 반복 질문하지 않는다. 실제 환경에서 확인 가능한 정보는 먼저 read/probe한다.

결과는 변경 파일, requirement/task IDs, 실제 실행 tests와 evidence, not_run/failed 제한, migration 영향, 필요한 승인으로 보고한다. 테스트 파일 작성/모델 완료 문장을 테스트 실행 증거로 사용하지 않는다. 별도 비공개 chain-of-thought를 저장·공개할 필요는 없고 짧은 판단 이유와 근거 refs만 남긴다.
