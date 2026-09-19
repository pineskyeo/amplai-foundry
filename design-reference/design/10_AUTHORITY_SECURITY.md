# 10. 권한·격리·신뢰 경계

설계 기준일: 2026-09-15 · 패키지: AMPLAI V3 Design 1.0 · 상태: 설계 명세 / 구현·운영 검증 아님


## 1. 보안 모델

LLM은 planning과 제안을 담당하지만 authority는 아니다. 사용자 메시지, repo README, 검색 결과, 외부 MCP 응답, golden artifact에는 모두 악성·오염 지시가 섞일 수 있다. **instruction처럼 보이는 data를 policy로 승격하지 않는다.** 실제 안전 경계는 AuthN/AuthZ + sandbox + credential broker + effect broker + immutable audit다. sandbox만으로 데이터 유출·잘못된 승인을 모두 해결한다고 보지 않는다. [R11,R20,R24]

신뢰 등급: `control`(배포·승인된 정책), `canonical`(governance로 승인된 도메인 지식), `observed`(도구로 관측한 사실), `untrusted`(외부·사용자 제공 데이터). canonical도 실행 권한을 갖지는 않는다. 모델 prompt에는 data/control 분리를 표시하지만 enforcement는 코드에서 수행한다.

## 2. Actor와 역할

| 역할 | 가능 | 불가능 |
|---|---|---|
| user/requester | 의도 제출, 정책 범위 내 steer/cancel, 본인 승인 요청 확인 | 자신에게 없는 production 권한 위임 |
| planner/critic | resolution·contract·graph 초안, 질문 | 승인 원장 수정, worker credential 발급 |
| worker | 발급받은 제한 capability로 sandbox 내 작업 | canonical write, 타 project data 조회 |
| verifier | 고정된 verifier policy로 결과 판정 | acceptance·골든·정책을 동시에 변경 |
| governor/authorized approver | 정확히 지정된 변경 승인·거절 | 다른 tenant 권한 자동 획득 |
| meta proposer | harness 변경 후보와 실험 계획 제안 | holdout 열람, 자기 변경 자동 promote |
| release service | 검증된 승인과 release set으로 배포 | 검증 실패를 성공으로 변환 |

단일 사람이 여러 역할을 가질 수 있어도 high-risk 승인자는 정책으로 분리 가능해야 한다. 시스템 역할 이름은 서비스 계정의 실제 권한과 매핑되어야 하며 prompt의 `you are governor`는 권한이 아니다.

## 3. Capability 계산

`effective = requested ∩ project_policy ∩ actor_grant ∩ sandbox_supported ∩ driver_qualified`.

정책은 namespace/project/app/repo/read-write roots/egress host+port/tool verbs/secret handles/max budget/production action을 포함한다. union으로 권한을 합치지 않는다. deny가 allow보다 우선한다. capability 미지원이면 넓은 shell로 우회하지 않고 `CAPABILITY_UNSUPPORTED`다. 신규 target 자동 발견은 registry 조회 권한일 뿐 그 target의 write 허용이 아니다.

`ExecutionGrant`는 scope, subject contract digest, graph revision, allowed action, artifact/environment bounds, expiry, max uses, policy version, generation, issuer를 묶는다. 민감 action은 **일회용 effect key**로 bind한다. payload나 tool arg가 바뀌면 재승인 또는 재평가가 필요하다. 실행 중 risk 상승은 자동 하향 조정 없이 escalation한다.

## 4. 승인 화면과 TOCTOU

사용자는 “승인” 버튼만 보지 않는다. 대상 repo/app, 현재→변경, 검증 근거, irreversible 여부, 필요한 credential 범위, rollback 조건, 실제 effect 종류, 예상 상한 비용을 본다. 사람의 선택은 authenticated actor + current revision + digest에 기록한다. 메시지에 “승인함”이라는 문자열이 있거나 이모지가 달렸다는 이유만으로 승인하지 않는다.

승인 이후 contract/graph/artifact/policy 중 binding이 변경되면 기존 승인은 stale다. 권한 체크와 전송 사이 race는 중앙 broker에서 grant lease를 소비하고 effect PREPARED를 원자 기록한 뒤 제어한다. 외부 call의 물리적 도착과 revoke를 완전히 원자화할 수 없는 한계는 명시한다. strict 업무는 외부 endpoint도 fencing/generation을 검증해야 한다.

## 5. Worker containment

기본 worker는 별도 unprivileged UID 또는 container/VM, isolated worktree, read-only policy mount, writable workspace allowlist, no host Docker socket, no home credential mounts, egress deny-by-default다. package 설치는 사전 승인 mirror/lockfile만 이용한다. host filesystem sandbox가 없는 드라이버는 capability를 낮추거나 isolated VM 안에서 실행한다. 격리 기능을 off하고 같은 risk로 표시하지 않는다.

production credential은 agent text/context/env에 장기 노출하지 않는다. broker가 짧은 scoped token 또는 exact server-side operation을 수행한다. shell에서 직접 production network 접근 가능한 worker를 “broker enforced”라고 표시하면 안 된다. 로컬모델/온프레미스 기본 profile은 외부 provider 전송 금지이며 cloud 전환은 데이터 분류·정책·명시 승인을 통과해야 한다.

## 6. Pack·MCP·remote input

pack은 실행 코드 및 지시문을 포함하는 supply-chain 단위다. registry 서명·digest·publisher trust·permissions·dependency closure를 확인한 후 설치한다. `SKILL.md` 자체는 sandbox가 아니다. [R17,R18]

MCP server별 audience/credential을 분리하고 token passthrough를 금지한다. OAuth approval/redirect/origin 정책은 SDK 문서와 서버 설정으로 강제한다. tool list가 변경되면 capability snapshot이 바뀐 것으로 보고 requalification한다. MCP task ID는 원격 추적 ID이지 AMPLAI 승인이나 완료 증거가 아니다. [R20,R21]

## 7. 정책 변경과 감사

권한 policy·verifier·holdout·approval code는 protected surface다. 일반 /work·meta proposal이 수정 초안을 만들 수는 있지만 독립 리뷰와 기존 validator에 의한 검증 없이 적용할 수 없다. evaluator를 고쳐 green을 만드는 경우 기존 acceptance 변경 건과 분리하여 명시적 계약 변경으로 처리한다.

감사 event에는 actor/service identity, causal command, scope, old/new revision, grant ref, effect key, result classification을 남긴다. raw prompts, secrets, private chain-of-thought는 기본 저장하지 않는다. reasoning 대신 짧은 decision rationale와 근거 참조를 저장한다. 보안 관련 evidence는 삭제·redact 이벤트 자체를 감사한다.

## 8. 필수 보안 부정 테스트

cross-project IDOR; forged actor in JSON; stale approval; revoked grant; replayed effect; tool alias permission bypass; symlink escape; archive traversal; malicious README instruction; pack signing mismatch; evaluator self-change; leaked token; inaccessible authority; old worker epoch; network redirect to disallowed host. 각각 `eval/test-catalog.json`에 독립 test ID로 추적한다.
