# Work 033 Pilot Runbook (S16)

작성: 2026-10-01. 대상: 운영자 1인(Mac, 로컬). 목적: AC-03, AC-05, AC-07, AC-08, AC-09, AC-11 을 실제 실행으로
닫는다 — 칸 4개 calibration, 제안 하나를 screening → focused → holdout → canary → promote(또는 정당한 reject)까지,
그리고 3일 밤의 야간 루프.

## 원칙

- **운영자만 하는 일**: credential 사본 만들기, 사람 승인이 들어가는 명령(`calibrate`, `search` 의 screen,
  `approve-stage`, `approve-canary`, `run-canary`, `promote`, `rollback`, `reconcile`, `nightly approve`,
  `evaluator approve-change`, `corpus freeze`). agent 는 사람 승인을 대신 쓰지 않는다(IC-17).
  Claude Code 안에서 실행하려면 프롬프트에 `! <명령>` 으로 운영자가 직접 친다.
- **agent 가 해도 되는 일**: 승인이 없는 준비(저장소·이미지 만들기, 자격 측정, cell probe, 제안자 실행,
  대시보드, 결과 읽기).
- **서버와 분리**: `--config` 를 받는 meta 명령에는 모두 `--config ~/.amplai/meta/local.json` 을 붙인다. 대부분은
  생략하면 지금 돌고 있는 서버의 `~/.amplai/local/local.json` 이 기본값이다(`nightly`, `quota` 는 meta 설정이 기본값이고,
  서버 설정을 거부한다: `NIGHT_DEPLOYMENT`). `meta corpus check` 와 `meta corpus import-work030` 은 `--config` 가 없어
  `--corpus <corpus v2 root>` 를 직접 준다.
- 아래에서 `M=~/.amplai/meta/local.json`, `A=.venv/bin/amplai` 로 쓴다. 경로는 amplai-foundry checkout 기준이다.

## 0. 먼저 정할 것 (운영자)

| 항목 | 기본안 | 근거 |
|---|---|---|
| 칸 | `codex-cli` gpt-5.6-sol medium, high; `claude-cli` claude-sonnet-5 high, claude-opus-5-5 high | OD-12 |
| calibration 규모 | bench 앱 dev+validation 49과제 × 4칸 × 1회, 엇갈린 과제만 최대 5회 | OD-11, 과제 수: 자체 71 − holdout 22 |
| 야간 | 3일, 밤마다 150 trial, 남는 여유의 절반 이상은 운영자 몫 | plan.md §10.5 |
| trial 당 token 상한 | 확인 필요 — 첫 calibration 몇 trial 의 실측으로 정한다 | 추정하지 않는다 |
| TB2 | docker image 를 과제마다 받는다(용량 확인 필요). 받을지 운영자가 정한다 | §10.5 |

## 1. 별도 meta deployment

1. bench 앱 저장소 (agent 가능):
   `.venv/bin/python scripts/corpus_base_repo.py --base bench --dest ~/.amplai/repos/amplai-bench-app --verify`
2. worker image (agent 가능, docker): `scripts/app_image_up.sh amplai-bench-app ~/.amplai/repos/amplai-bench-app`
   → `deployment/local-container-app-amplai-bench-app.json`
3. **credential 사본 (운영자 직접)**: Codex `scripts/sandbox_up.sh --codex-home ~/.amplai-sandbox-probes/codex-home-meta`,
   Claude 는 `CLAUDE_CODE_OAUTH_TOKEN=...` 한 줄짜리 0600 파일(`ClaudeEntry.token_file`, `local_deployment.py:167`).
4. 자격 측정 (agent 가능, 운영자의 사본 사용):
   `.venv/bin/python scripts/container_qualify.py --driver codex --codex-home ~/.amplai-sandbox-probes/codex-home-meta --container-profile deployment/local-container-app-amplai-bench-app.json --out specs/033-harness-taxonomy/runs/qualification-bench-codex.json --artifacts specs/033-harness-taxonomy/runs/artifacts`
   (Claude 도 같은 방식, `--driver claude`.)
5. 설정 만들기 (운영자):
   `$A ops local-init --home ~/.amplai/meta --app amplai-bench-app --repo ~/.amplai/repos/amplai-bench-app --codex-home ~/.amplai-sandbox-probes/codex-home-meta --container-profile deployment/local-container-app-amplai-bench-app.json --qualification-report specs/033-harness-taxonomy/runs/qualification-bench-codex.json --verifier 'suite=python -m pytest -q -p no:cacheprovider | bench suite'`
   그다음 `apps` 에 demo 앱(회귀 과제용)을 더한다(`$A ops local-add-app --config $M ...`).
6. 칸 등록과 추론강도 probe (등록은 운영자, probe 는 agent 가능):
   `$A ops local-cell add --config $M --driver codex-cli --model gpt-5.6-sol --effort medium` (나머지 3칸도 같은 형식),
   `$A ops local-cell probe <cell_id> --config $M`. probe 를 통과하지 않은 강도는 거부된다(`EFFORT_UNPROBED`).
7. `$M` 의 `meta` 항목: `corpus_root`, `evaluator_version`, `max_parallel_trials` 2, `trace_capture` true,
   `nightly` (§12.2). `roles.proposer` 에 칸 2개(싼 칸, 강한 칸).

## 2. 과제 목록과 평가기 (운영자)

1. `$A meta corpus check --repeats 3 --corpus specs/033-harness-taxonomy/corpus` — 모두 공정(fair)인지 다시 확인.
2. `$A meta corpus import-work030 --corpus specs/033-harness-taxonomy/corpus` 후 `$A meta corpus freeze --set main --holdout-use-limit <n> --config $M`,
   `$A meta corpus freeze --set regression --holdout-use-limit <n> --config $M`.
3. 평가기 버전: `$A meta evaluator propose-change ...` → `qualify-change` (Q-suite 를 돌려 code digest 에 묶음)
   → `approve-change` (사람만, `evaluator.approve`).
4. TB2 를 쓰기로 했으면 (agent 가능, docker·network): `scripts/tb2_adapter.py scan/select/spec/admit/write`.
   채택 수가 정해지면 `splits.json` 을 고정한다(validation ≥ 24). 과제 환경마다 칸의 추론강도 probe 가 따로 필요하다:
   `$A ops local-cell probe <cell_id> --environment <environment_id> --config $M` (없으면 그 환경에서 그 칸은 빠진다).

## 3. Calibration (운영자)

`$A meta calibrate --config $M --cells <4칸> --max-repeats 5 --max-trials <상한> --max-tokens <예산> --max-wall-seconds <초> --per-trial-tokens <상한> --basis <근거> --evidence <파일> --parallel 2`

- 실행자 자격(IC-29)이 이 명령으로 저장된다. 이후 명령과 야간은 저장된 기록을 쓴다.
- 결과: `$A meta calibration ... --config $M`, `$A meta dashboard --out ~/.amplai/meta/dashboard --config $M`.

## 4. 제안 한 바퀴 (AC-07, AC-08, AC-11)

1. 제안자 (agent 가능): `$A meta proposer run --cell <칸> --config $M` → 제안과 예측 저장.
2. screen 과 자동 단계 (운영자): `$A meta search <proposal> --cell <칸> --max-tokens <예산> --max-wall-seconds 604800 --per-trial-tokens <상한> --basis <근거> --evidence <파일> --config $M`
   → screening(dev) 이 돌고 focused gate 에서 멈춘다. `$A meta proposer score <proposal>` 로 예측 채점 확인.
3. focused (운영자): `$A meta approve-stage <proposal> --stage focused --config $M` (낮에 바로 실행) 또는
   `--queue` (다음 밤에 실행). 통과하면 ablation 이 자동으로 돌고 holdout gate 에서 멈춘다.
4. holdout (운영자): `$A meta approve-stage <proposal> --stage holdout --config $M`.
5. canary 와 promote (운영자): `$A meta approve-canary <proposal> --tasks <dev/validation 과제 id> --max-trial-tokens <상한> --config $M`
   → `$A meta run-canary <proposal> --config $M` (실행자 자격은 저장된 기록에서 복원) → `$A meta promote <proposal> --config $M`.
   holdout 과제는 canary 과제가 될 수 없다(`CANARY_TASKS`). 실패하면 canary 가 멈추고 release 는 그대로다.
   그때 `reject` 가 정상 결과다(AC-11 "correct reject"). 되돌리기는 `$A meta rollback <proposal> --config $M`.

## 5. 야간 루프 3일 (운영자 승인 후 자동)

1. `$A meta nightly approve --nights 3 --budget-trials 150 --max-tokens <밤당> --max-wall-seconds <초> --config $M`
2. `$A meta nightly print-agent --config $M` 출력을 `~/Library/LaunchAgents/` 에 운영자가 복사하고
   `launchctl bootstrap gui/$(id -u) <파일>`. 수동으로는 `$A meta nightly run --config $M`.
3. 매일 아침: `$A meta nightly status --config $M`, 대시보드의 approvals 페이지(대기 중 확정 실험, reconcile 대기).
4. 3일 뒤 `$A meta quota --since <첫 밤> --config $M` 으로 다음 예산 B 를 정한다.
5. 멈추려면 `$A meta nightly revoke --config $M` (다음 trial guard 에서 멈춤).

## 6. 끝났다고 보는 기준

| AC | 기록 |
|---|---|
| AC-03 | 드라이버마다 실제 자격을 통과한 칸 1개 이상 |
| AC-05 | calibration summary: 칸별 성공률과 informative/saturated/flaky 분류 |
| AC-07 | 실제 `proposer run` 의 제안이 screening 에 도달하고 `predscore-<id>-screening` 이 저장됨 |
| AC-08 | `search` 가 선언한 예산 안에서 단계를 돌고 gate 마다 멈춤 |
| AC-09 | 실제 기록으로 만든 대시보드 |
| AC-11 | 제안 하나가 holdout 을 지나 canary → promote, 또는 정당한 reject; 대시보드에 보임 |

## 확인 필요 (파일럿에서 답한다)

- §14 Q7: TB2 과제의 test 실행 명령과 작업 디렉터리 (TB2 채점 `tb2_tests` 는 그 전까지 `success: null`).
- §14 Q4, Q14, Q15, Q17, Q5: Codex 재개 turn 의 usage 합산, Claude block 이름, 추론강도 probe, quota 신호 기록.
- trial 당 token 실측, TB2 image 용량.
