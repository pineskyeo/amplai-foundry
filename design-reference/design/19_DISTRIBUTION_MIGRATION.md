# 19. 전면 V3 전환·삭제·롤백

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 단일 V3 목표, 안전한 전환 순서

V3 전체를 이번에 설계하고 다음 구현에서 통합한다. 그렇더라도 권한 원장·실행 상태·앱 설치를 동시에 덮어쓰는 방식은 금지한다. implementation wave는 V3 내부 의존 순서일 뿐 meta-harness/knowledge/visual verification을 다음 버전으로 미루는 범위 축소가 아니다.

| wave | 내용 | 통과 증거 |
|---|---|---|
| M0 | baseline freeze·inventory·기존 regression 확인 | tar/git digest, release facts, migration rehearsal plan |
| M1 | contracts/invariants/gates/storage/authority foundation | schema+state+fault+security tests |
| M2 | goal/context/compiler/runtime/drivers/effect/eval core | end-to-end intent→verified on isolated repo |
| M3 | packs·cross-app·steering·federation·meta evolution | conformance + shadow/canary/rollback drills |
| M4 | app installer·data migration·fleet staging | before/after digests, approval preservation, compatibility |
| M5 | authorized cutover·legacy retirement | final regression, restore, no live refs, deletion grants |

## 2. 기존 데이터 처리

V2 Work는 raw payload+schema version+source digest를 먼저 보존한다. active V2 run은 drain 또는 명시적 cancel/reconcile하고 lease를 V3에 그대로 옮겨 유효하게 만들지 않는다. 완료된 V2 evidence는 `legacy_imported` trust/source 상태로 보관하며 새로운 V3 trusted verdict로 재명명하지 않는다.

기존 approved Decision/ApplyGrant는 그 당시 scope/semantics를 유지한다. 새로운 action·contract binding이 필요하면 기존 승인자가 V3 grant를 새로 발급해야 한다. UUID 문자열을 복사해 권한을 확대하는 migration은 실패다. legacy approvals가 유효한지 판단하는 기존 gate를 대체 구현으로 port하기 전 삭제하지 않는다.

## 3. 코드 재배치

`migration/component-map.csv`와 `migration/source-disposition.csv`를 따르되 disposition은 **설계상 권장 조치이며 실행할 delete list가 아니다**. scripts runtime의 durable Work/lease/scheduler semantics는 Platform module로 옮기고 portable CLI는 service adapter로 얇게 만든다. local isolated mode가 필요한 Kit도 동일 Runtime port를 사용해야 하며 두 벌 state machine을 유지하지 않는다.

legacy_*는 이름 기준 폐기 금지. 실제 import/export/registry/CLI/DB behavior를 추적하고 guard equivalence·negative regression을 검증한다. 기존 shim은 호출 telemetry로 미사용을 확인한 후 sunset한다. canonical paths를 바꿀 때 existing refs/ID는 alias+tombstone으로 보존한다.

## 4. 삭제 승인 절차

삭제 후보마다 path + old digest + owner + reason + replacement + live refs + backup ref + required test IDs를 기록한다. path glob만으로 대량 삭제하지 않는다. computed generated surface와 user-authored file은 구분한다. expected digest가 달라졌으면 사용자의 변경으로 보고 conflict HOLD다.

동일 내용의 upstream payload와 installed mirror를 '중복'이라는 이유로 한쪽 삭제하기 전에 배포 생성 경로를 확인한다. required:false handoff marker absence는 오류로 단정하지 않는다. spec/evidence archive는 hash preserved move→backlink rewrite→reference scan→GC approval 순서다.

## 5. 릴리스·설치 receipt

ReleaseSet에는 Platform/Kit/protocol/schema/pack/driver qualification/OS matrix/migration version/digests를 고정한다. Foundry와 Kit 버전은 각각 독립이며 숫자가 같다고 release 상태가 같은 것은 아니다. 현재 archive의 README candidate2.5.0와 VERSION2.4.0 차이는 구현 전 release truth inventory에서 해소해야 한다.

installer는 plan→dry-run→policy/authority gate→backup→atomic supported steps→verify→receipt를 수행한다. 여러 app은 install receipt별 성공/실패가 나뉜다. 일부만 성공하면 부분 완료로 보고하고 전체 성공으로 표시하지 않는다. app-local overrides는 명확한 overlay 경로로 보존한다.

## 6. 롤백 경계

코드 rollback은 이전 signed release 재설치, runtime rollback은 checkpoint/DB schema 지원 여부, canonical rollback은 새 governed revert commit, 권한 rollback은 **최신 revocation 유지**다. 이전 DB backup으로 권한 취소를 되살려서는 안 된다. 외부 side effect는 compensation/reconciliation 절차이며 일반 release rollback과 별개다.

V3가 새로운 data를 기록한 뒤 V2가 읽을 수 없다면 자동 downgrade 금지다. drain→snapshot→explicit migration rollback→verify→admission resume 절차가 필요하다. 가역하지 않은 변환은 dry-run 보고서에 명시하고 원본을 보존한다.
