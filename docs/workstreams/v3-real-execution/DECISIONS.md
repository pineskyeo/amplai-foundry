# V3 Real Execution Decisions

## D-065 — Close V3 Execution Only Through A Real Qualified Driver

- Status: APPROVED
- Date: 2026-09-28
- Work: `AMPLAI-V3-EXEC01` (`specs/018-v3-real-execution`)
- Decision: V3 실행 경로(claim → WorkCoordinator → driver port → ContainerSandbox → verify)의 완료는
  실제 qualified driver 의 run evidence 로만 판정한다. RecipePort·FakeContainer·MockTransport 시험은 state
  machine 시험으로 유지하되 driver conformance 나 end-to-end 완료 근거로 세지 않는다.
- Reason: T-107, V3-026, V3-057 은 설계상 실제 baseline driver 를 요구했지만 fixture 로 `local_pass` 가
  됐다 (`tests/v3/test_rc01_e2e_cross_app.py:19,31`). 설계는 이를 금지한다
  (`design-reference/design/21_TEST_ACCEPTANCE.md:31,35`). 범위를 줄인 Decision 은 없었다.
- Rejected: 기존 `final_declared` 를 그대로 두고 다음 버전 과제로 넘긴다. 설계가 요구한 baseline 이
  빠진 상태를 완료로 계속 표시하게 된다.
- Consequence: Work 018 의 실제 Codex run evidence 로 해당 항목을 다시 판정한다. 그 전까지 이 경로는
  미완료로 보고한다.
- Source: `specs/018-v3-real-execution/spec.md#Why`.

## D-066 — Run The Worker Inside The Serve Process

- Status: APPROVED
- Date: 2026-09-28
- Decision: 첫 production worker 는 `amplai ops serve` 프로세스 안의 thread 로 claim → execute → verify 를
  수행한다.
- Reason: Runtime store 는 단일 owner lock 을 쓴다 (`src/amplai_foundry/runtime/storage/store.py:164-171`).
  별도 프로세스는 serve 와 동시에 store 를 열 수 없고, HTTP worker route 는 client 와 collector 가 없다.
- Rejected: 별도 worker 프로세스 + HTTP route. 이번 범위(단일 Mac)에서는 가치보다 작업이 크다.
- Consequence: 원격 worker 는 이후 Work 의 HTTP route 완성으로 추가한다.

## D-067 — Codex CLI Is The First Production Driver

- Status: APPROVED
- Date: 2026-09-28
- Decision: `codex-cli` 0.155.1 + `gpt-5.6-sol` 을 첫 production driver 로 등록한다. ChatGPT 계정 credential 은
  사용자가 만든 scoped 사본(`scripts/sandbox_up.sh --codex-home`)을 dispatch 마다 새 native home 에 seed 한다.
- Reason: 설계상 기본 로컬 worker 후보이고 (`design-reference/design/11_DRIVERS_SESSIONS.md:34`) 컨테이너 자격
  9/9 (`specs/016-container-egress-profile/driver-qualification.json`). 운영자가 가진 계정과 일치한다.
- Rejected: 실제 `~/.codex` mount (설계 `10_AUTHORITY_SECURITY.md:42` 위반), OpenAI API key (없음).

## D-068 — Git Commit Workspace And Host-Computed Patch

- Status: APPROVED
- Date: 2026-09-28
- Decision: workspace 는 대상 repo base commit 의 `git archive` 사본이다 (`.git` 제외, dotfile 포함). 작업 결과는
  container 종료 뒤 host 가 임시 index 로 계산한 patch 하나이며 `text/x-diff` artifact 로 admit 한다.
- Reason: 설계는 worktree base + diff artifact 를 증거로 삼는다 (`08_STATE_RECOVERY.md:36`,
  `27_END_TO_END_SCENARIOS.md:14`). agent 가 쓴 파일을 결과로 믿지 않는다. 기존 `WorkspaceManager` 는 dotfile 을
  거부해 실제 repo 를 담을 수 없다 (`src/amplai_foundry/sandbox/workspace.py:56-62`).
- Rejected: `git worktree` mount (agent 가 `.git` 과 원본 object DB 를 볼 수 있다).

## D-069 — Codex Read-Only Planner Without An API Key

- Status: APPROVED
- Date: 2026-09-28
- Decision: 계약 초안은 read-only container 안의 Codex turn 이 `--output-schema` 로 만든다. 모델은 좁은
  초안(목표, 범위, 제외, 제약, 설치된 verifier 명령 id 에 묶인 acceptance 문장, 위험도, 질문)만 쓴다.
  ref·digest·capability·budget 은 결정적 compiler(`runtime/execution/product.py`)가 만들고, 결과는
  `GoalService.freeze_contract`(스키마 + ContractCritic)와 `Runtime.save_graph`(GraphCompiler,
  validate_bindings) 검증을 통과해야 한다. 질문이 있으면 계약을 만들지 않고 운영자에게 돌려준다.
  (2026-09-28 구현 전 정밀화: 모델이 graph node 전체와 verifier digest 를 쓰는 PlanningService schema
  대신, 모델이 식별자를 지어낼 수 없는 좁은 schema 를 쓴다.)
- Reason: 기존 planner 는 OpenAI Responses API key 를 요구한다 (`src/amplai_foundry/runtime/deployment.py:326-374`).
  운영자에게 key 가 없다.
- Rejected: 결정적 template 계약 (acceptance 를 사람이 직접 써야 해서 목표 한 줄 UX 가 깨진다).

## D-070 — Local Operator Authority

- Status: APPROVED
- Date: 2026-09-28
- Decision: deployment 가 명시적으로 켠 경우, 인증된 `kind=human` 운영자가 정확한 contract_ref·graph_ref·
  capabilities 에 대해 남긴 승인 기록을 execution decision 으로 쓴다. 승인은 revoke 할 수 있고, publish
  capability 도 같은 승인에 명시돼야 한다.
- Reason: 운영자는 goal 마다 승인하기로 했다. 기존 경로는 V2 Foundry proposal 승인을 요구해 goal 단위 승인에
  맞지 않는다 (`src/amplai_foundry/runtime/contracts/foundry_authority.py:167-191`).
- Rejected: 승인 없는 자동 grant (운영자 결정과 설계 `01_SCOPE_AND_DECISIONS.md:41` 위반).
- Consequence: 이 adapter 는 단일 운영자 로컬 profile 전용이다. 여러 사용자 profile 은 Foundry bridge 를 쓴다.

## D-071 — Publish As A Governed Effect After Verification

- Status: APPROVED
- Date: 2026-09-28
- Decision: `goal.verified` 뒤에만 base commit 위에 verified patch 를 commit 한 `amplai/<goal_id>` branch 를
  push 하고 draft PR 을 만든다. 효과 key 는 goal_id + patch digest 로 멱등이다. main 과 사용자 working tree 는
  수정하지 않는다.
- Reason: 운영자 결정(draft PR 까지 자동). 설계는 git push 를 별도 effect 로 둔다
  (`design-reference/design/09_STORAGE_TRANSACTIONS.md:26`, `32_TOOL_EFFECT_PROTOCOL.md`).
- Implementation (2026-09-28): `EffectService` 는 running run 안의 tool effect 용이라(run running, goal
  active 요구) `goal.verified` 뒤에는 쓸 수 없다. publish 는 같은 규칙(prepared → pushed → done,
  중단 시 재실행 대신 원격 상태로 reconcile, force 금지)을 따르는 `publication` 기록으로 구현하고
  (`src/amplai_foundry/runtime/execution/publish.py`), 권한은 운영자 승인 기록의 `publish` 동의
  (mode·remote·base_branch)에서만 나온다. commit 은 임시 index plumbing 으로 만들어 운영자 checkout 을
  건드리지 않는다.

## D-073 — The Container Is Codex's Sandbox; Tool Use Is Qualified

- Status: APPROVED
- Date: 2026-09-28
- Decision: Codex 는 AMPLAI 컨테이너 안에서 `--dangerously-bypass-approvals-and-sandbox` 로 실행한다. 격리는
  자격을 받은 ContainerSandbox(read-only root, cap-drop ALL, uid 65534, pids/memory 제한, egress allowlist)가
  맡는다. planner 의 읽기 전용은 워크스페이스 read-only bind mount 로 강제한다. 실제 작업 등록에는 설계의
  9 probe 외에 `tool_use`(실제 turn 에서 셸 실행과 파일 쓰기) pass 가 필요하다.
- Reason: 실측(2026-09-28): 컨테이너 안에서 Codex 자체 bwrap sandbox 는 namespace 를 만들 수 없어
  (`bwrap: No permissions to create a new namespace`) 모든 셸 명령과 파일 쓰기가 실패했다. Work 016 의 9/9 는
  PONG turn 과 docker stop 으로 측정돼 도구 실행을 검증하지 않았다. Codex 문서는 이 옵션을 "외부에서 격리된
  환경 전용" 으로 둔다.
- Rejected: 컨테이너에 privileged/userns 권한을 줘서 bwrap 을 살린다 (컨테이너 격리를 약화한다).
- Consequence: container_qualify 와 production argv 가 같은 옵션을 쓴다. image 의 login shell 이 app venv 를
  PATH 에 넣는다 (`bash -lc` 가 PATH 를 다시 만든다).

## D-072 — Per-App Worker Image And Requalification

- Status: APPROVED
- Date: 2026-09-28
- Decision: 대상 app 마다 pinned worker base 위에 app 의존성을 설치한 image 를 만들고 digest 로 pin 한다.
  image digest 가 바뀌면 그 image 로 Codex 를 다시 자격 측정한 뒤에만 등록한다.
- Reason: agent 와 verifier 가 app 테스트를 돌리려면 의존성이 필요하다. driver 는 자격을 받은 환경에서만 작업을
  받는다 (`src/amplai_foundry/runtime/execution/service.py:136-137`).

## D-074 — Local Execution Metrics From The Audit Trail

- Status: APPROVED
- Date: 2026-09-28
- Decision: 로컬 실행 흐름은 사람 개입을 goal audit event 로 남긴다. 초안이 질문으로 끝나면 `question.asked`,
  승인 대기면 `approval.requested`, 사람이 승인하면 `approval.granted`, 사람이 승인을 취소하면
  `approval.revoked` 다. loop 가 스스로 멈추며 하는 revoke 는 사람 개입이 아니므로 남기지 않는다. Observatory 는
  다음을 계산한다.
  - `human_wait_ms`: `approval.requested` → `approval.granted` 의 중앙값.
  - `queue_ms`: `approval.granted` → 그 goal 의 첫 run 생성(worker claim) 의 중앙값.
  - 각 항목의 표본 수: `*_samples`.
  - 양 끝이 없는 goal 은 표본이 아니다. 표본이 없으면 값은 `null` 이다. 음수 구간은 integrity finding 이다.
  - `compute_ms`: 계속 `null` 이다. wall time 은 compute time 이 아니다.
  - `failure_reasons`: failure signature 를 그 run 의 attested verdict 이유(`AC: "outcome: reason"`)로 풀어 보인다.
  - `ended_run_reasons`: 멈춘 run 의 종료 이유를 센다.
  - event 범위: 필터가 없으면 한 번도 실행되지 않은 goal(질문, 승인 전 취소)의 event 도 센다.
- Reason: metrics review(2026-09-28)에서 확인했다. 승인이 event 로 남지 않아 사람 개입과 대기 시간이 비어
  있었고, 시간 분해는 `None` 으로 고정돼 있었다 (`src/amplai_foundry/evaluation/observatory.py`).
  failure signature 는 hash 뿐이었다. 설계는 사람 개입(질문·승인)과 queue/compute/human wait 분해를 요구한다
  (`design-reference/design/15_EVAL_OBSERVATORY.md:20-31`).
- Rejected: failure signature 형식을 읽을 수 있는 문자열로 바꾼다. "같은 실패 3회면 중단" 규칙이 signature 동등
  비교에 기대고 RunRecord schema 계약이 바뀐다. 이미 저장된 과거 승인의 대기 시간을 추정해 채운다.
- Found with it: `Runtime.end_goal` 은 run head 만 바꾸고 RunRecord(`status`, `finished_at`)는 그대로 둬서
  Observatory 가 멈춘 run 을 `running` 으로 셌다. 이제 `_run_state` 로 둘을 함께 바꾼다. 수정 전 행은 값을
  고쳐 쓰지 않고 `run_state_mismatch` finding 으로 보고한다.
- Open: task class 로 나눠 보기(`task_class`)는 workgraph/goal-contract/run-record schema 에 칸이 없다
  (`additionalProperties: false`). 계약 변경이라 운영자 결정 전에는 구현하지 않는다.

## D-075 — Re-Judgement Of T-107, V3-026 And V3-057 After Real Runs (D-065)

- Status: accepted
- Date: 2026-09-28
- Decision: D-065 가 정한 대로 실제 run evidence 로 세 항목을 다시 본다. 판정 범위는 evidence 가 실제로 덮는
  부분으로 한정한다.
  - V3-026 "Codex CLI qualified baseline driver": 실제 evidence 로 닫힌다. codex-cli 0.155.1(`gpt-5.6-sol`)이
    app image `9e09cc67…` 에서 9 probe 와 `tool_use` 를 pass 했다
    (`specs/018-v3-real-execution/driver-qualification-amplai-foundry.json`). production 과 같은 argv 로 실제
    goal 두 개가 첫 시도에 검증돼 draft PR 이 됐다 (`specs/018-v3-real-execution/runs/`).
  - V3-057 "Cross-app end-to-end goal/steer/replan", T-107 "rough intent → two sandbox apps": 실제 evidence 로
    닫히지 않는다. Work 018 의 실제 경로는 app 하나이고 steer/replan 을 쓰지 않는다. 두 항목의 cross-app·
    steer·replan 부분은 지금도 fixture evidence 뿐이다. closure ledger 는 이미 이 둘을 "local automated
    evidence; not live provider/container/holdout qualification" 으로 표시한다
    (`release/rc01-conformance-closure.json`).
- Reason: 단일 app 실제 run 을 cross-app 완료의 근거로 세면 D-065 가 막으려던 과장이 반복된다.
- Consequence: 남은 일은 실제 driver 로 두 app 이상과 steer/replan 을 거치는 run 이다. 이 Work 범위(Mac, 단일
  app)가 아니다. claude-cli 는 Work 016 의 9 probe 만 있고 container 안 `tool_use` 는 측정하지 않았다.

## D-076 — Task Class On Workgraph Nodes; Active Ledger As A Catalog Document

- Status: APPROVED (운영자 선택 2026-09-28: 두 추천안)
- Superseded (Decision 1 의 저장 위치만): D-077 이 대체한다. Decision 2 는 유효하다.
- Date: 2026-09-28
- Decision 1: workgraph node schema 에 선택 항목 `task_class` 를 추가한다. 값은 이 repo 의 Work type
  (`tiny_change`, `bug_fix`, `logic_change`, `refactor`, `new_feature`, `domain_heavy`, `architecture`,
  `operations`)이다. planner 가 초안에서 제안하고 운영자가 승인 화면(`class:`)에서 본다. 목록 밖 값은 node 에
  넣지 않아 Observatory 에서 `unreported` 로 남는다. D-074 의 Open 항목을 닫는다.
- Decision 2: 이 ledger 는 진행 중인 workstream 의 결정 기록이라 historical route 대신 소유 metadata 를 가진
  활성 catalog 문서(`kind: decision_record`, `lifecycle: active`)로 등록한다. 변경마다 문서 검토를 거친다.
  workstream 이 닫히면 lifecycle 을 바꾼다.
- Reason: 설계는 task class 로 지표를 나눠 보라고 요구하고(`design-reference/design/15_EVAL_OBSERVATORY.md:33`)
  Observatory 는 이미 `node.task_class` 를 읽지만 schema 에 칸이 없었다. 분류를 나중에 소급하면 추정이 된다.
  `/work` 절차는 `docs/workstreams/*/DECISIONS.md` 에 결정을 덧붙이라 하고 문서 정책은 그 경로를 불변
  이력으로 봐서 `docs impact` 가 `HISTORICAL_SOURCE_CHANGED` 로 멈췄다.
- Rejected: task_class 를 자가개선 Work 로 미룬다. 문서 도구가 append-only 이력을 받도록 고친다(kit release
  필요). 결정을 `specs/018` 로 옮긴다(절차와 어긋남).

## D-077 — Task Class Is Recorded Beside The Graph, Not In The 3.0.0 Workgraph Schema

- Status: accepted (D-076 Decision 1 의 목적은 그대로, 저장 위치만 바꾼다)
- Date: 2026-09-28
- Decision: `task_class` 는 workgraph node 에 넣지 않는다. 로컬 실행 흐름이 계약을 만들 때 work 마다
  `task-class` 기록(`work_id`, `graph_ref`, `task_class`, `source: planner`)을 따로 저장한다. Observatory 는 그
  기록으로 run 을 나눠 본다. 어휘, planner 제안, 승인 화면 `class:`, `--task-class` 필터는 D-076 그대로다.
- Reason: 실측(verifier v2): workgraph schema 는 structured output 에도 쓰인다. 그래서 모든 속성이 required
  (값이 없으면 null)여야 한다 (`src/amplai_foundry/runtime/contracts/provider_schema.py:55-62`). 선택
  항목은 이 규칙을 어겨 DEV-01 critic 시험 3건이 실패했다. required + nullable 로 바꾸면 승인된 V3 Design 1.0
  fixture(`design-reference/fixtures/valid/workgraph.json`)가 실패한다. design-reference 는 다시 쓰지 않는
  원본이고 wire schema 는 3.0.0 으로 고정이다. 두 조건을 모두 지키는 schema 변경은 없다.
- Rejected: design-reference fixture 를 고친다 (승인 원본 변경). structured-output 규칙에 예외를 둔다.
- Consequence: workgraph schema 두 사본은 main 과 같다. `task-class` 는 V3 wire 계약이 아닌 로컬 제품 기록이다.
  wire schema 에 넣으려면 schema version 을 올리는 별도 설계가 필요하다.

## D-078 — Draft PR Outcomes Are Recorded (Work 019 A)

- Status: APPROVED (운영자 선택 2026-09-28: A 우선 처리)
- Date: 2026-09-28
- Decision: 게시된 draft PR 의 상태를 운영자의 `gh` 로 읽기만 하고(`gh pr view --json state,headRefOid,…`),
  전이마다 한 번 goal audit event 로 남긴다: `publication.opened`(게시 때; 추적 전 PR 은 `backfilled: true`
  로 소급), `publication.merged`, `publication.closed`, `publication.revised`(branch head 가 AMPLAI 가 push 한
  commit 이 아님 = 사람이 고침). 실행 loop 가 한가할 때 10분마다 읽고 `amplai ops pr-sync` 로 즉시 읽는다.
  읽지 못한 PR 은 unknown 으로 두고 다음에 다시 읽는다. 여러 app goal 은 PR 들의 합계다(전부 merged 여야
  merged, 하나라도 closed 면 closed, 하나라도 고쳐지면 revised). Observatory 는 acceptance rate(merged/
  decided)와 revised share 를 낸다.
- Reason: 설계는 rework 와 사람 개입을 지표로 둔다 (`design-reference/design/15_EVAL_OBSERVATORY.md:20-31`).
  "결과가 받아들여졌나"가 빠져 있었다.
- Rejected: GitHub webhook(로컬 Mac 에 공개 endpoint 가 필요), PR 상태 추정.

## D-079 — The System Selects The Composition; Claude CLI Is The Qualified Fallback (Work 019 E)

- Status: APPROVED (운영자 선택 2026-09-28: 초기 정책 "Codex 우선, Claude fallback")
- Date: 2026-09-28
- Decision: app 마다 driver 별 HarnessComposition 과 task-class baseline router policy 를 둔다.
  `CompositionService.select` 가 eligibility(model enabled, data class, driver qualified, capabilities)로 먼저
  거르고 policy 순서로 고른다. `explain` 이 후보별 제외 이유를 낸다. planning 도 같은 선택을 따른다(Claude
  planner 는 `--json-schema` 의 `structured_output` 을 읽는다). 실행 선택은 plan 에 고정해 승인 화면에 보이고,
  승인 기록을 쓰기 전에 다시 확인한다(`COMPOSITION_CHANGED` 면 다시 계획). 운영자는 driver 를 고르지 않는다.
  `amplai ops local-driver <codex|claude> --enable/--disable` 가 eligibility 스위치다.
  Claude CLI 는 app image 에서 production argv 로 9 probe + 실제 tool-use turn 을 pass 해야 등록된다
  (`specs/019-v3-completion/driver-qualification-amplai-foundry.json`, 2.1.278 / claude-sonnet-5).
- Reason: 설계는 시스템 선택이다 (`design-reference/design/16_META_HARNESS.md:14-16`). 사람이 goal 마다 driver 를
  고르는 안은 사용자가 설계와 어긋난다고 거부했다.
- Consequence: 두 driver 는 한 sandbox environment 기록을 공유한다(실행과 검증이 같은 pinned container 를
  가리킨다). profile 기록은 image 별이다.

## D-080 — Design Mode Produces A Verified Design Document (Work 019 F)

- Status: APPROVED (운영자 선택 2026-09-28: 문서만 담은 draft PR)
- Date: 2026-09-28
- Decision: `amplai design "…" --app X` 는 mode=design 계약(`workspace.design_write` 만, protected
  `C-DESIGN`)을 만들고, agent 는 `specs/design/<goal>/design.md` 만 쓴다. `DesignDocumentCheck`(host, 변경을
  실행하지 않음)가 경로 범위, 일곱 section(Goal, Current State, Options, Decision, Risks, Implementation Plan,
  Sources), 해결되는 `path:line` 출처 3개 이상을 확인한다. 결과는 `[AMPLAI design]` draft PR 이다. 구현 node 는
  dispatch 하지 않는다.
- Reason: mode=design 은 설계 artifact 와 review evidence 만 만든다
  (`design-reference/design/03_INVARIANT_REGISTRY.md:59`, `05_GOAL_RESOLVER_CONTRACT.md:48`).

## D-081 — One Goal Across Several Apps (Work 019 D)

- Status: accepted
- Date: 2026-09-28
- Decision: planner 가 모든 app 을 읽기 전용으로 보고(첫 app 은 `/workspace`, 나머지는
  `/amplai-input/apps/<app>`) app 마다 work item(`after` 의존)을 만든다. app 마다 node 하나, `depends_on`/
  `consumes`; 후행 node 의 prompt 에 검증된 선행 patch 가 들어간다. node 는 자기 app suite 로, goal 은 합친
  global check(비어 있지 않은 변경 + 운영자가 설정한 integration 명령을 모든 app 의 base+patch 에서 함께
  실행, network none)로 검증한다. 실패하면 이유와 함께 goal failed. app 마다 draft PR 하나, 서로 링크한다.
  goal 정책은 모든 app ceiling 의 합이다. 한 goal 의 app 들은 같은 worker image 를 써야 한다
  (`MULTI_APP_IMAGE`) — goal 은 활성화된 profile 하나로 모든 node 를 실행한다.
- Reason: V3-057·T-107 은 fixture evidence 뿐이었다 (D-075).

## D-082 — Steering And Replanning On The Real Path (Work 019 D)

- Status: accepted
- Date: 2026-09-28
- Decision: `amplai steer <goal> "…"` 는 설계의 pause → exact-session resume 쌍이다. worker 는 pause 를 받으면
  process 를 스스로 취소하지 않고, loop 가 quiesce(process boundary, checkpoint, workspace snapshot) 후 같은
  native session 을 운영자 메시지로 resume 하고 결과를 평소처럼 검증한다. 계약은 바뀌지 않는다. Codex
  credential 은 pause 때 회수하고 resume 때 다시 넣는다. `amplai replan <goal> "…"` 은 acceptance_change
  steering 이다. 실행 중 attempt 를 멈추고(goal blocked), planner 가 같은 base 에서 이유와 함께 다시 초안을
  만들고, 다음 contract revision(resolution·target·policy·protected constraint 유지)과 이전 graph 를 가리키는
  graph revision 을 만든다. 운영자가 revision 을 승인하면 활성화하고 steering 을 적용한 뒤 실행한다.
  둘 다 실행 중인 goal 에만 쓴다(승인 전에는 cancel 후 다시 제출).
- Reason: 설계의 steering ledger 는 "frozen, admitted revision 또는 confirmed process boundary 만 steering 을
  유효하게 만든다" (`src/amplai_foundry/runtime/execution/steering.py:1-5`). 실제 경로에는 steer/replan 이 없었다.
