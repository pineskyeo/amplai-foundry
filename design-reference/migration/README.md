# Migration map 사용 지침

component-map.csv는 실제 첨부 경로에 매칭한 컴포넌트 재배치안이다. source-disposition.csv는 환경/캐시/Git 내부를 제외한 normal-file inventory 전체에 대한 기본 보존·이관 분류다. **모든 행의 delete_authorized=false**. 설계 승인과 실제 삭제 권한은 다르다.

명세에 없는 파일은 manual_review_preserve다. 기존 파일의 내용이 수정됐다면 expected hash 차이를 보고하고 재조정한다. 실제 이관 시에는 approved manifest, backup, reference scan, regression, rollback drill이 필요하다. 이 ZIP은 원본 파일이나 보안 토큰을 복사하지 않았으며 변경·삭제를 실행하지 않았다.

`src/amplai_foundry/` package 이름은 유지한다. V3 명칭 때문에 불필요한 package rename/import 대량 변경을 하지 않는다. 새 runtime/knowledge_runtime/meta_harness/agent_drivers 하위 모듈을 추가하고 기존 governance/domain/control_plane/projects/verification의 책임을 보존한다.
