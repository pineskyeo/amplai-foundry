# Work 018 Plan — V3 Real Execution

근거 조사: 실행 경로 지도와 범위 확인 (2026-09-28, 이 plan 의 file:line 인용은 HEAD `5b45155` 기준).

## 1. Architecture

```text
amplai CLI ──HTTP──▶ amplai ops serve (single process, single store owner)
                      ├─ API (existing)                 intents / plan / approve / status / cancel
                      ├─ PlanningService (existing)     planner = CodexPlanner (new, read-only container turn)
                      ├─ LocalOperatorAuthority (new)   approval record → decision resolver → grant
                      ├─ ExecutionLoop thread (new)     claim → WorkCoordinator.execute → verify → finish
                      │     ├─ DriverRegistry: codex-cli CliPort(CodexCliDriver) (existing, now registered)
                      │     ├─ GitWorkspaceManager (new) base commit → workspace → trusted patch
                      │     └─ PatchCommandVerifier (new) fresh base + patch → test argv in container
                      └─ PublishEffect (new)            goal.verified → branch + push + draft PR
```

store 는 single-owner (`runtime/storage/store.py:164-171`) 이므로 worker 는 serve 프로세스 안의 thread 다 (D-066).
LLM/network 호출은 DB transaction 밖에서만 한다 (`design-reference/design/07_RUNTIME_SCHEDULER.md:24`).

## 2. Decisions (docs/workstreams/v3-real-execution/DECISIONS.md)

| ID | 결정 |
|---|---|
| D-065 | V3 실행 완료 판정은 실제 qualified driver 경로로만 닫는다. fixture 로 닫힌 T-107/V3-026/V3-057 은 이 Work 의 실제 run evidence 로 재판정한다 |
| D-066 | worker 는 `amplai ops serve` 안의 thread. 원격 worker·HTTP worker route 는 이후 |
| D-067 | 첫 production driver 는 Codex CLI 0.155.1 / gpt-5.6-sol, ChatGPT 계정 scoped credential 사본을 per-dispatch native home 에 seed |
| D-068 | workspace 는 base commit 의 `git archive` 사본 (`.git` 제외, dotfile 포함). 결과는 host 가 임시 index 로 계산한 patch (`text/x-diff`) 하나 |
| D-069 | planner 는 read-only container Codex turn (`--output-schema`). OpenAI API key 없이 동작. 기존 PlanningService 검증을 그대로 통과해야 한다 |
| D-070 | local operator authority: 인증된 `kind=human` 운영자의 승인 기록이 decision 이다. 정확한 contract/graph ref 와 capabilities 에 묶이고 revoke 가능하다. deployment config 로 명시적으로 켠다 |
| D-071 | publish 는 `goal.verified` 뒤 governed effect. branch `amplai/<goal_id>`, push, draft PR. main·working tree 는 건드리지 않는다 |
| D-072 | per-app worker image = pinned worker base + app 의존성. image digest 가 바뀌면 Codex 를 그 image 로 재자격한다 |

## 3. Open Decision

- **OD-1 수정 횟수**: 운영자는 "수정 3회"(총 4 attempt)를 골랐다. 승인 설계 기본값은
  `max_verifier_repair_attempts_including_initial: 3` (`src/amplai_foundry/runtime/contracts/data/runtime-defaults.json:10`)
  이라 총 3 attempt(수정 2회)가 상한이다. 선택지: (a) 설계값 유지, (b) DESIGN_CHANGE_PROPOSAL 로 4 로 올림.
  답을 받기 전까지 (a) 로 구현하고 S7 에서 확정한다.
  **해결 2026-09-28: (a) 설계값 유지** — 총 3 attempt, 벽시계 30분 (`max_wall_seconds: 1800`).

## 4. Slices

| Slice | 내용 | 검증 |
|---|---|---|
| S1 | kind 불일치(`verification-profile` vs `verifier-profile`, `deployment.py:277`, `server.py:648` vs `goals/validation.py:55`) 수정; codex argv 에 `--skip-git-repo-check` (qualified argv `scripts/container_qualify.py:140` 와 일치); worker 가 collect 뒤 container destroy | unit + 기존 suite |
| S2 | `GitWorkspaceManager` (`snapshot/materialize/collect/assert_matches`, `worker.py:170,239,308,359` 가 호출하는 duck type) | unit: dotfile 포함, `.git` 제외, symlink/경로 탈출 거부, patch 정확성, 대용량 상한 |
| S3 | `deployment/worker-app/<app>` image = worker base + `pip install -e .[dev]`; digest pin; `container_qualify.py --driver codex --image` 재자격 | 실제 docker, 9 probe |
| S4 | codex composition: 실제 store record (environment/qualification/driver-capabilities/model-profile/composition/verifier-profile/app-binding) + `CliPort` 등록 + credential seed | unit + container marker |
| S5 | `PatchCommandVerifier`: 새 archive 에 `git apply --check`/`apply`, allowlist argv 를 container(network none)에서 실행, junit/exit 판정, per-run 이름·정리; global verifier | unit(FakeContainer) + container |
| S6 | `CodexPlanner`(StructuredPlanner) + reviewer; planning config 가 OpenAI 없이 켜짐; `LocalOperatorAuthority` + approve API | unit + 실제 1회 |
| S7 | `ExecutionLoop`: claim → execute(prompt builder: objective, acceptance, 실패 verdict 요약) → verify → finish_work → 재시도 → finish_goal; cancel; 벽시계 30분 | in-process e2e |
| S8 | `PublishEffect`: 임시 worktree 에서 base 위 patch commit → push → `gh pr create --draft`; 멱등 key = goal_id+patch digest | local bare remote e2e, gh 는 실제 1회 |
| S9 | CLI `work/approve/status/cancel/result`, 사용법 문서 | CLI 시험 |
| S10 | default-suite in-process e2e, container e2e, 실제 Codex 3~5 goal, 안전 시험, verifier, docs, review | A1~A6 |

## 5. Failure And Safety

| 상황 | 동작 |
|---|---|
| driver 실패/timeout | `WorkCoordinator` 가 `port.cancel` → container stop, work held (`worker.py:263-275`) |
| verifier 실패 | work ready(재시도) 또는 failed (`verification/runtime/service.py:545-565`); 같은 failure signature 3회면 중단 |
| 취소 | goal cancel → lease 무효화 → 실행 중 port.cancel → container 제거; publish 없음 |
| serve crash | 재시작 시 store owner epoch 증가로 이전 lease 무효 (`service.py:330`); 남은 `amplai-run-*` container 는 시작 시 reap |
| patch 적용 실패 | verifier fail (증거에 `git apply --check` 출력) |
| publish 실패 | effect `unknown` → reconcile 로 remote branch/PR 존재 확인 후 재시도; 중복 PR 없음 |
| 사용자 파일 | 모든 쓰기는 `~/.amplai/work/<goal>` 아래 임시 경로와 새 branch 뿐. 사용자 checkout 과 main ref 는 읽기만 |

## 6. Environment

macOS arm64, colima + docker 29.5, 이미지 digest pin, egress profile `local-colima`(Work 016/017 자격 pass),
Python 3.11 `.venv`. workspace root 는 `$HOME` 아래 실경로 (colima 공유, symlink 금지).

## 7. Validation

`.venv/bin/python .ai-team/verifiers/run.py --profile v2`, `pytest -m container tests/v3/test_rc06_*`,
실제 run evidence 는 `specs/018-v3-real-execution/runs/` 에 기록한다.
