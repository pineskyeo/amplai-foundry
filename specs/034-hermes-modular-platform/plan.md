# Work 034 Plan: 구조, 인터페이스, 순서

- 상태: 설계 초안 r7 (Q-8 = B 반영 r7; 독립 검토 2회 반영 r3, 운영자 결정 2026-10-11 반영 r5, 운영자 지시 2026-10-11 반영 r6: 야간 루프 제어 OD-12, 앞단 에이전트·메신저 모듈 OD-13; r6 의 코드 근거는 main `76e2874`). spec: `spec.md`.

## 0. 검토 이력

| 회차 | 관점 | 결과 | 반영 |
|---|---|---|---|
| r1 → r2 | 원본 설계 부합성·코드 실현성 | 막힘 2, 큼 8, 작음 5 | 권한 이름, 취소, 모듈 전달 경로, 두 층 모듈, 대리 identity, 승인 페이지 분리, 이벤트 API, migration-plan 용도, 되돌리기, release-set 충돌, AC 보강 |
| r2 → r3 | 보안·업그레이드 운영·r2 사실 확인 | 막힘 3, 큼 7, 작음 4 | steering 경로 취소 차단, Q-suite 자산화, 동결·명시적 이전, 되돌리기 순서, cursor 세대, 눈 감은 승인, 원격 listener, 승격 부품 가림, 백업 범위, 신뢰 루트, classify digest 범위, 알림 projection |

## 1. 근거

### 1.1 design-reference (불변)

| 조항 | 내용 | 이 설계에서 |
|---|---|---|
| design/02 §1-§2, §4, §7 | Hermes 는 Control Plane 밖 클라이언트, 권한의 유일 출처는 governance, `CanonicalMemoryPort`, 관측 불가 하위 에이전트 금지 | §3.2, §3.3 |
| design/10 §2-§8 | 역할 권한, 실효 권한, 인증된 승인, 격리, MCP credential, 필수 부정 test | §3.2, §4 |
| design/12 §1 | 메모리 8종, Source→Proposal→Decision→Apply | §3.3 |
| design/13 §2, §5 | pack manifest·서명, 소유 경로, 로컬 override namespace | §3.1, §3.4 |
| design/16 §3, §7-§9 | 부품 class, 승격·포인터, holdout·예산 | §3.1, §3.4 |
| design/17 §1-§6 | 계약 보여주기, 질문 종류, 취소 표현, Hermes = IntakeAdapter + 알림, 사용자 상태 | §3.2 |
| design/18 §1-§5 | 같은 서비스, Idempotency-Key, 재개 가능한 이벤트, `CURSOR_EXPIRED` | §3.2 |
| design/19 §5-§6 | installer 절차·승인 gate, 되돌리기 경계 | §3.4 |
| design/20 §1, §3, §5, §6 | profile, 시작 순서, 서명 키 폐기, staged qualification | §3.4, §3.5 |
| design/26 §3 | method contract 열 가지 | 모든 슬라이스 |
| design/29 §1, §3 | 위협·통제, 남는 한계 | §4 |

### 1.2 현재 코드 사실 (HEAD `5e38d6b`, 검토 2회가 확인)

API·권한
- `POST /api/v3/intents`: `goal.submit`, channel `"api"` 고정 (`control_plane/api_v3/server.py:387-404`).
- `POST /api/v3/goals/{id}/steering`: `goal.steer` 만으로 모든 종류를 받는다 (`server.py:498-506`). `pause`·`cancel` 은 사전 확인을
  건너뛰고 (`runtime/execution/steering.py:126-127`) 취소를 끝낸다 (`:377-399`). 로컬 `cancel` 경로만 `execution.approve`
  (`runtime/execution/loop.py:89-90`).
- `GET /api/v3/events`: 숫자 cursor·`Last-Event-ID`, follow 60회 후 종료 (`server.py:784-830`). 이벤트는 지우지 않는다.
- 질문 답변·의도 보정은 배정 actor 일치만 본다 (`runtime/goals/service.py:355,456`). 승인은 `execution.approve` + human
  (`runtime/execution/product.py:10,2588-2611`). `amplai approve` 는 goal id 만 보낸다 (`runtime/cli.py:221-224`,
  `control_plane/api_v3/client.py:104-105`).
- `Actor` 에 대리 칸 없음 (`runtime/contracts/authority.py:24-30`). intent 문서에는 `actor`, `source_channel`(`hermes` 포함).
- 서버는 loopback 전용, 운영자 토큰 1개, TLS 없음 (`runtime/cli.py:926-941`). 토큰 파일은 요청마다 읽는다
  (`runtime/local_deployment.py:857-864`).

모듈·부품
- 설정 부품 = `harness-component` 레코드 (`runtime/execution/policies.py:91-119`). `pack_refs` 는 읽는 곳 없음, `[]`
  (`product.py:566`, `reference.py:279`). `classify` 는 `pack_refs` 변경을 B 로 본다 (`meta_harness/composition.py:13-23`).
  ablation 은 설명용 (`runtime/execution/meta_ops.py:245,1189-1198`).
- pack 은 등록된 도구 어댑터만 받고 payload 를 소유 경로에 묶는다 (`distribution/packs.py:124-150,179-205`).
- 평가기 digest 범위는 `analysis.py`, `sequential.py`, `service.py`, `calibration.py` 뿐이다 (`evaluation/versions.py:33-34`).
  Q-suite 는 저장소 `tests/v3/test_033_*.py` 를 찾는다 (`evaluation/quality.py:112-120,143-144`). 배포 패키지는 `src` 와 data 만.

릴리스·업그레이드·복구
- `releases.effective()` 는 `component_refs` 를 모두 실행 구성으로 읽고, class A 필드만 같으면 후보 전체를 쓴다
  (`runtime/execution/releases.py:236-254`). 후보는 원본을 복사한다 (`:300-301`). router 변경은 `ROUTER_INCONSISTENT` (`:294-333`).
- store 는 열 때마다 DDL·inline 이전 (`runtime/storage/store.py:188-189,213-245`). 서버는 KeepAlive launchd agent, 야간 agent 01:00
  (`docs/v3/USING_AMPLAI_WORK.ko.md:59`, `meta_harness/nightly.py`). `ops local-update` = git ff-only + 재설치 (`runtime/cli.py:868-925`).
- 복원: 빈 대상, DB 전체 복사, kill switch 켬, `restored_admission_disabled` (`runtime/recovery/service.py:107-178`). 백업 범위는 DB + CAS
  (`:47-100`). CLI 는 `restore` 만 (`runtime/cli.py:1049`).

지식·사내
- canonical 변경 제안 `GOVERNANCE_REQUIRED` (`knowledge_runtime/service.py:84-93`). V2 `foundry.py`(327줄)·`readiness.py`(170줄)·
  `scripts/amplai_docs.py`(약 5,000줄)는 `f06a8e5^`.
- OpenCode 모델 = `<provider>/<model>` (`runtime/local_deployment.py:172-178`), base URL·키 칸 없음, `usage: None`
  (`agent_drivers/http.py:641`). onprem profile 은 disabled (`deployment/README.md:10-13`).

### 1.3 외부 조사 (2026-10-10)

- Hermes Agent: MIT, v0.21.6, OpenAI 호환 서버(도구 호출 + 64k), 메신저 다수, cron, MCP 클라이언트, 터미널 기본 `local`,
  공개 CVE 6건 이상(수정 여부 확인 필요). MCP 서버가 Hermes 대화에 먼저 말을 거는 방법, Hermes 가 스스로 쓴 메모리·스킬이
  주입을 지속시키는지는 확인하지 못했다.

## 2. 전체 구조

```text
운영자 ── 메신저(첫 구현 Slack, 교체 가능) ──▶ 앞단 에이전트(첫 구현 Hermes, 키트로 교체; 전용 VM, 버전 고정)
   ▲                                              │ cron: 아침 보고, feed 가져오기
   │ notifier(운영자 직접 알림, 첫 구현 Slack)       ▼
   │                                       접수 어댑터 intake_adapter (MCP 서버, 별도 프로세스, adapter 토큰은 여기만)
   │ 승인: CLI(계약·digest 표시) / 페이지(passkey, S1b)   │ /api/v3/intake/{adapter_id}: 접수, 필터 조회, 종류 제한 steer·답변,
   │                                              │ 야간 제어(조이는 방향만)
   ▼                                              ▼
AMPLAI Control Plane (제품) ── 계약 → 승인(human) → 실행 → 검증 → PR
   │  nightly-control 기록 ──읽기 전용──▶ 야간 루프 (meta 배포: launchd 시계 → NightlyRunner → 메타하네스)
   │ 설정 부품 ─가리킴─▶ 코드 모듈 (고정 배포물, 외부 입력 kind 는 프로세스 밖)
   ▼
작업 에이전트 (샌드박스) ── (사내) 모델 게이트웨이(별도 프로세스) ──▶ 사내 LLM
```

## 3. 영역별 설계

### 3.1 모듈 체계 (S0)

- **두 층**: 설정 부품(`harness-component`)이 정본, 코드 모듈은 설정 부품이 `module_id@version` 으로 가리킨다 (레코드 내부 필드).
  첫 kind: `driver`, `strategy`, `decider`, `judge`, `knowledge`, `doc_freshness`, `intake_adapter`, `model_gateway`.
- **레지스트리** (`runtime/modules/`): `ModuleSpec`(kind, id, version, interface_version, 적합성 시험 id, 권한, 실행 위치).
  - 내장: 패키지 정적 목록. 확장: 서명된 배포 릴리스 또는 사내 서명 overlay 의 목록이 패키지와 의존 closure 의 `RECORD` digest 를
    고정. 설치 파일을 `RECORD` 와 대조하고, 검증한 읽기 전용 사본에서 import. `.pth` 거부. 임의 entry point 탐색 없음.
  - 신뢰 루트: upstream 공개 키는 설치 시 고정, 교체·폐기 절차 (design/20 §5). 사내 overlay 키는 오프라인, `authority.pem` 과 다름.
  - 실행 위치: `intake_adapter`, `model_gateway` 는 별도 프로세스(컨테이너)이고 Control Plane 과는 API 로만 통신 (M-5, AC-M6).
- **고정 목록 교체**: 작은 PR 여러 개, 동작 불변 (AC-M2).
- **메타하네스**: 모듈 끔·교체는 설정 부품 변경 제안 → 보통 절차. `classify` kind 확장, `composition.py` 를 `SERVICE_FILES` 에
  넣는다 (이 PR 자체가 평가기 변경 → eval-N, AC-M5).
- **열린 항목**: design/16 §3 "승인된 pack 조합 = class A" 와 코드의 B 차이 (Q-7).

### 3.2 Hermes 연결 (S1a, S1b)

**서버 쪽 (S1a)**
1. actor 종류 `intake` 신설: Hermes 서비스 권한 집합(H-2), `authn_context_ref = intake:hermes`, `kind != human`.
2. 토큰·연결표: store 밖 파일 (`~/.amplai/local/intake/`), 요청마다 읽음 → 폐기 즉시 효력, 복원과 무관 (H-6, AC-H7).
   연결표는 운영자 1인 (H-5).
3. 접수 경로 `POST /api/v3/intake/hermes` (API DTO): 메신저 사용자 id·메시지 id → 연결표 → `intake` actor(귀속: 운영자),
   `source_channel = hermes`, idempotency key.
4. steering 종류 허용 목록: actor 종류별, `/goals/{id}/steering` 와 로컬 `steer`·`cancel` 경로 모두 (H-3, AC-H2).
   Hermes 는 `pause`·`cancel` 가능 (OD-5), `resume` 은 Hermes 가 멈춘 goal 만. 로컬 취소 경로는 새 `goal.cancel` 권한을 받는다
   (권한 코드 변경, class C). 일시정지·취소마다 운영자 알림(누가, 왜), 취소는 단계 표현(design/17 §4), 빈도 제한.
5. 질문·의도 보정: `intake` actor 는 비권한 종류만 (H-4, AC-H4). 의도 보정(pre-contract) 경로도 같은 검사.
6. 조회 필터: `intake` actor 는 연결된 운영자의 goal·event 만 (AC-H3).
7. 알림 projection: 고정 schema (goal id, 사용자 상태, 시각, AMPLAI 생성 요약). 원문 텍스트 없음 (H-8, AC-H10).
8. 이벤트 cursor: `<store incarnation>:<seq>`. 세대가 다르거나 seq 가 최대값보다 크면 `CURSOR_EXPIRED` + `GET /api/v3/snapshot`
   (design/18 §4, AC-H6). 복원은 세대를 바꾼다.
9. 승인 CLI: `amplai approve` 가 AMPLAI 렌더 계약(무엇이 달라지나 / 안 건드리나 / 끝 기준, design/17 §1)과 contract digest 를 보여 주고
   `expected_contract_ref` 를 보낸다. 서버는 현재 revision 과 다르면 거부 (H-13, AC-H1).
10. 빈도 제한: steer·replan·submit actor 별 (H-7).
11. 원격 Hermes(OD-8): 접수 listener 를 따로 띄워 위 경로만 열고 TLS 또는 출발지 고정 토큰 (H-12). 운영자 API 는 loopback 유지.

**Hermes 쪽 (S1a)**
- AMPLAI MCP 서버 (`integrations/intake_mcp/`, `intake_adapter`, 별도 프로세스, 앞단 에이전트와 무관 §3.7): 도구 `submit_work`, `submit_design`, `goal_status`,
  `list_goals`, `steer`(허용 종류), `replan`, `pause_goal`, `cancel_goal`, `resume_goal`(Hermes 가 멈춘 goal 만), `answer_question`(허용 종류),
  `propose_knowledge`, `pending_events`(projection),
  `meta_summary`. 토큰은 MCP 서버 환경에만, Hermes 모델 문맥 밖 (design/10 §6).
- 알림: Hermes cron 이 `pending_events` 를 주기적으로 가져와 보낸다. push 는 확인 후.
- VM: Docker 터미널, 호스트 mount 없음, egress 허용 목록(메신저, 모델 API, AMPLAI listener), 버전 고정·staged 업그레이드,
  외부 스킬·자체 코딩 끔 (H-9). 침해 영향 범위 문서 (H-14).

**승인 페이지 (S1b, OD-6)**: AMPLAI 고정 origin, passkey 인증만(토큰 붙여넣기 금지: 가짜 링크에서 운영자 토큰이 새는 것을 막음),
링크는 goal·revision·digest 에 묶인 1회용·짧은 유효, 승인은 POST + CSRF (메신저 링크 미리보기가 링크를 소모하지 않게).
페이지가 digest 를 다시 보여 준다 (AC-H11).

### 3.3 지식·문서 최신성 (S2a, S2b)

- S2a `knowledge`: `CanonicalMemoryPort`, 메모리 8종, Source→Proposal→Decision→Apply, Markdown 정본 + 출처. `f06a8e5^` 의
  `foundry.py`·`readiness.py` 를 V3 store·권한으로 옮김. `governed_submit` 연결, G-04. `memory_notes` 를 이 모듈 위로.
- S2b `doc_freshness`: 영향 분석 + 검토 기록만.
- **지식 공간** (K-5~K-9, OD-10): 공간 하나 = git 저장소 하나. 공간 manifest(`space.json`: id, owner, approvers, contributors,
  readers, data_class)와 `CODEOWNERS`(경로별 승인자)를 저장소에 둔다. 프로젝트는 `knowledge_bindings`(공간 id, follow|pin, revision)로
  연결한다. 지식 모듈은 연결된 공간만 색인·검색하고 context-bundle 에 넣는다.
  - 변경: 제안 → 지식 모듈 검증(형식·출처·모순) → 공간 저장소에 merge request → 승인자 승인(사내 git 서버 리뷰) → merge → 지식 모듈이
    git 서버 API 로 승인자 신원·승인 commit 을 확인하고 Decision·Apply 를 기록 → 연결 프로젝트에 알림 (follow 면 다음 run 부터 반영).
  - 동시 수정: 제안의 기준 revision 이 바뀌면 재검증·재승인 (stale). 모순 제안은 표시.
  - 외부(개인) 환경은 같은 구조를 GitHub 저장소와 운영자 1인 승인으로 쓴다.
  - git 서버 승인을 사람 승인 증거로 쓰는 것은 계약 결정 (D-115).
- 지식 저장소 (OD-10): 프로젝트별 **별도 git 저장소**. 3층으로 나눈다.
  - 파일(정본): Markdown, 메모리 8종 폴더, 각 문서에 출처·결정 ref. git 이력이 감사 기록.
  - 지식 모듈(메모리 레이어): 저장소에 쓰는 유일한 주체. 제안 → 사람 승인 → 적용(commit), 출처 검증, 검색 색인(파일에서 재생성
    가능한 파생물), 준비도 점검, 작업별 context-bundle. 되돌리기는 governed revert commit (U-4).
  - MCP(창구): `intake_adapter` 와 같은 별도 프로세스. 도구는 `knowledge_search`, `knowledge_read`, `knowledge_propose` 뿐.
    직접 쓰기 없음. Hermes·작업 에이전트·다른 클라이언트가 같은 창구를 쓴다.
  - Hermes 개인 기억(선호, 대화)은 이 저장소에 넣지 않는다 (H-10).

### 3.4 3층 업그레이드 (S3)

| 층 | 내용 | 저장 | 업그레이드 때 |
|---|---|---|---|
| L1 upstream | wheel, 모듈 목록, 기본 부품, 기본 과제, 평가기 버전, Q-suite 자산(OD-7) | `distribution-release` (서명) | 교체 |
| L2 사내 overlay | 사내 승격 부품(기준 digest 포함), 사내 과제, 사내 칸, 사내 모듈 목록(사내 서명) | store site namespace + 설정 | 유지 + stale 시 재비교 제안 |
| L3 사내 데이터 | 실행 기록, 증거, 결정, 지식, 예산, 승인 원장 | 제품·meta store + CAS + 지식 저장소 | 동결 → 승인 → 백업 → 이전 → 검증 |

**업그레이드 명령** `amplai ops upgrade --release <id>`:
1. 릴리스 서명·protocol major 3·의존 확인 (G-07). 실패 시 아무것도 안 바꿈.
2. **동결**: kill switch 켬 → 서버·야간 launchd agent bootout → 모든 store owner lock 획득 → drain 확인 (U-2, AC-U4).
3. 미리보기: 소유 경로 파일 작업은 `migration-plan`(KitInstaller 가 실행), store 레코드는 번호 붙은 `migrate` 단계 목록, 되돌릴 수
   없는 변환, 되돌리기 방법. 보고서 digest 산출.
4. 운영자 승인 (보고서 digest, G-20). 승인 대기 동안 동결 유지.
5. 백업 (manifest 범위, U-4) — 승인 직후, 적용 직전.
6. 적용: wheel 설치, `amplai ops migrate` (store 는 열 때 자동 이전 안 함, U-3), KitInstaller, 검증, 영수증.
7. 평가기: 릴리스의 Q-suite 자산으로 새 평가기 버전을 사내에서 승인 (U-5, AC-U5). 승인 전 실험·calibration 불가 (기존 fail-closed).
8. 재기준화: 승격 부품마다 기준 digest 와 새 기본값 비교 → 다르면 `stale` + 재비교 제안 (U-7, AC-U7). 포인터는 승인 전까지 그대로.
9. 동결 해제: launchd agent bootstrap, kill switch 끔.

**되돌리기** (U-6, AC-U6): 새 디렉터리로 복원 → store 밖 폐기 기록 재적용 → reconcile → admission 재개 → 권한 재승인.
데이터 되돌리기는 admission 재개 전에만. 지식 저장소는 governed revert commit, pack 은 KitInstaller 되돌리기.

**local-update 제한** (U-8): local-dev profile 에서만.

### 3.5 사내 구성 (S4)

- onprem profile qualify (C-1): 전제 9개, T-112, egress 는 사내 주소만.
- 모델 게이트웨이 (OD-4, `model_gateway`, 별도 프로세스): 사내 LLM 키는 게이트웨이만, 에이전트에는 dispatch 별 짧은 토큰,
  `model-profile` 로 허용 판단, 요청별 토큰·시간 측정 → 서버 기록 영수증(`usage.status = measured`). 스트리밍 사용량 제공 여부 Q-6.
- OpenCode 사내 모델 (C-2, AC-C3): OpenAI 호환 provider 설정을 qualification probe 로 확인한 뒤 의존. 추론 강도 축 없음.
- 가격 없음 → 비용 비교 안 함 (D-088). 사내 과제는 L2.
- 사내 Hermes 는 사내 모델이 도구 호출·64k 를 지원해야 한다 (Q-6).

### 3.6 Jev (S5)

- `judge` kind. 외부 환경에서 접근 권한 확보 후, 질문 유형별 자격 시험 통과 전 사용 금지. 사내는 데이터 정책 허용 시만.

### 3.7 앞단 에이전트·메신저 모듈 (S1c, OD-13)

| 층 | 모듈 | 첫 구현 | 바꿀 때 |
|---|---|---|---|
| 접수 어댑터 | `intake_adapter` (코드, 별도 프로세스) | AMPLAI MCP 서버 `integrations/intake_mcp/` | 같은 도구 계약을 구현한 다른 어댑터 |
| 앞단 에이전트 키트 | 설정 묶음 (버전, 설치 영수증) | Hermes 키트 `integrations/front_agents/hermes/` | 다른 에이전트 키트 |
| 대화 메신저 | 키트 안 gateway 설정 | Slack (OD-9 가정) | 키트 설정만 |
| 운영자 직접 알림 | `notifier` (코드) | Slack | 다른 notifier |

- **코어 일반화 (S1a 고정값 제거)**: adapter 마다 intake gate 를 만든다. 설정 `intake.adapters[]` 의 필드는 `adapter_id`,
  `token_file`, `identity_map_file`, `permissions`(H-2 이하로만), `rate_limits` 다.
  - 경로 `/api/v3/intake/{adapter_id}`, actor 표지 `intake:<adapter_id>`, 중복 key `<adapter_id>:<digest>`.
  - wire `source_channel` 은 `hermes` 그대로 둔다 (F-3).
  - 지금 `intake_actor(..., adapter=HERMES)` 는 이미 adapter 를 인자로 받는다 (`runtime/contracts/intake.py:78`).
    경로·key·channel 은 고정이다 (`control_plane/api_v3/intake.py:50,89,249`).
- **도구 계약 v1**: §3.2 의 도구에 `night_status`, `night_stop`, `night_skip`, `night_tighten`, `night_clear_own`(S1d)을 더한다.
  이름과 입력·출력 schema 를 계약 파일 하나에 고정하고 적합성 시험을 둔다.
- **Hermes 키트**:
  - config 틀: 모델과 `agent.reasoning_effort`, 끈 toolset, `skills.write_approval`, `memory.write_approval`.
  - 지시문: AMPLAI 에서의 역할과 금지 사항(승인 불가, 코드 직접 수정 불가).
  - MCP 연결: 토큰은 MCP 서버 환경에만 둔다.
  - cron: 아침 보고, feed 가져오기.
  - 메신저 gateway 설정: Slack Socket Mode, 허용 사용자.
  - VM 점검표 (H-9).
  - Hermes 의 MCP 호출은 Hermes 자체 승인 단계를 거치지 않는다(외부 조사 2026-10-10, Hermes issue #49167). 그래서 권한은
    AMPLAI 서버가 판정한다.
- **notifier v1**: `send(event_id, projection) -> receipt`.
  - 같은 event_id 를 다시 보내면 receipt 를 재생한다.
  - 실패하면 제품 store outbox 에서 재시도한다.
  - Slack 구현은 운영자 채널 하나에만 보낸다.

### 3.8 야간 메타 루프 제어 (S1d, OD-12)

| 층 | 누가 | 하는 일 |
|---|---|---|
| 시계 | launchd | 5분마다 `amplai meta nightly tick` (지금은 01:00 에 `run` 한 번, `deployment/launchd/ai.amplai.meta-nightly.plist.template`). tick 은 예정 시각 또는 유효한 시작 요청(N-9)일 때만 그날 밤을 시작하고, 밤이 이미 돌고 있으면(`ACTIVE_OWNER`) 바로 끝난다 |
| 밤 진행 | `NightlyRunner` | preflight → drift → 설계 → search → confirmation → dreaming → dashboard, 결정적 (`meta_harness/nightly.py:89-91`) |
| 제어 요청 | 앞단 에이전트·운영자 | 멈춤·건너뛰기·조이기·시작(N-9) → 제품 authority service → `nightly-control` 기록 |
| 승인 | 사람 | 상시 허락 발급·연장, 단계 gate (CLI, 승인 페이지) |

- **제어 기록** `nightly-control` (제품 store, 새 내부 kind):
  `{control_id, action: stop|skip|tighten|start|clear, dates, limits: {budget_trials?, stop_at?}, issued_by, reason, at, clears?}`.
  - 가장 엄격한 것이 이긴다.
  - `clear` 는 같은 actor 가 건 것만 지운다(사람은 모두 지운다).
  - 앞단 에이전트의 `tighten` 은 줄이는 방향만 받는다.
  - `start` 는 접수 때 N-9 의 조건(허락, 시간대, 사람 정지, 빈도)을 검사하고, tick 이 시작 직전에 다시 검사한다(그 사이 바뀐
    허락·정지를 반영). 날짜당 한 번은 기존 `NIGHT_STATE` 가 막는다. 시작 가능 시간대는 설정 `meta.nightly.agent_start_window`
    이고 사람만 바꾼다.
- **밤 쪽 적용**:
  - 설정 `meta.control_store` 가 제품 runtime root 를 가리킨다.
  - `NightlyRunner` 는 preflight, 단계 사이 검사(`_night_reason`, `nightly.py:686-687`), trial guard 에서
    `Store(readonly=True)` 로 제어 기록을 읽는다.
  - 적용한 제어는 밤 기록 `data.controls` 에 남긴다.
  - 멈춤 이유 `front_agent_stop`, `operator_stop`, `skipped`, `control_unreadable` 을 `HOLD_REASONS` 에 더한다
    (`nightly.py:94-97`).
  - 지금 쓰는 meta store 의 kill switch(`runtime-control/kill`, `nightly.py:1178-1184`)는 밤이 store 를 잡고 있어 밖에서 켤 수 없다.
- **상태**: `meta nightly status` 는 읽기 전용으로 연다. 앞단 에이전트용 요약은 제품 서버가 meta store 를 읽기 전용으로 열어
  고정 schema 로 만든다.
- **알림**:
  - 제품 서버가 meta store event 를 읽기 전용으로 읽어 앞단 에이전트 feed 로 준다(`/api/v3/intake/{adapter_id}/meta/feed`,
    cursor `<meta incarnation>:<seq>`).
  - notifier 대상: 앞단 에이전트가 건 멈춤, unknown effect, drift, 상시 허락 만료 임박.
- **검토한 대안**:
  - (a) meta 상주 서버가 meta store 를 소유하고 밤을 안에서 돌린다. meta CLI 전체를 client 로 바꿔야 해서 크다.
  - (b) meta 폴더의 제어 파일. 트랜잭션 store 밖이고 쓰는 쪽이 둘이다.
  - (c) 제품 store 기록(추천, Q-9). S1a 의 authority service, 감사, 중복 방지를 그대로 쓴다.
- **평가기 digest**: `nightly.py` 는 평가기 digest 범위 밖이다(`evaluation/versions.py:33-34`). 새 권한 `nightly.control` 은
  권한 코드 변경이라 class C 다.

## 4. 위협·통제 (design/29 §1, design/10 §8)

| 위협 | 경로 | 통제 | test |
|---|---|---|---|
| confused deputy | 사칭, 다른 사용자 goal 조회 | 서버 계산 `intake` actor, 연결표, 조회 필터, JSON actor 무시 | AC-H3 |
| prompt injection | 채팅·PR 제목·로그 속 지시 | 승인·권한 질문 불가, 알림은 projection 만 | AC-H2, AC-H4, AC-H10 |
| excessive agency | Hermes 의 취소·코드 실행 | steering 종류 목록(두 경로), 자체 코딩 끔, Docker 터미널 | AC-H2, AC-H8 |
| blind approval | Hermes 가 꾸민 카드 | CLI 가 AMPLAI 렌더 계약·digest, `expected_contract_ref` | AC-H1 |
| phishing | 가짜 승인 링크 | passkey 만, 고정 origin, POST + CSRF, digest 재표시 | AC-H11 |
| exfiltration | VM·작업자 외부 유출 | egress 허용 목록, 사내 profile 외부 제공자 금지 | AC-H8, AC-C2 |
| supply chain | Hermes CVE, 외부 스킬, 모듈 | 버전 고정·외부 스킬 끔, closure `RECORD` 고정, `.pth` 거부, 오프라인 overlay 키 | AC-M3, AC-X1 |
| key exposure | 모듈이 authority 키 프로세스에서 실행 | 외부 입력 kind 는 프로세스 밖 | AC-M6 |
| evidence fabrication | 보고된 사용량·통과 | 게이트웨이 측정, 서버 판정 | AC-C1 |
| eval gaming | 분류·평가 모듈 교체 | class C, `composition.py` digest 포함 | AC-M5, AC-X1 |
| stale authority | 복원 후 폐기 권한·토큰 부활 | store 밖 토큰·폐기 기록, 재적용 후 재개 | AC-H7, AC-U6 |
| duplicate effects | 재전송, 중복 알림 | idempotency, 세대 cursor | AC-H5, AC-H6 |
| unapproved migration | 재시작·야간 작업이 이전 실행 | 동결, 명시적 `migrate`, local-update 제한 | AC-U4, AC-U8 |
| shadowed upgrade | 승격 부품이 새 검증 정책을 가림 | 기준 digest, stale 표시 | AC-U7 |
| VM compromise | Hermes VM 침해 | 영향 범위 문서, 빈도 제한, 토큰 즉시 폐기 | AC-H5, AC-H7 |
| 야간 루프 남용 | 앞단 에이전트가 밤을 늘리거나 낮에 켬 | 시작은 허락·날짜당 한 번·시간대·사람 정지·빈도 안에서만, 시작마다 notifier 알림, 연장·해제는 사람, 자기가 건 것만 해제 | AC-N2, AC-N3, AC-N6 |
| 알림 차단 | 침해된 앞단 에이전트가 경고를 숨김 | 앞단 에이전트와 독립된 `notifier` | AC-F3, AC-N1 |
| adapter 혼동 | 한 adapter 토큰으로 다른 adapter 행세 | adapter 별 토큰·연결표·경로 | AC-F2 |
| 제어 채널 장애 | 제어 기록을 못 읽어 멈춤 요청이 무시됨 | 읽지 못하면 밤을 멈춤 (fail closed) | AC-N5 |

## 5. 슬라이스와 순서 (OD-3)

| 슬라이스 | 내용 | 끝 기준 | gate | 선행 |
|---|---|---|---|---|
| S0 | 모듈 레지스트리, 신뢰 루트, 프로세스 밖 실행, 내장 모듈 감싸기(작은 PR), `classify`·digest 범위(eval-N) | AC-M1~M6, AC-X1 | G-07 | — |
| S1a | `intake` actor, store 밖 토큰·연결표, 접수 경로, steering·질문 종류 제한, 조회 필터, projection, 세대 cursor, 승인 CLI digest, 빈도 제한, MCP 서버, cron 알림, VM runbook | AC-H1~H10 | — | S0 |
| S1b | 승인 페이지 (OD-6), 원격 listener (OD-8 이 원격일 때) | AC-H11 | — | S1a |
| S1c | 앞단 에이전트·메신저 모듈: adapter 별 intake 설정, 경로·표지 일반화, `notifier` kind + Slack, 도구 계약 v1, MCP 서버, Hermes 키트 | AC-F1~F3, AC-H8, AC-H9 | — | S1a |
| S1d | 야간 루프 제어: `nightly.control`, `nightly-control` 기록, launchd tick, 밤의 읽기 전용 확인, 읽기 전용 status, meta feed, CLI | AC-N1~N6 | class C(권한) | S1c |
| S2a | 지식 모듈 | AC-K1, AC-K3 | G-04 | S0 |
| S2b | 문서 최신성 모듈 | AC-K2 | — | S2a |
| S3 | 배포 릴리스·Q-suite 자산, 동결, 명시적 이전, 백업 manifest CLI, 업그레이드, 되돌리기, stale, local-update 제한 | AC-U1~U8 | G-07, G-15, G-20 | S0 |
| S4 | onprem qualify, 모델 게이트웨이, OpenCode 사내 모델, 사내 과제 도구 | AC-C1~C3 | G-07 | S0, S3, OD-4 |
| S5 | Jev | 자격 시험 | — | S0, 접근 권한 |

- 평가기 digest 범위의 파일을 바꾸는 PR 은 eval-N 승인을 함께 묶는다.
- 메타하네스 파일럿과 같은 Claude 계정을 쓴다. 구현은 파일럿이 쉬는 시간에.

## 6. 결정 후보 (승인 후 `DECISIONS.md`)

- D-106 Hermes = IntakeAdapter + 오케스트레이터. `intake` actor(비 human), store 밖 토큰·연결표, 종류 제한, projection 알림.
- D-107 배포는 upstream → 사내 한 방향.
- D-108 두 층 모듈 체계, 고정 배포물만, 신뢰 루트, 외부 입력 kind 는 프로세스 밖.
- D-109 지식·문서 최신성 V3 모듈 복원 (S17 `f06a8e5` 의 D1·X9 삭제를 정정, design/12 §1).
- D-110 3층 업그레이드: 동결, 명시적 이전, 승인 후 백업, 되돌리기 순서, 기준 digest·stale.
- D-111 모델 게이트웨이 (OD-4). D-112 Hermes 일시정지·취소 허용, 재개는 Hermes 가 멈춘 goal 만, `goal.cancel` 신설 (OD-5).
  D-113 승인 페이지 passkey (OD-6).
- D-114 Q-suite 배포 자산화 (OD-7).
- D-115 지식 공간과 git 서버 승인을 사람 승인 증거로 쓰는 대응 (OD-10, 확인 대기).
- D-116 앞단 에이전트·메신저는 세 층 모듈(접수 어댑터, 앞단 에이전트 키트, notifier)이다. wire `source_channel=hermes` 는 앞단 에이전트 채널 이름으로 두고, adapter 는 `authn_context_ref` 로 구분한다 (OD-13).
- D-117 야간 루프 제어: 시계 launchd tick, 진행 NightlyRunner, 앞단 에이전트는 조이기와 조건부 시작(N-9, `nightly.control`), 제어 기록은 제품 store, 밤은 읽기 전용으로 확인하고 못 읽으면 멈춘다 (OD-12, Q-8 = B).

## 7. 열린 질문

| # | 질문 | 영향 |
|---|---|---|
| Q-1 | OD-8 Hermes VM 위치 | 원격이면 S1b listener |
| Q-2 | Hermes 가 쓸 모델(외부 환경)과 비용 | 품질·비용 |
| Q-3 | 메신저: Slack / Telegram / 둘 다 | 연결표·알림 |
| Q-4 | 지식 저장소 위치: 앱 저장소 안 / 별도 저장소 | S2a |
| Q-5 | OD-4~OD-7 | S1a, S1b, S3, S4 |
| Q-6 | 사내 LLM 서버 사양 (OpenAI 호환, 도구 호출, 컨텍스트, 스트리밍 사용량) | S4 |
| Q-7 | design/16 §3 pack 조합 class A 와 코드 B 의 차이 | S0 이후 |
| Q-8 | 앞단 에이전트가 상시 허락 안에서 밤을 "지금 시작" 할 수 있나 → 결정: B, N-9 조건부 (2026-10-11) | S1d |
| Q-9 | 제어 기록 위치: 제품 store (추천) / meta 제어 파일 / meta 상주 서버 (§3.8 대안) | S1d |
| Q-10 | notifier 의 Slack 방식: incoming webhook / bot token (OD-9 확정 뒤) | S1c |

## 8. 위험

- Hermes 보안: 공개 CVE 다수, VM 침해 시 연결 사용자 사칭·steer 주입은 남는다 (H-14). Hermes 가 스스로 쓰는 메모리·스킬이 주입을
  지속시킬 수 있는지 확인 필요 → 스킬 자동 작성을 끄거나 주기 검토를 qualification 항목으로.
- S0·S3 는 코어(권한, store 열기, 릴리스)를 건드린다. 동작 불변 PR 과 장애 훈련 test 로 나눈다.
- S2a 이식 크기는 추정이 어렵다.
- S4 는 사내 확인 사항(Q-6, OpenCode provider 설정)에 막힐 수 있다.
- 알림은 cron 주기만큼 늦다.
- 계정 한도: 구현·리뷰와 파일럿이 같은 Claude 계정.
