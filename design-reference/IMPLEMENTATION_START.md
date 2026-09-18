# 다음 구현 작업 시작문

아래 본문은 **다음 구현 세션에 전달할 지시문**이다. 이번 설계 작업에서 실행한 내용이 아니다.

---

첨부된 AMPLAI V3 Design 패키지와 현재 amplai-foundry 저장소를 기준으로 V3 전체를 구현한다. 목표는 **Intent to Verified Work**이며 Goal Resolver/Contract/WorkGraph/adaptive runtime/knowledge/packs/visual verification/evidence/eval/federation/meta evolution/canary/promote/rollback/migration을 모두 포함한다. 기능을 축소하거나 다음 버전으로 조용히 미루지 않는다.

먼저 README, design/01·02·03·04, audit/BASELINE_AUDIT.md, contracts/README.md, implementation/tasks.yaml, requirements-traceability.json을 읽는다. 실제 현재 source hash와 audit baseline을 비교하고 기존 test 결과를 수집한다. source가 바뀌었으면 변경된 부분만 재검토하며 확인 가능한 사실을 사용자에게 반복 질문하지 않는다.

구현은 tasks.yaml의 dependency DAG 순서로 진행한다. 별도 branch/worktree에서 작업하고 저장소 밖 배포·운영 변경은 기존 authority와 사용자 승인 범위 내에서만 수행한다. code deletion은 owned digest/live refs/backup/replacement tests/승인된 migration plan 뒤에 한다. 기존 Foundry governance·domain-kit 독립·project isolation·fail-closed 권한은 유지한다.

각 구현 단위에서 먼저 해당 acceptance와 부정 tests를 명확히 하고 구현→검증→bounded repair→review를 진행한다. JSONSchema 검증만으로 semantic/authority/실제 효과의 성공을 주장하지 않는다. fake driver 결과와 실제 qualified provider 결과를 구분한다. `/work`와 `/design` 외의 내부 capability가 불필요하게 public workflow 단계로 다시 증식하지 않도록 한다.

최종 결과에는 requirement/task별 구현 commit·test/evidence·지원 matrix·migration rehearsal·meta canary/promote/rollback 결과를 포함한다. not_run/미지원/승인 대기/실패는 사실대로 표시한다. 공급자별 미검증 optional profile은 disabled로 두고 baseline 핵심 기능을 stub으로 완료 처리하지 않는다. 설계와 충돌하면 변경 사유/영향/대안/test를 DESIGN_CHANGE_PROPOSAL에 기록한다.

---

**범위 확인:** 설계 ZIP 안에는 실행 서비스·migration script·runtime patch가 없다. JSON/YAML은 계약/규격/작업 명세이고 fixtures는 합성이다. 실제 구현은 이 다음 세션에서 수행한다.
