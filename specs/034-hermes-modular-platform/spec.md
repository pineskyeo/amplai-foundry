# Work 034 Spec: Hermes 오케스트레이터, 모듈 체계, 사내 설치

- 상태: 설계 초안 r8 (독립 검토 2회 반영 r3, 운영자 결정 2026-10-11 반영 r5, 사내 점검 2026-10-12 반영 r8: 사내 설치 세부 C-8~C-14, 업그레이드 리허설 U-9). 구현 없음.
- 근거 표기: `file:line` 은 HEAD `5e38d6b`, design-reference 는 `design/NN §k`. 추정은 "(추정)", 미확인은 "확인 필요".
  r8 에서 더한 C-8~C-14, U-9 의 `file:line` 은 main `2ab10a2` 이다.

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
| OD-5 | Hermes 는 연결된 운영자의 goal 을 일시정지·취소할 수 있다. 재개는 Hermes 가 멈춘 goal 만 (운영자가 멈춘 goal 은 운영자가 재개) | 확정 (2026-10-11, 변경) |
| OD-6 | 승인 페이지(S1b) 인증은 passkey 만, POST + CSRF | 확정 (2026-10-11) |
| OD-7 | 사내 평가기 재승인용 Q-suite 를 배포 릴리스 자산으로 싣는다 | 확정 (2026-10-11) |
| OD-8 | Hermes VM 은 이 Mac 안의 VM. VM 에서 AMPLAI 로는 접수 전용 listener(H-12)로만 들어온다 (VM 의 127.0.0.1 은 VM 자신이라 loopback 으로는 닿지 않는다; Hermes 를 VM 없이 Mac 에 두면 운영자 토큰 파일을 읽을 수 있어 제외) | 확정 (2026-10-12 갱신) |
| OD-9 | 메신저는 Slack. 운영자 직접 알림(`notifier`)은 Hermes 와 다른 별도 Slack 앱의 Incoming Webhook (Q-10) | 확정 (2026-10-11) |
| OD-10 | 지식은 별도 git 저장소. 3층: Markdown 파일(정본) / 지식 모듈(유일한 쓰기·승인·색인) / MCP(읽기·검색·제안 창구). 사내는 지식 공간(Knowledge Space) 단위로 관리 주체·승인자를 두고 프로젝트가 골라 연결한다 (K-5~K-9) | 방향 확정, 세부 확인 대기 |
| OD-11 | Hermes 가 쓸 모델·비용: 구독 연결 가능 여부를 먼저 확인한 뒤 결정 | 확인 중 |
| OD-12 | 야간 메타 루프는 앞단 에이전트로 제어한다: 상태 보고, 멈춤·건너뛰기·조이기, 상시 허락 안의 시작(조건부, N-9)은 앞단 에이전트, 확장·승인은 사람 (N-1~N-9) | 확정 (2026-10-11, Q-8 = B, Q-9 = 제품 store) |
| OD-13 | 앞단 에이전트와 메신저는 교체 가능한 모듈이다. Hermes·Slack 은 첫 구현이다 (F-1~F-6) | 확정 (2026-10-11) |
| OD-14 | 모델 제공자와 메신저는 꽂았다 뺐다 하는 모듈이다. 사내는 외부 모델 API·Slack 이 막혀 있으므로(2026-10-12 점검) 사내 LLM 과 대체 메신저가 먼저 붙고, 연결이 허용되면 외부 모델·Slack 을 설정만으로 더한다. WSL 은 docker 를 설치한다 (C-6, C-7) | 확정 (2026-10-12) |
| OD-15 | 코드 저장소 연결(PR 게시, 지식 승인 증거)도 모듈이다 (`git_host` kind). 첫 구현은 GitHub·사내 GitHub, 다른 git 서버는 나중에 붙인다. 지금 `gh` 고정(`runtime/execution/publish.py:86-104`)을 이 모듈 뒤로 옮긴다 | 확정 (2026-10-12) |
| OD-16 | AMPLAI 는 여러 분야에 쓴다. 모든 기능은 모듈이다(에이전트 역할 포함). 모듈은 교체·수정·추가·삭제할 수 있고, 그 변경이 다른 기능에 영향을 주지 않는다. 모듈은 묶음(pack: foundation·kit·plugin 수준)으로 구성해 설치·해제한다 (M-10~M-15) | 확정 (2026-10-12) |
| OD-17 | 지식: 정본은 git 파일(공간별 저장소), 쓰기·검증·승인 기록은 지식 모듈 하나, 접근은 MCP. 검사(중복·모순·stale)·정리 후보·검색 색인은 교체 가능한 부품(후보·보고만, 쓰기 없음). 보관 방식도 모듈(첫 구현 git). 개인·작업 기억은 별도 메모리 모듈/MCP, 정본 반영은 제안으로만 (K-10~K-12) | 확정 (2026-10-12) |

## 3. 범위

포함: 모듈 체계, Hermes 연결(S1a, S1b), 앞단 에이전트·메신저 모듈(S1c), 야간 메타 루프 제어(S1d), 지식·문서 최신성 모듈, 3층 업그레이드, 사내 구성, Jev.
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

- M-10 **모든 기능은 모듈**이다 (OD-16). 코어에 남는 것은 모듈을 묶는 최소 골격뿐이다: 저장소·이벤트, 권한·승인, 스케줄·예산,
  검증 판정, 모듈 레지스트리·로더. 그 밖의 기능(드라이버, 실행 전략, 판단자, 지식, 문서 최신성, 접수 어댑터, 알림, 모델 게이트웨이,
  코드 저장소 연결, 에이전트 역할 등)은 모듈 kind 로 둔다.
- M-11 **에이전트 역할도 모듈**이다 (`agent_role` kind): 계획자, 리뷰어, 조사자, 지휘자(lead), 분할·단계 계획, 판정자, 제안자, 정리(dreaming).
  역할마다 입력·출력 schema 를 고정하고 모델·지시문·도구는 교체한다. 역할 모듈은 권한이 없다(기본 읽기 전용, 제안만). 지금은
  `runtime/execution/readonly_turn.py` 를 공유하는 각 호출부(`planner_codex.py`, `strategy_runner.py`, `meta_harness/judges.py`,
  `meta_harness/proposer.py`)에 흩어져 있다.
- M-12 **격리 불변식**: 모듈은 선언한 인터페이스(kind 의 interface version)와 선언한 의존 외에 다른 모듈을 참조하지 않는다.
  - 코드 경계: 모듈 사이 직접 import 금지를 정적 검사(import 경계 lint)로 CI 에서 막는다.
  - 데이터 경계: 모듈은 자기 namespace 의 기록만 쓴다. 다른 모듈의 기록은 공개 조회 API 로만 읽는다.
  - 실패 경계: 한 모듈의 실패·제거가 다른 모듈을 멈추지 않는다(해당 기능만 Hold 또는 기본 구현으로 대체).
- M-13 **추가·삭제·교체 안전**: 모듈을 켜고 끄고 바꿀 때 아래를 기계로 확인한다.
  - kind 별 적합성 시험(conformance suite) 통과 없이는 켤 수 없다 (M-2 와 같음).
  - 제거 시험: CI 에 "각 선택 모듈을 뺀 구성" 으로 코어 시험을 돌리는 단계를 둔다. 다른 기능 시험이 깨지면 그 모듈은 격리 위반이다.
  - 의존하는 모듈이 있으면 제거를 막고 의존 목록을 보여 준다.
- M-14 **묶음(pack)**: 여러 모듈을 design/13 의 Capability Pack 으로 묶는다 (id, 버전, 게시자, 서명, 의존, 필요 권한, 검증 프로필,
  평가 과제; `capability-pack.schema.json`). 수준 예: foundation(코어 골격 + 필수 모듈), kit(분야 묶음: software, documents, research,
  semiconductor-dc 등), plugin(단일 기능). 설치 전 실패 조건은 design/13 §2 그대로(의존 순환, 버전 충돌, 모르는 게시자, 권한 상승).
- M-15 메타하네스는 모듈·묶음 단위로 켜고 끈 구성을 보통 제안으로 측정한다 (M-7). 분야가 달라도 같은 측정·승격 절차를 쓴다.

### H. Hermes
- H-1 Hermes 는 `/api/v3` 만 쓴다 (design/18 §1).
- H-2 Hermes 서비스 actor 권한: `goal.submit`, `runtime.read`(연결된 사용자의 goal 만), `goal.steer`(종류 제한, H-3),
  `question.answer`(종류 제한, H-4), `knowledge.propose`, `meta.read`(요약), `goal.cancel`(신설, 좁은 권한, OD-5).
  없음: `execution.approve`, grant 발급.
- H-3 steering 종류는 actor 종류별 허용 목록으로 모든 경로에서 검사한다. Hermes 허용: `constraint_add`, `priority_change`,
  `new_evidence`, `acceptance_change`(새 contract revision → 재승인), `pause`, `cancel`(OD-5). `resume` 은 그 goal 을 멈춘
  actor 가 Hermes 일 때만 (멈추기는 쉽게, 다시 켜기는 신중하게). 로컬 취소 경로는 `execution.approve` 대신 새 `goal.cancel` 권한도
  받는다 (`runtime/execution/loop.py:89-90`, 권한 코드 변경이라 class C). Hermes 의 일시정지·취소는 즉시 운영자에게 알리고(누가, 왜),
  취소는 design/17 §4 의 단계 표현을 쓰며, 빈도 제한을 둔다.
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
- H-11 Hermes 를 다른 앞단 에이전트로 바꿔도 AMPLAI 쪽은 바뀌지 않는다 (구체화: F-1~F-6).
- H-12 Hermes 가 다른 기기에 있으면(OD-8) 접수·필터 조회만 여는 별도 listener 를 TLS(또는 출발지 고정 토큰)로 둔다.
  지금 서버는 loopback 전용, 운영자 토큰 1개, TLS 없음 (`runtime/cli.py:926-941`).
- H-13 승인은 운영자 본인 인증 + 현재 revision + 내용 digest 로만 (design/10 §4). S1a 의 `amplai approve` 는 AMPLAI 가 렌더한
  계약과 digest 를 보여 주고 `expected_contract_ref` 를 보낸다 (지금은 goal id 만 보낸다, `runtime/cli.py:224`,
  `control_plane/api_v3/client.py:104-105`). Hermes 가 보여 준 카드는 참고일 뿐 승인 근거가 아니다.
- H-14 Hermes VM 침해 시 영향 범위를 문서화한다: 연결된 사용자 사칭, goal 조회, 실행 중 에이전트에 steer 문장 주입,
  메신저를 통한 피싱. 승인·grant·권한 질문·취소는 범위 밖.

- H-15 일시정지는 진짜 `paused` 상태다 (운영자 결정 2026-10-11, design-reference 기준). 지금 로컬 제품은 loop 가 요청하지 않은
  pause 를 받으면 시도를 끝내고 goal 을 `held` 로 둔다 (`runtime/execution/loop.py:237-238`,
  `tests/e2e/test_rc07_steer.py::test_a_pause_nobody_takes_over_stops_the_process`).
  - Run 상태에 `pausing → paused` 를 둔다 (design/08 §상태). 안전한 체크포인트에서 멈추고 세션·작업 공간을 보존한다.
  - 재개는 같은 세션을 이어 가되, 세션 id·드라이버·모델 버전·작업 공간 digest·계약 revision 이 맞을 때만이다 (design/11 resume).
    맞지 않으면 현재 계약·체크포인트·근거 요약으로 새 세션을 만들어 이어 실행하고, "같은 세션 재개" 와 "이어 실행" 을 구분해
    기록한다 (design/11, design/08 복구).
  - 멈춘 동안 들어온 지시·계약 revision 을 반영한 뒤에만 재개한다 (design/08 test 요구).
  - 재개 권한: Hermes 가 멈춘 것은 Hermes 도, 운영자가 멈춘 것은 운영자만 (OD-5).
  - 멈춘 동안의 자원(작업 공간, lease)과 시간 초과 처리는 S1e 설계에서 정한다.

- H-16 운영자 이벤트 목록 `GET /api/v3/events` 도 cursor 를 `<store incarnation>:<seq>` 로 쓴다 (운영자 결정 2026-10-11).
  세대가 다르거나 seq 가 최대값보다 크면 `CURSOR_EXPIRED` 와 공통 snapshot 경로 `GET /api/v3/snapshot` 을 돌려준다
  (design/18 §4). intake feed 응답 안의 snapshot 도 이 경로로 통일한다. SSE `Last-Event-ID` 도 같은 형식이다.
  지금 운영자 목록은 숫자 cursor 이고 저장소 안에 읽는 곳은 없다 (`control_plane/api_v3/server.py` events, client·cli 검색 0건).

### F. 앞단 에이전트·메신저 교체 (OD-13)
- F-1 세 층으로 나눈다.
  1. 접수 어댑터: `intake_adapter` 코드 모듈(별도 프로세스). 첫 구현은 AMPLAI MCP 서버이고, 도구 계약은 앞단 에이전트와 무관하다.
  2. 앞단 에이전트 키트: 설정 묶음(에이전트 설정 틀, 지시문, MCP 연결, 끌 도구 목록, cron 작업, 점검표). 첫 구현은 Hermes 키트.
  3. 메신저: 대화 채널은 앞단 에이전트의 gateway 설정(키트 안)이고, 운영자 직접 알림은 `notifier` 코드 모듈이다. 첫 구현은 Slack.
- F-2 코어에는 앞단 에이전트·메신저 이름이 없다. 설정 `intake.adapters[]` 가 adapter id 마다 토큰 파일, 연결표, 권한 상한을
  정한다. 경로는 `/api/v3/intake/{adapter_id}`, actor 표지는 `authn_context_ref = intake:<adapter_id>`, 중복 방지 key 는
  `<adapter_id>:<digest>`, 연결표 항목은 (메신저, workspace, 사용자 id) 다. 지금은 `hermes` 가 고정돼 있다
  (`control_plane/api_v3/intake.py:50,89,249`, `runtime/contracts/intake.py:33-35`).
- F-3 wire `source_channel` 은 3.0.0 에서 `cli|api|hermes|import` 로 고정이다
  (`runtime/contracts/data/schemas/intent-envelope.schema.json:31-37`). 값 `hermes` 를 "앞단 에이전트 접수 채널" 의 wire 이름으로
  문서화하고, 실제 adapter 는 `authn_context_ref` 로 구분한다. wire 는 바꾸지 않는다.
- F-4 도구 계약(도구 이름, 입력·출력 schema)에 버전과 적합성 시험이 있다. 다른 앞단 에이전트는 같은 MCP 서버에 키트만 바꿔
  붙인다. MCP 를 못 쓰는 에이전트는 같은 도구 계약을 구현한 다른 `intake_adapter`(예: webhook)를 쓴다.
- F-5 `notifier` kind 를 S0 의 kind 목록에 더한다(인터페이스 v1, `runtime/modules/spec.py:24-30`). 입력은 고정 schema
  projection 과 이벤트 id 뿐이고(원문 없음, H-8), 같은 이벤트 id 는 한 번만 보내며, 받는 곳은 설정된 운영자 연결뿐이다.
  앞단 에이전트가 꺼지거나 침해돼도 운영자 알림은 간다 (H-3, S1a contract review P2-2).
- F-6 교체 절차: 새 키트·구현 → 적합성 시험(AC-H9 의 stub client 포함) → 설정 전환 → 이전 adapter 토큰 폐기. 두 adapter 를
  함께 켤 수 있고 서로 분리된다 (AC-F2).

### N. 야간 메타 루프 제어 (OD-12)
- N-1 역할을 나눈다.
  - 시계: 서비스 관리자(macOS launchd, Linux systemd timer 또는 supervisor, C-8). 5분마다 `amplai meta nightly tick` 을 실행한다. tick 은 결정적 규칙으로 "예정 시각이 됐거나 유효한 시작
    요청이 있으면 그날 밤을 시작" 하고, 아니면 바로 끝난다. 지금은 01:00 에 `run` 한 번이다
    (`deployment/launchd/ai.amplai.meta-nightly.plist.template`).
  - 밤 진행: `NightlyRunner`. 결정적 절차다 (`meta_harness/nightly.py`).
  - 대화·보고·요청: 앞단 에이전트.
  - 시작·확장·승인: 사람.
  - 스케줄러는 LLM 으로 목표를 만들지 않는다 (design/02 §2).
- N-2 새 권한 `nightly.control` 은 앞단 에이전트용 좁은 권한이다.
  - 할 수 있는 것:
    - 지금 멈춤. 실행 중인 밤은 다음 trial guard 에서 멈춘다.
    - 날짜 건너뛰기.
    - 오늘 밤 조이기. 밤 예산을 줄이거나 종료 시각을 앞당긴다.
    - 상시 허락 안에서 그날 밤 시작 (N-9 조건).
    - 자기가 건 건너뛰기·조이기를 밤 시작 전에 되돌리기 (OD-5 와 같은 원칙).
  - 할 수 없는 것: N-9 밖의 시작, 상시 허락 발급·연장, 예산·시간 늘리기, 사람이 건 정지 해제,
    focused·holdout·canary·promote·평가기 승인.
- N-3 제어 요청은 제품 Control Plane 의 authority service 를 거쳐 제품 store 의 `nightly-control` 기록으로 남는다
  (design/17 §5). 감사·중복 방지는 S1a 경로를 그대로 쓴다. 운영자는 같은 경로를 CLI 로 쓴다
  (`amplai meta nightly stop|skip|tighten`).
- N-4 실행 중인 밤도 멈출 수 있어야 한다.
  - 지금은 밤 프로세스가 meta store 를 배타적으로 잡는다 (`runtime/storage/store.py:166-173`; 제품 배포 객체는 store 를 쓰기
    모드로 연다, `runtime/local_deployment.py:378`). 그래서 밤 동안에는 `meta nightly revoke`·`status` 도 `ACTIVE_OWNER` 로 막힌다.
  - 밤 프로세스는 preflight 와 모든 trial guard 에서 제품 store 를 읽기 전용으로 열어 제어 기록을 읽고, 가장 엄격한 것을
    적용한다. 읽지 못하면 멈춘다.
- N-5 상태 조회는 meta store 를 읽기 전용으로 연다 (대시보드 방식, `runtime/meta_commands/dashboard.py:48`). 앞단 에이전트에는
  고정 schema 요약만 준다.
- N-6 알림: 밤 시작, 멈춤(이유), 끝, drift, 승인 대기를 앞단 에이전트 feed 로 보낸다(meta 세대 cursor). 그중 앞단 에이전트가 건
  멈춤, unknown effect, drift 는 `notifier` 로도 보낸다.
- N-7 아침 보고는 키트의 cron 이 요약을 가져와 보낸다. 승인 대기 항목은 승인 페이지 링크(S1b)로만 넘긴다. 앞단 에이전트는
  승인하지 못한다 (H-13).
- N-8 일정·예산·칸 변경은 앞단 에이전트가 초안을 낼 수 있다. 효력은 사람이 상시 허락을 다시 발급해야 생긴다.
- N-9 앞단 에이전트의 시작 요청은 다음을 모두 만족할 때만 효력이 있다 (운영자 결정 Q-8 = B, 2026-10-11).
  - 유효한 상시 허락이 있고, 그 기간·칸·밤 예산·상한 안이다 (`meta nightly run` 은 허락 없이 `NIGHT_STOPPED`).
  - 그날 밤이 아직 돌지 않았다. 이미 돈 날짜는 다시 돌지 않는다 (`meta_harness/nightly.py:520-529`, `NIGHT_STATE`).
  - 사람이 정한 시작 가능 시간대 안이다 (설정, 예: 22:00~06:00). 같은 계정의 낮 사용량을 지킨다.
  - 사람이 건 멈춤·건너뛰기가 없다.
  - 빈도 제한 안이다.
  - 시작하면 `notifier` 로 운영자에게 바로 알린다 (누가, 언제, 왜).

### K. 지식·문서 최신성
- K-1 메모리 8종과 Source→Proposal→Decision→Apply 를 `knowledge` 모듈로 (design/12 §1, design/02 §4). G-04.
- K-2 `KnowledgeService.governed_submit` 연결 (지금 `GOVERNANCE_REQUIRED`, `knowledge_runtime/service.py:84-93`).
- K-3 `doc_freshness` 모듈은 영향 분석과 검토 기록만 (V2 `scripts/amplai_docs.py` 약 5,000줄 전체 이식 안 함).
- K-4 두 모듈을 켜고 끌 수 있고, 메타하네스가 효과를 측정한다.
- K-5 **지식 공간(Knowledge Space)**: 지식 관리 단위. 공간 하나 = git 저장소 하나 (예: 결제 도메인, 사내 코딩 표준). 공간마다
  관리 주체(owner), 승인자(approver), 기여자(contributor, 제안 가능), 독자(reader), 데이터 등급을 둔다 (design/10 §2 governor 역할).
- K-6 프로젝트(app)는 쓸 공간을 골라 연결한다. 연결은 "최신 따라가기" 또는 "revision 고정". 작업 context 는 연결된 공간에서만 고른다
  (design/02 §4 교차 프로젝트 무제한 검색 금지, design/12 §3-§4).
- K-7 변경 흐름: 제안(사람·Hermes·작업 에이전트의 관찰) → 지식 모듈 검증(형식, 출처, 기존 지식과 모순 검사, design/12 §6) →
  공간 저장소에 변경 요청(merge request) → 승인자 승인 → 반영 → AMPLAI 가 Decision·Apply 와 승인자·commit digest 를 기록 →
  연결된 프로젝트에 새 revision 알림. 승인 없이는 정본이 안 바뀐다.
- K-8 여러 명이 동시에 고칠 때: 제안은 기준 revision 을 갖는다. 승인 전 대상이 바뀌면 다시 검증·재승인한다 (stale). 서로 모순되는
  제안은 모순 검사에서 표시한다.
- K-9 승인 경로: 사내 git 서버의 리뷰·승인(CODEOWNERS, 보호 브랜치)을 쓰고, 지식 모듈이 git 서버 API 로 승인자 신원과 승인된 commit 을
  확인해 사람 승인 증거로 기록한다. 이 대응은 계약 결정이다 (D-115, 확정 2026-10-12). 조건: 보호 브랜치, 공간별 CODEOWNERS, 승인 뒤 새 커밋이면 승인 무효(dismiss stale approvals), 관리자 우회 병합은 사람 승인으로 인정하지 않는다. AMPLAI 는 git 서버 API 로 승인자·승인 commit 을 확인한다.

- K-10 지식 정본은 git 파일이고, 지식 모듈이 유일한 쓰기 창구다. 접근은 MCP(읽기·검색·제안) (OD-17).
- K-11 검사·정리·검색 부품: 중복 탐지, 모순 탐지, stale 탐지, 정리 후보 보고, 검색 색인(키워드·의미 검색). 부품은 후보·보고만 내고
  정본을 쓰지 않는다. 색인은 git 에서 다시 만들 수 있다. 메타하네스가 부품별 효과를 측정한다.
- K-12 보관 방식도 모듈(첫 구현 git). 다른 보관 방식으로 바꿔도 승인·감사 규칙(K-7~K-9)은 같다. 개인·작업 기억(Hermes 메모리,
  에이전트 작업 메모)은 별도 메모리 모듈·MCP 이고 정본이 아니다. 정본 반영은 제안으로만 (H-10).

### U. 업그레이드
- U-1 배포 릴리스는 별도 kind `distribution-release`. 부품 릴리스 포인터와 섞지 않는다 (`runtime/execution/releases.py:241-242`).
  사내 overlay·데이터·사내 과제는 들어가지 않는다.
- U-2 **동결 먼저**: 업그레이드 시작 시 kill switch 를 켜고, 서버·야간 시계(서비스 관리자: macOS launchd, Linux systemd 또는 supervisor, C-8)를 내리고, 모든 store(제품, meta)의
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
- U-9 **업그레이드 리허설**: 이전 배포 릴리스로 만든 사내 모양 데이터(L2 승격 부품·사내 과제·사내 칸, L3 실행 기록·CAS·지식 저장소·
  store 밖 토큰·폐기 기록·notifier outbox·야간 제어 기록)를 새 릴리스로 `ops upgrade` 하고, 불변식(AC-U2, AC-U6, AC-U7)을 기계로
  비교한다. 데이터는 새 코드가 아니라 이전 릴리스 wheel 이 만든다. CI 리허설은 S3 끝 기준이고, 사내 실제 업그레이드 전에는 최신 백업을
  새 디렉터리에 복원한 사본으로 같은 리허설을 돌린다 (plan §3.5a.6).

### C. 사내 구성
- C-1 정의돼 있는 `onprem` profile(disabled, 전제 9개, T-112, `deployment/README.md:10-13`)을 qualify 한다.
- C-2 OpenCode 가 사내 OpenAI 호환 모델을 쓴다 (설정 지원 확인 필요 → qualification probe).
- C-3 사내 trial 의 토큰·시간을 게이트웨이가 측정한다 (지금 OpenCode 는 `usage: None`, `agent_drivers/http.py:641`).
- C-4 사내 과제는 L2 에 있고 배포 릴리스에 없다.

- C-5 사내 설치 대상은 WSL2 와 Ubuntu 서버다 (운영자 2026-10-12; 사내 환경 확인은 운영자가 한다). 지금까지 실제 컨테이너 실행은
  이 Mac 의 colima 에서만 검증했다. CI 는 ubuntu-latest 에서 test 만 돈다 (`.github/workflows/ci.yml:11`).
  - colima 전제 두 곳을 Linux 에서 확인한다: 작업 공간이 `$HOME` 아래여야 한다는 전제 (`runtime/local_deployment.py:300`),
    bind mount 소유자를 colima 가 컨테이너 uid 로 맞춰 준다는 전제 (`runtime/execution/codex.py:78`,
    `agent_drivers/opencode_port.py:104`). Linux Docker 는 호스트 소유자를 그대로 쓰므로 uid 65534 가 쓰지 못할 수 있다 (추정, 확인 필요).
  - 앞단 에이전트 격리: AMPLAI 와 앞단 에이전트를 같은 사용자·같은 파일 시스템에 두지 않는다. 권장 구성은 앞단 에이전트를 호스트 폴더
    연결 없는 별도 컨테이너에 두고 접수 전용 listener(H-12)만 여는 것이다. WSL2 의 배포판끼리는 같은 가상 컴퓨터를 쓰고 Windows 를 거쳐
    파일에 닿을 수 있어 격리로 보지 않는다.
  - Linux 자격 시험(container qualify, 칸 probe, 짧은 calibration)을 WSL2 와 Ubuntu 에서 각각 통과해야 사내 설치를 시작한다.

- C-6 모델 제공자는 모듈이다 (S0 `model_gateway`·`driver` kind). 사내 첫 구현은 사내 LLM(OpenAI 호환 여부는 Q-6) + OpenCode 이고,
  Anthropic·OpenAI 같은 외부 제공자는 연결이 허용되면 설정으로 켠다. 칸(cell)은 제공자와 무관하게 같은 측정·승격 절차를 쓴다.
  사내 점검(2026-10-12): Ubuntu 18.04 서버·RHEL 7.6 은 외부 접속이 전부 막혀 있고, WSL 은 proxy 경유로 GitHub·PyPI 만 열려 있다
  (Anthropic·OpenAI·Slack 은 403, 원인 확인 중).
- C-7 닫힌 망 설치: 서버에서 PyPI·이미지 레지스트리에 닿지 못하므로 배포물은 wheel 묶음(hash 고정)과 이미지 tar(또는 사내 레지스트리)로
  들어간다. 메신저도 사내 메신저 구현을 `notifier`·앞단 에이전트 키트로 추가할 수 있어야 한다 (F-1, F-5).

- C-8 **서비스 관리자**: 상시 프로세스(제품 서버, 모델 게이트웨이, 원격 listener)와 야간 시계는 서비스 관리자 하나로 다룬다. 구현은
  launchd(macOS), systemd(Linux, system unit 우선, user unit + linger 차선), AMPLAI supervisor(systemd·sudo 가 없는 호스트)다.
  AMPLAI 는 unit 파일을 출력만 하고 설치하지 않는다 (지금 `meta nightly print-agent` 와 같은 원칙,
  `runtime/meta_commands/nightly.py:18-19`). 업그레이드 동결(U-2)은 이 인터페이스로 세 구현 모두에서 같은 결과를 낸다.
  지금 서비스 관리 코드는 launchd 뿐이다 (`runtime/cli.py:923,1012-1019`, `deployment/launchd/`; main 의 저장소 전체에서 `systemd` 검색 0건).
- C-9 **샌드박스 uid**: Linux 에서 컨테이너 uid 는 AMPLAI 호스트 사용자와 다른 고정 uid(기본 65534)로 둔다. 작업 공간과 run home 접근은
  호스트 사용자가 POSIX ACL 로 그 uid 에만 준다. 컨테이너를 호스트 uid 로 돌리지 않는다 (탈출 시 store·키 노출). 지금 코드는 colima 가
  bind 소유자를 맞춰 준다는 전제로 자격 파일을 0644 로 바꾼다 (`runtime/execution/codex.py:78-79`, `agent_drivers/opencode_port.py:104-105`).
- C-10 **컨테이너 엔진 설정**: 엔진(`docker`|`podman`)은 설정 한 곳에서 정하고 모든 컨테이너 호출이 그것을 쓴다. 지금 `ContainerSandbox` 는
  engine 인자를 받지만(`sandbox/container.py:33,56-59`) 제품 경로는 기본값만 쓰고(`runtime/local_deployment.py:617,644,711,722,913`),
  정리 호출은 `docker` 를 직접 부른다 (`runtime/execution/readonly_turn.py:207,290,394`,
  `verification/runtime/patch_commands.py:80,96,159,171`, `verification/runtime/integration.py:108,114`).
- C-11 **오프라인 설치 묶음**: 서명된 묶음 하나가 wheel 묶음(의존 closure 전체, 버전·hash 고정), 이미지 tar, 이미지 config digest 목록,
  Q-suite 자산(OD-7), 설치 검증 도구를 싣는다. 설치는 서명·hash 검증을 통과하기 전에는 아무것도 바꾸지 않는다 (G-07). 지금 hash 고정
  목록은 intake MCP 서버용 하나뿐이다 (`integrations/intake_mcp/requirements-hashes.txt:1-4`).
- C-12 **경로·로그·백업 배치**: 설치본(릴리스별 venv), 데이터(store, CAS, 작업 공간), 비밀, 로그, 백업을 설정 가능한 별도 경로로 둔다.
  store 와 작업 공간은 로컬 Linux 파일 시스템이어야 한다 (store 는 `flock` + SQLite WAL, `runtime/storage/store.py:169,186`).
  NFS·WSL 의 `/mnt/<drive>` 같은 경로는 거부한다. 백업은 live store 밖이어야 한다 (지금 검사, `runtime/recovery/service.py:43-44`).
- C-13 **지원 OS 표**: 사내 대상 OS 마다 필요한 것(Python, git, 엔진, cgroup, 서비스 관리자, 파일 시스템)과 자격 시험 결과를
  `deployment/support-evidence.json` 의 근거로 남긴다. 근거 없는 OS 는 `supported` 가 아니다 (`deployment/README.md:5-13`).
- C-14 **사이트 사전 점검**: `scripts/site_env_check.sh`(읽기 전용, `origin/feat/034-site-env-check`)를 C-9·C-10·C-12 항목(파일 시스템 종류,
  ACL 지원, 엔진 도달, cgroup 제한 적용, 아키텍처)까지 넓히고, 그 보고서가 C-13 의 첫 근거가 된다.

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
| AC-H2 | Hermes 토큰으로 승인·grant 발급은 거부된다. `pause`·`cancel` 은 연결된 운영자의 goal 에만 되고, 운영자가 멈춘 goal 의 `resume` 은 거부된다. 일시정지·취소마다 운영자 알림이 남는다 |
| AC-H3 | JSON actor 위조, 연결표에 없는 사용자, 다른 사용자의 goal·event 조회가 거부된다 |
| AC-H4 | Hermes 대리 actor 가 `authority`·`destructive_change` 질문에 답하거나 의도 보정하면 거부된다 |
| AC-H5 | 같은 메시지 재전송은 goal 하나만 만든다. steer 빈도 제한이 작동한다 |
| AC-H6 | 구독 cursor 에 store 세대(incarnation)가 들어 있고, 복원 뒤 이전 cursor 는 `CURSOR_EXPIRED` + snapshot 이 된다 |
| AC-H7 | 폐기한 Hermes 토큰은 즉시 거부되고, 복원 뒤에도 되살아나지 않는다 |
| AC-H8 | Hermes VM 에서 허용 목록 밖 연결이 막힌다 |
| AC-H9 | 같은 도구 계약을 쓰는 stub 클라이언트가 Hermes 없이 AC-H1 을 통과한다 |
| AC-H10 | 알림 projection 에 PR 제목·로그 원문이 없다 |
| AC-H11 | (S1b) 만료·사용된·다른 revision 의 링크, GET 요청, CSRF 없는 POST 는 승인되지 않는다 |
| AC-F1 | 다른 adapter id 의 stub 앞단 에이전트가 설정만 바꿔 AC-H1 을 통과한다. 코어 코드는 바뀌지 않는다 |
| AC-F2 | adapter 두 개를 함께 켜면 토큰·연결표·권한 상한이 분리되고, 한 adapter 의 토큰은 다른 adapter 경로에서 401 이다 |
| AC-F3 | `notifier` 는 같은 이벤트를 한 번만 보내고 projection 밖 텍스트를 받지 않는다. 앞단 에이전트를 꺼도 운영자 알림이 간다 |
| AC-N1 | 실행 중인 밤에 앞단 에이전트가 멈춤을 요청하면 다음 trial guard 에서 밤이 멈추고(이유 기록) 운영자 알림이 간다 |
| AC-N2 | 앞단 에이전트의 연장·예산 증가·사람이 건 정지 해제 요청은 거부된다 |
| AC-N3 | 건너뛴 날짜의 밤은 preflight 에서 멈춘다. 앞단 에이전트는 자기가 건 건너뛰기만 해제한다 |
| AC-N4 | 밤이 도는 동안 `amplai meta nightly status` 와 앞단 에이전트 요약 조회가 된다 |
| AC-N5 | 제어 기록을 읽을 수 없으면 밤이 멈춘다 |
| AC-N6 | 앞단 에이전트의 시작 요청은 N-9 를 모두 만족할 때만 다음 tick 에서 그날 밤을 시작하고 운영자 알림을 남긴다. 허락 밖, 이미 돈 날짜, 시간대 밖, 사람 정지 중, 빈도 초과면 거부된다 |
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
| AC-C4 | systemd 와 supervisor 각각에서: 서버가 죽으면 다시 뜨고, 야간 tick 이 5분마다 돌며 밤이 돌고 있으면 바로 끝나고(`ACTIVE_OWNER`), AMPLAI 는 unit 을 출력만 하고 설치하지 않는다 |
| AC-C5 | launchd·systemd·supervisor 세 구현 모두에서 동결 중 서버가 다시 뜨지 않고 tick 이 밤을 시작하지 않는다. AC-U4 가 세 구현에서 통과한다 |
| AC-C6 | Linux 에서 컨테이너 uid 가 호스트 uid 와 다르고, 에이전트가 작업 공간에 쓰고, 호스트가 diff 를 읽고 정리한다. 컨테이너는 runtime root·키·자격 원본을 읽지 못한다 |
| AC-C7 | 설정 엔진이 `podman` 이면 실행·정리 경로 어디에서도 `docker` 를 부르지 않는다 |
| AC-C8 | 네트워크 없는 호스트에서 묶음으로 설치된다(`--no-index`). 서명·wheel hash·이미지 config digest 중 하나라도 어긋나면 아무것도 바꾸기 전에 거부된다 |
| AC-C9 | runtime root·작업 공간이 로컬 파일 시스템이 아니면(NFS, 9p/drvfs) 서버가 시작을 거부한다 |
| AC-C10 | 지원 표의 OS 마다 `support-evidence.json` 근거(사이트 점검 보고서, container qualify, 칸 probe)가 있고, 근거 없는 OS 는 `supported` 가 아니다 |
| AC-U9 | 이전 릴리스가 만든 사내 모양 데이터를 새 릴리스로 올린 리허설에서 AC-U2·AC-U6·AC-U7 불변식이 기계 비교로 통과하고, 같은 리허설이 백업 복원 사본에서도 돈다 |
| AC-X1 | 서명 불일치 모듈·pack, 평가기 자기 변경 시도가 거부된다 (design/10 §8) |

## 6. 제약

- 3.0.0 wire schema, design-reference 불변. gate: G-04(지식), G-07(릴리스 무결성), G-15(pack), G-20(이전·삭제).
- class C 는 메타하네스가 제안만, 사람이 승인. 평가기 코드 변경은 eval-N.
- 외부 제품(Hermes, OpenCode, 사내 LLM)의 동작은 qualification 으로 확인한 뒤 의존한다.
