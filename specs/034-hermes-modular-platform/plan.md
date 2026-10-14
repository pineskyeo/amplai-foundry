# Work 034 Plan: 구조, 인터페이스, 순서

- 상태: 설계 초안 r8 (사내 설치 세부 §3.5a 반영 r8, 코드 근거 main `2ab10a2`, 사내 점검 2026-10-12; Q-8 = B 반영 r7; 독립 검토 2회 반영 r3, 운영자 결정 2026-10-11 반영 r5, 운영자 지시 2026-10-11 반영 r6: 야간 루프 제어 OD-12, 앞단 에이전트·메신저 모듈 OD-13; r6 의 코드 근거는 main `76e2874`). spec: `spec.md`.

## 0. 검토 이력

| 회차 | 관점 | 결과 | 반영 |
|---|---|---|---|
| r1 → r2 | 원본 설계 부합성·코드 실현성 | 막힘 2, 큼 8, 작음 5 | 권한 이름, 취소, 모듈 전달 경로, 두 층 모듈, 대리 identity, 승인 페이지 분리, 이벤트 API, migration-plan 용도, 되돌리기, release-set 충돌, AC 보강 |
| r2 → r3 | 보안·업그레이드 운영·r2 사실 확인 | 막힘 3, 큼 7, 작음 4 | steering 경로 취소 차단, Q-suite 자산화, 동결·명시적 이전, 되돌리기 순서, cursor 세대, 눈 감은 승인, 원격 listener, 승격 부품 가림, 백업 범위, 신뢰 루트, classify digest 범위, 알림 projection |
| r7 → r8 | 사내 설치 세부 (운영자 사내 점검 2026-10-12, `scripts/site_env_check.sh`) | 독립 검토 전 | §3.5a 추가: 서비스 관리자(systemd·supervisor), 샌드박스 uid·ACL, 엔진 설정, 오프라인 묶음, 경로·로그·백업, 지원 OS 표, 업그레이드 리허설. C-8~C-14, U-9, AC-C4~C10, AC-U9, D-118~D-123, Q-11~Q-20 |

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
   │  nightly-control 기록 ──읽기 전용──▶ 야간 루프 (meta 배포: 서비스 관리자 시계 → NightlyRunner → 메타하네스)
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
   확정 (운영자 2026-10-14, D-127): `GET /api/v3/snapshot` 은 운영자 전용(scope 전체 heads, intake actor 403)이고, intake feed 의
   snapshot 은 adapter 별 `GET /api/v3/intake/{adapter_id}/snapshot` (`{items: [{goal_id, status, at}], truncated, cursor}`,
   끝나지 않은 goal 전부, 상한 1000, H-8)로 둔다.
   공유하면 scope 전체 자료가 새거나 intake actor 에게 항상 403 이다 (H-8, AC-H3). 숫자만 있는 cursor(`0` 포함)는 409
   `CURSOR_EXPIRED`, `Last-Event-ID` 가 `after` 보다 우선(만료면 쓸 수 있는 `after` 로), 409 본문은 `snapshot_ref` 만 주고 cursor 는 없다. 운영자 snapshot 은
   `{items[kind, id, state, row_version], truncated, cursor, scope}`, 상한 1000, `truncated: true` 의 이어 받기는 아직 없다.
   복원은 `amplai ops restore` 로만 (파일 복사·disk rollback 금지). 세부와 근거는 spec H-16 의 제안 항목.
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
2. **동결**: kill switch 켬 → 서비스 관리자(§3.5a.1)로 서버·게이트웨이·listener 정지와 야간 시계 해제 → 모든 store owner lock 획득 → drain 확인 (U-2, AC-U4).
3. 미리보기: 소유 경로 파일 작업은 `migration-plan`(KitInstaller 가 실행), store 레코드는 번호 붙은 `migrate` 단계 목록, 되돌릴 수
   없는 변환, 되돌리기 방법. 보고서 digest 산출.
4. 운영자 승인 (보고서 digest, G-20). 승인 대기 동안 동결 유지.
5. 백업 (manifest 범위, U-4) — 승인 직후, 적용 직전.
6. 적용: wheel 설치, `amplai ops migrate` (store 는 열 때 자동 이전 안 함, U-3), KitInstaller, 검증, 영수증.
7. 평가기: 릴리스의 Q-suite 자산으로 새 평가기 버전을 사내에서 승인 (U-5, AC-U5). 승인 전 실험·calibration 불가 (기존 fail-closed).
8. 재기준화: 승격 부품마다 기준 digest 와 새 기본값 비교 → 다르면 `stale` + 재비교 제안 (U-7, AC-U7). 포인터는 승인 전까지 그대로.
9. 동결 해제: 서비스 관리자로 시계·서비스 재개(§3.5a.1), kill switch 끔.

**끝 기준에 리허설 포함** (U-9, AC-U9, §3.5a.6): S3 는 업그레이드 리허설 test 가 통과해야 끝난다.

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
- Linux 이식·닫힌 망 설치·서비스·지원 OS 는 §3.5a 다.

### 3.5a 사내 배포 세부 (S4a, S4b; C-8~C-14, U-9)

근거: 코드는 main `2ab10a2`. 사내 사실은 운영자 점검(2026-10-12, `scripts/site_env_check.sh`, 브랜치 `origin/feat/034-site-env-check`)이다.

#### 사내 점검 사실 (2026-10-12)

| 호스트 | 자원 | 기반 | 엔진 | 서비스 관리 | 네트워크 |
|---|---|---|---|---|---|
| Ubuntu 18.04 서버 | 32 CPU, 157 GiB | glibc 2.27, Python 3.11.4(`/usr/local/bin`), git 2.17.1 | docker 20.10.21, cgroup v1, 사용자가 `appdocker` 그룹 | systemd 여부·sudo 여부 확인 필요 | 외부 접속 전부 없음, proxy 없음 |
| RHEL 7.6 | 8 CPU, 15 GiB, `/home` 여유 1.8G | glibc 2.17, Python 3.11.7(사용자 설치), git 1.8.3.1, SELinux disabled | docker·podman 없음 | 확인 필요 | 외부 접속 없음 |
| WSL2 Ubuntu 24.04 (WSL 3.0.1.0, kernel 6.18) | 16 CPU, 15.5 GiB | Python 3.12.3, git 2.43 | docker.io 29.1.3 설치됨. 엔진 도달·bind 쓰기 시험은 운영자 확인 대기 | systemd(PID 1, `/etc/wsl.conf`), sudo 는 비밀번호로 됨 | proxy `http://12.26.204.100:8080` 경유만. GitHub·PyPI 200, Anthropic·OpenAI·Slack 403 |
| RHEL 8 | 미점검 | — | — | — | — |

- 운영자가 전달한 점검 요약에 세 호스트의 CPU 아키텍처 값이 없다 (`uname -m` 는 스크립트가 찍는다, `site_env_check.sh:16`). 확인 필요 (Q-19).
- Ubuntu 18.04 서버의 bind 쓰기 시험(`site_env_check.sh:48-71`, 이미지 인자가 있을 때만 돈다) 결과도 요약에 없다. 확인 필요 (Q-18).

#### 3.5a.1 서비스 관리 (C-8, AC-C4, AC-C5)

지금: 서버는 launchd agent `ai.amplai.local-serve`(`RunAtLoad`·`KeepAlive`, `docs/v3/USING_AMPLAI_WORK.ko.md:59`)이고 그 plist 는 저장소에
없다. 야간은 `deployment/launchd/ai.amplai.meta-nightly.plist.template` 를 `meta nightly print-agent` 가 채워 출력만 한다
(`runtime/meta_commands/nightly.py:18-19,309-318`). `ops local-update` 는 `launchctl kickstart` 를 부른다 (`runtime/cli.py:1012-1019`).
notifier 는 서버 프로세스 안의 thread 다 (`runtime/local_deployment.py:1150-1157`, `runtime/notifier/pump.py:205-210`). 그래서 notifier 를 위한
별도 unit 은 없다. 서버는 `127.0.0.1:5083` 에만 묶인다 (`runtime/cli.py:1027-1040`).

**인터페이스** `ServiceManager` (설계, 새 코드):
`render(units) -> files`(출력만), `status()`, `stop(names)`, `start(names)`, `timer_off()`, `timer_on()`, `is_quiescent()`.
구현 세 개: `launchd`, `systemd`, `supervisor`. 설정 `service.manager` 로 고르고, 기본값은 OS 로 정한다 (macOS → launchd, Linux 에서 PID 1 이
systemd 면 systemd, 아니면 supervisor).

**관리 대상** (unit 이름은 `amplai-` 접두):

| 대상 | 형태 | 비고 |
|---|---|---|
| `amplai-server` | 상시 (`Restart=on-failure`) | `amplai ops local-serve --config …`. notifier 포함 |
| `amplai-model-gateway` | 상시 | S4c 의 `model_gateway`, 별도 프로세스 (M-5) |
| `amplai-intake-listener` | 상시 | S1b 원격 listener 가 별도 프로세스로 정해질 때만 (H-12). 지금 코드에 없다 |
| `amplai-meta-tick.service` + `.timer` | `Type=oneshot`, timer `OnCalendar=*:0/5` | `amplai meta nightly tick` (N-1, S1d). 밤이 돌고 있으면 `ACTIVE_OWNER` 로 바로 끝난다 |
| 앞단 에이전트 컨테이너 | 엔진의 컨테이너, 호스트 mount 없음 | H-12, OD-8. AMPLAI 서비스 관리자 밖이다. 엔진 restart 정책 또는 운영자 unit 으로 띄운다 |

**systemd**:
- 1순위 system unit(`/etc/systemd/system/`, `User=<AMPLAI 전용 계정>`). 설치에 sudo 가 한 번 필요하다.
- 2순위 user unit(`~/.config/systemd/user/`) + `loginctl enable-linger`. linger 를 본인이 켤 수 있는지는 호스트 polkit 설정에 달렸다. 확인 필요.
- unit 에 `NoNewPrivileges=yes`, `PrivateTmp=yes`, `UMask=0077` 을 둔다. 엔진 소켓 접근이 필요하므로 `ProtectSystem=strict` 같은 강한 제한의
  호환성은 자격 시험에서 확인한다 (확인 필요).
- 로그는 stdout·stderr → journald (`journalctl -u amplai-server`). 보존 기간은 호스트 journald 설정이다. 확인 필요.
- 렌더: `amplai ops service print --manager systemd` 가 unit 파일을 출력한다. 설치·`systemctl enable` 은 운영자가 한다 (C-8, 자동 설치 없음).

**supervisor (systemd·sudo 가 없을 때)**: `amplai ops supervise --config …` 한 프로세스가 자식으로 서버·게이트웨이를 띄우고, 죽으면 지수
backoff 로 다시 띄우고, 5분마다 tick 을 자식 프로세스로 돌린다.
- store 를 열지 않는다. store 의 단일 owner 는 그대로 자식이다 (`runtime/storage/store.py:166-173`).
- pid 파일을 `flock` 으로 잡아 두 개가 뜨지 않게 한다.
- 로그는 `<log_root>/<name>.log`, 크기 기준 회전.
- 재부팅 뒤 자동 시작은 호스트 수단(사용자 crontab `@reboot`, 운영자 수동)에 맡긴다. crontab 사용 가능 여부는 확인 필요 (Q-11).

**동결 일반화 (U-2, S3 단계 2·9)**:
- 동결은 `ServiceManager.timer_off()` → `stop(all)` → `is_quiescent()` 확인 → 모든 store owner lock 획득 순서다.
- systemd: `systemctl stop` 은 `Restart=` 재시작을 일으키지 않는다. timer 는 `systemctl stop` + 동결 표지.
- supervisor: 동결 표지 파일 `<runtime_root>/../freeze` 를 보면 자식을 내리고 새로 띄우지 않는다.
- 세 구현 공통 이중 잠금: 동결 표지가 있으면 `local-serve` 와 `meta nightly tick` 은 store 를 열기 전에 `FROZEN` 으로 끝난다. 서비스 관리자가
  실수로 프로세스를 띄워도 owner lock 은 업그레이드가 잡고 있어 `ACTIVE_OWNER` 로 막힌다.
- `ops local-update` 의 재시작도 `ServiceManager` 를 거친다 (U-8 로 local-dev 전용은 그대로).

**WSL2**: systemd 가 PID 1 이므로 systemd 구현을 쓴다. Windows 절전·재부팅·WSL 유휴 종료 때 배포판과 서비스가 멈추는지는 확인 필요 (Q-16).
WSL 을 상시 서버로 쓸지 개발 기기로만 쓸지는 운영자 결정이다.

#### 3.5a.2 컨테이너 권한 (C-9, C-10, AC-C6, AC-C7)

**지금 전제**:
- 샌드박스는 uid·gid 65534(`sandbox/container.py:22-23`), `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, 작업 공간 bind
  (`:89-118`), run home bind(`:135-137`)로 돈다. uid 0 은 거부한다 (`:37`).
- 자격 파일은 0700 부모 안에 0644 로 둔다. colima 가 bind 소유자를 컨테이너 uid 로 맞춰 준다는 주석이 근거다
  (`runtime/execution/codex.py:78-79`, `agent_drivers/opencode_port.py:104-105`).
- 작업 공간이 `$HOME` 아래여야 한다는 전제는 `runtime/local_deployment.py:398`(`workspace_root`, colima 가 `$HOME` 만 공유)과
  `meta_harness/tb2.py:733`, `meta_harness/proposer.py:518`, `meta_harness/tb2_grading.py:213` 에 있다. Linux 엔진은 이 제약이 없다
  (colima 의 공유 범위 제약이므로). 단 C-12 의 로컬 파일 시스템 조건은 남는다.
- Linux 엔진(userns 없음)에서 컨테이너 uid 65534 는 호스트 권한 검사도 uid 65534 로 받는다. 그러면 호스트 사용자 소유 0700 부모 아래는
  읽지 못한다 (추정, 사이트 점검 `site_env_check.sh:57-70` 의 700·755·777 시험이 이를 확인한다, Q-18).

**선택지**:

| 안 | 방법 | root 필요 | 탈출 시 호스트 권한 | 정리·diff | 판단 |
|---|---|---|---|---|---|
| A chown | 작업 공간을 65534 로 chown | chown 마다 root (또는 엔진을 통한 root 우회) | nobody | 호스트가 못 지우는 파일이 남아 다시 root 필요 | 제외 |
| B 호스트 uid | `--user $(id -u):$(id -g)` | 없음 | AMPLAI 계정 = store·`authority.pem`·토큰 소유자 | 단순 | 기각 (키 노출, design/10 §5 격리) |
| C userns-remap | daemon `userns-remap` | daemon 설정·재시작 (root) | subuid 범위 | uid 가 다시 어긋나 A 와 같은 문제 | 제외 (공유 daemon, 운영 주체가 다름, Q-11) |
| D rootless | rootless docker/podman, podman `--userns=keep-id` | `/etc/subuid` 설정에 root 한 번 | 호스트 사용자 | 단순 | RHEL8 후보, 별도 자격 시험 (cgroup v1 rootless 의 자원 제한 적용 여부 확인 필요) |
| E ACL | uid 65534 유지, 호스트 사용자가 `setfacl` 로 작업 공간·run home 에 `u:65534:rwx` 와 default ACL `u:65534:rwx`, `u:<host>:rwx` | 없음 (소유자가 ACL 설정) | nobody | 호스트가 default ACL 로 읽고 지움 | **권장** |

**권장 (D-119)**: E. 컨테이너 uid 는 호스트 사용자와 다른 고정 uid(설정 `container.sandbox_uid`, 기본 65534)이고, 접근은 ACL 로만 준다.
- 자격 사본은 0600 + ACL `u:<sandbox_uid>:r` 로 둔다. 0644 로 바꾸는 줄(`codex.py:79`, `opencode_port.py:105`)은 Linux 에서 ACL 부여로
  바뀐다. macOS colima 경로는 그대로다.
- `workspace_root` 부모는 0700 + ACL 로 sandbox uid 만 통과시킨다. 같은 uid(nobody)로 도는 다른 호스트 daemon 도 통과할 수 있으므로
  sandbox uid 는 호스트에서 쓰지 않는 값으로 바꿀 수 있게 한다.
- 남는 위험: default ACL 의 mask 는 생성 mode 에서 온다. 컨테이너가 0755 디렉터리·0644 파일을 만들면 호스트 사용자의 실효 권한이 줄어
  지우지 못할 수 있다 (추정). 그래서 S4a 의 Linux container qualify 에 "컨테이너가 git checkout·중첩 mkdir 한 뒤 호스트가 diff 를 읽고
  전부 지운다" 시험을 넣고(AC-C6), 실패하면 정리를 sandbox uid 컨테이너 안의 `rm` 으로 한다(`--network none`, 같은 프로필).
- ACL 지원은 파일 시스템 조건이다 (ext4·xfs 는 지원, 실제 마운트 옵션 확인 필요, C-14 점검 항목).
- docker 그룹 사용자(`appdocker`)는 엔진을 통해 호스트 root 와 같은 일을 할 수 있다. AMPLAI 계정이 침해되면 호스트 root 침해다. 그래서
  AMPLAI 는 사람과 분리된 전용 계정으로 돌린다 (§4).

**엔진 (C-10, D-120)**:
- 설정 `container.engine: docker|podman` 하나를 `LocalConfig` 에 두고 모든 `ContainerSandbox(...)` 생성(`local_deployment.py:617,644,711,722,913,1314,1323`,
  `execution/codex.py:470,483,500`)과 정리 호출(C-10 의 `docker kill`·`docker rm -f` 9곳)에 넘긴다. `ContainerSandbox.kill(name)`·`remove(name)`
  메서드로 모아 직접 `subprocess.run(["docker", …])` 를 없앤다.
- `meta_harness/tb2.py:727`·`tb2_grading.py:242`·`agent_drivers/opencode_launcher.py:148` 은 이미 engine 인자를 받는다. 기본값 문자열
  `"docker"` 대신 설정을 넘긴다.
- 정적 test: `src/` 에 `["docker"` 리터럴이 없다 (AC-C7).
- 스크립트(`scripts/sandbox_up.sh` 등)는 `docker` 를 직접 쓴다 (`sandbox_up.sh:35-82`). Linux·닫힌 망 설치 경로는 이 스크립트 대신
  §3.5a.3 의 설치 명령이 맡는다.
- podman 차이 중 확인할 것: `--init`, `--pids-limit`·`--memory` 의 cgroup v1 적용(rootless), `--network none`, `--mount type=bind` 의
  SELinux 라벨(`:Z`, `site_env_check.sh:61`). 모두 limits·egress·container qualify 로 확인한다 (확인 필요).

#### 3.5a.3 오프라인 설치 묶음 (C-11, AC-C8)

**묶음 구성** `amplai-bundle-<release>-<platform>/`:

| 항목 | 내용 | 검증 |
|---|---|---|
| `release.json` | `distribution-release` manifest (U-1): 버전, 각 파일 sha256, 이미지 목록, 지원 platform 태그, protocol major 3 | upstream 서명 (Ed25519, 신뢰 루트 M-4). 지금 pack 신뢰는 Ed25519 공개 키 표다 (`runtime/cli.py:1080-1096`) |
| `release.json.sig` | 위 서명 | 설치 시 고정한 upstream 키 |
| `wheelhouse/` | `amplai_foundry` wheel + 의존 closure 전체 (서버용, intake MCP 용, 게이트웨이용 extra 별) | `requirements-hashes-<extra>.txt` 로 `pip install --no-index --find-links wheelhouse --require-hashes` |
| `images/*.tar` | worker, worker-opencode, worker-app 기반, egress sidecar, 사이트 로컬 registry 이미지, 앞단 에이전트 이미지 | `release.json` 의 이미지 config digest(= `docker image inspect` 의 `Id`) |
| `qsuite/` | Q-suite 자산 (OD-7, U-5) | `release.json` sha256 |
| `units/` | 서비스 unit 틀 (§3.5a.1) | sha256 |

- **hash 고정**: 지금 hash 고정 목록은 intake MCP 용 하나다 (`integrations/intake_mcp/requirements-hashes.txt:1-11`, `uv pip compile …
  --generate-hashes`). 같은 방법을 서버·게이트웨이 extra 로 넓힌다. 저장소에 그 밖의 lock 파일은 없다 (같은 파일 :3-4).
- **platform**: 대상 Python 은 3.11(Ubuntu 18.04, RHEL 7.6)과 3.12(WSL2 24.04)다. wheelhouse 는 Python 버전·glibc 별로 만든다. glibc 2.17
  (RHEL 7)은 `manylinux2014` wheel 이 있어야 하고, 고정 버전의 `cryptography`·`pydantic-core` 등이 그 태그를 내는지는 확인 필요다. 소스
  빌드는 받지 않는다(`--only-binary=:all:`).
- **도구 버전 고정**: worker Dockerfile 의 기본 ARG 가 `latest` 다 (`deployment/worker/Dockerfile:5-6`). 릴리스 빌드는 버전을 고정하고
  `release.json` 에 기록한다 (지금 `deployment/local-container.json` 의 `tools` 와 같은 정보).
- **이미지와 digest**: `ContainerSandbox` 는 `<name>@sha256:<64hex>` 만 받는다 (`sandbox/container.py:34-35`). `docker load` 한 이미지는
  registry digest 가 없다 (추정, docker 20.10 과 29 에서 확인 필요). 그래서:
  1. 이미지 tar 를 load 하고 `Id`(config digest)를 `release.json` 과 대조한다. 다르면 거부.
  2. 사이트 로컬 registry(묶음 안의 이미지, loopback 전용)에 push 해서 `localhost:<port>/<name>@sha256:…` 를 얻는다. 지금 Mac 도 같은 방식이다
     (`scripts/sandbox_up.sh:18`, `deployment/local-container.json:5`).
  3. push 로 생긴 manifest digest 는 엔진·압축에 따라 upstream 과 다를 수 있다 (추정). 신뢰 근거는 config digest 이고, 사이트 manifest
     digest 는 사이트 설치 영수증에 기록한다.
  - 사내 공용 registry 가 있으면 2단계를 그 registry 로 바꿀 수 있다. 있는지 확인 필요 (Q-14).
- **설치 순서** `amplai-bundle verify` → `install` (최초 설치는 묶음 안의 POSIX `install.sh` + 고정 Python 으로 시작):
  1. 서명 검증 → 모든 파일 sha256 → 실패 시 종료 (아무것도 안 바꿈).
  2. 새 venv `<install_root>/releases/<release>/` 에 `--no-index --require-hashes` 설치.
  3. 이미지 load → config digest 대조 → 로컬 registry push → 사이트 영수증.
  4. 사이트 점검(C-14) → container qualify·limits·egress qualify → 칸 probe.
  5. 최초 설치면 `ops local-init`, 업그레이드면 S3 `ops upgrade --release <release>` 가 이 묶음을 쓴다 (동결 → 미리보기 → 승인 → 백업 →
     적용 → `current` 링크 전환). 이전 venv 는 되돌리기용으로 남긴다 (U-6).
- **새 릴리스 반입 경로**: upstream 에서 빌드·서명 → 운영자가 반입 가능한 기기(WSL 은 GitHub 에 닿는다)로 받음 → `bundle verify` → 사내 반입
  절차 → 서버에서 다시 `bundle verify` → S3 업그레이드. 사내 반입 절차(매체, 검사, 승인자)는 확인 필요 (Q-14).
- **사내 overlay**: 사내 서명 overlay(M-3, M-4)는 별도 묶음이고 upstream 묶음에 섞지 않는다 (U-1, AC-U3).

#### 3.5a.4 경로·프로세스·로그·백업 (C-12, AC-C9)

| 용도 | 기본 경로 (설정 가능) | 권한 | 비고 |
|---|---|---|---|
| 설치본 | `<install_root>/releases/<release>/`, `<install_root>/current` 링크 | AMPLAI 계정 | sudo 가 있으면 `/opt/amplai`, 없으면 `~/.amplai/install` |
| 설정 | `<amplai_home>/local/local.json`, `<amplai_home>/meta/local.json` | 0600 | 지금 기본값 `~/.amplai/local/local.json` (`runtime/cli.py:1029`) |
| 비밀 | `<amplai_home>/secrets/` (운영자 토큰, intake 토큰·연결표, 서명 키, 웹훅 URL 파일) | 0700/0600 | store 밖 (H-6) |
| 데이터 | 제품 `runtime_root`, meta root, CAS, `workspace_root` | 0700 + sandbox ACL (작업 공간만) | 로컬 파일 시스템만 |
| 로그 | systemd 는 journald, supervisor 는 `<amplai_home>/logs/` | 0700 | 서버는 access log 를 끈다 (`runtime/cli.py:1040`) |
| 백업 | `<backup_root>/<timestamp>/` | 0700 | live store 밖 (`runtime/recovery/service.py:43-44`). 다른 디스크 권장 |

- **로컬 파일 시스템 검사** (AC-C9): 서버·tick 시작 시 `runtime_root`·`workspace_root` 의 파일 시스템 종류를 읽어 `nfs`, `cifs`, `9p`,
  `drvfs`(WSL `/mnt/<drive>`) 등이면 `Hold("STORE_FILESYSTEM")`. 근거는 store 의 `flock` + WAL (`runtime/storage/store.py:169,186`)이다.
  허용 목록(ext4, xfs, btrfs, apfs)으로 둔다.
- **용량**: RHEL 7.6 의 `/home` 여유는 1.8G 다. 이미지 tar·wheelhouse·작업 공간·백업 크기 합계는 묶음 빌드 때 계산해 `release.json` 에
  싣고, 설치가 여유 공간을 먼저 확인한다. 실제 크기 숫자는 확인 필요.
- **백업**: U-4 manifest 범위를 `ops backup --manifest` 로 만든다 (S3). 지금 백업은 DB + CAS 뿐이다 (`runtime/recovery/service.py:47-100`).
  보존 개수·주기(systemd timer 또는 supervisor 주기)는 운영자 설정. 기본 숫자는 정하지 않는다.
- **프로세스 계정**: AMPLAI 전용 계정 하나(서버·tick·게이트웨이). 사람 계정·앞단 에이전트와 나눈다 (C-5). 전용 계정 생성은 root 가 필요하다
  (Q-11).

#### 3.5a.5 지원 OS 표 (C-13, C-14, AC-C10)

| OS | 필요한 것 | 지금 상태 (2026-10-12) | 권장 |
|---|---|---|---|
| Ubuntu 18.04 서버 | 묶음(py3.11, glibc 2.27), docker 20.10 + ACL, systemd 또는 supervisor, 로컬 fs | Python·docker 있음. bind 시험·sudo·systemd 사용 권한·git 2.17 호환성 확인 필요. Ubuntu 18.04 표준 지원은 2023-05 에 끝났다 | **조건부 지원**. 사이트 점검 + Linux container qualify 통과 시 |
| WSL2 Ubuntu 24.04 | 묶음(py3.12), docker.io 29 + ACL, systemd, ext4 경로(`/mnt/c` 금지) | systemd·sudo 있음. 엔진 도달·bind 시험 확인 대기. 절전·재부팅 뒤에도 유지(Q-16) | **지원** (개발·시험 호스트로 먼저). 앞단 에이전트 격리는 같은 배포판이 아닌 별도 컨테이너 (C-5) |
| RHEL 8 | 묶음(py3.11, glibc 2.28), podman(rootful 또는 rootless + keep-id), SELinux 라벨 | 미점검 | 점검 후 결정. `site_env_check.sh` 를 먼저 돌린다 (Q-13) |
| RHEL 7.6 | 컨테이너 엔진(없음), glibc 2.17 wheel, git 1.8.3.1 호환, 디스크 | 엔진 없음, `/home` 1.8G. RHEL 7 유지보수 지원은 2024-06 에 끝났다 | **실행 호스트로 미지원 권장**. 결정은 운영자 (Q-12) |

- RHEL 7 사실 정리: AMPLAI 실행은 컨테이너 샌드박스가 필수다 (`ContainerSandbox`, design/10 격리). RHEL 7.6 에는 엔진이 없고, 설치하려면
  root 가 필요하다. podman 을 RHEL 7 에 쓸 수 있는지, user namespace(`user.max_user_namespaces`)가 켜져 있는지는 확인 필요. 또 서버 API 는
  loopback 전용이라(`runtime/cli.py:1040`) RHEL 7 을 다른 호스트 서버의 원격 클라이언트로 쓰는 경로도 지금은 없다.
- git 최소 버전: 저장소 코드가 쓰는 git 기능(`git apply --binary`, `fetch --no-tags`, `-C`, `push <commit>:<ref>`;
  `sandbox/git_workspace.py:266`, `runtime/execution/product.py:114`, `runtime/execution/publish.py:321`)의 최소 버전은 정리돼 있지 않다.
  확인 필요. container qualify 에 host git 버전 시험을 더한다.
- PR 게시: `publish` 는 `git_host` 모듈을 부르고 첫 구현은 GitHub CLI `gh` 다 (§3.9). 사내 git 서버 종류에 맞는 게시 경로가
  필요하다 (Q-15, K-9 와 같은 git 서버).
- 표의 각 행은 `deployment/support-evidence.json` 근거로 남긴다. 근거 없는 행은 `disabled` 다 (`deployment/README.md:5-13`).

#### 3.5a.6 업그레이드 리허설 (U-9, AC-U9; S3 끝 기준)

- **데이터 생성**: 이전 릴리스 wheel(N-1)을 격리 venv 에 설치하고, 그 버전의 CLI·API 만으로 사내 모양 데이터를 만든다. 새 코드로 옛 모양을
  흉내 내지 않는다.
  - L2: 사내 승격 부품(기준 digest 포함, 하나는 다음 릴리스에서 기준이 바뀌게), 사내 과제, 사내 칸, 사내 서명 overlay 모듈 하나.
  - L3: 완료·실패·취소 goal, 증거 CAS, 결정 기록, 예산, 승인 원장, 지식 저장소 commit, notifier outbox 미전송 항목, 야간 제어 기록,
    meta store 밤 기록.
  - store 밖: 운영자 토큰, intake 토큰·연결표, 폐기된 토큰 하나(폐기 기록 포함).
- **실행**: N-1 데이터 → 묶음(N)으로 `ops upgrade` 전 단계(동결, 미리보기, 승인, 백업, 이전, 평가기 승인, 재기준화, 해제).
- **불변식 (기계 비교)**:
  - AC-U2: L2·L3 레코드 수와 내용 digest 가 같다 (이전 단계가 바꾸는 필드는 미리보기 보고서에 적힌 것만).
  - AC-U7: 기준이 바뀐 부품만 `stale`, 포인터는 그대로.
  - AC-H7·AC-U6: 폐기 토큰은 계속 거부. 이어서 되돌리기 리허설(U-6 순서) 뒤에도 같다.
  - 업그레이드 뒤 기존 goal 조회, 새 goal 접수, notifier 전송, 야간 dry-run 이 된다.
- **어디서 돌리나**:
  - CI: 릴리스 후보마다 N-1 → N. Linux 컨테이너 안에서 돌린다 (CI 는 `ubuntu-latest`, Python 3.11·3.12, `.github/workflows/ci.yml:11,14`).
    컨테이너 엔진이 필요한 단계(이미지 load)는 `container` marker 로 나눈다 (`pyproject.toml:63`).
  - 사내: 실제 업그레이드 전, 최신 백업을 새 디렉터리에 복원한 사본(U-6 의 복원 방식)에 같은 리허설을 돌리고 결과 보고서를 업그레이드
    승인(S3 단계 4) 화면에 붙인다.
- **건너뛰기 업그레이드**(N-2 → N)를 지원할지는 Q-20.

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
  - Slack 구현은 Incoming Webhook 으로 운영자 채널 하나에만 보낸다. Hermes 의 Slack 앱과 다른 앱이고, 웹훅 URL 은 store 밖
    비밀 파일(0600)에 둔다.

### 3.8 야간 메타 루프 제어 (S1d, OD-12)

| 층 | 누가 | 하는 일 |
|---|---|---|
| 시계 | 서비스 관리자 (macOS launchd, Linux systemd timer 또는 supervisor, §3.5a.1) | 5분마다 `amplai meta nightly tick` (지금은 01:00 에 `run` 한 번, `deployment/launchd/ai.amplai.meta-nightly.plist.template`). tick 은 예정 시각 또는 유효한 시작 요청(N-9)일 때만 그날 밤을 시작하고, 밤이 이미 돌고 있으면(`ACTIVE_OWNER`) 바로 끝난다 |
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
  - (c) 제품 store 기록(채택, Q-9). S1a 의 authority service, 감사, 중복 방지를 그대로 쓴다.
- **평가기 digest**: `nightly.py` 는 평가기 digest 범위 밖이다(`evaluation/versions.py:33-34`). 새 권한 `nightly.control` 은
  권한 코드 변경이라 class C 다.

### 3.9 코드 저장소 연결 (`git_host`, OD-15)

구현됨 (PR #93, D-130). 코드: `runtime/git_host/`.

- **옮긴 호출부** (main `a31d66a` 기준, 코드 저장소에 닿는 호출 전부):
  - `runtime/execution/publish.py:87` `gh pr list`, `:94` `gh pr create --draft`, `:105` `gh pr comment`
  - `runtime/execution/publish.py:313` `git ls-remote`, `:325` `git push` (branch push, force 없음)
  - `runtime/execution/outcomes.py:34` `gh pr view --json state,headRefOid,mergedAt,closedAt`
  - 옮기지 않은 것: `runtime/execution/product.py:113` 의 `git fetch`(계획 기준 commit 을 remote 에서 가져옴). 코드 저장소 API 가
    아니라 일반 git 이고, git host 가 없어도 계획은 돌아야 한다.
- **interface v1** (`git_host/interface.py`): `check_remote(repo, remote)`, `push_branch(repo, remote, branch, commit)`, `open_draft_pr(repo, remote, head,
  base, title, body) -> url`(찾거나 만든다), `comment_pr(repo, url, body)`, `pr_state(repo, url) -> PullRequestState`
  (`state`, `head_commit`, `merged_at`, `closed_at`). 거절 코드는 모듈 이전과 같다: `PUBLISH_REMOTE`, `PUBLISH_PUSH`,
  `PUBLISH_BRANCH_EXISTS`(Conflict), `PR_CREATE`, `PR_COMMENT`, `PR_STATE`. 새 코드는 둘이다: `GIT_HOST_MISMATCH`(아래 host
  확인), `PUBLISH_REF`(`-` 로 시작하거나 빈 remote·branch·head·base, `product.py:105-106` 과 같은 이유).
  - 적합성 시험 `conformance.git_host.v1` 은 위 코드 전부를 확인한다. 시험용 transport 는 interface docstring 의 "conformance
    host" 다(`conformance-down` 은 연결 안 됨, `conformance-readonly` 는 push 거부, `conformance-elsewhere` 는 다른 host,
    `conformance-pushurl` 은 fetch 는 같은 host·push 는 다른 host, `conformance-missing` 은 없는 remote, base
    `conformance-refused` 는 PR 생성 거부).
- **host 확인** (D-130): 이유는 `gh` 가 checkout 의 remote 에서 host 를 읽어서(`gh help environment`) `GH_HOST` 만으로는 PR 이
  갈 곳이 정해지지 않기 때문이다.
  - remote: `check_remote`·push·PR 생성 전에 `git remote get-url --push --all <remote>`(30초, 읽기 전용)의 URL 전부가
    `remote_hosts`(설정, 없으면 `[host]`) 안에 있어야 한다. `pushurl`·`pushInsteadOf` 가 반영된 URL 이다.
  - PR URL: 댓글·상태 읽기 전, 그리고 `gh` 가 찾거나 만든 뒤 돌려준 URL 이 웹 host `host` 에 있어야 한다.
  - 비교: `https://`, `ssh://`, `git://`, `user@host:path` 를 읽고 대소문자·port·끝의 `.` 는 무시한다. 로컬 경로·`file://` 는
    host 가 없어 다르다고 본다. 다르면 `GIT_HOST_MISMATCH`(details `{configured: [hosts], remote}`, 이미 생긴 PR 이면 `pr_url`)다.
  - app `remote` 가 URL·로컬 경로면(`is_remote_url`) `get-url` 없이 그 host 를 `remote_hosts` 와 바로 비교한다. 설정 schema 가
    `remote` 를 자유 문자열로 받고(`local_deployment.py` `AppEntry.remote`), 모듈 이전에도 URL 로 push 할 수 있었기 때문이다.
  - checkout 에 없는 remote 이름, 읽기 실패, 시간 초과는 `PUBLISH_REMOTE` 다.
  - 여러 app: publisher 가 끝나지 않은 app 전부에 `check_remote` 를 부른 뒤에 push 한다. 한 app 이라도 다르면 아무것도 push 하지
    않는다.
  - PR 상태 읽기: tracker 는 코드가 있는 거절(예: `GIT_HOST_MISMATCH`)을 goal 별로 메모리에 두고 `ops pr-sync` 응답의 `unread` 로
    보여 준다. 다음 성공 읽기에서 지운다. `publication_outcome` 에 저장하는 방식은 저장 모양 변경이라 하지 않았다(운영자 결정
    대상).
- **GitHub 구현** (`git_host/github.py`, 내장 `git_host.github@1`, in_process): push·PR 명령은 모듈 이전과 같은
  argv·cwd·timeout(쓰기 300초, 읽기 120초)이다. 더해진 명령은 host 확인의 `git remote get-url --push --all` 뿐이다. `host` 가
  `github.com` 이 아니면 `gh` 명령에만 `GH_HOST=<host>` 를 더한다(설정값 그대로, port 를 적었으면 port 포함. `gh` 가 port 를
  받는지는 확인하지 않았다). 사내 GitHub 에서의 동작은 아직 확인하지 않았다 (Q-15, S4c).
- **설정**: `local.json` 의 `git_host` (선택). 없으면 `{"module": "git_host.github", "host": "github.com"}` 이고 push·PR 은
  모듈 이전과 같다. `host`·`remote_hosts` 는 소문자로 바꿔 저장하고 끝의 `.` 는 거절한다. `remote_hosts`(선택, 1~16개)는 remote
  가 push 해도 되는 host 이고 없으면 `[host]` 다. `null` 이면 git host 모듈이 없다. 기본 설정인데 구현 파일이 없으면
  deployment 를 만들 때 `ImportError` 로 실패한다(fail-closed: 게시 경로가 조용히 꺼지지 않는다).
- **제거 시험** (M-13, 대상 한정): `git_host: null` 이면 검증된 goal 은 `verified` 로 남고 게시는 `PUBLISH_NO_GIT_HOST` 로
  hold 된다(commit·push·publication 기록 없음). PR 상태 읽기(`ops pr-sync`)도 같은 코드로 hold 다. 구현 모듈은 설정에서 고를
  때만 import 한다 (`tests/v3/test_034_git_host.py`). M-13 의 "선택 모듈을 뺀 구성으로 코어 시험 전체" CI 단계는 아직 없다
  (`check` kind, M-16 과 함께 둔다).
- **아직 강제하지 않는 것**: runtime 은 deployment 를 만들 때 적합성 결과나 registry 를 보지 않는다(`MODULE_UNQUALIFIED` 연결은
  남은 작업이고 notifier 도 같다). module spec 의 `permissions` 는 비어 있다. 권한 어휘가 아직 없어서다. pack 의 "필요 권한"
  (M-14)을 정할 때 함께 정한다.
- **PR 상태 읽기의 작은 차이**: `gh pr view` 가 객체가 아닌 JSON 을 주면 이전에는 tracker 의 `.get` 이 `AttributeError` 로
  sync 전체를 끊었다. 지금은 `Hold PR_STATE` 라 그 PR 은 unknown 으로 남고 다른 PR 은 계속 읽는다. `headRefOid` 가 `""` 이면
  `head_commit` 은 `None` 이다. 이전에도 빈 문자열은 falsy 라 revised 로 보지 않았으므로 판정은 같고, `publication_outcome.head`
  에 `""` 대신 `null` 이 남는 것만 다르다.
- **지식 승인 증거 (D-115, S2a)**: v1 에 없다. S2a 에서 interface v2 와 새 적합성 시험으로 더하며 그때 운영자 결정이 필요하다.
  제안 모양 (운영자 확인 대기): `review_evidence(repo, change_url) -> {approvers: [{login, commit}], merged_commit, protected_branch,
  codeowners_required, stale_dismissed, admin_bypass}`. 판정(D-115 조건을 만족하는가)은 지식 모듈이 하고, git host 는 사실만
  읽는다.

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
| 반입 묶음 변조 | 닫힌 망으로 들어오는 wheel·이미지 바꿔치기 | 서명 manifest, wheel hash, 이미지 config digest, 검증 전 무변경 | AC-C8 |
| 샌드박스 탈출 → 키 | 컨테이너가 호스트 uid 로 돌면 store·`authority.pem` 에 닿음 | 호스트와 다른 sandbox uid, ACL 은 작업 공간·run home 에만 | AC-C6 |
| 엔진 그룹 = root | `appdocker` 그룹 계정 침해가 호스트 root 침해 | AMPLAI 전용 계정, 사람·앞단 에이전트와 분리, 앞단 에이전트 컨테이너는 엔진 소켓 mount 없음 | AC-C6, AC-H8 |
| 동결 우회 | 서비스 관리자 재시작·timer 가 동결 중 store 를 엶 | 서비스 정지 + 동결 표지 + owner lock 이중 잠금 | AC-C5, AC-U4 |
| 공유 파일 시스템 store 손상 | NFS·drvfs 위 SQLite WAL·`flock` | 로컬 파일 시스템 허용 목록, 시작 거부 | AC-C9 |
| 검증 안 된 업그레이드 | 사내 데이터가 새 릴리스에서 깨짐 | 이전 릴리스가 만든 데이터로 리허설, 사내는 백업 사본 리허설 후 승인 | AC-U9 |

## 5. 슬라이스와 순서 (OD-3)

| 슬라이스 | 내용 | 끝 기준 | gate | 선행 |
|---|---|---|---|---|
| S0 | 모듈 레지스트리, 신뢰 루트, 프로세스 밖 실행, 내장 모듈 감싸기(작은 PR), `classify`·digest 범위(eval-N) | AC-M1~M6, AC-X1 | G-07 | — |
| S1a | `intake` actor, store 밖 토큰·연결표, 접수 경로, steering·질문 종류 제한, 조회 필터, projection, 세대 cursor, 승인 CLI digest, 빈도 제한, MCP 서버, cron 알림, VM runbook | AC-H1~H10 | — | S0 |
| S1b | 승인 페이지 (OD-6), 원격 listener (OD-8 이 원격일 때) | AC-H11 | — | S1a |
| S1c | 앞단 에이전트·메신저 모듈: adapter 별 intake 설정, 경로·표지 일반화, `notifier` kind + Slack, 도구 계약 v1, MCP 서버, Hermes 키트 | AC-F1~F3, AC-H8, AC-H9 | — | S1a |
| S1d | 야간 루프 제어: `nightly.control`, `nightly-control` 기록, tick(launchd; Linux systemd·supervisor 는 S4a), 밤의 읽기 전용 확인, 읽기 전용 status, meta feed, CLI | AC-N1~N6 | class C(권한) | S1c |
| S1e | 진짜 일시정지(H-15): Run `pausing → paused`, 체크포인트 보존, 같은 세션 재개 또는 이어 실행, 멈춘 동안 지시 반영, 자원·시간 초과 | 추가 예정 | — | S1a |
| S1f | 운영자 이벤트 세대 cursor 와 공통 snapshot 경로 (H-16) | 추가 예정 | — | S1a |
| S2a | 지식 모듈 | AC-K1, AC-K3 | G-04 | S0 |
| S2b | 문서 최신성 모듈 | AC-K2 | — | S2a |
| S3 | 배포 릴리스·Q-suite 자산, 동결(`ServiceManager` 인터페이스 + launchd 구현), 명시적 이전, 백업 manifest CLI, 업그레이드, 되돌리기, stale, local-update 제한, 업그레이드 리허설 | AC-U1~U9 | G-07, G-15, G-20 | S0 |
| S4a | Linux 이식: `ServiceManager` systemd·supervisor 구현, unit 출력, 동결 표지, 엔진 설정 단일화, sandbox uid + ACL, 로컬 파일 시스템 검사, 사이트 점검 확장, Linux container qualify | AC-C4~C7, AC-C9 | — | S3(인터페이스), S1d(tick) |
| S4b | 오프라인 묶음: 묶음 빌드·서명, wheelhouse(hash, platform 별), 이미지 tar + config digest + 사이트 로컬 registry, `bundle verify`·설치, 지원 OS 근거 | AC-C8, AC-C10 | G-07 | S3, S4a |
| S4c | onprem qualify, 모델 게이트웨이, OpenCode 사내 모델, 사내 과제 도구 (기존 S4) | AC-C1~C3 | G-07 | S0, S3, S4b, OD-4, Q-6 |
| S5 | Jev | 자격 시험 | — | S0, 접근 권한 |

- 평가기 digest 범위의 파일을 바꾸는 PR 은 eval-N 승인을 함께 묶는다.
- 메타하네스 파일럿과 같은 Claude 계정을 쓴다. 구현은 파일럿이 쉬는 시간에.

## 6. 결정 (`docs/workstreams/v3-real-execution/DECISIONS.md` D-106~D-123 에 기록)

- D-106 Hermes = IntakeAdapter + 오케스트레이터. `intake` actor(비 human), store 밖 토큰·연결표, 종류 제한, projection 알림.
- D-107 배포는 upstream → 사내 한 방향.
- D-108 두 층 모듈 체계, 고정 배포물만, 신뢰 루트, 외부 입력 kind 는 프로세스 밖.
- D-109 지식·문서 최신성 V3 모듈 복원 (S17 `f06a8e5` 의 D1·X9 삭제를 정정, design/12 §1).
- D-110 3층 업그레이드: 동결, 명시적 이전, 승인 후 백업, 되돌리기 순서, 기준 digest·stale.
- D-111 모델 게이트웨이 (OD-4). D-112 Hermes 일시정지·취소 허용, 재개는 Hermes 가 멈춘 goal 만, `goal.cancel` 신설 (OD-5).
  D-113 승인 페이지 passkey (OD-6).
- D-114 Q-suite 배포 자산화 (OD-7).
- D-115 지식 공간과 git 서버 승인을 사람 승인 증거로 쓰는 대응 (OD-10, 확정 2026-10-12).
- D-116 앞단 에이전트·메신저는 세 층 모듈(접수 어댑터, 앞단 에이전트 키트, notifier)이다. wire `source_channel=hermes` 는 앞단 에이전트 채널 이름으로 두고, adapter 는 `authn_context_ref` 로 구분한다 (OD-13).
- D-117 야간 루프 제어: 시계 tick(서비스 관리자, D-118), 진행 NightlyRunner, 앞단 에이전트는 조이기와 조건부 시작(N-9, `nightly.control`), 제어 기록은 제품 store, 밤은 읽기 전용으로 확인하고 못 읽으면 멈춘다 (OD-12, Q-8 = B).
- D-118 서비스 관리자 인터페이스 하나에 launchd·systemd·supervisor 구현을 둔다. AMPLAI 는 unit 을 출력만 한다. 동결은 서비스 정지 + 동결 표지 + owner lock 이중 잠금이다 (C-8).
- D-119 Linux 샌드박스는 호스트와 다른 고정 uid(기본 65534)를 유지하고 접근은 POSIX ACL 로만 준다. 호스트 uid 실행은 기각, chown·userns-remap 은 제외, rootless podman `keep-id` 는 별도 자격 시험 후보 (C-9).
- D-120 컨테이너 엔진은 설정 한 곳에서 정하고 직접 `docker` 호출을 없앤다 (C-10).
- D-121 닫힌 망 배포는 서명된 묶음(hash 고정 wheelhouse, 이미지 tar + config digest, 사이트 로컬 registry)으로 한다. 검증 전에는 아무것도 바꾸지 않는다 (C-11, G-07).
- D-122 지원 OS: WSL2 Ubuntu 24.04 지원, Ubuntu 18.04 조건부 지원, RHEL 8 점검 후 결정, RHEL 7 은 실행 호스트 미지원 권장. 운영자 결정 대기 (Q-12, C-13).
- D-123 업그레이드 리허설(이전 릴리스가 만든 사내 모양 데이터 → 새 릴리스)을 S3 끝 기준으로 하고, 사내 업그레이드 승인 전 백업 사본 리허설을 필수로 한다 (U-9).
- D-127 (확정, 운영자 2026-10-14) H-16 snapshot 경로 분리: 운영자 `GET /api/v3/snapshot`, intake 는 adapter 별
  `/api/v3/intake/{adapter_id}/snapshot`. 숫자만 있는 cursor 는 `CURSOR_EXPIRED` (§3.2 항목 8, spec H-16, S1f PR #87).

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
| Q-9 | 제어 기록 위치 → 결정: 제품 store (2026-10-11, 운영자 "정석대로") | S1d |
| Q-10 | notifier 의 Slack 방식 → 결정: Incoming Webhook, Hermes 와 다른 Slack 앱 (보내기만, 최소 권한, 독립 채널). 여러 사람 DM·메시지 갱신이 필요해지면 bot 구현을 추가 (2026-10-11) | S1c |
| Q-11 | Ubuntu 서버: 신청하면 sudo·systemd·crontab 을 모두 쓸 수 있고, 받은 기간 동안 한시적으로 쓴다 → 결정: system unit 1순위(D-118). 한시 사용이라 반납 전 백업·이전 절차가 필요하다 (운영자 답 2026-10-12) | S4a |
| Q-12 | RHEL 7 지원 여부. 사실: 엔진 없음, glibc 2.17, git 1.8.3.1, `/home` 여유 1.8G, RHEL 7 유지보수 지원 종료(2024-06). 권장: 실행 호스트 미지원 | S4b 묶음 platform, 지원 표 |
| Q-13 | RHEL 8 사이트 점검 (`site_env_check.sh`): Python 3.11 유무, podman 버전·rootless·cgroup 버전, SELinux 모드 | S4a 엔진·권한 |
| Q-14 | 사내 공용 컨테이너 registry 는 없다. 서버만 있다 (운영자 답 2026-10-12) → 이미지는 tar 로 반입해 `docker load` 하고, registry 없이 이미지 ID(config digest)로 고정하는 방식으로 바꾼다 (지금 `sandbox/container.py:34` 는 `@sha256` registry digest 만 받는다, D-121 수정 대상). 반입 매체·승인자 절차는 확인 필요 | S4b |
| Q-15 | 사내 GitHub 가 있다 (운영자 답 2026-10-12; GitHub Enterprise Server 인지는 확인 필요) → 코드 저장소 연결도 모듈로 둔다 (OD-15, `git_host` kind: 첫 구현 GitHub·GitHub Enterprise, 다른 서버는 나중에). PR 게시 `runtime/execution/publish.py:86-104` 의 `gh` 고정을 이 모듈 뒤로 옮긴다. 모듈과 GitHub 구현은 됐다(§3.9). 사내 GitHub 종류·동작 확인은 남았다 | S4c, S2a |
| Q-16 | WSL2 는 Windows 절전·재부팅 뒤에도 유지된다 (운영자 답 2026-10-12). systemd 는 `/etc/wsl.conf` 로 켤 수 있고 켜졌다(PID 1 systemd), docker.io 29.1.3 설치됨 | S4a |
| Q-17 | 데이터·백업 디스크의 파일 시스템 종류(NFS 여부)와 용량 | S4a 경로, AC-C9 |
| Q-18 | bind 쓰기 시험 결과 (Ubuntu 18.04 는 이미지 하나로 `sh site_env_check.sh <image>`, WSL 은 엔진 도달부터) | S4a uid·ACL |
| Q-19 | 각 호스트 CPU 아키텍처 (`uname -m`) | S4b wheel·이미지 platform |
| Q-20 | 건너뛰기 업그레이드(N-2 → N)를 지원하나, 연속 릴리스만 지원하나 | S3 리허설 범위 |

## 8. 위험

- Hermes 보안: 공개 CVE 다수, VM 침해 시 연결 사용자 사칭·steer 주입은 남는다 (H-14). Hermes 가 스스로 쓰는 메모리·스킬이 주입을
  지속시킬 수 있는지 확인 필요 → 스킬 자동 작성을 끄거나 주기 검토를 qualification 항목으로.
- S0·S3 는 코어(권한, store 열기, 릴리스)를 건드린다. 동작 불변 PR 과 장애 훈련 test 로 나눈다.
- S2a 이식 크기는 추정이 어렵다.
- S4 는 사내 확인 사항(Q-6, OpenCode provider 설정)에 막힐 수 있다. S4a·S4b 는 Q-11~Q-19 의 사이트 사실에 막힐 수 있다.
- Ubuntu 18.04·docker 20.10 은 오래된 기반이다. 고정한 의존 wheel 이 glibc 2.27(RHEL 7 이면 2.17) 태그를 계속 낼지, 엔진 동작 차이가 있는지는 릴리스마다 자격 시험으로 확인한다.
- ACL 방식은 default ACL mask 때문에 호스트 정리가 막힐 수 있다 (추정). Linux container qualify 로 확인하고, 막히면 컨테이너 안 정리로 대신한다.
- 알림은 cron 주기만큼 늦다.
- 계정 한도: 구현·리뷰와 파일럿이 같은 Claude 계정.
