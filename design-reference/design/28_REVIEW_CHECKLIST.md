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
