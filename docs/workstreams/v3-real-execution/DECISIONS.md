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

## D-072 — Per-App Worker Image And Requalification

- Status: APPROVED
- Date: 2026-09-28
- Decision: 대상 app 마다 pinned worker base 위에 app 의존성을 설치한 image 를 만들고 digest 로 pin 한다.
  image digest 가 바뀌면 그 image 로 Codex 를 다시 자격 측정한 뒤에만 등록한다.
- Reason: agent 와 verifier 가 app 테스트를 돌리려면 의존성이 필요하다. driver 는 자격을 받은 환경에서만 작업을
  받는다 (`src/amplai_foundry/runtime/execution/service.py:136-137`).
