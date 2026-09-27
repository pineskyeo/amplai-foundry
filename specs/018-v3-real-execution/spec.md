# Work 018 — V3 Real Execution (Mac, single operator)

**Work ID**: AMPLAI-V3-EXEC01 · **Created**: 2026-09-28 · **Type**: new_feature (architecture)

## Why

V3 는 `final_declared` 이지만 사용자 목표가 실제 agent 로 실행되는 경로가 없다.

- 설계는 실제 driver 실행을 범위에 넣었다: V3-026 "Codex CLI qualified baseline driver",
  V3-057 end-to-end, T-107 "qualified baseline driver" (`design-reference/implementation/tasks.yaml:873,2168`,
  `design-reference/eval/test-catalog.json:2333`).
- 그 항목들은 RecipePort·FakeContainer·MockTransport 로 `local_pass` 처리됐다
  (`tests/v3/test_rc01_e2e_cross_app.py:19,31`, `tests/v3/test_rc02_driver.py:27-51`).
- 설계는 이를 금지한다: "fake driver는 실제 Claude/Codex provider conformance를 대체하지 않는다",
  "baseline driver/runtime/meta 핵심은 stub으로 닫지 않는다" (`design-reference/design/21_TEST_ACCEPTANCE.md:31,35`).
- `amplai work` 는 intent 를 저장만 하고 (`tests/v3/test_api.py:59-76`), `amplai ops serve` 는 API 만 띄운다
  (`src/amplai_foundry/runtime/cli.py:259-291`). DriverRegistry 에 등록되는 것은 RecipePort 뿐이다.

이 Work 는 그 구멍을 실제 driver 로 닫는다 (D-065).

## Goal

pinesky 가 이 Mac 에서 목표 한 줄을 주면 AMPLAI 가 계약을 만들고, 사람이 승인한 뒤, qualified Codex CLI 가
ContainerSandbox(egress profile) 안에서 작업하고, 계약의 테스트로 검증된 결과만 draft PR 로 돌려준다.

```text
amplai work "…" --app amplai-foundry     → 계약 초안 (acceptance, 바꾸는 것/안 바꾸는 것, 테스트)
amplai approve <goal>                     → 사람 승인 = execution grant + publish grant
(worker) Codex in container → patch → 테스트 in container → 실패 시 수정 (≤3회, ≤30분)
goal.verified → branch amplai/<goal> push → draft PR
amplai status | cancel | result <goal>
```

## Operator Decisions (2026-09-28, pinesky)

| 항목 | 결정 |
|---|---|
| 결과 반영 | draft PR 까지 자동 (branch push + `gh pr create --draft`). merge 는 사람 |
| 예산 | 30분, 시도는 설계값(첫 시도 포함 3번 = 수정 2회, OD-1 2026-09-28). 넘으면 멈추고 보고 |
| 시작 승인 | 매번 승인 (계약 초안 확인 후 `approve`) |

## Scope

- 이 Mac (macOS, colima+docker), 단일 운영자, 대상 app 은 등록된 로컬 git repo (첫 대상: amplai-foundry).
- driver: Codex CLI 0.155.1 + model gpt-5.6-sol (Work 016 컨테이너 자격 9/9), ChatGPT 계정 credential 의
  사용자 생성 scoped 사본.
- node 하나 = write 하나 (`bounded_loop`). 여러 node 병렬은 이후.

## Non-goals

회사 Windows 환경, 다른 driver 자동 선택(Claude CLI/OpenCode/Responses), Slack 접수, 원격 worker,
여러 사용자, 자동 merge/배포, OpenAI API key 기반 planner.

## Requirements

- **R1 실제 실행**: 활성화된 goal 의 node 는 DriverRegistry 에 등록된 qualified `codex-cli` port 로만 실행된다.
  RecipePort/fixture 는 production deployment 에 등록되지 않는다.
- **R2 격리**: agent 는 Work 016 egress profile 과 resource limit 을 가진 ContainerSandbox 안에서만 돈다.
  workspace 는 base commit 의 사본이며 실제 checkout·`.git`·사용자 HOME 은 mount 되지 않는다.
- **R3 계약**: 계약 초안은 read-only Codex planner 가 만들고 기존 PlanningService 검증(스키마, 설치된 verifier,
  권한 상한, critic)을 통과해야 한다. 검증 command 는 app 에 설치된 allowlist 에서만 고른다.
- **R4 승인**: execution grant 는 인증된 human operator 가 정확한 contract/graph/capabilities 에 대해 내린 승인
  기록에서만 발급된다. publish(push, draft PR) 권한도 같은 승인에 명시적으로 포함된다.
- **R5 신뢰 가능한 결과**: 변경 결과는 agent 가 쓴 파일이 아니라 host 가 container 종료 후 계산한 patch 다.
- **R6 검증**: patch 를 새 base 사본에 적용해 container(network none) 안에서 계약 test command 를 실행하고,
  모든 mandatory acceptance 가 pass 여야 `goal.verified` 다.
- **R7 예산**: 시도는 첫 시도 포함 최대 3번(`max_verifier_repair_attempts_including_initial`), 벽시계 30분. 초과 시 goal 은 held/failed 로 멈추고 이유를 보고한다.
- **R8 publish**: `goal.verified` 뒤에만 `amplai/<goal_id>` branch 를 base commit 위에 만들고 push 후 draft PR.
  main 과 사용자 working tree 는 절대 수정하지 않는다. 같은 goal 의 publish 는 멱등이다.
- **R9 안전**: 취소·driver 실패·process crash 어느 시점에도 main, 사용자 working tree, 다른 branch 가 바뀌지 않고
  container 와 임시 디렉터리가 정리되거나 reap 된다.
- **R10 관측**: `amplai status/result` 가 상태, attempt, 검증 증거, 비용(usage), PR URL, 실패 이유를 보여준다.

## Acceptance

- **A1**: `amplai work` → `approve` → draft PR 이 실제 Codex 로 끝까지 동작한다 (실제 run evidence).
- **A2**: 이 repo 의 작은 실제 목표 3~5개를 돌려 성공/실패/usage/시간을 기록한다. 실패도 사실대로 남긴다.
- **A3**: 취소, verifier 실패 초과, driver crash, serve 재시작 시 R9 를 만족한다 (시험).
- **A4**: deterministic in-process end-to-end 시험(실제 CliPort·git workspace·command verifier·publish 를
  local bare remote 로)이 default suite 에 있고, 실제 docker 를 쓰는 e2e 가 `container` marker 로 있다.
- **A5**: verifier profile v2 PASS, ruff 0, mypy 0, docs validate RESOLVED.
- **A6**: 사용법 한 장 (`docs/v3/USING_AMPLAI_WORK.ko.md`).

## Human Gates

- Codex credential scoped 사본 생성은 사용자만 한다 (`scripts/sandbox_up.sh --codex-home DIR`).
- 매 goal 의 승인, draft PR merge 는 사람이다.
- 이 Work 의 branch push/PR 은 사용자 승인 후.
