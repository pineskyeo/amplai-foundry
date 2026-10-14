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
  dispatch 하지 않는다. 변경 경로는 patch 글자를 해석하지 않고 git 이 실제로 적용한 path 로 판정한다. 출처는 base
  commit 에 추적된 regular file 의 정확한 path 만 인정한다(symlink, 대소문자 변형, design directory 제외).
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
- Superseded (resume 된 turn 에서 steer·replan 을 거절하는 부분만): D-083 이 대체한다. 나머지는 유효하다.
- Date: 2026-09-28
- Decision: `amplai steer <goal> "…"` 는 설계의 pause → exact-session resume 쌍이다. worker 는 pause 를 받으면
  process 를 스스로 취소하지 않고, loop 가 quiesce(process boundary, checkpoint, workspace snapshot) 후 같은
  native session 을 운영자 메시지로 resume 하고 결과를 평소처럼 검증한다. 계약은 바뀌지 않는다. Codex
  credential 은 pause 때 회수하고 resume 때 다시 넣는다. `amplai replan <goal> "…"` 은 acceptance_change
  steering 이다. 실행 중 attempt 를 멈추고(goal blocked), planner 가 같은 base 에서 이유와 함께 다시 초안을
  만들고, 다음 contract revision(resolution·target·policy·protected constraint 유지)과 이전 graph 를 가리키는
  graph revision 을 만든다. 운영자가 revision 을 승인하면 활성화하고 steering 을 적용한 뒤 실행한다.
  둘 다 attempt process 가 실행 중일 때만 받는다(승인 전에는 cancel 후 다시 제출). 요청이 닿기 전에
  process 가 끝나면 요청은 superseded 로 철회되고 plan 에 적용되지 않은 steering 으로 남는다. boundary 가
  확인되지 않거나 resume 이 실패하면 process 를 멈추고(abort) 요청을 철회하며 goal 은 held 다. loop 가
  요청하지 않은 pause 도 process 를 남기지 않는다. steer 로 resume 된 turn 에는 quiesce 경로가 없으므로 그동안
  steer·replan 은 거절한다(`REPLAN_NOT_RUNNING`/`STEER_NOT_RUNNING`). 승인이 활성화 뒤에 실패하면 재승인은 새 grant 없이
  남은 단계만 끝낸다.
- Reason: 설계의 steering ledger 는 "frozen, admitted revision 또는 confirmed process boundary 만 steering 을
  유효하게 만든다" (`src/amplai_foundry/runtime/execution/steering.py:1-5`). 실제 경로에는 steer/replan 이 없었다.

## D-083 — A Steered Turn Accepts Steering And Replanning Again

- Status: accepted (운영자 선택 2026-09-28: 다음 작업 추천안 진행)
- Date: 2026-09-28
- Decision: steer 로 resume 된 turn 도 같은 attempt 의 turn 이다. 첫 turn 처럼 process 가 도는 동안 steer 와
  replan 을 받는다. worker 의 `continue_resumed` 는 pause 를 받으면 `execute` 와 같이 process 를 취소하지 않고
  `pause_requested` 로 넘긴다. loop 는 take-over 를 반복한다: 다시 quiesce(boundary, checkpoint)하고 같은
  native session 을 새 메시지로 resume 하거나(steer), turn 을 멈추고 다음 contract revision 을 만든다(replan).
  steering 은 resume 이 성공한 시점(session 이 메시지를 받은 시점)에 applied 로 기록한다.
- Reason: D-082 의 거절은 resume 된 turn 에 quiesce 경로가 없어서였다(실제 run
  `goal-2b823b3387ce4b79a7f6487804bb2bcf` 에서 replan 이 거절됨, `specs/019-v3-completion/runs/README.md`).
  quiesce 는 run 의 현재 driver handle 에 동작하므로 resume 된 turn 에도 같은 경계 규칙이 적용된다.
- Evidence: `tests/e2e/test_rc07_steer.py` 의 두 번 steer 와 steer 뒤 replan test(수정 전 코드에서 실패).

## D-084 — Budget Settlement Of Driver-Estimated Usage

- Status: accepted (운영자 선택 2026-09-28: 추천안 1)
- Date: 2026-09-28
- Decision: usage status 가 `estimated` 이고 input/output token 이 있으면(Claude CLI 의 `total_cost_usd`, PR #19)
  reservation 은 보고된 token 으로 정산하고 status 를 `estimated` 로 둔다. token 이 예약을 넘으면 기존처럼
  `overrun` 이고 work 는 verified 가 되지 않는다. 추정 비용은 usage 기록에 남기고 reservation 의 비용 칸은
  예약값 그대로 둔다. 추정 비용이 예약을 넘으면 `budget.estimate_over` event 만 남기고 work 를 막지 않는다.
  token 이 없는 추정은 이전처럼 `unknown` 이다. `totals` 에 `estimated_runs` 를 더한다.
- Reason: 설계는 exact cost 가 없을 때 상한 추정과 `unknown` 보존을 요구하고(`design-reference/design/07_RUNTIME_SCHEDULER.md:40`),
  비용은 "provider 확정 usage 또는 bounded estimate/unknown" 이다(`design-reference/design/15_EVAL_OBSERVATORY.md:28`).
  이전 정산(`src/amplai_foundry/runtime/budgets/service.py` settle)은 `measured` 만 반영해 estimated run 의
  token 초과를 감지하지 못했다. 가격표 추정만으로 work 를 막지 않는 것은 PR #16 설계의 원칙(추정을 확정처럼
  다루지 않는다)을 따른다.
- Evidence: `tests/v3/test_rc02_runtime.py::test_estimated_usage_settles_tokens_and_records_the_cost_estimate`.

## D-085 — Codex CLI Run Cost Stays Unknown On A ChatGPT Account

- Status: accepted (운영자 선택 2026-09-28: 추천안 진행)
- Date: 2026-09-28
- Decision: Codex CLI run 의 비용은 추정하지 않고 `unknown` 으로 둔다. 가격표를 추가하지 않는다. usage 계약
  (3.0.0 wire schema)도 바꾸지 않는다. token 이 보고된 Codex run 의 usage 는 지금처럼 `status: measured` 와
  `cost_microunits: null` 이다. budget 은 이를 `unknown` 으로 정산해 reservation 비용을 유지한다
  (`src/amplai_foundry/runtime/budgets/service.py` settle). Observatory 는 비용이 없는 run 을 unknown 비용으로
  센다.
- Reason: 이 환경의 Codex 는 ChatGPT 계정 로그인으로 실행된다
  (`specs/018-v3-real-execution/driver-qualification-amplai-foundry.json:10`, `"auth":
  "chatgpt_account_sandbox_home"`). token 단가 과금이 아니다. Codex 비용 설계(draft PR #24)는 계정 계약 때문에
  요율을 하나로 정할 수 없으면 `unknown` 으로 두고 그럴듯한 요율을 고르지 않는다고 정했다. 그 조건에 해당하므로
  설계의 조건부 추정은 이 환경에서 적용되지 않는다.
- Revisit: API key 과금 Codex profile 을 도입할 때 PR #24 의 조건부 추정(가격 snapshot pin, token 종류별 완전한
  매핑, fail-closed)을 그 profile 에 한해 다시 검토한다.

## D-086 — A Plan Starts From The Remote Base Branch Fetched Now

- Status: accepted (운영자 선택 2026-09-28: 다음 작업 추천안 진행)
- Date: 2026-09-28
- Decision: plan 직전에 app repo 에 설정의 remote(`origin`)가 있으면
  `git fetch <remote> +refs/heads/<base_branch>:refs/remotes/<remote>/<base_branch>` 를 하고, 그 remote-tracking
  ref 의 commit 을 base 로 쓴다. fetch 는 remote-tracking ref 만 바꾼다. 운영자의 local branch, checkout,
  작업 파일은 움직이지 않는다. fetch 가 실패하면 옛 base 로 계획하지 않고 `BASE_FETCH` 로 멈춘다. remote 가
  없는 repo 는 이전처럼 local `base_branch` 를 쓴다. replan 은 이전과 같이 원래 plan 의 base 를 유지한다.
  launchd 로 도는 server 설치본은 `amplai ops local-update` 로 갱신한다. 이 명령은 local 변경이 있으면 거부하고,
  fast-forward 만 하며, `pyproject.toml` 이 바뀐 경우에만 다시 설치하고, launchd agent 를 재시작한다.
- Reason: app clone(`~/.amplai/repos/amplai-foundry`)의 local `main` 을 아무도 갱신하지 않았다. 그래서 2026-09-28
  의 goal 여러 개가 옛 base `575f51d` 로 계획됐다. 그 결과 draft PR #22 가 main 과 충돌했고, planner 는 이미
  merge 된 D-084 를 찾지 못해 질문으로 멈췄다(`goal-03b21dff4b714c66b4ac8c2da5fba80d`).
- Evidence: `tests/e2e/test_rc07_fresh_base.py`(수정 전 코드에서 2개 실패),
  `tests/v3/test_rc07_local_config.py::test_local_update_fast_forwards_a_clean_checkout_and_refuses_a_dirty_one`.

## D-088 — An Experiment May Declare That Cost Is Not Compared

- Status: accepted (운영자 선택 2026-09-29: 성공률만 비교)
- Date: 2026-09-29
- Decision: analysis plan 은 선택 항목 `cost_basis` 를 가진다(`compared` 기본, `not_compared`).
  `not_compared` 이면 cost unknown 과 `measured` 가 아닌 usage 를 inconclusive 사유로 세지 않는다. 대신
  cost benefit 분석(`benefit`)을 선언할 수 없다. 누락 결과, safety, drift, 오염 규칙은 그대로다. evolution
  budget 은 그런 실험의 trial 비용이 없어도 allocation 을 `settled` 로 두고, 비용은 예약값을 그대로
  유지한다(0 으로 세지 않는다). token 은 여전히 있어야 한다. 기본값(`compared`)의 동작은 바뀌지 않는다.
- Reason: 이 환경의 driver 는 구독 계정이라 비용을 알 수 없다(Codex, D-085). Claude usage 는 `estimated`
  다(D-084). 그대로면 모든 실험이 `unknown_cost`/`nonmeasured_usage` 로 inconclusive 가 되고, 첫 trial 뒤
  `budget_overrun_or_unknown_usage` 로 멈춘다(`src/amplai_foundry/evaluation/analysis.py`,
  `src/amplai_foundry/meta_harness/budget.py` settle). 평가기 변경이라 후보 실험과 따로 먼저 넣는다
  (`design-reference/design/16_META_HARNESS.md:27`).
- Evidence: `tests/v3/test_dev03_evaluation.py` 의 cost basis 3개 test(수정 전 코드에서 실패).

## D-089 — The Meta-Harness Runs On Real Runs (Work 030)

- Status: accepted (운영자 선택 2026-09-29)
- Date: 2026-09-29
- Decision: 메타하네스를 실제 local 제품의 run 위에서 돌린다(`specs/030-meta-harness-live/`).
  - 첫 개선 surface 는 class A 인 IMPLEMENTER prompt 다.
  - 과제 묶음은 demo-app 의 결정적 verifier 과제 20개다.
  - 예산은 offline 40 run(20 × 2 arm × 1 회)과 canary 3 goal 이다.
  - 분석은 `confirmatory`, 비열등성 margin 0.25, confidence 0.95, `cost_basis: not_compared`(D-088)다.
  - 역할: V3 의 meta-proposer service identity 가 제안한다. 운영자가 screen, 실험 승인, canary 승인, 승격을
    한다. 제안자와 검토자는 다르다.
- Reason: V3 완료 기준은 실제 V3 실행 pipeline 을 통과한 개선 후보 하나의 실험, canary, 승격, rollback 연습
  증거를 요구한다(`design-reference/design/16_META_HARNESS.md:76-78`). V3 FINAL 의 연습은 LLM 이 아닌 결정적
  recipe 를 비교했다(`src/amplai_foundry/meta_harness/pipeline_reference.py:1-6`).

## D-090 — The Document Review Store Limit Is 16 MiB

- Status: accepted (운영자 선택 2026-09-29: 전부 진행)
- Date: 2026-09-29
- Decision: `.ai-team/policy/documentation.json` 의 `max_file_bytes` 를 8 MiB(8388608)에서 16 MiB(16777216)로
  올린다. 정책 검증기가 허용하는 상한은 64 MiB 다(`scripts/amplai_docs.py:1161`). 정본 검토 기록(문서 검토
  저장소)은 append-only 이력이라(`history` 1782건, `reference_history`
  1350건) 검토 cycle 마다 커진다. 8 MiB 에 도달하면 `review-batch` 가 `SCOPE_INCOMPLETE` 로 새 검토를 기록하지
  못한다(`atomic_json`, 같은 파일 2203-2204). 기록을 잘라 내거나 요약하지 않는다.
- Reason: 이력을 줄이는 것은 검토 근거를 지우는 일이다. 한도를 올리는 것이 되돌릴 수 있는 가장 작은 조치다.
  cycle 마다 검토 이력이 약 1 MiB 씩 늘어난다(이번 Work 019~030 cycle 로 8 MiB 에 도달). 16 MiB 는 대략 8번 더
  쓸 수 있는 여유다. 그 전에 이력을 나누는 방식(연도별 파일 등)을 별도 Work 로 정해야 한다.
- Open: 이력 분할 방식은 정하지 않았다.

## D-091 — Driver Provider Credentials Are Readable By The Agent's Tools; The Risk Is Accepted And Recorded

- Status: accepted (운영자 선택 2026-09-30: 추천대로)
- Date: 2026-09-30
- Decision: driver 가 provider 를 부르려고 컨테이너 안에 둔 credential 은 같은 컨테이너·같은 uid 에서 도는 agent 의
  tool 이 읽을 수 있다고 본다. 이 위험을 accepted risk 로 기록하고, 격리된 것처럼 표시하지 않는다.
  - Claude: `CLAUDE_CODE_OAUTH_TOKEN` 이 컨테이너 process env 에 있다. container uid(65534)로 모델 없이 측정하면
    env 에 있고 `/proc/1/environ`, `/proc/7/environ` 에서도 읽힌다
    (`specs/032-credential-broker-probe/baseline-exposure.json`).
  - Codex: run home 의 `auth.json` 이 0644 로 놓인다(`src/amplai_foundry/runtime/execution/codex.py:69`). agent 는
    `--dangerously-bypass-approvals-and-sandbox` 로 실행된다(`src/amplai_foundry/agent_drivers/cli.py:142-147`).
    (추정: 같은 uid 의 tool 이 읽을 수 있는 구조다. 실측하지 않았다.)
  - OpenCode: 같은 기준을 적용한다. 서버 password/auth 는 켠다(`design-reference/design/11_DRIVERS_SESSIONS.md:36`).
    driver 마다 다른 기준을 쓰지 않는다. tool uid 분리(M-c)는 file tool 이 서버 process 안에서 돈다는 이유로
    credential 격리를 보장하지 못한다(이전 조사 기준, 실측하지 않았다).
- Not measured: agent 의 tool 이 실제로 그 값을 읽는지는 측정하지 못했다. 컨테이너 안의 Claude 모델이 marker 만
  출력하는 probe 실행을 거부했고, 우회하지 않았다. 따라서 "agent tool 이 읽는다"는 구조에서 나온 추정이고 측정한
  fact 는 위 container uid 측정까지다.
- Reason: 이 위험을 줄이는 정석은 credential broker 다. `design-reference/design/10_AUTHORITY_SECURITY.md:44` 는
  production credential 을 agent env 에 장기 노출하지 않고 broker 가 짧은 scoped token 이나 server-side 동작을 하게
  한다. 다만 이 문장은 production credential 규칙이고 driver 의 provider credential 을 직접 다룬 문장은 design 에서
  찾지 못했다. broker 가 Codex(ChatGPT 계정)와 Claude(OAuth token)에서 성립하는지는 확인 전이다. 그 전까지
  isolation 을 없애고 있다고 쓰거나, 있다고 쓰지 않는다(`design-reference/design/29_THREAT_MODEL.md:31`).
  영향을 줄이는 장치는 이미 있다: 운영자가 만든 scoped 사본, 계정 단위 revoke, egress allowlist, stream·workspace·
  sidecar log·argv 의 leak scan(`scripts/container_qualify.py:427`).
- Follow-up: Work 032 (`specs/032-credential-broker-probe/`)가 broker 성립 여부를 probe 로 정한다. 성립하면 새
  Decision 으로 이 항목을 supersede 한다.
- Open: broker 가 성립하지 않으면 accepted risk 를 유지할지, 해당 driver 를 격리된 VM 로 옮길지는 정하지 않았다.

## D-092 — A Canary May Declare That Cost Is Not Compared

- Status: accepted (운영자 선택 2026-09-30: 추천대로)
- Date: 2026-09-30
- Decision: canary 정책은 선택 항목 `cost_basis` 를 가진다(`compared` 기본, `not_compared`). `not_compared` 이면
  canary 결과의 cost 가 unknown 이어도 되고 usage 는 `measured` 또는 `estimated` 여도 된다. cost 는 예약값을 그대로
  유지하고(0 으로 세지 않는다) 누적 cost 상한 검사는 하지 않는다. token, safety, unknown effect 카운터와 receipt
  결합, success 는 그대로 필수다. 결과가 주장한 `usage_status` 는 receipt 가 그대로 결합해야 한다. 기본값
  (`compared`)의 동작은 바뀌지 않는다: 모든 카운터가 알려진 정수이고 usage 는 `measured` 다.
- Reason: 이 환경의 driver 는 구독 계정이라 Codex 비용을 알 수 없고(D-085) Claude 는 `estimated` 다(D-084).
  canary 코드는 비용이 정수로 알려져 있고 `usage_status` 가 `measured` 인 결과만 받는다
  (`src/amplai_foundry/meta_harness/service.py` canary_trial, 수정 전). 비용을 0 으로 적으면 사실이 아닌 값을 알려진
  값으로 기록하는 것이다. 분석 쪽은 D-088 이 이미 같은 방식으로 풀었다.
- Evidence: `tests/v3/test_rc11_canary_cost_basis.py` 10개(기본 동작 유지, unknown 비용 수용, token·usage class·
  receipt 결합·success 는 그대로 필수, 완주 후 승격 가능). 기존 dev03 canary 시험 55개는 그대로 통과한다.

## D-093 — The First Real Evolution Claims No Improvement, Only Safe Change

- Status: accepted (운영자 선택 2026-09-30: 추천대로)
- Date: 2026-09-30
- Decision: Work 030 S7 의 실제 진화는 성공률의 **비열등성**으로 판정하고 "성능이 좋아졌다"고 주장하지 않는다.
  후보는 token 절감처럼 실제 이득이 기대되는 class A prompt 변경으로 고른다. token 과 시간은 탐색용 수치로 함께
  보고하고 판정 기준으로 쓰지 않는다. 일부러 나쁜 prompt 를 negative control 로 넣어 fail 이나 inconclusive 로 끝나는지
  확인한다. 분석 계획은 D-089 의 OD-4·OD-5(성공률만, `confirmatory`, margin 0.25, confidence 0.95)를 그대로 쓴다.
- Reason: 실제 Codex 로 corpus 20개를 baseline 으로 한 번씩 돌렸을 때 20개 모두 통과했다
  (`specs/030-meta-harness-live/runs/calibration-codex-1.json`, 입력 약 203만 token, 17분). 성공률이 천장이면 후보가
  성공률을 높였다는 결과는 나올 수 없다. 나올 수 있는 것은 나빠지지 않았다는 결과와 나쁜 변경이 걸러진다는 결과다.
  과제당 1회라 표본 1개이고 과제별 통과율은 아니다.
- Open: 성공률 개선을 실제로 보이려면 baseline 통과율이 60~80% 인 어려운 과제가 필요하다. 그 corpus 는 만들지
  않았다(스펙을 완전하게 유지하면서 어렵게 만들기 어렵다).

## D-094 — Usage Detail, Dated Price Tables And API-Equivalent Cost; Descriptive Trial Metrics

- Status: accepted (운영자 선택 2026-09-30: 진행)
- Date: 2026-09-30
- Decision:
  - driver 가 받은 사용량 세부(Codex: input, cached input, cache write, output, reasoning output / Claude: input,
    cache read, cache write 5m·1h, output)를 버리지 않는다. 3.0.0 `usage` 는 닫힌 형식이라(`common.schema.json`
    `usage`, `additionalProperties: false`) 세부는 `usage-detail` 기록으로 저장하고 `usage.source_ref` 가 가리킨다.
    schema 는 바꾸지 않는다. Claude CLI 비용 증거는 그 기록의 `cost_source_ref` 로 옮긴다.
  - 단가표는 `deployment/prices/<id>.json` 에 둔다. 모델별 입력·캐시 읽기·캐시 쓰기·출력 단가(백만 token 당
    microunit), 출처 URL, 받은 날짜, 적용 시작일을 가진다. 한 번 넣은 표는 고치지 않고, 단가가 바뀌면 적용
    시작일이 뒤인 새 표를 추가한다. run 은 그 날 적용되던 표로 계산하고 결과에 표 id 를 남긴다. 첫 표는
    2026-09-30 공식 페이지 값이다(gpt-5.6-sol 4/0.4/20, 캐시 쓰기 5, 프로모션 가격으로 최소 2026-11-21 까지;
    claude-sonnet-5 2/0.2/10, 캐시 쓰기 2.5·4).
  - token 은 공급자별로 서로 겹치지 않는 묶음으로 나눈 뒤 계산한다. OpenAI 는 `input_tokens` 가 캐시를 포함하므로
    `input − cached − cache_write` 가 캐시 아닌 입력이다. Anthropic 은 포함하지 않으므로 캐시 묶음을 더한다. 추론
    token 은 출력에 포함돼 있어 더하지 않는다.
  - API 환산 비용은 run 의 `usage.cost_microunits` 와 분리한다. 구독 run 의 청구액이 아니며(D-085 유지) 판정 기준이
    아니다(D-093 유지). 단가가 없는 모델은 `no_price` 로 보고하고 다른 단가로 대체하지 않는다. 캐시 세부가 없는
    옛 run 은 모든 입력을 캐시 아닌 단가로 계산하고 `upper_bound` 로 표시한다.
  - 실험 시도의 참고 지표(`amplai meta report`): 검증은 통과했지만 숨긴 시험은 실패한 수, 새 시험 추가와 기존 시험
    수정·삭제의 구분, 성공 1건당 token·시간·API 환산 비용, 변경 크기. corpus 검사는 `--repeats N` 으로 같은 판정이
    반복되는지 본다(채점 흔들림). 추가 agent 실행 없이 이미 저장된 기록에서 계산한다.
- Not done: pass^k(과제 반복 실행)와 판정 기준 변경은 하지 않았다. model-profile 의 `price_snapshot_ref` 는 쓰지
  않았다. 채우면 composition digest 가 바뀌어 대기 중 plan 이 `COMPOSITION_CHANGED` 가 되고 baseline release 가
  다시 잡힌다.
- Reason: 사용자는 구독이라도 API 가격으로 비용을 환산하고 계속 관리되기를 원했다. 조사한 도구(LiteLLM 가격표,
  ccusage, Langfuse)는 모두 token × 공개 단가로 계산하고 날짜별 단가를 보관한다. 캐시를 두 번 세는 실수가 실제로
  있었다(openusage issue #360). 캐시를 버리면 비용이 크게 부풀려진다: 실제 Codex 1회(2026-09-30)는 입력 72,047 중
  캐시 64,128(89%)이었고 환산 $0.080 인데, 캐시를 모르면 약 $0.31 로 계산된다
  (`specs/030-meta-harness-live/runs/README.md`).
- Evidence: `tests/v3/test_rc13_usage_pricing.py`, `tests/e2e/test_rc13_usage_detail.py`,
  `tests/v3/test_rc12_meta_cli.py`(prices, report). corpus 20개 `--repeats 3` 모두 같은 판정.

## D-095 — OpenCode Is The Third Driver: A Server Per Dispatch, Password On, Env Guard

- Status: accepted (운영자 선택 2026-09-30: 3번 끝내기)
- Date: 2026-09-30
- Decision: OpenCode(`opencode serve` 1.17.13)를 system-selected driver 의 세 번째 후보로 연결한다(router 순서
  codex, claude, opencode; OpenCode 는 계획하지 않는다).
  - dispatch 하나마다 app image 안에 서버 container 하나를 띄우고(`agent_drivers/opencode_launcher.py`), 그
    run 의 workspace 와 dispatch 전용 HOME 에만 묶는다. host 는 `docker exec curl` 로 container loopback 에 붙고,
    SSE 는 `curl -N` 으로 흘린다.
  - 서버 비밀번호는 켠다(design 11:36). run 마다 새로 만들고 `docker run` 에는 변수 이름으로만, curl 에는 stdin
    config 로만 넘긴다. 읽기 전용 config dir 의 `shell.env` plugin 과 `SHELL` wrapper 가 agent 의 shell 환경에서
    `OPENCODE_SERVER_*` 이름을 지운다(031 M1 측정). `/proc/<server pid>/environ` 으로는 같은 uid 가 읽을 수 있다.
    이 위험은 D-091 과 같은 기준으로 받아들인다.
  - 자격은 app image 별 report(`reports["opencode-server"]`, top-level `container_image`, `tool_use`)로만 받는다.
    report 가 없거나 통과하지 않으면 `DRIVER_UNQUALIFIED` 로 멈춘다.
  - 운영자 명령 `amplai ops local-opencode` 를 둔다.
- Superseded path: PR #28(D-087, 비밀번호 없는 서버)은 닫혔고 merge 하지 않았다. tool uid 분리(031 M-c)는 file tool 이
  서버 process 안에서 도는 한 credential 격리를 보장하지 못해(031 design) 채택하지 않았다.
- Evidence: `specs/031-opencode-driver/runs/README.md`: 새 app image 에서 OpenCode 9 check + `tool_use` pass, Codex
  재자격 pass, corpus 과제 1개를 OpenCode 로 실제 실행해 verified(숨긴 시험 통과, 46 s).
  `tests/v3/test_031_opencode_{port,events,launcher,wiring}.py`.
- Open: OpenCode driver 는 token 사용량을 넘기지 않는다(usage `unknown`, design R5). 서버 재시작 뒤 세션 복원은
  qualification 의 raw HTTP resume 으로만 확인했고 `OpenCodeDriver.resume` 경로로는 측정하지 않았다(R3).
  운영 서버의 image 전환은 Codex·Claude 재자격이 함께 필요해 하지 않았다.

## D-096 — Harness Components And Layers

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: harness 를 레이어(L1 과제 해석, L2 작업 구조, L3 역할별 칸, L4 문맥, L5 agent 옵션, L6 루프 제어,
  L7 중간 확인, L8 상한 값, L9 실행 환경)의 부품으로 나눈다. 부품은 id·kind·version·surface class·content digest 를
  가진 store 기록이고, composition 의 기존 ref(`prompt_bundle_ref`, `context_policy_ref`, `budget_policy_ref`, router
  policy)가 가리키는 기록에 manifest 로 담긴다. 3.0.0 schema 는 바꾸지 않는다. 후보의 등급은 바뀐 부품 중 가장 높은
  등급이다(design 16 §3 기준: prompt 발췌·승인된 메모는 A, 문맥 검색·수리 방식·라우팅·driver 배정은 B). 기본
  manifest 는 오늘의 실행 prompt 를 byte 단위로 재현한다. L9 는 부품만 정의하고 이번 라운드 실험에서 제외한다.
- Reason: 지금은 IMPLEMENTER prompt 하나만 바꿀 수 있다(`runtime/execution/meta_ops.py:61-139`). 연구상 효과가 큰
  부품(기억 +5.6pp, 도구 +3.3pp, loop 제어 +2.2pp)은 고정이었다(`specs/033-harness-taxonomy/research-meta-harness.md`).
  field 단위 등급(`meta_harness/composition.py:12-23`)은 design 16 §3 과 어긋났다.

## D-097 — Cells: Model And Reasoning Effort Per Role

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 칸(cell)은 model-profile 하나다: driver, provider model, `reasoning_profile`(추론 강도). 추론 강도는
  driver 에 전달된다(Codex `-c model_reasoning_effort=<e>`, Claude Code `--effort <e>`). 모델이 지원하지 않는 강도는
  거절하고 다른 값으로 바꾸지 않는다. qualification 은 image·driver version·model 에 묶이고, 같은 모델의 강도 변형은
  그 report 를 공유한다. 역할(계획자·실행자·검토자·제안자)마다 칸을 따로 배정할 수 있다(L3). 첫 라운드 칸은 Codex
  gpt-5.6-sol {medium, high}, Claude {claude-sonnet-5, claude-opus-5-5} high 이고 각각 먼저 자격을 받는다.
- Reason: `reasoning_profile: "provider-default"` 는 읽는 곳이 없었다(`runtime/execution/codex.py:316`). Claude Code
  2.1.278 은 `--effort` 를, Codex 0.155.1 은 `-c` 로 `model_reasoning_effort` 를 받는다(worker image 확인,
  https://code.claude.com/docs/en/cli-reference, https://learn.chatgpt.com/docs/config-file/config-reference).

## D-098 — Corpus v2: Public Benchmark Subsets Plus Own Tasks

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 과제 창고는 약 120개다. 공개 벤치 일부(Terminal-Bench 2.0 부터, 라이선스를 먼저 확인)를 adapter 로
  들여와 넓이와 외부 비교에 쓰고, 자체 과제는 holdout 과 모호한 지시(A1) 영역에 쓴다. Work 030 과제 20개는 퇴행
  확인 묶음이다. 개발·검증·holdout split 은 `CorpusService` 로 고정한다. 난이도는 선언하지 않고 칸별 calibration 으로
  잰다. 모든 칸이 늘 통과하는 과제는 퇴행 묶음으로 옮긴다. 누출 차단: 부품 내용에 검증·holdout 과제 id, 숨긴 시험
  이름, 정답에만 있는 식별자가 들어가면 거절한다.
- Reason: Codex baseline 이 기존 20개를 모두 통과했다(D-093). 과제 20개의 표준오차는 약 11pp 다.

## D-099 — Evaluator: Superiority, Decision Classes, Accumulating Evidence

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 분석에 성공률 우월성 endpoint 와 판정 등급(개선·효율·동등·퇴행·맞바꿈·판정 불가)을 더한다. 같은 예산의
  best-of-n baseline 과 비교한다. 과제별 통과율과 pass^k 를 보고한다. 여러 밤에 걸쳐 결과를 쌓을 때는 오류율이
  부풀지 않는 검정(e-process 또는 always-valid)을 쓰고, 잡음 범위보다 작은 차이는 미결정으로 둔다. 평가기 변경이므로
  후보 실험과 섞지 않고 먼저 따로 검증한다: 저장된 실험은 기존 판정을 그대로 받는다.
- Reason: 지금 판정은 비열등성뿐이다(`evaluation/analysis.py:203-215`). 같은 예산이면 단순 병렬 시도가 harness
  진화보다 나았다는 결과가 있다(https://arxiv.org/html/2607.12227).

## D-100 — Proposer Reads Sanitized Traces And States Predictions

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: meta-harness 연습 과제 실험의 실행 기록만 저장한다: 보이는 메시지·도구 호출·잘린 도구 출력, 추론 항목은
  제외, 비밀값 검사 후 저장, 제안자 역할만 읽고 내보내지 않는다. 사용자 목표의 기록은 저장하지 않는다. 제안자는
  개발 split 의 기록과 점수로 부품 변경안을 만들고, 좋아질 과제와 나빠질 과제를 예측한다. 평가 뒤 예측 정확도를
  기록한다. 누출 차단이 출력을 검사한다.
- Reason: 기록을 주면 median 50.0%, 점수만 주면 34.6% 였다(https://arxiv.org/html/2603.28052). 지금 driver 는
  digest·id·usage 만 남긴다(`agent_drivers/codex_app_server.py:299`).

## D-101 — Remove V1, V2 And Unused V3

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: V1·V2 를 제거하고 V3 에서 쓰지 않는 것도 정리한다. V3 방식 개발에 꼭 필요한 것만 옮기고, 옛 기능을
  유지하려는 이전은 하지 않는다. 순서: 근거를 붙인 목록 → 운영자 승인 → 필수만 이전 → 삭제 → CI 통과. 이 Work 의
  마지막 단계다.
- Reason: 운영자는 앞으로 V3 방식으로만 개발한다(2026-09-30).

## D-102 — Execution Strategies And Per-Layer Deciders

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 실행 방식 10가지(싱글, 수리 루프, 워크그래프, 계획→실행, Best-of-N, 생성자+검토자, 단계적 상향,
  오케스트레이터, 병렬 읽기, 투표)를 모두 구현한다. 레이어마다 판단자가 정보가 생기는 시점(접수, 계획 후, 실행 직전,
  실패 시, 최종 검증 전)에 판단한다. 판단 방식도 부품이다(입력 정보, 추정 방식, 선택 규칙, 대체 규칙). 기본 규칙:
  최고보다 확실히 나쁘지 않은 선택지 중 성공 1건당 비용이 가장 낮은 것, 표본이 부족하면 오늘의 동작. 묶음은 근거가
  있는 만큼만 잘게 나눈다(partial pooling). 실제 사용자 목표에서는 탐색하지 않고, dispatch 뒤 조합은 바꾸지 않는다.
  쓰기 동시 실행은 저장소당 1개, 읽기 전용 단계는 최대 4개다(design 07 §4).
- Reason: 운영자 결정(2026-09-30). 연구상 부품 효과는 서로 더해지지 않고 모델에 따라 반대로 나온다
  (https://arxiv.org/html/2604.25850, https://arxiv.org/abs/2609.20804).

## D-103 — The Judge Is A Selectable Component

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 판정 모델은 부품이다(없음, Jev, LLM 칸). (판정 모델, 질문 종류) 짝마다 알려진 결과로 정확도·보정·반복
  안정성·비용·시간을 재서 자격을 준다. 자격이 있는 것 중 가장 정확한 것보다 확실히 덜 정확하지 않은 것 가운데 가장
  싸고 빠른 것을 쓴다. 외부로 목표 문장을 보내는 판정 모델은 데이터 등급으로 제한한다(기본: 연습 과제). 연결부는
  하나다. Jev 연결부는 지금 만들고 실제 연결은 시스템 완성 뒤 접근이 확인되면 한다.
- Reason: 운영자 결정(2026-09-30). Jev 는 형식이 정해진 답과 확률을 준다(https://www.langchain.com/blog/jev-agent-evals-langsmith).

## D-104 — Experiment Operations: The Nightly Loop

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 하룻밤 예산 안에서 변화 감지(약 10%), 중요 레이어 찾기(초기), 탐색(약 60%: 예측 모델과 제안자가 고른
  후보를 과제 일부로 거름), 확인(약 30%)을 돌린다. 이미 잰 (설정, 과제, 모델 snapshot, corpus 버전)은 다시 돌리지
  않는다. 실험 시험은 과제마다 container 가 분리되므로 병렬로 돌린다. 예산은 3밤 동안 약 150회 시범으로 구독 사용량을
  잰 뒤, 측정된 여유의 비율로 정한다(기본: 구독 한도의 절반 이상을 운영자 몫으로 남긴다).
- Reason: 조합 공간은 수백 가지 이상이다. 중요한 설정은 일부뿐이었다(HARBOR, https://arxiv.org/html/2604.20938).

## D-105 — The Evaluation Is Measured And Versioned

- Status: accepted (운영자 결정 2026-09-30, Work 033 설계)
- Date: 2026-09-30
- Decision: 평가 자체를 잰다: 구별력(좋은·나쁜 대조군이 갈리는가), 포화, 채점 흔들림, 오염, 개발·holdout 차이,
  실제 결과(canary 목표, merge 된 PR, D-078)와의 일치, 영역 비중, 결론당 비용. 평가 변경(과제 퇴출·추가·가중치,
  반복, 단계)은 별도 버전 트랙이다: 따로 검증하고 운영자가 승인하며 후보 실험 안에서 바꾸지 않는다.
- Reason: 평가를 후보와 함께 바꾸면 효과를 구분할 수 없다(`design-reference/design/16_META_HARNESS.md:27`).

## D-106 — Hermes Is An Intake Adapter And Orchestrator

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: Hermes 는 IntakeAdapter + 오케스트레이터다. `intake` actor 는 human 이 아니다. 토큰·연결표는 store 밖에 두고, 요청 종류를 제한하며, 알림은 projection 으로 보낸다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-1, `specs/034-hermes-modular-platform/plan.md` §6, PR #69

## D-107 — Deployment Flows One Way, Upstream To Site

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 배포는 upstream → 사내 한 방향이다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-2

## D-108 — Two-Layer Module System With Pinned Artifacts

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 두 층 모듈 체계, 고정 배포물만 허용, 신뢰 루트를 둔다. 외부 입력을 받는 kind 는 프로세스 밖에서 돈다.
- Source: `specs/034-hermes-modular-platform/plan.md` §6

## D-109 — Knowledge And Documentation Freshness Return As V3 Modules

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 지식·문서 최신성을 V3 모듈로 복원한다. S17 `f06a8e5` 의 D1·X9 삭제를 정정한다(design/12 §1).
- Source: `specs/034-hermes-modular-platform/plan.md` §6

## D-110 — Three-Layer Upgrade

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 업그레이드는 동결, 명시적 이전, 승인 후 백업, 되돌리기 순서, 기준 digest·stale 표시를 갖는다.
- Source: `specs/034-hermes-modular-platform/plan.md` §6

## D-111 — Site LLM Usage Goes Through A Model Gateway

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 사내 LLM 사용량은 모델 게이트웨이로 측정한다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-4

## D-112 — Hermes May Pause And Cancel; It Resumes Only What It Paused

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: Hermes 는 연결된 운영자의 goal 을 일시정지·취소할 수 있다. 재개는 Hermes 가 멈춘 goal 만 할 수 있다. `goal.cancel` 을 신설한다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-5

## D-113 — Approval Page Uses Passkeys Only

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 승인 페이지(S1b) 인증은 passkey 만 쓰고, POST + CSRF 로 받는다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-6

## D-114 — The Q-Suite Ships As A Release Asset

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 사내 평가기 재승인용 Q-suite 를 배포 릴리스 자산으로 싣는다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-7

## D-115 — Knowledge-Space And Git-Server Approvals Count As Human Approval

- Status: accepted (운영자 확정 2026-10-12)
- Date: 2026-10-12
- Decision: 지식 공간과 git 서버의 승인을 사람 승인 증거로 쓴다. 조건: 보호 브랜치, 공간별 CODEOWNERS, 승인 뒤 새 커밋이 올라오면 승인 무효(dismiss stale approvals). 관리자 우회 병합은 사람 승인으로 인정하지 않는다. AMPLAI 는 git 서버 API 로 승인자와 승인 commit 을 확인한다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-10, PR #86

## D-116 — Front Agent And Messenger Are Three Module Layers

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 앞단 에이전트·메신저는 세 층 모듈(접수 어댑터, 앞단 에이전트 키트, notifier)이다. wire `source_channel=hermes` 는 앞단 에이전트 채널 이름으로 두고, adapter 는 `authn_context_ref` 로 구분한다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-13

## D-117 — Nightly Loop Control Through The Front Agent

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-11
- Decision: 시계 tick 은 서비스 관리자(D-118)가, 진행은 NightlyRunner 가 맡는다. 앞단 에이전트는 조이기와 조건부 시작(N-9, `nightly.control`)만 한다. 제어 기록은 제품 store 에 남긴다. 밤에는 읽기 전용으로 확인하고, 못 읽으면 멈춘다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-12 (Q-8 = B)

## D-118 — One Service Manager Interface

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-12
- Decision: 서비스 관리자 인터페이스 하나에 launchd·systemd·supervisor 구현을 둔다. AMPLAI 는 unit 을 출력만 한다. 동결은 서비스 정지 + 동결 표지 + owner lock 이중 잠금이다. Ubuntu 서버는 system unit 이 1순위다(Q-11).
- Source: `specs/034-hermes-modular-platform/spec.md` C-8, `specs/034-hermes-modular-platform/plan.md` Q-11

## D-119 — Linux Sandbox Keeps A Fixed Foreign Uid With ACL Access

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-12
- Decision: Linux 샌드박스는 호스트와 다른 고정 uid(기본 65534)를 유지하고, 접근은 POSIX ACL 로만 준다. 호스트 uid 실행은 기각한다. chown·userns-remap 은 제외한다. rootless podman `keep-id` 는 별도 자격 시험 후보다.
- Source: `specs/034-hermes-modular-platform/spec.md` C-9, `specs/034-hermes-modular-platform/plan.md` §3.5a

## D-120 — One Configured Container Engine

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-12
- Decision: 컨테이너 엔진은 설정 한 곳에서 정하고, 직접 `docker` 호출을 없앤다.
- Source: `specs/034-hermes-modular-platform/spec.md` C-10

## D-121 — Closed-Network Install Uses A Signed Bundle

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-12
- Decision: 닫힌 망 배포는 서명된 묶음(hash 고정 wheelhouse, 이미지 tar + config digest, 사이트 로컬 registry)으로 한다. 검증 전에는 아무것도 바꾸지 않는다. 사내 공용 registry 는 없으므로(Q-14) 이미지는 tar 로 반입해 config digest 로 고정한다.
- Source: `specs/034-hermes-modular-platform/spec.md` C-11, `specs/034-hermes-modular-platform/plan.md` Q-14

## D-122 — Supported Operating Systems

- Status: proposed (RHEL 7·8 운영자 결정 대기)
- Date: 2026-10-12
- Decision: WSL2 Ubuntu 24.04 지원, Ubuntu 18.04 조건부 지원, RHEL 8 은 점검 후 결정, RHEL 7 은 실행 호스트 미지원을 권장한다.
- Source: `specs/034-hermes-modular-platform/spec.md` C-13, `specs/034-hermes-modular-platform/plan.md` Q-12

## D-123 — Upgrade Rehearsal Is The S3 Exit Criterion

- Status: accepted (운영자 승인 2026-10-12, PR #69 설계 승인)
- Date: 2026-10-12
- Decision: 이전 릴리스가 만든 사내 모양 데이터를 새 릴리스로 올리는 업그레이드 리허설을 S3 완료 기준으로 한다. 사내 업그레이드 승인 전에는 백업 사본으로 리허설해야 한다.
- Source: `specs/034-hermes-modular-platform/spec.md` U-9

## D-124 — Planning Text Is Never Cut; A Long Node Objective Is A Digest Reference

- Status: accepted (운영자 결정 2026-10-12, option A)
- Date: 2026-10-12
- Context: pilot calibration 10(corpus 2.1.0)에서 `_clean_draft` 가 planner text 를 말없이 4000자로
  잘랐다(`value.strip()[:4000]`, main 의 `runtime/execution/product.py:303-307`). implementer prompt 는
  잘린 objective 만 받았다(`loop.py:809,819`). 4000자를 넘는 task 는 16개이고 그중 15개가 4096자를 넘는다(최장
  7750). feature task 6개(customer-returns, purchase-receiving, warehouse-transfers, stock-allocation,
  recurring-orders, price-lists)는 끝부분의 reader/CLI 규칙이 잘려 모든 cell 에서 실패했다. fairness check 는
  planner/contract 경로를 거치지 않아 이 문제를 잡지 못했다.
- Decision: contract, workgraph, prompt 로 가는 planner·strategy·operator text 는 말없이 자르지 않는다. frozen
  3.0.0 schema 한도(contract objective 8192, node objective·항목 text 4096, steering text·replan_reason 4096)까지
  받는다. 넘으면 coded Hold 를 낸다(`PLANNING_OBJECTIVE_LENGTH`, `PLANNING_TEXT_LENGTH`, `STEER_TEXT_LENGTH`,
  `REPLAN_REASON_LENGTH`; details 는 field, length, limit). corpus v2 loader 는 8192자를 넘는 objective 를
  `TASK_OBJECTIVE_LENGTH` 로 거부한다. 4096자를 넘는 node objective 는 참조 문장으로 남기고, 원문 그대로의
  `sha256:` digest 를 함께 적는다. whole-goal node 의 참조는 graph `contract_ref` 가 가리키는 immutable contract
  objective 를 원본으로 삼는다. integration node(goal + notes) 의 참조는 plan work item 을 원본으로 삼는다. agent 와
  PR body 는 항상 원문을 읽는다(`text_bounds.full_objective`). 원문은 digest 로 검증하고, 일치하는 원본이 없으면
  `OBJECTIVE_REFERENCE_MISMATCH` Hold 를 낸다.
- Rejected: 4096자에서 Hold 한다. 그러면 단일 app goal 의 objective 가 contract 한도(8192)가 아니라 node 한도로
  막혀, 영향받은 6개 task 를 포함해 4096자를 넘는 15개 task 가 돌지 못한다. 긴 objective 를 자동으로 split
  한다. 사용자 요청의 의미를 시스템이 임의로 나누게 되고, 나눈 결과를 검증할 근거가 없다.
- Consequence: objective 가 4096자를 넘으면 workgraph 만으로는 완결되지 않는다. node 의 원문은 contract 또는 plan
  work item 에 있다. 대신 digest 가 node definition 에 들어가므로 objective 가 다르면 node definition digest 도
  달라진다(`design-reference/design/06_WORKGRAPH_REPLAN.md:40` 의 reuse 조건 node definition digest). 원문은 언제든 검증할 수 있다. 표시·logging 용 excerpt 와 §5.2 auxiliary-turn 한도는
  이번 결정 범위 밖이다(PR #85 에 지점별로 기록).
- Source: PR #85, `src/amplai_foundry/runtime/execution/text_bounds.py`.

## D-125 — Work 033 Provisional Interface Decisions Are Confirmed

- Status: accepted (운영자 확정 2026-10-07, 2026-10-12 재확인)
- Date: 2026-10-07
- Decision: IC-15~IC-32 를 모두 추천안대로 확정한다. 지금까지 provisional 이던 IC-15~IC-21, IC-23~IC-29, IC-31, IC-32 와 §0 결정 IC-22 가 포함된다. IC-30 은 (A)로 정해 구현했다: 야간 identity 가 class A 초안의 mechanical screen 을 권한 `harness.screen` 으로 직접 실행한다. 야간 loop 를 켜기 전 확인 조건은 이것으로 충족된다. 이 결정은 `interfaces.md` "Operator Decisions Of 2026-10-07" 에 먼저 기록됐고, 여기서는 결정 목록에 옮겨 적는다.
- Source: `specs/033-harness-taxonomy/interfaces.md`, `docs/v3/DEV03_OBSERVATORY_META.ko.md` §10

## D-126 — Every Capability Is An Isolated Module, Including Checks

- Status: accepted (운영자 확정 2026-10-12)
- Date: 2026-10-12
- Decision: AMPLAI 는 여러 분야에 쓴다. 모든 기능(에이전트 역할 포함)은 교체·수정·추가·삭제할 수 있는 모듈이고, 그 변경이 다른 기능에 영향을 주지 않는다. 코어에는 저장소·권한·스케줄·검증·레지스트리 골격만 남는다. 모듈은 pack(foundation·kit·plugin)으로 묶는다. 지식 정본은 git 파일, 쓰기는 지식 모듈 하나, 접근은 MCP 다. 코드 저장소 연결은 `git_host` kind 다. 신뢰성 장치도 `check` kind 모듈이다. 추가·강화는 보통 PR 로, 삭제·완화는 사람 승인(class C)으로 한다. 권한·격리·비밀값 기본 검사는 pack 이 끌 수 없다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-15~OD-18, M-10~M-20, K-10~K-12, PR #86

## D-127 — Resync Uses The Caller's Own Scoped Snapshot

- Status: accepted (운영자 확정 2026-10-14)
- Date: 2026-10-14
- Decision: 이벤트 cursor 가 만료되면 호출자는 자기 범위의 snapshot 을 다시 받고 그 cursor 에서 이어 받는다. 운영자 snapshot `GET /api/v3/snapshot` 은 운영자 전용이다(`runtime.read`, intake actor 403). scope 의 goal·execution-plan heads 를 준다. intake snapshot `GET /api/v3/intake/{adapter_id}/snapshot` 은 연결된 운영자의 끝나지 않은 goal(`PLAN_ENDED` 밖)을 고정 schema `{goal_id, status, at}` 로 준다. 둘 다 상한 1000 이고, 넘으면 `truncated: true` 다(이어 받기는 아직 없다). cursor 는 `<incarnation>:<seq>` 이고 projection 과 같은 store lock 안에서 읽는다. 숫자만 있는 cursor(`0` 포함)는 409 `CURSOR_EXPIRED` 다. `Last-Event-ID` 를 먼저 쓰고, 그것을 쓸 수 없으면 쓸 수 있는 `after` 로 넘어간다. 409 본문은 `snapshot_ref` 만 주고 cursor 는 없다. 복원은 `amplai ops restore` 로만 한다(파일 복사·disk snapshot·VM rollback 금지). 그 명령만 store 세대를 새로 만든다.
- Reason: 표준 방식인 list-then-watch 를 따른다. Kubernetes 는 410 Gone 을 받으면 client 가 cache 를 비우고 다시 list 한 뒤 그 list 의 resourceVersion 부터 watch 를 이어 간다. collection 은 URL 경로(namespaced·cluster)로 범위가 정해지고 그 범위에 권한을 건다 (https://kubernetes.io/docs/reference/using-api/api-concepts/). 최소 권한 원칙도 같은 결론이다. 공유 경로 하나를 두면 scope 전체 자료가 앞단 에이전트에 새거나(H-8, AC-H3) intake actor 에게는 항상 403 이 된다.
- Rejected: 공유 경로 하나에서 actor 마다 걸러 주는 방식. 경로 하나 안에 권한 분기가 생겨 유출될 수 있는 면이 넓어진다.
- Source: PR #87, `specs/034-hermes-modular-platform/spec.md` H-16, `src/amplai_foundry/control_plane/api_v3/cursor.py`

## D-128 — Calibration Overrun Is Recorded, Stopped Calibrations Resume, Trials Are Imported

- Status: accepted (운영자 승인 2026-10-14, "전부 추천대로 진행, 승인한다"; PR #92 리뷰 결정은 같은 승인 범위 안에서 coordinator 가 정했다)
- Date: 2026-10-14
- Decision: (1) calibration trial 이 per-trial token 상한을 넘으면 trial·run head·summary 에 기록하고 run 을 계속한다(`ceiling_overrun`, `overruns`, summary `warnings` `TRIAL_OVER_CEILING`). root token budget, wall time, kill switch 는 그대로 hard stop 이다. calibration root 에서는 기록된 overrun 이 다음 reserve 를 막지 않는다(`META_PRIOR_OVERRUN` 제외). 상한 판정은 run 이 이미 쓰는 deployment price table 로 cache read·cache write 를 가중한 uncached-equivalent token 수로 한다. price 가 없거나 계산이 실패하면 raw token 으로 판정하고 이유를 기록한다. trial `tokens` 와 ledger 는 raw 그대로다(archive `tokens_per_solved`). IC-34 는 상한 판정에 한해 이렇게 고친다. stage experiment 는 지금처럼 overrun 에서 멈춘다. (2) 아무것도 실행하지 않았음이 증명된 `not_run` trial 은 0 token 으로 settle 한다(calibration·experiment 공통). failed·cancelled·lost run 의 goal reservation 은 driver journal 의 usage 로 settle 한다(`amplai meta settle-reservations`). (3) `amplai meta calibration resume <plan-id>` 는 stopped(owner_lost 포함) plan 을 같은 승인으로 다시 돌린다. 완료·채점된 trial 은 유지하고, 나머지 slot 만 다시 돌리고, summary 는 새 revision 으로 쓴다. evaluator version, trial code digest, driver version·image·model(`model_snapshot`), corpus version 이 다르면 `CALIBRATION_RESUME_CHANGED` 로 차이를 나열하고 거부한다. (4) plan 의 optional field `imports`·`import_policy` 로 다른 plan 의 완료·채점된 round-1 v1 trial 을 가져온다. plan digest 에 들어가므로 운영자가 승인한다. reuse key(composition ref, cell, task, corpus ref·version, trial code digest, model snapshot, evaluator version, mode)가 정확히 같고, 모든 cell 이 완료된 case 만, source trial 종료에서 freeze 까지 72h(설정) 안의 것만 가져온다. per-trial 상한과 root budget 은 key 에 넣지 않는다(회계 전용). 가져온 trial 은 provenance 와 0-charge allocation 을 가진다. pool 하기 전 cell 마다 표본을 다시 돌려 flip 비율의 Wilson 95 % 하한이 기준 A/A discordance 를 넘으면 그 cell 의 import 를 버리고 새로 돌린다. (5) done 이 아닌 run 의 summary 는 `partial` 로 남기고 어떤 reader(latest_summary, nightly baseline, StageRunner, dashboard, quality)도 고르지 않는다.
- Review decisions (PR #92, coordinator, 2026-10-14): (P1-1) reuse key·resume 비교는 checkout 전체의 `harness_sha` 대신 `trial_code_digest` 를 쓴다. 설치된 package 의 모든 파일(data 포함)을 정렬된 (경로, bytes) 로 hash 한다. trial root(local_executor, agent_drivers, runtime/execution 의 loop·worker·product·service·codex)의 static import closure 밖에 있는 명시 목록(dashboard, quality, surrogate, quota, meta_commands 의 `__init__` 외, runtime/cli, control_plane, integrations, distribution, packs/catalog)과 evaluator version 이 묶는 evaluation 의 analysis·sequential·versions·calibration·service 만 뺀다. 나머지(deciders, meta_ops, pricing, observatory, telemetry, archive, proposer, stages 등)는 안전하게 남긴다. `harness_sha` 는 receipt 에 provenance 로만 남는다. Claude CLI auth mode 는 argv 를 바꾸지만 배포는 code 상수(`build_claude_port` 의 `oauth_token`)로 정하므로 digest 가 덮는다. (P1-2) calibration root 의 wall budget 은 실제로 돈 시간(`wall_used_seconds` + 열린 segment)만 센다. 멈춘 동안은 세지 않는다. 멈춘 지 `window_hours` 를 넘긴 resume 은 drift 처리 대신 `CALIBRATION_RESUME_STALE` 로 거부한다(더 단순하다; 운영자는 새 plan 으로 import 해서 drift 점검을 받는다). import 가 있는 plan 을 resume 하면 끝나지 않은 drift 점검을 다시 돈다. (P2-a) evaluator change 는 `trial_compatible_with` 로 grading·trial 동작을 바꾸지 않는 이전 version 을 지정할 수 있고, 승인과 함께 evaluator-version record 에 저장된다(이어지지 않는다). eval-12 는 `["eval-11"]` 이다. eval-12 는 회계·resume·import·summary 선택만 바꾸고 채점은 바꾸지 않는다. (P2-b) `--import-from` 은 `--dry-run` 으로 미리 보고(승인 없음), 실제 실행은 `--expect-imported N` 이 맞아야 하며 N 은 plan(`expected_imported`)에 들어가 승인에 묶인다. (P2-c) drift 표본은 source run 에서 결과가 바뀌지 않은 과제에서만 뽑고 cell 당 최소 8(설정)이다. 기준은 source A/A discordance, 없으면 배포 전체 complete summary 의 A/A, 없으면 0 이다. manifest variant 는 import 하지 않고 새로 돌린다. 판정은 Wilson 하한을 유지한다(기준이 count 없는 고정값일 수 있어 Newcombe 두 표본 구간을 쓸 수 없다). (P2-d) provider 가 보고한 model id 는 정규화된 driver 이벤트에 남지 않아 key 에 넣지 않았다(Claude Code·codex-cli 원본 stream 노출 여부는 확인 필요). (P3) 가져온 repeat 0 과 새 repeat 1 의 쌍은 summary 의 A/A discordance·noise band 에서 뺀다. runaway in-flight token stop 은 이 PR 밖의 후속 작업이다.
- Failure review decisions (PR #92 f75128d, coordinator, 2026-10-14): (P2-1) owner_lost run 을 recover 할 때 reserved allocation 이 남아 있으면 segment 를 지금 시각에 닫고(보수적), 없으면 마지막 활동 시각에 닫는다. 남은 active wall time 이 trial 하나의 마감(1800초, `product.Budget`)보다 적으면 resume 을 거부한다. (P2-2) drift 표본 하한은 min(가져온 과제 수, cell 당 최소값)이다. 안정 과제로 모자라면 source summary 가 `flaky_grading` 으로 분류하지 않은 불안정 과제로 채우고, 그래도 모자라면 실행 전에 inconclusive 로 두고 그 cell 의 import 를 버린다. 비교한 쌍이 4개 미만이면 증거로 보지 않는다. (P2-3) resume 은 trial-metrics 가 없는 모든 pool trial 에 metrics 를 쓴다. (P2-4) calibration reserve 는 per-trial 상한과 이 root 에서 settle 된 가장 큰 raw token 중 큰 값을 잡는다. 상한 판정 기준은 그대로다. (P3) resume 은 원래 run 의 executor qualification·per-trial 상한을 다시 고정하고, 다르면 `--requalify` 없이는 거부한다(기록됨). import 저장은 run 시작과 같은 transaction 에서 한다. quality 의 discrimination 은 run 의 trial_refs 만 센다. trial code digest 는 `.DS_Store`·`*.swp`·`*~` 를 무시하고 읽기 오류는 `TRIAL_CODE_UNREADABLE` 이다.
- Reason: budget enforcement 는 class C protected control 이고 운영자가 이 변경을 승인했다. per-trial 상한은 executor-qualification record 에 있고(`runtime/execution/meta_ops.py:682-709`) dispatch 전 reserve, 후 settle 하는 회계값이다. executor 는 이 값을 읽지 않는다(`meta_harness/local_executor.py`). 그런데 overrun 하나가 calibration 전체를 멈췄다. calplan-7c314b2f 는 caltrial-12b6e71f(claude-sonnet-5, raw 4,703,518 token, CLI 비용 $2.142273 = price table 과 일치)에서 멈췄다. 이 trial 의 cache read 4,514,585·cache write(1h) 108,217 을 `deployment/prices/2026-09-30.json` 으로 가중하면 input 쪽 667,977, output 포함 748,609 다. stopped run 207 개, owner_lost run 488 개 trial 은 resume 수단이 없어 잃었다. `not_run` trial 은 실행한 것이 없는데 4M reservation 을 청구받았다. failed run 66 개의 goal reservation 이 `active` 로 남았고 그중 33 개는 journal 에 estimated usage 가 있다(사본 DB, 2026-10-14). 업계 방식: Inspect 는 `eval_retry`/`eval_set` 에서 task identifier hash(token·time·message limit 포함, retry·concurrency·timeout 제외)가 같은 완료 sample 만 재사용한다(https://inspect.aisi.org.uk/eval-sets.html). Harbor `jobs resume` 은 config 가 다르면 거부하고 완료 trial 만 유지한다(https://github.com/harbor-framework/harbor/blob/main/src/harbor/job.py).
- Rejected: per-trial 상한을 없앤다(회계 기록과 runaway 신호를 잃는다). 상한을 in-flight kill 로 바꾼다(실행 중 끊으면 측정값이 왜곡된다). resume 을 두지 않는다(지금까지 207 + 488 trial 을 잃었다). checkout 전체 `harness_sha` 를 key 로 쓴다(dashboard·CLI 만 바꾼 commit 도 모든 trial 을 버리게 한다).
- Consequence: 이 변경은 evaluator digest 파일(`evaluation/calibration.py`, `evaluation/service.py`)을 바꾸므로 eval-12 가 필요하다(`specs/033-harness-taxonomy/runs/eval-12.json`). 이 변경 전 receipt 에는 `trial_code_digest` 가 없으므로 이전 run(calibration 12 포함)의 trial 은 resume·import 대상이 아니다.
- Source: `specs/033-harness-taxonomy/interfaces.md` "Clarification: Calibration Resume (D-128)", `src/amplai_foundry/evaluation/calibration.py`, `src/amplai_foundry/meta_harness/local_executor.py` `trial_code_digest`, `docs/v3/DEV03_OBSERVATORY_META.ko.md` §10.7, PR #92

## D-129 — Drift Is A Free Nightly Check And A Weekly Paid Measurement

- Status: accepted (운영자 승인 2026-10-14)
- Date: 2026-10-14
- Decision: drift 는 두 부분이다. (1) 무료 점검 — `NightlyRunner.drift_check` 가 champion snapshot(model id, CLI/driver version, image)을 calibration 과 비교한다. trial 없이 매일 밤 돌고, `changed` 는 지금처럼 search 를 막고 재calibration 을 예약한다. (2) 유료 측정 — `meta.nightly.drift_tasks` 전부(현재 20개)에 대한 drift calibration(purpose `"drift"`, eval-11)은 N 밤에 한 번만 돈다(새 설정 `meta.nightly.drift_every_nights`, 기본 7, 1 이면 지금처럼 매일 밤). 측정하는 밤에는 cell 마다 `len(drift_tasks)` trial 을 밤 예산에서 먼저 떼고 나머지 단계 몫을 계산한다. drift share 상한(PHASE_SHARE, `_drift_fit`)은 drift 에 더 이상 적용하지 않는다. preflight 는 trial 전에 `len(drift_tasks) × len(cells) <= budget_trials` 를 확인하고 넘으면 `NIGHT_CONFIG` 로 멈춘다. 측정 사이의 밤에는 마지막 유료 측정(그 cell 의 drift-purpose calibration summary, 지금 evaluator version, 같은 corpus)이 `drift_every_nights` 밤보다 오래되지 않았고 결과가 ok(band 안)인 cell 만 search, screening, confirmation 을 돈다. 아니면(측정이 없거나, 오래됐거나, 이전 측정이 unchecked·outside_band 이고 아직 해소되지 않았으면) 그 밤은 그 cell 의 측정하는 밤이 된다. fail-closed 는 유지한다: 측정하는 밤의 `unchecked` 는 그 밤 search 를 막는다. 밤은 `nightly-run` 기록의 날짜로 센다. PR #91 검토로 다음을 더한다. (a) 운영자 baseline calibration 이 측정한 밤보다 새로우면 다시 측정한다(`baseline_newer`). (b) 측정은 drift case 를 모두 관측했을 때만(observed runs >= drift case 수) 다음 밤들에 fresh 로 남는다. budget 정지로 일부만 돈 측정은 그 밤에는 지금처럼 측정으로 치지만, 다음 밤에 다시 측정한다(`incomplete`). (c) 측정 summary 의 plan `case_ids` 가 지금 `drift_tasks` 의 frozen case(corpus 순서)와 같아야 한다. 다르면 `not_current` 다. (d) band 가 없는 cell(`uncalibrated`)은 유료 측정을 건너뛴다(`no_band`). search 동작은 그대로다. (e) 남은 예산은 search 와 confirmation 이 자기 비율대로 나눈다: search = floor(R × search / (search + confirmation)), screening design 은 같은 방식으로 search 에서 떼고, confirmation 은 나머지다. 이 결정은 D-104 의 drift 약 10% 몫을 고친다. standing approval 의 `shares.drift` 는 더 이상 상한이 아니다. 두 번째 검토로 다음을 더한다. (f) `drift_every_nights` 는 1~31 이다. (g) search·confirmation 비율이 모두 0 이면(drift 전용) 둘 다 0 trial 이다. (h) 측정 summary 의 plan 은 지금 설치된 그 cell 의 champion composition 을 돌린 것이어야 한다. (i) 측정의 나이는 날짜 차이와 그 밤이 실제로 시작한 뒤 지난 일수 중 큰 값이다. (j) baseline 시각을 모르면 측정한다(`baseline_unknown`). (k) cell 마다 측정 결과를 바로 기록한다. (l) search 가 막히지 않았고 search 비율이 0 보다 크면 쓰지 않은 drift 몫을 search 로 돌린다. (m) 읽을 수 없는 밤 기록은 `NIGHT_STATE` Hold 로 멈춘다.
- Reason: 무료 version 점검이 흔한 원인(말없이 바뀐 model·CLI·image)을 매일 밤 비용 없이 잡는다. 유료 측정은 version 변화 없는 행동 drift 를 잡는다. 20개 task 전부로 주 1회 재는 편이 매일 밤 7개로 재는 것보다 검정력이 높고, 비용은 한 주 예산의 약 4% 다.
- Rejected: 매일 밤 7개 task(검정력이 낮다). drift share 27%(search 를 깎는다).
- Source: `src/amplai_foundry/meta_harness/nightly.py`(`drift_due`, `_drift`, `_drift_fit`, `phase_budget`, `LocalNightlyBackend.drift_measurement`), `specs/033-harness-taxonomy/interfaces.md` "Clarification: Drift Subset" 의 "Drift cadence (D-129)", `docs/v3/DEV03_OBSERVATORY_META.ko.md` 야간 절

## D-130 — Code Host Is The git_host Module (OD-15)

- Status: accepted (운영자 확정 OD-15 2026-10-12, D-126)
- Date: 2026-10-14
- Decision: 코드 저장소 연결은 `git_host` 모듈 kind 다. interface v1 의 연산은 `check_remote(repo, remote)`(읽기 전용), `push_branch(repo, remote, branch, commit)`, `open_draft_pr(repo, remote, head, base, title, body) -> url`(찾거나 만든다), `comment_pr(repo, url, body)`, `pr_state(repo, url) -> PullRequestState(state, head_commit, merged_at, closed_at)` 이고 적합성 시험은 `conformance.git_host.v1` 이다. 거절 코드는 모듈 이전과 같다(`PUBLISH_REMOTE`, `PUBLISH_PUSH`, `PUBLISH_BRANCH_EXISTS`, `PR_CREATE`, `PR_COMMENT`, `PR_STATE`). 첫 구현은 내장 `git_host.github@1`(운영자의 `gh`·`git`)이다. 설정 `local.json` `git_host` 가 없으면 GitHub `github.com` 이고 push·PR 명령은 이전과 같다. `null` 이면 git host 모듈이 없다. 그러면 게시는 commit·기록 전에 `PUBLISH_NO_GIT_HOST` 로 hold 되고, `ops pr-sync` 도 같은 코드로 hold 된다. host 확인: 모듈은 쓰거나 읽기 전에 확인한다. remote 의 push URL 전부(`git remote get-url --push --all`, `pushurl`·`pushInsteadOf` 반영)는 `remote_hosts` 안에 있어야 한다. 설정 `git_host.remote_hosts` 는 선택이고 없으면 `[host]` 다(SSH 별칭, SSH host 가 웹 host 와 다른 서버). PR URL(댓글·상태 읽기, `gh` 가 찾거나 만든 뒤 돌려준 URL)은 웹 host 인 `host` 에 있어야 한다. 대소문자, port, 끝의 `.` 는 무시하고, 설정 host 의 끝 `.` 는 거절한다. 다르면 `GIT_HOST_MISMATCH` 이고 details 는 `{configured: [hosts], remote}` 다. 이미 생긴 PR 이면 `pr_url` 이 더해진다. checkout 에 없는 remote, 읽을 수 없거나 시간이 넘은 remote 는 `PUBLISH_REMOTE` 다. 여러 app 의 goal 은 모든 app 의 remote 를 확인한 뒤에야 push 한다. `ops pr-sync` 는 코드가 있는 이유로 읽지 못한 PR 을 응답의 `unread` 로 보여 주고 store 에는 남기지 않는다(`publication_outcome` 모양은 그대로다). `-` 로 시작하거나 빈 remote·branch 이름은 `PUBLISH_REF` 다. `github.com` 이 아닌 host 에는 `gh` 명령에 `GH_HOST=<host>`(설정값 그대로, port 포함)를 붙인다. 지식 공간 승인 증거(D-115)를 읽는 hook 은 v1 에 없다. S2a 에서 interface v2 와 새 적합성 시험으로 더하며, 그 모양은 그때 운영자가 결정한다(제안: `specs/034-hermes-modular-platform/plan.md` §3.9).
- Reason: `gh` 는 checkout 의 remote 에서 host 를 읽는다(`gh help environment`, gh 2.88.1: `GH_HOST` 는 "cannot be inferred from the context of a local Git repository" 일 때만 쓴다). 그래서 설정 `host` 만으로는 PR 이 어디로 가는지 정해지지 않는다. 모듈이 직접 확인해야 설정이 의미를 가진다.
- Consequence: 기본 설정에서도 push·PR 생성 전에(여러 app 이면 app 마다 먼저 한 번 더) 읽기 전용 `git remote get-url --push --all` 이 돈다. app 설정 `remote` 는 자유 문자열이라(`runtime/local_deployment.py` `AppEntry.remote: str = "origin"`, 형식 검사 없음) URL 도 들어갈 수 있고 모듈 이전에도 `ls-remote`·`push` 가 URL 을 받았다. 그래서 `remote` 가 URL(`https://`, `ssh://`, `git://`, `file://`, `user@host:path`)이나 로컬 경로면 `get-url` 없이 그 host 를 `remote_hosts` 와 바로 비교하고, 이름일 때만 `git remote get-url --push --all` 을 쓴다(없는 이름은 `PUBLISH_REMOTE`). URL 로 적은 remote 에 운영자 git 설정의 `pushInsteadOf` 가 걸리는 경우는 반영하지 않는다(확인 필요). 계획 쪽 `git_fresh_base` 는 URL remote 에 `git remote get-url` 이 실패해 local branch 를 기준으로 쓴다(`runtime/execution/product.py:107-112`, 이전과 같다). GitHub 이 아닌 remote(로컬 경로 포함)로 `branch` 모드 게시를 하던 배포는 이제 `GIT_HOST_MISMATCH` 로 멈춘다(지금 그런 배포가 있는지는 확인하지 않았다).
- Source: PR #93, `src/amplai_foundry/runtime/git_host/`, `specs/034-hermes-modular-platform/plan.md` §3.9

## D-131 — Reliability Checks Are check Modules (OD-18)

- Status: proposed (운영자 결정 대기: 승인자 확인 방식)
- Date: 2026-10-14
- Decision (제안): 신뢰성 장치는 `check` 모듈 kind 다 (OD-18, D-126, M-16~M-20). 이 PR 이 정하는 계약은 다음이다.
  1. kind `check`: `modules/spec.py` KINDS 에 추가, interface v1(`run(context) -> CheckResult`), 적합성 시험
     `conformance.check.v1`(정적). 선언 `CheckSpec`: id(= module id `check.<name>`), version, targets(`core`, `kind:<kind>`,
     `kind:*`, `pack:<id>`), when(`ci`, `enable`, `pack_install`, `upgrade`), required, protected, config(문자열 목록),
     `loosens_when_added`.
  2. CLI `amplai checks run|list|lock`. 결과 `pass`, `pass_with_skips`, Hold `CHECK_FAILED`(검사 id·버전·상태·findings),
     `CHECK_NONE_SELECTED`, `CHECK_PROTECTED`, `CHECK_PROTECTED_MISSING`, `CHECK_SKIP_REFUSED`; 거절 `CHECK_UNKNOWN`,
     `CHECK_WHEN`, `CHECK_TARGET`. 보호 검사(`BASE_PROTECTED` ∪ `protected`)는 네 시점 모두, `--target` 과 관계없이 돈다.
  3. `checks.lock.json`(`amplai.checks-lock.v1`): 선언 전체와 `ci_external`(skip 가능한 검사 목록). 완화 정의: 제거,
     required·protected 낮춤, when·targets 축소, config 완화, version 낮춤, `ci_external` 추가.
  4. 승인 기록(`amplai.check-approval.v1`, `docs/workstreams/v3-real-execution/check-approvals/*.json`): check_id,
     before·after(lock entry + `ci_external` 소속의 digest), approver, reason, approved_at(RFC 3339 UTC, 미래 금지). 한 번만
     쓴다(base ref 에 이미 있으면 `CHECK_APPROVAL_REUSED`, 같은 변경에 둘이면 `CHECK_APPROVAL_DUPLICATE`). `--base-ref` 가
     있으면 판정(`evaluate`, `lock.loosenings`)은 base ref 의 policy 코드(`git archive` 사본, `POLICY_API = 1`)가 한다.
     이것은 판정 코드를 바꾸는 변경만 막는다. 호출부(`CheckPolicyCheck`, runner, `checks_cli.py`)나 CI workflow 를 바꾸는
     변경은 막지 못한다. base ref 에 `runtime/checks/policy.py` 가 확실히 없을 때만(`git cat-file -e` 실패 + 빈 `git
     ls-tree`) 이 tree 의 코드로 bootstrap 하고, 그 밖의 읽기·풀기·실행 실패는 `CHECK_POLICY_BASE`(fail closed)다. lock 의
     schema 를 base 코드가 모르면 lock 전체가 완화 하나(`check_id` `checks.lock.json`)이고 승인 기록이 필요하다. 승인 없이
     바꾸려면 두 단계다: 먼저 판정 코드가 새 schema 를 읽게 하고(lock 은 그대로), 다음 변경에서 lock 을 바꾼다.
  5. 실행 기록(`amplai.check-run.v1`): 로컬 JSON 한 개와 CI artifact. 제품 store 에는 쓰지 않는다.
  6. 모듈 코드 위치: `modules/builtin.py` `BUILTIN_CODE`(구현, 적합성 subject, `local.json` 끄기 키), `KIND_PACKAGES`,
     `KIND_SUITES`, `modules/loader.py`. `runtime/notifier` 패키지는 Slack 구현을 더 이상 export 하지 않는다(composition root 가
     설정에서 고를 때 import).
  7. 첫 검사와 그 기준: boundary lint(composition root `local_deployment.py`), kind conformance, 제거 시험(`CORE_TESTS` 14
     파일), secret 노출(`cas.py` 패턴 + 검사 전용 5 패턴, 출현 단위 허용 15 항목), check policy, 기존 도구 5개 감싸기.
  8. 주변 변경: `TRIAL_CODE_EXCLUDED` 에 `runtime/checks/**`, `runtime/checks_cli.py`, 두 conformance transport,
     `modules/loader.py` 추가(D-128 closure 시험 통과). `test_033_s14_quality` 의 "quality 를 import 하는 곳" 불변식에
     `runtime/checks/tools.py`(Q-suite 검사, `upgrade` 시점만) 추가.
- CI base (coordinator 기본값, 운영자가 완화할 수 있다): pull_request 는 `pull_request.base.sha`, merge queue 는
  `merge_group.base_sha`, push 는 `github.event.before` 를 base 로 fetch 한다. PR·merge queue 에서 base 를 fetch 하지 못하면
  검사 단계가 실패한다(fail closed). push 에서 fetch 하지 못하면(force push) base 없이 돌고 경고와 `--note` 기록을 남긴다.
  all-zero SHA(첫 push)는 base 가 없다. 승인 기록은 내용 기준으로 한 번만 쓴다: base ref 에 같은 (check_id, before, after) 의
  기록이 어떤 경로로든 있으면 `CHECK_APPROVAL_REUSED` 다.
- 운영자 결정 대기:
  - (A) 승인 기록을 사람이 썼는지, 그리고 검사 호출부·CI 를 바꾸는 변경을 사람이 봤는지 확인하는 방식: CODEOWNERS 와 code
    owner 필수 리뷰를 아래 경로에 걸고 `amplai checks run --when ci` 를 필수 status check 로 두는 저장소 보호, 또는 운영자 키
    서명, 또는 store 승인. 보호가 필요한 경로: `src/amplai_foundry/runtime/checks/**`,
    `src/amplai_foundry/runtime/checks_cli.py`, `.github/workflows/ci.yml`, `checks.lock.json`,
    `docs/workstreams/v3-real-execution/check-approvals/`, `src/amplai_foundry/runtime/evidence/cas.py`. 저장소 설정은 이 PR 이
    바꾸지 않는다.
  - (B) 실행 기록을 제품 store 에 둘지(M-20 표시 화면).
  - (C) pack 이 required 검사를 더할 수 있는지(M-18).
- Source: `specs/034-hermes-modular-platform/spec.md` OD-18, M-16~M-20, `specs/034-hermes-modular-platform/plan.md` §3.10,
  `src/amplai_foundry/runtime/checks/`, PR #94

## D-132 — Nightly Control Records (S1d core)

- Status: proposed (운영자 결정 대기)
- Date: 2026-10-14
- Decision (제안): D-117 (OD-12, Q-8 = B, Q-9 = 제품 store) 의 코어 계약을 다음으로 정한다. 근거 `specs/034-hermes-modular-platform/plan.md` §3.7, §3.8, spec N-1~N-9.
  1. 기록: 제품 store kind `nightly-control`, schema `amplai.nightly-control.v1`. 필드는 정확히 `schema`, `scope`, `control_id`, `action`(`stop|skip|tighten|start|clear`), `dates`(1~7개 `YYYY-MM-DD`), `limits`(tighten 만: `budget_trials` 정수 ≥ 0, `stop_at` `HH:MM`), `issued_by`(wire actor: human, 또는 service + `intake:<adapter>`), `reason`(1~4000자, 기록에만), `at`, `clears`(clear 만, 지우는 `control_id`). 기록은 revision 1 하나로 불변이다. 접수는 `NightlyControlService`(`runtime/nightly_control.py`)이고 actor mark + key 로 `Store.command` 중복 방지를 쓴다. 응답은 `{control_ref, control_id, action, dates, requested_dates}` 다.
  2. 규칙: 가장 엄격한 것이 이긴다. 멈춤 이유 순서는 `operator_stop` > `front_agent_stop` > `skipped` 다. 예산은 최소값, 종료 시각은 가장 이른 것이다. 모든 `stop_at`(설정과 조이기)은 그 날짜 밤의 **예정 시작**(`meta.nightly.start_at`) 뒤 첫 시각으로 푼다(`scheduled_end`). 그래서 늦게 시작한 밤도 받아들인 조이기의 뜻을 지킨다. 넓히는 조이기와 효과 없는 조이기(그 밤의 종료(설정 `stop_at`, 예정 시작 + `max_hours`, 이미 걸린 조이기 중 가장 이른 것) 이후의 `stop_at`)는 `Hold NIGHT_CONTROL_WIDEN`(details `effective`)이다. 앞단 에이전트는 자기 skip·tighten·start 만, 그 밤 시작 전에만 지운다. 운영자는 모두 지운다. 같은 규칙을 읽는 쪽(`fold`)도 다시 적용한다. 시작한 밤에 대한 clear 의 응답은 `effective: false`(meta 를 못 읽으면 `null`)다. 날짜 없는 stop 은 지금 도는 밤이다. head 가 `running` 이어도 창(시작 뒤 `stop_at`·`max_hours`)이 닫혔으면(죽은 밤) 도는 밤으로 치지 않는다. 이 창은 실제 시작 기준의 설정 종료이고 조인 종료는 반영하지 않는다(직접 `run` 은 예정 시작과 무관하게 돌 수 있어서다). 그래서 조인 종료와 설정 종료 사이에 죽은 head 는 그 동안 도는 밤으로 보이고, 그 사이의 날짜 없는 stop 은 그 죽은 밤에 걸린다. 그 시간이 지나면 다음 밤으로 간다. 도는 밤이 없으면 오늘 밤이고, 오늘 밤에 이미 head 가 있으면(아침, 죽은 밤) 다음 밤이다. stop·tighten 은 전날 밤(정오를 넘겨 도는 밤)도 이름으로 지정할 수 있다. 끝난 밤을 이름으로 지정한 stop·skip·tighten 은 `Hold NIGHT_CONTROL_ENDED` 다. 시작 요청(N-9)은 접수 때와 tick 시작 직전에 검사한다. 거절은 `Hold NIGHT_START_REFUSED`, details `condition` ∈ `no_start_window`, `outside_window`, `already_ran`, `held`(사람뿐 아니라 모든 stop·skip), `no_standing_approval`, `budget_trials`, `cells`, `shares`, `already_requested`, `frequency`, `date` 다.
  3. 밤 날짜: 예정 시작(`meta.nightly.start_at`) 기준이다. 시작이 정오 전이면 그 시작 전 12시간 안의 순간은 그 날짜의 밤이다(23:00 은 다음 날짜). 앞단 에이전트의 이른 시작은 그 밤을 대신하고 밤을 더 만들지 않는다.
  4. Hold·거절 코드: `NIGHT_CONTROL_WIDEN`, `NIGHT_CONTROL_ENDED`, `NIGHT_CONTROL_STARTED`(시작한 밤의 자기 기록을 지우려 함), `NIGHT_START_REFUSED`, `NIGHT_CONTROL_META`(meta deployment 를 읽을 수 없어 규칙을 확인할 수 없음; stop·skip 은 이것으로 막히지 않는다), `NIGHT_CONTROL_UNREADABLE`(밤 쪽, details `code`, `NIGHT_CONTROL_STORE` 포함), `NIGHT_CONTROL_INPUT`, `NIGHT_CONTROL_RECORD`, `FORBIDDEN`.
  5. 권한(class C, plan.md:494): `nightly.control` 을 `INTAKE_CEILING`(= `INTAKE_PERMISSIONS` ∪ {`nightly.control`})에 넣는다. adapter 설정에 명시해야만 받고, 기본 부여와 S1c 이전 단일 `intake` 설정 이전에는 들어가지 않는다. 운영자 `OPERATOR_PERMISSIONS` 에 `nightly.control` 을 더한다.
  6. 설정: `meta.nightly.start_at`(기본 `"01:00"`, tick 의 예정 시작), `meta.nightly.agent_start_window` `{start, end}`(`HH:MM`, 자정을 넘을 수 있음; 없으면 앞단 에이전트가 시작할 수 없다), `meta.nightly.agent_start_limit`(밤당 앞단 에이전트 시작 요청 수, 지운 것 포함, 기본 1, 1~10), `meta.control_store` `{runtime_root, scope}`(없으면 제어를 읽지 않는다, S1d 이전과 같다).
  7. 저장소 신원: 모든 deployment 는 부팅 때 자기 store 의 `meta` 테이블 key `deployment_identity` 에 값 하나(`amplai.deployment-identity.v1`, `{schema, scope, role}`, role = `meta.nightly` 가 있으면 `meta`, 아니면 `product`)를 쓴다(`Store.set_identity`). store 당 하나이고 지금의 scope 다. scope 가 바뀌면 바꿔 쓴다. 밤의 reader 는 `control_store` 가 meta 자신의 runtime root 가 아니고, 그 store 의 신원이 이 scope 의 `product` 일 때만 읽는다. 아니면 `control_unreadable`(원인 `NIGHT_CONTROL_STORE`)로 멈춘다. 이 신원이 없는 이전 제품 store 는 다음 부팅 전까지 읽히지 않는다.
  7a. 배포 순서: 제품 서버를 새 코드로 다시 띄운 뒤(신원이 써진다) meta config 에 `meta.control_store` 를 넣는다. 거꾸로 하면 그 사이 밤은 `control_unreadable` 로 멈춘다.
  7b. 깨진 기록 복구: 밤은 엄격하게 읽어 깨진 `nightly-control` 기록이 하나라도 있으면 멈춘다(원인 `NIGHT_CONTROL_RECORD`). 서비스는 유효한 기록만 읽어서 stop 등은 계속 받는다. 운영자(사람만)가 깨진 기록의 object id 를 `clear` 하면 격리된다(응답 `quarantined: true`). 그 뒤 읽기는 그 기록을 건너뛴다. 기록은 지우지 않는다(불변).
  7c. 읽기 범위: 밤은 날짜가 [그 밤 - 1, 그 밤 + 1] 에 닿는 기록과 모든 clear 만 검증하고 접는다. 날짜를 읽을 수 없는 기록은 검증 대상이다.
  8. Event: 제품 store `nightly.control_recorded`(aggregate `nightly-control`, payload `{control_id, action, dates, by: operator|front_agent, mark, clears}`; reason 없음). meta store(aggregate `nightly-run`, id `night-<date>`, 실제 밤만, dry run 없음): `nightly.started` `{date, trigger}`(trigger `{by: schedule, start_at}` | `{by: start_request, control_id, issued_by}` | `null` = 직접 `run`), `nightly.ended` `{date, state, reason, trials}`, `nightly.drift` `{date, cells}`, `nightly.approval_pending` `{date, queued}`.
  9. 밤 쪽: `NightlyRunner(controls=...)` 가 preflight, 단계 사이, `_night_reason`(모든 trial guard)에서 읽는다. 멈춤 이유 `operator_stop`, `front_agent_stop`, `skipped`, `control_unreadable` 은 `HOLD_REASONS` 와 `NO_DREAM` 에 든다. 적용한 제어는 그 밤 동안 풀리지 않는다. `applied` 에는 멈춘 stop·skip 과 실제로 예산·종료를 옮긴 tighten 만 든다. tick 이 시작한 밤(trigger 있음)의 종료는 설정 `stop_at` 을 예정 시작 기준으로도 푼 값 이하다. 직접 `run`(trigger 없음)은 운영자의 실행이라 실제 시작 기준이다. 조인 종료가 실제 시작보다 앞이면 preflight 전에 `stop_at` 으로 멈추고 `stop_passed_before_start: true` 를 남긴다. `nightly-run` 의 `data.controls` 는 `{applied, budget_trials, stop_at, trigger, unreadable?, cause?, stop_passed_before_start?}` 이다(`cause` 는 `NIGHT_CONTROL_STORE`, `NIGHT_CONTROL_RECORD`, `NOT_FOUND` 같은 원인 코드).
  10. 앞단 에이전트 요약(아직 route 없음): `amplai.night-summary.v1` `{schema, date, standing: {current, valid_until}, night: null | {state, phase, trials, stopped, queued_confirmations, reconcile_pending}, controls: null | [{control_id, action, by, at}], controls_unreadable}`. 값은 닫힌 집합과 수만 쓴다.
  11. CLI: `amplai meta nightly status` 는 deployment 를 부팅하지 않고 meta store 를 읽기 전용으로 연다(밤이 도는 동안에도 된다). `control_store` 가 있으면 `controls` 를 더 보인다. `run`·`status` 의 기본 날짜는 달력 날짜가 아니라 지금의 밤 날짜(3.)다. 그래서 정오 뒤에 `--date` 없이 수동 `run` 을 하면 **다음** 밤의 이름으로 돌고, 날짜당 한 번이라 그 예정된 밤을 써 버린다. 낮의 임시 실행은 `--date` 를 준다. `print-agent` 는 01:00 `run` 대신 `StartInterval 300` + `tick` 을 출력한다. 이미 설치된 agent 는 다시 설치하기 전까지 그대로다.
  12. tick: 읽기 전용으로 판단한다. 그 날짜의 밤 기록이 있으면 `ran` 이다. 예정 시작 ≤ 지금 < 그 밤의 종료(`scheduled_end`: min(`stop_at`, 시작 + `max_hours`, 걸린 조이기), 모두 예정 시작 기준)이면 시작하고, 아니면 유효한 시작 요청이 있을 때만 시작한다. standing 이 덮지 않으면 `idle`(reason)이다. meta deployment 를 여는 순간 다른 owner 가 있으면 `busy` 이고, 다음 tick(5분 뒤)이 다시 시도한다. 예정 시작은 그 밤의 종료까지, 시작 요청은 N-9 를 만족하는 동안(시간대 안) 다시 시도한다. 그 뒤 그 밤은 돌지 않는다. 밤이 도는 중에 난 `ACTIVE_OWNER` 는 `busy` 가 아니고 그대로 올라간다.
- Not decided here (결정 대기): 접수 route(운영자·intake·`/meta/feed`), MCP 도구 계약 1.1(`night_*`, `night_start` 포함 여부), notifier projection 확장(N-9 시작·앞단 에이전트 멈춤·drift), 제품 → meta config 경로 설정 키.
- Consequence: 이 변경은 D-128 `trial_code_digest` 범위의 파일(`meta_harness/nightly.py`, `runtime/local_deployment.py`, `runtime/storage/store.py`, `runtime/contracts/intake.py`, 새 `runtime/nightly_control.py`)을 바꾼다. 그래서 merge 하면 digest 가 바뀌고, 이전 trial 은 resume·import key 가 맞지 않는다(예상된 결과). 배포 순서는 7a 다.
- Source: PR #95, `src/amplai_foundry/runtime/nightly_control.py`, `src/amplai_foundry/meta_harness/nightly.py`, `src/amplai_foundry/runtime/meta_commands/nightly.py`, `src/amplai_foundry/runtime/local_deployment.py` `mark_identity`

## D-133 — The Intake-Only Listener (S1g, H-12)

- Status: proposed (운영자 결정 대기: 보호 방식, VM 이 닿는 주소, 운영자 API 의 intake 경로)
- Date: 2026-10-14
- Decision (제안): OD-8 의 VM 은 접수 전용 listener 로만 들어온다 (H-12, plan.md §3.2 item 11).
  1. 형태: `ops local-serve` 프로세스 안의 두 번째 socket 이다(store owner 는 하나). 설정 `local.json`
     `intake_listener: {host, port}` 는 둘 다 기본값이 없다. `host` 는 IP 주소 하나이고 이름·`0.0.0.0`·`::` 는 거부한다. 없으면
     두 번째 listener 가 없고 동작은 이전과 같다. 서비스 관리자 unit `amplai-intake-listener` 는 필요 없다(plan.md §3.5a.1 표).
  2. 경로: 설정된 adapter 의 `/api/v3/intake/<adapter_id>` 아래만 연다. 운영자 API 와 같은 intake router 를 쓰므로 adapter 하나의
     토큰·연결표·빈도 제한 창은 하나다(H-7). 그 밖의 경로(운영자 API, meta, evaluation, `/api/v3/local`, `/healthz`, OpenAPI·docs),
     `.`·`..` path segment, 설정되지 않은 adapter 는 body 를 읽기 전에 `404 INTAKE_LISTENER_ROUTE` 다. adapter root 아래의 없는
     경로와 끝의 `/`(slash redirect 없음, `Location` 없음)도 같은 코드, method 가 틀리면 `405 METHOD_NOT_ALLOWED` 다.
     그 밖의 `HTTPException` 은 `HTTP_ERROR`(그 status 그대로)다. 지금 intake 경로는 `HTTPException` 을 내지 않는다(모두
     `RuntimeFault`). WebSocket 은 accept 전에 닫는다. 운영자 토큰은 intake 경로에서 `401 UNAUTHENTICATED` 다(intake gate,
     `control_plane/api_v3/intake.py`). body·query 검증에 실패한 요청은 먼저 adapter 토큰을 확인한다. 인증되지 않은 요청은
     gate 의 `401`(또는 `503 INTAKE_UNAVAILABLE`)만 받고 `422` 의 field 이름을 보지 못한다. 운영자 listener 의 검증 순서는
     그대로다.
  2a. 서버: listener 의 uvicorn 은 `proxy_headers=False`(`X-Forwarded-For`·`X-Forwarded-Proto` 를 믿지 않는다. peer 는 TCP
     peer 다)이고 `Server` header 를 보내지 않는다. 두 서버 모두 종료 때 실행 중 요청을 최대 10초(`GRACEFUL_SECONDS`) 기다린 뒤
     취소한다. uvicorn 0.54 에는 header·body 읽기 시간 제한이 없다(`Config` 의 시간 설정은 `timeout_keep_alive` — 응답이 끝난
     뒤에만 걸린다, `uvicorn/protocols/http/h11_impl.py:317-326` — 와 `timeout_graceful_shutdown` 뿐이다). header 를 끝내지 않은
     연결은 종료 때 바로 닫히고(`h11_impl.py:336-343`), body 를 끝내지 않은 요청은 10초 뒤 취소된다. 실행 중 읽기 시간 제한과
     연결 수 제한(`limit_concurrency`)은 두지 않았다(결정 대기 D).
  3. 시작 거부 코드(`ops local-serve` 만 검사한다. deployment 를 여는 다른 명령은 `intake_listener` 를 보지 않는다):
     `INTAKE_LISTENER_NO_INTAKE`(intake adapter 없음), `INTAKE_LISTENER_UNPROTECTED`(loopback 이 아닌 주소, 아래 결정 전까지),
     `INTAKE_LISTENER_PORT`(`--port` 와 같은 port), `INTAKE_LISTENER_BIND`(bind 실패). 한 서버가 끝나면 다른 서버도 멈춘다.
     Ctrl-C·SIGTERM 은 두 서버를 멈춘 뒤 `uvicorn.run` 처럼 다시 올린다.
- 운영자 결정 대기:
  - (A) H-12 의 보호 방식. spec 은 "TLS(또는 출발지 고정 토큰)" 이라 둘 중 하나를 요구하지만 어느 것인지 정하지 않았다. 그래서
    지금은 loopback 주소만 연다(fail closed). 선택지: (A1) TLS — store 밖 0600 인증서·키 파일, VM 의 MCP 서버가 그 인증서를
    고정해 검증(MCP 서버에 CA 경로 설정 추가 필요). (A2) 출발지 고정 토큰 — adapter 마다 허용 출발지 IP 목록, TCP peer 주소가
    목록 밖이면 토큰 비교 전에 거부. (A3) 둘 다.
  - (B) VM 이 닿는 주소. VM 소프트웨어와 네트워크 모드에 달렸다(확인 필요). host-only·shared 네트워크면 Mac 쪽 bridge 주소를
    `host` 로 쓴다. 사용자 모드 NAT(QEMU slirp 계열)는 VM 의 gateway 주소가 호스트 loopback 으로 이어질 수 있어 운영자 API
    (`127.0.0.1:5083`)까지 VM 에서 닿을 수 있다(추정, 확인 필요).
  - (C) listener 를 켰을 때 운영자 API 가 intake 경로를 계속 열지. 지금은 계속 연다(S1a·S1c 동작 그대로).
  - (D) listener 의 요청 읽기 시간 제한(ASGI 에서 body 읽기에 기한)과 연결 수 제한(`limit_concurrency`)을 둘지, 둔다면 값.
- Consequence: `runtime/local_deployment.py`(설정·조립)를 바꾸므로 D-128 `trial_code_digest` 가 바뀐다. 이전 trial 은 resume·
  import key 가 맞지 않는다. 새 listener 코드(`control_plane/api_v3/intake_listener.py`)와 `runtime/cli.py` 는 digest 밖이다.
- Source: `specs/034-hermes-modular-platform/spec.md` OD-8, H-12, `specs/034-hermes-modular-platform/plan.md` §3.2 item 11,
  `src/amplai_foundry/control_plane/api_v3/intake_listener.py`, `tests/v3/test_034_s1g_intake_listener.py`
