# 17. Intent·질문·중간지시·운영 UX

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 사람에게 목표 작성 부담을 넘기지 않는다

사용자는 자연어로 의도를 말한다. 시스템은 확인된 프로젝트 사실을 바탕으로 목표·경계·검증 방법 초안을 만들고, 다음 세 가지를 읽기 쉬운 표현으로 보여준다: **무엇이 달라지는가 / 무엇을 건드리지 않는가 / 무엇을 보면 끝이라고 할 수 있는가**. 불확실한 부분은 조용히 채우지 않고 assumption label을 붙인다.

승인이 불필요한 tiny deterministic 변경은 정책이 허용하는 auto-accept contract 경로로 진행 가능하다. 자동 accept 여부는 기존 actor 권한·risk·불변조건으로 판단하고 '대답 없으면 동의'가 아니다. 비용·대상·production 접근·데이터 외부 전송은 추측으로 넓히지 않는다.

## 2. 질문 원장

Question은 question_id, goal/contract ref, issue kind, evidence already checked, options/tradeoffs, safe default(if any), blocks capabilities, assigned actor, asked_at, status를 가진다. 이미 답한 질문은 answer의 applicability revision을 확인하여 재사용한다. 질문 여러 개를 사용자에게 던지기 전에 repo/registry로 해결할 수 있는지 확인한다.

허용 kind: business_intent, authority, ambiguous_target, conflicting_constraint, subjective_direction, destructive_change. 파일에서 확인할 수 있는 경로·빌드 명령·기존 규칙은 discovery 대상이다. unresolved question과 관련 없는 read-only discovery는 계속할 수 있지만 해당 권한이 필요한 work를 시작하지 않는다. deadline은 expiry/escalation이지 승인이다.

## 3. Steering 상태

사용자 중간 요청은 envelope로 먼저 저장한다. `received → validated → queued → applied | rejected | superseded`와 `effective_contract_revision`을 별도로 둔다. provider ACK만 오면 “전달됨”이지 “변경 반영 완료”가 아니다. 중간지시는 cancel/pause/resume/priority_change/constraint_add/acceptance_change/new_evidence로 분류한다. [R05]

priority 변경은 authority/계약을 바꾸지 않으면 scheduler metadata만 바꾼다. acceptance/target/non-goals/required effects가 바뀌면 contract new revision + critic + graph replan이 필요하다. 새 계약이 활성화되면 old lease는 더 이상 새 effect를 시작할 수 없다. minor presentation preference도 artifact 재검증이 필요할 수 있으므로 subject digest를 다시 bind한다.

## 4. Cancel의 정확한 언어

UI 단계: “취소 요청 접수”→“새 작업 차단”→“실행 중 도구 종료 확인”→“외부 변경 결과 확인”→“취소 완료/확인 필요”. pending external effect가 있으면 '완전히 취소됨'으로 표시하지 않는다. child worker가 남거나 remote ACK가 없으면 `unknown_effect` 또는 `cancellation_pending`을 노출한다.

## 5. Hermes와 UI boundary

Hermes/Slack/Telegram은 IntakeAdapter + notification/projection이다. 사용자 identity가 app 계정과 연결됐는지 검증한 후 authority service에 command를 전달한다. conversation text에 들어있는 repo ID/role/approval token을 신뢰하지 않는다. webhook 재전송은 event idempotency로 제어한다. 메시지 링크는 status projection이고 원본 Decision/Evidence ref로 drill down한다.

초기 V3는 CLI + API + 읽기 가능한 status projection으로 충분하다. 별도 웹 대시보드가 Core Runtime 필수 의존성이 되지 않게 한다. 향후 UI가 생겨도 동일 command/query API를 사용한다. app-native install/update 버튼은 배포 승인 flow를 우회하지 않는다.

## 6. 상태 표현

사용자 기본 상태: 의도 확인 중 / 확인 필요 / 실행 대기 / 작업 중 / 검증 중 / 완료 / 중단 / 실패 / 외부 결과 확인 필요. 내부 leased/pending/outbox ACK를 그대로 제목으로 노출하지 않는다. 완료 화면은 achieved acceptance, remaining limitations, changed targets, evidence, next required approval를 보여준다. 단순 green score나 'AI 판단 완료' 대신 검증된 사실을 표시한다.
