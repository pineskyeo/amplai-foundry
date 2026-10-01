# AMPLAI 로컬 실행 사용법 (Mac, 단일 운영자)

목표 한 줄을 주면 AMPLAI 가 계약 초안을 만들고, 운영자가 승인하면 Codex 가 격리된 컨테이너에서
작업하고, 계약의 테스트로 검증된 결과만 draft PR 로 돌아온다 (Work 018·019, D-065..D-082).

## 한 번만 하는 준비

순서대로 실행한다. 경로는 amplai-foundry checkout 기준이다.

1. 컨테이너 런타임과 egress 를 올린다: `scripts/sandbox_up.sh`, `scripts/sandbox_up.sh --egress`
2. 대상 app 의 worker image 를 만든다: `scripts/app_image_up.sh <app> <repo 경로>`
   → `deployment/local-container-app-<app>.json`
3. Codex credential 의 scoped 사본을 **운영자가 직접** 만든다 (agent 는 credential 을 복사하지 않는다):
   `scripts/sandbox_up.sh --codex-home ~/.amplai-sandbox-probes/codex-home`
4. 그 image 에서 Codex 를 자격 측정한다 (image 가 바뀔 때마다 다시):

   ```bash
   .venv/bin/python scripts/container_qualify.py --driver codex \
     --codex-home ~/.amplai-sandbox-probes/codex-home \
     --container-profile deployment/local-container-app-<app>.json \
     --out specs/018-v3-real-execution/driver-qualification-<app>.json \
     --artifacts specs/018-v3-real-execution/artifacts
   ```

   9개 probe 와 `tool_use`(실제 turn 에서 셸 실행과 파일 쓰기)가 모두 pass 가 아니면 serve 가 기동을
   거부한다 (`DRIVER_UNQUALIFIED`, D-073).
5. 설정·키·운영자 token 을 만든다 (모두 0600):

   ```bash
   .venv/bin/amplai ops local-init --app <app> --repo <repo 경로> \
     --codex-home ~/.amplai-sandbox-probes/codex-home \
     --container-profile deployment/local-container-app-<app>.json \
     --qualification-report specs/018-v3-real-execution/driver-qualification-<app>.json \
     --verifier 'unit=python -m pytest -q -p no:cacheprovider tests/v3 | v3 unit tests' \
     --verifier 'lint=ruff check src tests | ruff lint'
   ```

   `--verifier` 목록이 이 app 의 acceptance suite 다. 모든 acceptance 는 이 suite 하나에 묶이고, 변경은
   목록의 **모든** 명령을 통과해야 한다. 그래서 주장한 acceptance 밖의 회귀도 잡힌다.

### 설정을 바꾸는 명령

| 명령 | 하는 일 |
|---|---|
| `amplai ops local-claude --token-file F --qualification-report R` | Claude CLI 를 후보로 추가한다. token 파일(0600)은 운영자가 만든다 |
| `amplai ops local-opencode --credential-home D --qualification-report R` | OpenCode 를 세 번째 후보로 추가한다(계획은 하지 않는다). D 는 운영자가 만든 scoped 사본, R 은 그 app image 에서 통과한 자격 기록이다 (D-095) |
| `amplai ops local-driver <codex\|claude\|opencode> --enable/--disable` | 그 driver 를 선택 후보에서 넣거나 뺀다 |
| `amplai ops local-add-app --app A --repo …` | app 을 추가한다 (최대 4개) |
| `amplai ops local-verifier --app A --verifier 'id=command \| 설명'` (`--remove id`) | app 의 acceptance 명령을 추가·교체(같은 id 는 같은 자리)·삭제한다. 새 goal 부터 적용된다. CI 가 보는 검사(예: `format=ruff format --check .`)를 같이 둬야 검증된 변경이 CI 에서 떨어지지 않는다 |
| `amplai ops local-integration --id I --app A --app B --command …` | 여러 app goal 의 integration 명령을 등록한다. app 들은 `/amplai-input/apps/<app>` 에 읽기 전용이다 |
| `amplai ops local-update` | server 설치본 checkout 을 remote branch 로 fast-forward 하고 launchd agent(`ai.amplai.local-serve`)를 재시작한다. local 변경이 있으면 거부한다 (D-086) |

설정을 바꾼 뒤에는 `local-serve` 를 다시 시작한다.

**상시 실행 (Mac)**: 재부팅 뒤에도 쓰려면 세 가지가 로그인 때 떠야 한다.

- colima: `brew services start colima`(Homebrew 가 `colima start -f` 를 launchd 로 띄운다).
- registry 와 egress proxy: `scripts/sandbox_up.sh` 가 `--restart unless-stopped` 로 만든다. 그래서 docker 와 함께 돌아온다. proxy 는 `deployment/egress` 를 bind mount 하므로, 지워질 수 있는 작업 worktree 가 아닌 상시 checkout 에서 `--egress` 를 실행한다.
- `local-serve`: 상시 checkout 의 venv 로 도는 launchd agent(`ai.amplai.local-serve`, `RunAtLoad`·`KeepAlive`)다. main 이 바뀌면 `amplai ops local-update` 로 갱신한다.

## 매번 쓰는 흐름

```bash
.venv/bin/amplai ops local-serve &            # API + 실행 루프 (loopback)
export AMPLAI_TOKEN_FILE=~/.amplai/local/operator.token
amplai work "로그인 실패 메시지를 한국어로 바꿔줘" --app <app>
```

`amplai work` 는 목표를 제출하고, 시스템이 고른 agent 가 base commit 의 사본을 **읽기만 해서** 만든 계약 초안
(objective, 범위, 하지 않을 것, acceptance 와 검증 명령, 위험도)을 보여준다. 질문이 있으면 계약을
만들지 않고 질문을 보여준다. 초안과 함께 수정 전 base 에서 suite 를 한 번 돌린 결과(base check, commit
마다 한 번)를 보여준다. base 가 이미 실패하면 `WARNING` 이 나온다. 그 실패를 고치는 목표가 아니면 승인하지
않는다. 모든 시도가 같은 이유로 실패하게 된다.

| 명령 | 하는 일 |
|---|---|
| `amplai approve <goal>` | 초안을 승인한다. 이 승인이 실행 권한과 draft PR 권한의 유일한 근거다 |
| `amplai status [<goal>]` | 상태, attempt 별 검증 결과, PR 링크와 PR 상태 |
| `amplai steer <goal> "…"` | 실행 중인 agent 에 안내를 준다. checkpoint 에서 멈췄다가 같은 session 이 메시지를 받고 이어서 한다. 계약은 바뀌지 않는다 (D-082) |
| `amplai replan <goal> "…"` | 실행 중인 goal 의 계획을 바꾼다. 멈추고, 다음 contract revision 을 만들고, 승인을 기다린다 (D-082) |
| `amplai cancel <goal>` | 승인을 철회하고 멈춘다. 실행 중 컨테이너는 다음 확인에서 멈춘다 |
| `amplai design "…" --app <app>` | 설계 문서만 만든다. `specs/design/<goal>/design.md` 외에는 바꿀 수 없고 결과는 `[AMPLAI design]` draft PR 이다 (D-080) |
| `amplai ops pr-sync` | 열린 draft PR 의 상태(merged/closed/사람이 고침)를 지금 읽는다. 평소에는 10분마다 읽는다 (D-078) |

**agent 선택 (D-079)**: 운영자는 driver 를 고르지 않는다. 시스템이 자격·권한·데이터 등급으로 후보를 거르고,
작업 종류별 기본 정책(지금은 Codex 우선, Claude 는 fallback)으로 고른다. 초안의 `driver :` 줄이 선택과
제외 이유다. 승인 직전에 다시 확인해서 그 사이 바뀌었으면 다시 계획하게 한다.

**여러 app (D-081)**: `amplai work "…" --app A --app B` 는 app 마다 할 일과 순서(`after`)를 계획한다. 뒤 app 은
앞 app 의 검증된 patch 를 보고 작업한다. 각 app 은 자기 suite 로, goal 전체는 운영자가 등록한 integration
명령으로 함께 검증한다. app 마다 draft PR 이 생기고 서로 링크된다. 한 goal 의 app 들은 같은 worker image 를
써야 한다.

승인 뒤 일어나는 일:

1. `main`(설정의 `base_branch`) commit 의 사본이 컨테이너에 들어간다. 이 commit 은 계획할 때 remote 에서 새로
   가져온 branch 의 commit 이다. remote 가 없는 repo 는 local branch 를 쓴다 (D-086). 사본은 그 commit 하나만 가진 새 local
   git repo 다. 원래 repo 의 `.git`(remote, 다른 branch, 이력), 사용자 HOME, 실제 checkout 은 들어가지
   않는다. 인터넷은 egress allowlist 로만 나간다.
2. 선택된 agent(Codex 또는 Claude)가 작업한다. 끝나면 host 가 변경을 patch 로 계산한다 (agent 가 쓴 파일을
   믿지 않는다).
3. 새 사본에 patch 를 적용하고 네트워크 없는 컨테이너에서 acceptance 명령을 실행한다.
4. 실패하면 실패 출력과 함께 다시 시도한다. 첫 시도 포함 최대 3번, 승인부터 30분.
5. 모두 통과하면 `amplai/<goal>` branch 를 push 하고 draft PR 을 만든다. merge 는 사람이 한다.

## 메타하네스: 구현자 prompt 를 시험해 바꾸기 (`amplai meta`)

구현자(IMPLEMENTER) prompt 를 바꾼 후보를 실제 driver 로 시험하고, 나쁘지 않을 때만 새 goal 에 쓰게 한다.
각 단계는 운영자가 따로 내리는 명령이다. 여러 단계를 한 번에 돌리는 명령은 없다. 제안은 `amplai-meta-proposer`
신원이 하고, 나머지는 사람 운영자로 실행한다. 제안자는 검토·승인·승격을 못 한다.

명령은 배포를 프로세스 안에서 직접 연다. 그래서 `amplai ops local-serve` 를 멈추거나 별도 배포
(`amplai ops local-init --home ~/.amplai/local-smoke ...`)에서 실행한다. 두 프로세스가 한 store 를 열면 안 된다.

| 순서 | 명령 | 하는 일 |
|---|---|---|
| 1 | `amplai meta propose --suffix S --prompt-file F --hypothesis … --benefit … --observation … --risk …` | 후보 prompt(파일의 각 줄)를 제안한다. `{app_id}`, `{base_commit}` 외의 자리표시자는 거절한다 |
| 2 | `amplai meta screen ID` | 보호된 표면을 건드리지 않았는지 검사한다 |
| 3 | `amplai meta approve-experiment ID --max-tokens N --max-wall-seconds N` | corpus 와 사전 등록 분석(비열등성, 성공률만)을 고정하고 실험을 승인한다 |
| 4 | `amplai meta run-experiment ID --per-trial-tokens N --basis … --evidence …` | 모든 시도를 실제 goal 로 돌리고(게시는 끔) 판정을 기록한다 |
| 5 | `amplai meta approve-canary ID --tasks a,b,c --max-trial-tokens N` | 낮은 위험 과제 몇 개로 canary 를 승인한다(시작하지는 않는다) |
| 6 | `amplai meta run-canary ID …` | canary 를 시작하고 그 과제를 후보로 돌린다. 모두 통과하면 승격을 요청한다 |
| 7 | `amplai meta promote ID` | 활성 release 를 후보로 옮긴다. 새 plan 부터 후보 prompt 를 쓰고 실행 중인 goal 은 그대로다 |
| | `amplai meta rollback ID` | 활성 release 를 승격 전 release 로 되돌린다 |
| | `amplai meta reject ID --reason …` / `abort ID --reason …` / `status ID` | 후보를 끝낸다 / 승인된 실험·canary 를 멈춘다 / 상태를 본다 |

알아 둘 점:

- 실제 Codex 시도는 한 번에 입력 약 9만 token 을 쓰고 40 시도는 30분 안팎이 걸린다(측정). 구독 사용량이 든다.
- 이 실험이 보이는 것은 **성공률이 나빠지지 않았다**(비열등성, margin 0.25, 신뢰도 0.95)이다. 성공률이 올랐다고
  주장하지 않는다. baseline 이 corpus 20개를 모두 통과했기 때문이다 (D-093).
- 시도 하나라도 답을 못 내면(driver 오류 등) 판정은 `inconclusive` 다. 실패로도 통과로도 세지 않는다.
- canary 과제는 운영자의 실제 프로젝트 goal 이 아니라 corpus 과제를 실제 goal 로 돌린 것이다. 비용은 비교하지 않고
  (D-092) run 수·token·시간으로 상한을 건다.
- canary 는 시작한 프로세스만 이어받는다. `run-canary` 가 도중에 끊기면 그 canary 는 이어 갈 수 없고 `abort` 로 끝낸 뒤
  후보를 새로 제안해야 한다(첫 실제 실행에서 확인했다).
- 모든 승인은 그 행동과 대상에 묶인 기록이고 사람 운영자만 발급한다. 후보가 통과하지 못하면 다음 단계 명령이 거절된다.

### 비용 환산과 참고 지표 (D-094)

- run 마다 캐시·추론 token 까지 기록한다. `amplai ops prices` 는 적용 중인 단가표와 설정된 모델에 단가가 있는지
  보여 준다. 단가표는 `deployment/prices/` 의 날짜별 파일이다. 단가가 바뀌면 파일을 고치지 않고 적용 시작일이
  뒤인 새 파일을 추가한다. 출처 URL 을 보고 사람이 반영한다.
- `amplai meta report ID` 는 실험의 참고 지표를 보여 준다: 성공 수, 검증 통과·숨긴 시험 실패 수, 새 시험 추가와
  기존 시험 수정·삭제, 성공 1건당 token·시간·**API 환산 비용**, 변경 크기. 판정은 바꾸지 않는다.
- API 환산 비용은 "API 로 썼다면 이만큼"이다. 구독 청구액이 아니다. 캐시 세부가 없는 옛 run 은 상한으로 표시된다.
- `scripts/corpus_check.py --repeats 3` 은 과제 채점이 매번 같은지 본다.

## 메타하네스 운영 (Work 033)

위 `amplai meta propose`~`rollback` 은 구현자 prompt 하나를 시험하는 Work 030 흐름이다. Work 033 은 harness 를 cell(driver × model × effort)과 component manifest 로 나누고, calibration 과 단계별 실험(screening, focused, ablation, holdout), 야간 loop, dashboard 를 더했다. 설계는 `docs/v3/DEV03_OBSERVATORY_META.ko.md` §10 에 있다. IC-15~IC-21, IC-23~IC-29, IC-31, IC-32 는 잠정(provisional) 결정이고 IC-30 은 미결정이다. IC-22 는 `interfaces.md` §0 결정이며 확정 여부는 확인 필요다. 야간 loop 를 켜기 전에 운영자가 확인해야 한다.

### 별도 meta deployment

- 메타하네스 명령은 서버와 다른 deployment 에서 돌린다. store 의 소유자는 하나뿐이다. 서버를 쓰는 중에 같은 store 를 열지 않는다.
- **`--config` 를 받는 모든 `amplai meta` 명령에 meta deployment 의 `--config` 를 붙인다.** `meta corpus check` 와 `meta corpus import-work030` 에는 `--config` 가 없다. 두 명령은 설정을 읽지 않고, `--corpus` 를 생략하면 checkout 의 `specs/033-harness-taxonomy/corpus` 를 쓴다(`runtime/meta_commands/corpus.py:57,127`, `runtime/meta_commands/__init__.py:83-90`). 다른 corpus 를 쓰려면 `--corpus <corpus v2 root>` 를 준다. `--config` 를 생략하면 기본값은 지금 돌고 있는 서버의 `~/.amplai/local/local.json` 이다. `meta nightly …` 와 `meta quota` 는 기본값이 `~/.amplai/meta/local.json` 이다(`--help` 로 확인). 야간 명령에 서버 설정을 주면 `NIGHT_DEPLOYMENT` 로 거절한다.
- 아래 예에서 `M=~/.amplai/meta/local.json`, `A=.venv/bin/amplai` 로 쓴다. deployment 만드는 순서(저장소, worker image, credential 사본, qualification, `ops local-init`)는 `specs/033-harness-taxonomy/runs/pilot-runbook.md` §1 이다.
- cell 의 model 은 qualification report 가 있어야 한다. report 가 없으면 `DRIVER_UNQUALIFIED` 이다.

### 명령 순서

옵션의 정확한 이름은 `amplai meta <명령> --help` 로 확인한다. 표의 옵션은 주요한 것만 적었다.

| 순서 | 명령 | 하는 일 |
|---|---|---|
| 1 | `$A ops local-cell add --config $M --driver codex-cli --model … --effort …` / `list` | cell 을 등록한다. 문서에 없는 effort 는 `EFFORT_UNSUPPORTED` 로 거절한다 |
| 2 | `$A ops local-cell probe CELL --config $M` (task environment 마다 `--environment ID`) | effort probe turn 을 한 번 돌려 기록한다. probe 가 없는 effort cell 은 설치되지 않는다(`EFFORT_UNPROBED`). docker 와 driver credential 이 필요하다 |
| 3 | `$A meta corpus check --repeats 3 --corpus …` | 모든 과제의 공정성(base 실패, reference 통과)을 본다 |
| 4 | `$A meta corpus import-work030 --source <Work 030 corpus> --corpus <corpus v2 root>` 후 `$A meta corpus freeze --set main --holdout-use-limit N --config $M` (`--set regression` 도) | Work 030 과제를 regression 과제로 들이고 set 을 동결한다. `--source` 와 `--corpus` 는 둘 다 선택이다(`--source` 는 repository 의 `specs/030-meta-harness-live/corpus` 가 기본값이다) |
| 5 | `$A meta evaluator status --config $M` / `propose-change` / `qualify-change` / `approve-change` / `requalify` | evaluator version 을 본다. 바꾸려면 change 를 올려 단독으로 qualification 하고 사람이 승인한다 |
| 6 | `$A meta calibrate --config $M --cells A,B --max-repeats 5 --max-trials N --max-tokens N --max-wall-seconds N --per-trial-tokens N --basis … --evidence … --parallel 2` | cell 의 과제별 통과율을 측정한다. 이 명령이 executor 자격(`per_trial_tokens`, `basis`, `evidence`)도 저장한다. 결과는 `amplai meta calibration show PLAN --config $M` |
| 7 | `$A meta proposer run --cell C --config $M` | proposer 가 component 후보와 예측을 초안해 draft 로 저장한다 |
| 8 | `$A meta review ID --outcome pass --note … --config $M` (class B 만) / `$A meta search ID --cell C --max-tokens N --max-wall-seconds N --per-trial-tokens N --basis … --evidence … --config $M` | search 가 계획하고 gate 없는 단계(screening)를 돌린 뒤 focused gate 에서 멈춘다. 상태는 `amplai meta stages ID --config $M`. 예측 채점은 `amplai meta proposer score ID` |
| 9 | `$A meta approve-stage ID --stage focused --config $M` (또는 `--queue [--digest D]`) | focused 를 이 프로세스에서 승인하고 돌린다. `--queue` 는 밤이 대기열에 올린 실험을 정확한 digest 로 승인만 하고 실행하지 않는다. 통과하면 ablation 이 자동으로 돌고 holdout gate 에서 멈춘다 |
| 10 | `$A meta approve-stage ID --stage holdout --config $M` | holdout 을 돌린다. 한 번 쓴다 |
| 11 | `$A meta approve-canary ID --tasks a,b --max-trial-tokens N --config $M` → `run-canary ID --config $M` → `promote ID --config $M` | corpus v2 proposal 의 canary 과제는 development 나 validation 과제만이다. 플래그 없는 `run-canary` 는 저장된 executor 자격을 쓴다(IC-29, provisional). 실패하면 canary 가 멈추고 release 는 그대로이며 `reject` 가 정상 결과다. 되돌리기는 `rollback ID` |
| 12 | `$A meta nightly approve --nights N --budget-trials B --max-tokens N --max-wall-seconds N --config $M` | 야간 standing approval 을 발급한다(최대 7밤) |
| 13 | `$A meta nightly print-agent --config $M` 의 출력을 `~/Library/LaunchAgents/` 에 복사하고 `launchctl bootstrap gui/$(id -u) <파일>` | launchd 로 매일 밤 돌린다. 자동 설치는 없다. 수동 실행은 `$A meta nightly run --config $M` (`--dry-run` 이면 trial 을 돌리지 않는다) |
| 14 | `$A meta nightly status --config $M` / `$A meta nightly revoke --config $M` | 그 밤의 상태와 standing approval 을 본다 / 철회한다(도는 밤은 다음 trial guard 에서 멈춘다) |
| 15 | `$A meta dashboard --out ~/.amplai/meta/dashboard --config $M` | 저장된 record 로 정적 HTML 을 쓴다(읽기 전용). approvals 페이지에 대기 중인 승인이 있다 |
| 16 | `$A meta quota --since YYYY-MM-DD --config $M` | pilot 밤의 quota 관측, 여유, 제안 B 를 본다 |
| | `$A meta reconcile ID --stage S` 또는 `--allocation A --tokens N --cost N --receipt F --config $M` | 끊긴 stage 실험이나 unknown 할당을 정리한다 |

보조 명령: `meta component list|show|add`, `meta decider fit|show|regret|decisions`, `meta judge list|label|qualify`, `meta trace list|show`, `meta proposer dream|sweep`, `meta evaluator quality|reject-change`, `meta status|abort|report ID`.

### 사람 운영자만 하는 일

- **credential 사본 만들기**(Codex home, Claude token 파일). agent 는 복사하지 않는다(D-091).
- 사람 승인이 들어가는 명령: `calibrate`, `search` 의 screen, `review`, `approve-stage`(`--queue` 포함), `approve-canary`, `run-canary`, `promote`, `rollback`, `reconcile`, `nightly approve`, `nightly revoke`, `evaluator propose-change|qualify-change|approve-change|requalify`, `corpus freeze`. 이 중 canary 네 명령은 사람이 아니면 `APPROVAL_HUMAN` 으로 거절한다. Claude Code 안에서는 프롬프트에 `! <명령>` 으로 운영자가 직접 친다. agent 가 사람 승인을 대신 쓰지 않는다.
- 야간 신원(`amplai-meta-nightly`)은 standing approval 이 허락한 development split 의 exploratory 실험, calibration, drift 점검만 돌린다. focused, holdout, canary, promote 는 하지 않는다. focused 는 낮에 `approve-stage --queue` 로 승인한 것만 다음 밤에 돈다. holdout 은 사람이 직접 돌린다.
- proposer 는 `proposer run` 으로 제안만 한다. 검토, 승인, 실행, promote 는 못 한다.
- agent 가 해도 되는 일: 승인 없는 준비(저장소와 image 만들기, 자격 측정, cell probe), 제안자 실행, dashboard, 결과 읽기. 이 구분은 pilot runbook 의 "원칙" 절을 따른다.
- 한계(IC-30 미결정): draft 의 screen 에는 사람의 review 권한이 필요해서, 밤에는 낮에 운영자가 screen 한 제안만 screening 과 ablation 을 돈다.

### Pilot

실제 pilot(cell 4개 calibration, 제안 한 바퀴를 holdout 과 canary 까지, 3일 밤)의 절차와 끝났다고 보는 기준은 `specs/033-harness-taxonomy/runs/pilot-runbook.md` 에 있다. trial 당 token 상한, TB2 image 용량, quota 신호의 기록 방식은 확인 필요이고 pilot 에서 답한다. 그 전에 이 문서의 수치를 근거로 예산을 정하지 않는다.

## 보장과 한계

- 사용자 checkout(현재 branch, index, 커밋 안 한 파일)과 `main` 은 어떤 경우에도 쓰지 않는다. branch 는
  plumbing 으로 만든 commit 을 push 만 하고, 원격에 다른 commit 이 있으면 덮어쓰지 않는다.
- Codex credential 은 run 마다 새 home 에 넣었다가 끝나면 지운다(steer 로 멈추면 회수했다가 이어갈 때 다시
  넣는다). 갱신된 token 만 사본에 되돌려 쓴다. Claude 는 운영자가 만든 0600 token 파일을 참조만 하고
  환경 변수 이름으로만 컨테이너에 넘긴다.
- **driver credential 은 agent 의 tool 이 읽을 수 있다고 본다 (D-091).** Claude token 은 컨테이너 env 에 있고 같은
  uid 에서 `/proc` 으로 읽힌다(측정). Codex `auth.json` 도 같은 구조다(추정, 미측정). 계정 revoke, egress
  allowlist, 산출물 leak scan 으로 영향을 줄일 뿐 credential 격리를 보장하지 않는다. credential broker 는
  Work 032 에서 성립 여부를 확인한다.
- 검증 명령은 설치된 목록에서만 고른다. 모델은 verifier, 권한, 예산을 정할 수 없다.
- 격리는 자격을 받은 컨테이너가 맡는다. 컨테이너 안에서는 Codex 자체 sandbox(bwrap)가 동작하지 않아
  Codex 를 `--dangerously-bypass-approvals-and-sandbox` 로 실행한다 (D-073). 계획 단계의 읽기 전용은
  워크스페이스 read-only mount 로 강제한다.
- 한 번에 goal 하나만 실행한다. 회사(Windows) 환경, 원격 worker, 여러 사용자는 아직 지원하지 않는다.
- steer 와 replan 은 attempt 가 실행 중일 때만 받는다. 승인 전 goal 을 바꾸려면 cancel 하고 다시 제출한다.
  steer 로 이어서 도는 turn 에도 다시 steer 하거나 replan 할 수 있다 (D-083).
- 취소·실패로 멈춘 goal 은 승인이 철회된 상태로 남는다. 다시 하려면 새 `amplai work` 를 연다.
- `local-serve` 는 시작할 때 기록을 맞춘다. 멈춘 goal 의 runtime 상태와 점유를 닫는다. 죽은 프로세스가
  남긴 실행 중 goal 은 실패로 끝낸다. 한 번도 시도하지 못한 승인 goal 은 다시 대기열에 넣는다.

## 지표

`amplai ops observatory` 가 실제 run 기록으로 계산한 지표를 보여준다 (D-074).

- 검증 성공률과 첫 시도 성공률
- 사람 개입(`question.asked`, `approval.*`, `steering.*`)
- draft PR 결과(`publication_outcomes`): 받아들여진 비율(merged/결정됨)과 사람이 고친 비율
- 승인까지 걸린 시간(`human_wait_ms`)과 승인에서 실행 시작까지의 대기(`queue_ms`), 각각 표본 수와 함께
- 실패 이유(`failure_reasons`)

모르는 값은 0 이 아니라 `null` 이다. Codex 는 금액을 보고하지 않고 Claude 의 `total_cost_usd` 는 아직
기록하지 않으므로 cost 는 unknown 이다. 초안의
`class:` 가 작업 종류(`task_class`, D-076·D-077)이고 `--task-class` 필터로 종류별로 볼 수 있다.
