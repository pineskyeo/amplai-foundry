# Work 034 Spec: Hermes 오케스트레이터, 모듈 체계, 사내 설치

- 상태: 설계 초안 r3 (독립 검토 2회 반영, 2026-10-10). 구현 없음. 운영자 결정 대기.
- 근거 표기: `file:line` 은 HEAD `5e38d6b`, design-reference 는 `design/NN §k`. 추정은 "(추정)", 미확인은 "확인 필요".

## 1. 목적

1. 운영자가 메신저로 Hermes 에게 말하면 Hermes 가 일을 나눠 AMPLAI 에 맡기고 진행·결과를 알린다 (오케스트레이터 + 소통 창구).
2. 기능을 붙였다 떼고, 메타하네스가 그 선택을 실험 대상으로 삼는다.
3. 사내(OpenCode + 사내 LLM)에 설치하고, 사내 메타루프가 사내 하네스를 강화한다.
4. 새 upstream 버전을 사내에 적용해도 사내에 쌓인 내용을 유지한다.
5. 잘못 지운 지식(메모리)·문서 최신성 기능을 V3 모듈로 되살린다.

## 2. 운영자 결정

| # | 결정 | 상태 |
|---|---|---|
| OD-1 | Hermes Agent 를 오케스트레이터 + 소통 창구로, 교체 가능한 어댑터로 붙인다 | 확정 |
| OD-2 | 배포는 upstream → 사내 한 방향 | 확정 |
| OD-3 | 순서: 모듈 체계 → Hermes → 지식·문서 → 업그레이드 → 사내 구성 → Jev | 확정 |
| OD-4 | 사내 LLM 사용량은 모델 게이트웨이로 측정 | 확정 (2026-10-11) |
| OD-5 | Hermes 는 일시정지·취소를 할 수 없다 (운영자 CLI) | 확정 (2026-10-11) |
| OD-6 | 승인 페이지(S1b) 인증은 passkey 만, POST + CSRF | 확정 (2026-10-11) |
| OD-7 | 사내 평가기 재승인용 Q-suite 를 배포 릴리스 자산으로 싣는다 | 확정 (2026-10-11) |
| OD-8 | Hermes VM 은 처음에 이 Mac 의 VM (loopback 접근, H-12 원격 listener 는 옮길 때) | 확정 (2026-10-11) |
| OD-9 | 메신저는 Slack (운영자 확인 대기, 가정) | 가정 |
| OD-10 | 지식 저장소는 별도 git 저장소. 3층: Markdown 파일(정본) / 지식 모듈(유일한 쓰기·승인·색인) / MCP(읽기·검색·제안 창구) | 방향 확정, 3층 구조 확인 대기 |
| OD-11 | Hermes 가 쓸 모델·비용: 구독 연결 가능 여부를 먼저 확인한 뒤 결정 | 확인 중 |

## 3. 범위

포함: 모듈 체계, Hermes 연결(S1a, S1b), 지식·문서 최신성 모듈, 3층 업그레이드, 사내 구성, Jev.
제외: 사내 → upstream 반출, Hermes 경유 코드 변경, 웹 대시보드를 Core Runtime 필수 의존으로 만들기 (design/17 §5),
3.0.0 wire schema·design-reference 변경, 상용 VM 에이전트 연동, 다중 사용자 권한 모델(사내 다중 사용자는 S4 이후 별도 설계).

## 4. 요구사항

### M. 모듈 체계
- M-1 두 층: 설정 부품(`harness-component` 레코드, `runtime/execution/policies.py:91-119`)이 켬/끔·버전의 정본이고,
  코드 모듈(kind 별 구현체)은 설정 부품이 가리킨다.
- M-2 코드 모듈은 kind 별 Protocol·`interface_version`·적합성 시험을 가진다. 미통과는 켤 수 없다 (`MODULE_UNQUALIFIED`).
- M-3 코드 모듈은 고정된 배포물에서만 들어온다: 내장은 정적 목록, 확장은 서명된 배포 릴리스(또는 사내 서명 overlay)가
  패키지와 그 의존 전체(closure)의 wheel `RECORD` digest 를 고정한다. `.pth` 파일은 거부한다. 검증한 읽기 전용 사본에서
  로드한다. 임의 entry point 는 받지 않는다.
- M-4 신뢰 루트: upstream 서명 키는 설치 시 고정하고 교체 절차를 둔다 (design/20 §5). 사내 overlay 키는 오프라인 보관하며
  authority 키(`authority.pem`)와 다르다.
- M-5 `intake_adapter`·`model_gateway` kind 는 Control Plane 프로세스 밖(별도 프로세스·컨테이너)에서 돈다. authority 키를 가진
  프로세스에 외부 입력을 다루는 코드를 넣지 않는다 (design/02 §4).
- M-6 `capability-pack` 은 자산(skill, context, verifier 정의, 평가 사례)에만 쓴다 (design/13 §2, `distribution/packs.py:190-195`).
- M-7 메타하네스는 설정 부품 변경(모듈 교체·끔 포함)을 보통 제안으로 다룬다. `classify` 를 kind 기준으로 넓혀
  권한·평가·검증 kind 를 class C 로 둔다. `meta_harness/composition.py` 를 평가기 digest 범위에 넣어 이 변경이
  eval-N 승인 없이는 효력을 갖지 못하게 한다 (지금 범위 밖, `evaluation/versions.py:33-34`).
- M-8 고정 목록은 같은 동작의 내장 모듈로 감싼다. 켬/끔·교체는 새 run 부터 (design/16 §7).
- M-9 새 application service method 는 design/26 §3 의 열 가지 contract 를 문서화한다.

### H. Hermes
- H-1 Hermes 는 `/api/v3` 만 쓴다 (design/18 §1).
- H-2 Hermes 서비스 actor 권한: `goal.submit`, `runtime.read`(연결된 사용자의 goal 만), `goal.steer`(종류 제한, H-3),
  `question.answer`(종류 제한, H-4), `knowledge.propose`, `meta.read`(요약). 없음: `execution.approve`, grant 발급.
- H-3 steering 종류는 actor 종류별 허용 목록으로 모든 경로에서 검사한다. Hermes 허용: `constraint_add`, `priority_change`,
  `new_evidence`, `acceptance_change`(새 contract revision → 재승인). `pause`·`cancel` 은 OD-5 전까지 불가.
  지금 `POST /api/v3/goals/{id}/steering` 은 `goal.steer` 만으로 모든 종류를 받고 `pause`·`cancel` 은 사전 확인을 건너뛴다
  (`control_plane/api_v3/server.py:498-506`, `runtime/execution/steering.py:126-127`).
- H-4 Hermes 경유 답변은 `ambiguous_target`, `subjective_direction`, `conflicting_constraint`, `business_intent` 만.
  `authority`, `destructive_change` 는 운영자 직접 경로. 대리 actor 는 `authn_context_ref = intake:hermes` 로 표시하고,
  답변·의도 보정(refine) 두 경로 모두 이것으로 거른다 (지금은 배정 actor 일치만 본다, `runtime/goals/service.py:355,456`).
- H-5 대리 actor: 서버가 메신저 사용자 id → 연결표 → 운영자를 찾고, 권한은 Hermes 권한, 귀속(attribution)은 그 운영자인
  `intake` 종류 actor 를 만든다. `kind = human` 이 아니다 (승인은 human 만, `runtime/execution/product.py:2599`).
  JSON 의 actor·role·repo id 는 무시한다. 이번 범위의 연결표는 운영자 1인 (다중 사용자 권한 교집합은 제외 범위).
- H-6 Hermes 토큰과 연결표는 store 밖 파일에 둔다 (토큰 파일 요청마다 읽기 방식, `runtime/local_deployment.py:858-861`).
  복원(U-6)이 폐기된 토큰을 되살리지 않는다. 폐기는 즉시 효력 (AC-H7).
- H-7 같은 메시지 재전송은 한 번만 접수 (`hermes:<digest>` key). steer·replan 은 actor 별 빈도 제한.
- H-8 알림은 고정 schema 의 projection(goal id, 사용자 상태, 시각, AMPLAI 가 만든 요약)만 Hermes 에 준다. PR 제목·로그 같은
  원문은 넣지 않는다 (prompt injection 차단). 상태 표시는 design/17 §6, 취소 표현은 §4.
- H-9 Hermes 는 전용 VM 에서 Docker 터미널로 돌고 버전이 고정된다. 허용 통신은 메신저, 모델 API(또는 사내 게이트웨이),
  AMPLAI 접수 경로뿐. 외부 스킬 설치·자체 코딩 위임은 끈다 (design/02 §7).
- H-10 Hermes 개인 메모리와 AMPLAI 프로젝트 지식은 분리한다. 지식 변경은 제안 제출로만.
- H-11 Hermes 를 다른 앞단 에이전트로 바꿔도 AMPLAI 쪽은 바뀌지 않는다.
- H-12 Hermes 가 다른 기기에 있으면(OD-8) 접수·필터 조회만 여는 별도 listener 를 TLS(또는 출발지 고정 토큰)로 둔다.
  지금 서버는 loopback 전용, 운영자 토큰 1개, TLS 없음 (`runtime/cli.py:926-941`).
- H-13 승인은 운영자 본인 인증 + 현재 revision + 내용 digest 로만 (design/10 §4). S1a 의 `amplai approve` 는 AMPLAI 가 렌더한
  계약과 digest 를 보여 주고 `expected_contract_ref` 를 보낸다 (지금은 goal id 만 보낸다, `runtime/cli.py:224`,
  `control_plane/api_v3/client.py:104-105`). Hermes 가 보여 준 카드는 참고일 뿐 승인 근거가 아니다.
- H-14 Hermes VM 침해 시 영향 범위를 문서화한다: 연결된 사용자 사칭, goal 조회, 실행 중 에이전트에 steer 문장 주입,
  메신저를 통한 피싱. 승인·grant·권한 질문·취소는 범위 밖.

### K. 지식·문서 최신성
- K-1 메모리 8종과 Source→Proposal→Decision→Apply 를 `knowledge` 모듈로 (design/12 §1, design/02 §4). G-04.
- K-2 `KnowledgeService.governed_submit` 연결 (지금 `GOVERNANCE_REQUIRED`, `knowledge_runtime/service.py:84-93`).
- K-3 `doc_freshness` 모듈은 영향 분석과 검토 기록만 (V2 `scripts/amplai_docs.py` 약 5,000줄 전체 이식 안 함).
- K-4 두 모듈을 켜고 끌 수 있고, 메타하네스가 효과를 측정한다.

### U. 업그레이드
- U-1 배포 릴리스는 별도 kind `distribution-release`. 부품 릴리스 포인터와 섞지 않는다 (`runtime/execution/releases.py:241-242`).
  사내 overlay·데이터·사내 과제는 들어가지 않는다.
- U-2 **동결 먼저**: 업그레이드 시작 시 kill switch 를 켜고, 서버·야간 launchd agent 를 내리고, 모든 store(제품, meta)의
  owner lock 을 잡은 뒤에 drain 을 확인한다. 동결은 적용·검증이 끝날 때까지 유지한다.
- U-3 store 이전은 명시적 `migrate` 단계로만 한다. store 를 열 때 자동 이전하지 않고, 버전이 다르면 열기를 거부한다
  (지금은 열 때마다 DDL·inline 이전, `runtime/storage/store.py:188-189`). 이전 단계는 번호·재실행 안전, 미리보기 보고서,
  보고서 digest 에 묶인 운영자 승인 (design/19 §5, G-20). 백업은 승인 직후, 적용 직전에 만든다.
- U-4 백업 범위는 manifest 로 정한다: 제품·meta store, CAS, 키, 토큰 파일, 설정, installer 영수증(앱 저장소 소유 파일), 지식 저장소
  HEAD. 지식 저장소는 백업 복원이 아니라 governed revert commit 으로 되돌린다 (design/19 §6).
- U-5 새 upstream 평가기 버전을 사내에서 승인해야 실험이 돈다 (`evaluation/versions.py:215-233`). Q-suite 는 OD-7 에 따라 배포
  릴리스 자산으로 싣고 릴리스 루트에서 찾는다 (지금은 저장소의 `tests/v3` 를 찾는다, `evaluation/quality.py:112-120`).
- U-6 되돌리기 순서: 새 디렉터리로 복원 → store 밖 폐기 기록(revocation journal) 재적용 → reconcile → admission 재개 → 권한
  재승인. 데이터 되돌리기는 admission 재개 전에만 허용한다. pack 도 이전 상태로 되돌린다 (KitInstaller, `distribution/installer.py:236-364`).
- U-7 사내 승격 부품은 기준 부품 digest 를 기록한다. 기준이 바뀌면(어느 필드든) `stale` 로 표시하고 재비교 제안을 만든다.
  지금은 class A 필드만 같으면 후보 전체가 적용돼 새 upstream 의 다른 필드(class C 검증 정책 포함)를 가린다
  (`runtime/execution/releases.py:244-253,300-301`). 포인터 변경은 기존 승격 승인 (design/16 §7-9).
- U-8 `ops local-update` 는 local-dev 에서만 허용한다 (`runtime/cli.py:868-925`, 승인 없는 우회 경로).

### C. 사내 구성
- C-1 정의돼 있는 `onprem` profile(disabled, 전제 9개, T-112, `deployment/README.md:10-13`)을 qualify 한다.
- C-2 OpenCode 가 사내 OpenAI 호환 모델을 쓴다 (설정 지원 확인 필요 → qualification probe).
- C-3 사내 trial 의 토큰·시간을 게이트웨이가 측정한다 (지금 OpenCode 는 `usage: None`, `agent_drivers/http.py:641`).
- C-4 사내 과제는 L2 에 있고 배포 릴리스에 없다.

### J. Jev
- J-1 `judge` kind. 접근 권한 확보 후 연결, 질문 유형별 자격 시험 전 사용 금지, 사내는 데이터 정책 허용 시만.

## 5. 수용 기준

| ID | 기준 |
|---|---|
| AC-M1 | 시험용 코드 모듈을 고정 배포물로 추가·켬·끔·제거하고 코어 파일은 바뀌지 않는다 |
| AC-M2 | 고정 목록을 내장 모듈로 감싼 뒤 기존 전체 test 가 그대로 통과한다 |
| AC-M3 | 적합성 시험 미통과, `RECORD` digest 불일치, 의존 closure 밖 패키지, `.pth`, 고정 안 된 entry point 는 거부된다 |
| AC-M4 | "모듈 X 끈 부품" 제안이 보통 실험 절차로 비교된다 |
| AC-M5 | 권한·평가·검증 kind 변경은 class C 이고, `composition.py` 변경은 새 평가기 버전 없이 실험을 막는다 |
| AC-M6 | `intake_adapter`·`model_gateway` 는 authority 키에 접근할 수 없는 프로세스에서 돈다 |
| AC-H1 | 메시지 → intent → 계약 → CLI 승인(계약·digest 표시, `expected_contract_ref`) → 실행 → 완료 알림 (S1a e2e) |
| AC-H2 | Hermes 토큰으로 승인, grant 발급, `pause`·`cancel` steering(두 경로 모두)을 호출하면 거부된다 |
| AC-H3 | JSON actor 위조, 연결표에 없는 사용자, 다른 사용자의 goal·event 조회가 거부된다 |
| AC-H4 | Hermes 대리 actor 가 `authority`·`destructive_change` 질문에 답하거나 의도 보정하면 거부된다 |
| AC-H5 | 같은 메시지 재전송은 goal 하나만 만든다. steer 빈도 제한이 작동한다 |
| AC-H6 | 구독 cursor 에 store 세대(incarnation)가 들어 있고, 복원 뒤 이전 cursor 는 `CURSOR_EXPIRED` + snapshot 이 된다 |
| AC-H7 | 폐기한 Hermes 토큰은 즉시 거부되고, 복원 뒤에도 되살아나지 않는다 |
| AC-H8 | Hermes VM 에서 허용 목록 밖 연결이 막힌다 |
| AC-H9 | 같은 도구 계약을 쓰는 stub 클라이언트가 Hermes 없이 AC-H1 을 통과한다 |
| AC-H10 | 알림 projection 에 PR 제목·로그 원문이 없다 |
| AC-H11 | (S1b) 만료·사용된·다른 revision 의 링크, GET 요청, CSRF 없는 POST 는 승인되지 않는다 |
| AC-K1 | Source→Proposal→Decision→Apply 가 사람 승인으로 끝까지 되고, 승인 없이는 canonical 이 안 바뀐다 |
| AC-K2 | 문서 영향 분석이 바뀐 코드·계약을 언급한 문서를 찾고 검토 기록이 남는다 |
| AC-K3 | 지식·문서 모듈을 끈 부품 제안이 측정된다 |
| AC-U1 | 미리보기 보고서(바뀔 것·지킬 것·되돌릴 수 없는 변환·되돌리기)가 있고, 승인 없이는 적용되지 않는다 |
| AC-U2 | 업그레이드 후 사내 승격 부품·사내 과제·실행 기록·지식이 그대로 있다 |
| AC-U3 | 배포 릴리스에 사내 overlay·데이터·사내 과제가 없다 |
| AC-U4 | 동결 없이, 또는 실행 중 작업이 있으면 업그레이드가 거부된다. 서버 재시작이 승인 전 이전을 일으키지 않는다 |
| AC-U5 | wheel 설치에서 새 평가기 버전을 사내 Q-suite 로 승인할 수 있고, 승인 전에는 실험이 시작되지 않는다 |
| AC-U6 | U-6 순서로 되돌린 뒤 폐기된 권한·토큰은 계속 폐기 상태이고, 지식 저장소는 revert commit 으로 맞춰진다 |
| AC-U7 | 기준이 바뀐 사내 승격 부품은 `stale` 로 표시되고, 재비교 제안이 생기며, 포인터는 승인 전까지 그대로다 |
| AC-U8 | local-dev 밖에서 `ops local-update` 가 거부된다 |
| AC-C1 | 사내 모델 trial 토큰이 게이트웨이 영수증으로 기록되고 결과 누락이 되지 않는다 |
| AC-C2 | onprem profile 에서 허용 목록 밖 외부 연결이 막힌다 (T-112 포함) |
| AC-C3 | OpenCode 사내 모델 qualification probe 가 통과한다 |
| AC-X1 | 서명 불일치 모듈·pack, 평가기 자기 변경 시도가 거부된다 (design/10 §8) |

## 6. 제약

- 3.0.0 wire schema, design-reference 불변. gate: G-04(지식), G-07(릴리스 무결성), G-15(pack), G-20(이전·삭제).
- class C 는 메타하네스가 제안만, 사람이 승인. 평가기 코드 변경은 eval-N.
- 외부 제품(Hermes, OpenCode, 사내 LLM)의 동작은 qualification 으로 확인한 뒤 의존한다.
