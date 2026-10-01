# DEV-03 — Observatory & Meta-Harness

상태: **3.0.0.dev3 개발 스냅샷**. 승인 설계의 관측·평가·개선 경계를 구현하고 로컬 시험으로 검증한 단계이며 전체 V3 또는 운영 자격 인증이 아니다. DEV-01/02 기능과 승인 스키마 3.0.0을 유지한다. 현재 요구사항의 완전한 인수 판정은 배포 ZIP의 `_v3_delivery/REQUIREMENT_TRACE.json`을 따른다. §1~§9 는 이 스냅샷과 Work 030 까지의 내용이다. Work 033(메타하네스 시스템)은 §10 에 있고, §6 의 로컬 연결 서술 중 일부는 §10.14 에서 대체로 표시했다.

## 1. 실행 경로

`RunRecord / evidence → scoped Observatory → frozen corpus / experiment plan → paired trials → immutable report → explicit canary → independent approval → signed promotion / rollback`

관측은 실행 상태를 바꾸지 않는다. 평가자가 임의의 점수만 보내 승격시킬 수 없다. 실행기는 서버에 등록한 자격·모드·상한으로 제한하고, 각 결과를 불변 trial 및 신뢰된 실제 실행 receipt에 연결한다. 하네스 개선은 모델의 추론문이나 비공개 사고과정을 저장하지 않는다.

주요 코드:

| 책임 | 구현 |
|---|---|
| 일관된 scoped 조회·집계 | `src/amplai_foundry/evaluation/observatory.py` |
| bounded metadata queue | `src/amplai_foundry/evaluation/telemetry.py` |
| corpus ACL·노출·오염 추적 | `src/amplai_foundry/evaluation/corpus.py` |
| 계획 동결·실험 실행·중단 복구 | `src/amplai_foundry/evaluation/service.py` |
| 별도 통계 판정 | `src/amplai_foundry/evaluation/analysis.py` |
| 엄격한 JSON receipt 검사 | `src/amplai_foundry/evaluation/receipts.py` |
| proposal 공통 비용·횟수·동시성 | `src/amplai_foundry/meta_harness/budget.py` |
| 검토·카나리·승격·롤백 | `src/amplai_foundry/meta_harness/service.py` |
| 실제 Runtime을 통한 로컬 재현 | `src/amplai_foundry/meta_harness/pipeline_reference.py` |

위 경로의 기준 디렉터리는 `src/amplai_foundry/`이다.

## 2. 관측 지표의 의미

`Observatory`는 tenant/project 범위에서 RunRecord, goal, verdict, effect와 조회 시점의 audit sequence를 일관된 snapshot으로 읽는다. composition/model/driver/repo/risk/task_class와 시간 범위를 구분한다. CLI는 자주 쓰는 필터를, Python/API는 전체 필터를 제공한다.

- 성공률의 단위는 attempt 수가 아니라 **서로 다른 goal**이다. 재시도 세 번은 목표 세 개가 아니다.
- 성공은 현재 계약·그래프에 연결된 실제 global verification이 있어야 한다. `state=verified` 문자열만 있는 손상된 head는 성공으로 세지 않고 integrity finding을 낸다.
- 완료 조건 coverage는 현재 계약의 mandatory acceptance와 attested verdict를 대조한다.
- 비용은 measured / estimated / unknown 및 currency별로 분리한다. 미보고를 0원으로 합치거나 서로 다른 통화를 더하지 않는다. 전체 확정 비용이 아닌 경우 그 한계를 노출한다.
- budget 정산에서 estimated usage 는 보고된 token 으로 정산하고(초과하면 `overrun`) 추정 비용은 기록만 한다. 추정 비용이 예약을 넘으면 `budget.estimate_over` 로 알리고 work 를 막지 않는다 (D-084).
- ChatGPT 계정으로 도는 Codex CLI run 의 비용은 추정하지 않고 unknown 으로 둔다. token 단가 과금이 아니기 때문이다 (D-085).
- 시간을 보고하지 않은 곳은 측정됐다고 가정하지 않는다. RunRecord의 wall time은 queue/compute/wait의 개별 실측값이 아니다.
- `human_wait_ms`와 `queue_ms`는 goal audit event에서 잰다 (D-074). human wait는 `approval.requested`에서 `approval.granted`까지, queue는 `approval.granted`에서 그 goal의 첫 run 생성까지의 중앙값이다. `*_samples`가 표본 수다. 양 끝이 없으면 표본이 아니고, 표본이 없으면 `null`이다. `compute_ms`는 측정하지 않으므로 `null`이다.
- 사람 개입은 `question.*`, `steering.*`, `approval.*` event 수다. 로컬 실행 흐름의 loop가 스스로 멈추며 하는 승인 철회는 사람 개입이 아니므로 기록하지 않는다.
- `publication_outcomes`는 로컬 실행 흐름이 게시한 draft PR 의 결과다 (D-078): 열림·merged·closed·사람이 고침(revised) 수, 받아들여진 비율(merged/결정됨)과 고친 비율. 운영자의 `gh` 로 읽은 전이만 세고, 읽지 못한 PR 은 결정되지 않은 것으로 둔다. 추적 전에 게시된 PR 의 열림 event 는 소급 기록이며 `backfilled` 로 표시된다.
- `failure_reasons`는 failure signature(hash)를 그 run의 attested verdict 이유로 풀어 보인다. `ended_run_reasons`는 멈춘 run의 종료 이유다. run head와 RunRecord 상태가 다르면 어느 쪽도 고르지 않고 `run_state_mismatch` integrity finding을 낸다.
- 실제 task_class가 없는 과거 기록은 `unreported`다. 실행 전략을 작업 종류라고 이름만 바꾸지 않는다. `task_class`는 3.0.0 workgraph schema 밖의 work별 `task-class` 기록으로 남는다(D-076, D-077). 로컬 실행 흐름의 planner가 repository Work type 중에서 제안하고 운영자가 승인 때 본다. 목록 밖 값과 그 이전 기록은 `unreported`다.
- 시간 필터는 timezone이 있는 ISO-8601, `[since, until)`를 쓴다. 잘못된 범위나 시간대 없는 값은 거절한다.
- metadata event export에는 scope, sequence, event identity/type/time만 포함한다. 원문 prompt, tool payload, credentials, chain-of-thought를 내보내지 않는다.

예시(미리 부여받은 token 파일과 승인된 control plane 필요):

```bash
export AMPLAI_URL=http://127.0.0.1:5083/
export AMPLAI_TOKEN_FILE=/secure/path/operator-token
amplai ops observatory --risk low
```

`TelemetrySpool`은 독립 SQLite queue에 count/byte 상한을 적용한다. export 실패 시 같은 event ID로 재시도할 수 있는 at-least-once 전달이다. exporter는 deduplicate해야 한다. queue overflow의 drop count와 export cursor는 남기되 **원본 authority audit는 삭제하지 않는다**. 이는 OpenTelemetry에 연결하기 위한 versioned metadata projection과 spool이지, 완전한 OpenTelemetry SDK/OTLP collector를 구현·인증했다는 뜻이 아니다. 다중 노드 데이터웨어하우스나 증분 OLAP 성능 인증도 이번 범위가 아니다.

## 3. 평가 자료의 보관과 독립성

Corpus freeze에서 case ID와 content digest를 모두 검사한다. 개발·검증·holdout의 역할을 분리하고, `harness.propose` 권한이 있는 identity는 다른 권한도 갖고 있다는 이유로 보호 corpus를 읽거나 발행할 수 없다. holdout 읽기는 evaluator 권한과 frozen experiment 목적이 필요하다.

노출 기록은 corpus ID뿐 아니라 콘텐츠 hash에도 묶는다. 새 corpus ID를 붙여 예전 holdout의 노출 한도를 초기화하지 못한다. 개발 split에서 사용했던 bytes를 holdout이라고 다시 등록하면 오염으로 기록한다. 오염은 기존 실험을 다시 평가할 때도 반영하며, 검증 기록을 지워 정상으로 돌리지 않는다.

license/privacy/provenance는 명시적으로 기록한다. license가 미확인인 자료를 자동 재배포하지 않는다. **현재 구현은 `local_acl` qualification이다. 동일 호스트 root/operator까지 격리한 독립 비밀 평가 서비스가 아니다.** 실제 holdout은 별도 서비스·계정·네트워크 경계로 배치한 뒤 외부 자격 시험을 받아야 한다.

## 4. 계획과 평가 실행

계획은 실행 전에 case/repeat/arms/order/split/mode/environment/executor와 분석 규칙을 동결한다. 평가기의 모드는 `sandbox_rerun`, `shadow`, `trace_replay`, `static`을 같은 것으로 취급하지 않는다. 등록된 실행기가 지원하지 않는 모드는 시작 전에 보류한다. 재현 가능한 trace를 실제 재생하지 않았는데 replay라고 부르지 않는다.

실행기는 서버의 trusted dependency다. API body에 실행 결과, 콜백, 승인자, 평가기를 넣어 교체할 수 없다. `ApiServices`의 `evaluation`, `eval_executor`, `canary_executor`를 관리자가 등록하지 않으면 해당 실행은 fail-closed다. 서비스의 Python 진입점은 `freeze`, `run`, `recover_interrupted`이며 상세 signature는 코드와 시험 fixture가 기준이다.

분석:

- 비교 단위는 distinct task다. 같은 문제의 반복 trajectory가 표본 수를 부풀리지 않는다.
- non-inferiority는 사전 margin에 대해 paired binary 결과의 보수적인 동시 Wilson 구간을 쓴다. 모두 통과했다고 차이의 불확실성을 0으로 만들지 않는다.
- cost benefit을 요청하면 계획에 최소 개선·bootstrap seed/반복 수를 고정한다. paired task mean을 비교한다. 이 bootstrap은 근사값이며 효과가 보장되는 통계 검정이라고 과장하지 않는다.
- confidence, margin, sample rationale, variance basis를 선언한 confirmatory plan과 exploratory / local_qualification을 구분한다.
- 누락·unknown 비용·환경 drift·오염은 inconclusive, safety 위반은 fail이다. 단 analysis plan 이 `cost_basis: not_compared`(구독 계정처럼 비용을 알 수 없을 때)를 선언하면 unknown 비용과 measured 가 아닌 usage 는 사유에서 빠지고, 성공률만 비교하며 cost benefit 은 선언할 수 없다 (D-088). 결과를 보고 유리한 중단 시점을 고르는 adaptive peeking은 허용하지 않는다.
- 분석 자원도 한도를 둔다. 과도한 bootstrap은 inconclusive로 반환한다.
- 승인 `eval-report` 스키마의 최대 256 run refs를 보존한다. `2 × case_count × repeats`가 범위를 넘는 계획은 freeze 때 거절한다.
- 실제 trial이 하나도 시작되지 않았다면 report를 꾸며내지 않는다. 독립적인 `experiment-stop` 기록과 aborted/inconclusive 사유를 남긴다.

## 5. 하나의 proposal, 하나의 상한

각 experiment/canary는 proposal의 공통 EvolutionBudget을 소비한다. 계획을 새 ID로 만들거나 owner가 재시작했다고 max attempts/cost/tokens/concurrency/wall time을 초기화하지 않는다.

`BEGIN IMMEDIATE → 사전 budget reserve → dispatch receipt 저장 → transaction 밖에서 실제 실행 → 결과/usage 정산` 순서다. 콜백 도중 동시 요청이 들어와도 먼저 할당된 quota를 본다.

timeout, 연결 유실, 비정상 receipt는 외부 실행이 끝났다는 증거가 아니다. unknown reservation을 유지하고 무조건 재실행하지 않는다. 비용/종료를 증명하는 trusted receipt와 **별도 `experiment.reconcile` actor**로 정산해야 한다. 정산해도 aborted experiment나 철회된 작업 권한은 살아나지 않는다.

## 6. 변경 등급, 카나리, 승격

변경 표면을 먼저 분류하고 그 이후 후보를 평가한다. authority, verifier, protected evaluation, budget, signer, meta-harness 자체 등 보호 핵심의 변경은 일반 자동 개선에서 제외한다. class B는 후보/proposal/reviewer에 묶인 신뢰된 독립 human review가 필요하다. proposer 자신이나 `harness.propose` 권한을 가진 identity는 자기 제안을 승인할 수 없다.

카나리는 별도 explicit opt-in이다. 정책은 low risk target, eligible task IDs, exact `task_binding_refs`, 비용/시간/token/동시성, hard abort, fallback을 고정한다. task ID만 맞고 실제 target이 다르거나 risk/scope가 다르면 거절한다. fallback은 시작 시 현재 active release와 같아야 한다.

각 카나리는 실행 전에 quota를 예약한다. 승인이 실행 도중 철회되거나 kill switch가 켜지면 늦게 돌아온 성공 callback으로 이를 덮어쓰지 않는다. owner crash 이후 unknown execution은 보존하며 새 owner가 aborted 상태와 pending allocation을 수습한다.

승격은 immutable report/trial/receipt를 다시 대조하고 analyzer를 재계산한다. 점수 JSON을 복사해서 passing report로 대체하지 못한다. 오염·현재 승인·정책·unknown root budget·target set·fallback을 재확인하고 Ed25519 서명 및 current pointer CAS로 반영한다. 진행 중인 RunRecord를 새 버전으로 덮어쓰지 않는다.

롤백은 선언된 이전 signed release로만 가능하다. **권한·예산·외부 효과·사용자 데이터는 되돌리지 않는다.** 알 수 없는 외부 실행이 남아 있으면 롤백을 보류한다. kill switch도 proposer가 임의로 다시 켜지 못한다. 이 단계는 signed release pointer와 상태 수명주기를 검증하며, 사내 설치·전환·운영 migration 자격은 RC에서 별도로 확인해야 한다.

### 로컬 제품에서의 연결 (Work 030 S3, D-089)

로컬 제품(`LocalProductDeployment`)은 같은 store 위에 MetaHarness와 EvaluationService를 둔다(`src/amplai_foundry/runtime/execution/meta_local.py`).

- 제안자는 `amplai-meta-proposer` 서비스 신원이고 `harness.propose`만 가진다. 검토·승인·실행·승격·거절은 못 한다. 그 권한을 얹어도 자기 제안에는 `SELF_APPROVAL`로 거절된다.
- 검토자는 사람 운영자다. 목표용 권한에 meta 검토 권한을 더한 신원(`meta_operator()`)이며 `harness.propose`는 가지지 않는다.
- 실험·카나리·승격·롤백 승인은 운영자가 발급하는 durable 기록이다. 기록은 action과 승인 대상의 digest에 묶이고, 사람 운영자만 발급하며, 철회하면 이후 검사가 거절한다. 재시작 후에도 store에서 다시 읽는다.
- `reject`는 draft, screened, offline_evaluated, promotion_pending의 후보를 끝낸다. 사유가 필수이고 검토자와 직전 상태를 후보 기록에 남긴다. 끝난 후보는 다시 움직이지 않는다.
- 오프라인 시험 실행기는 corpus 과제 하나를 실제 goal 하나로 돌린다(`src/amplai_foundry/meta_harness/local_executor.py`, Work 030 S4). 계획 단계의 모델 대신 과제의 고정 contract를 쓰고, 고정한 base commit과 고정한 composition(설치된 것 또는 그 class-A 후보)에서 시작하며, 게시는 끈다. 결과는 검증을 통과한 변경의 scratch 사본에 agent가 못 본 hidden 시험을 얹어 판정한다. 답을 못 낸 시도(driver 오류, 멈춘 goal)는 `success=None`이라 후보의 실패로 세지 않는다.
- corpus는 `specs/030-meta-harness-live/corpus/`의 demo-app 과제 20개(작은 7, 중간 7, 큰 6)다. 각 과제는 base에서 hidden 시험이 실패하고 기준 해답에서 통과해야 하며(`scripts/corpus_check.py`), 과제 문구에는 hidden 시험 내용을 넣지 않는다.
- canary 정책은 선택 항목 `cost_basis`(`compared` 기본, `not_compared`)를 가진다(D-092, Work 030 S5). `not_compared` 이면 canary 결과의 비용이 unknown 이어도 되고 usage 가 `estimated` 여도 된다. 비용은 예약값을 유지하고 0 으로 세지 않으며, token·safety 카운터·receipt 결합·success 는 그대로 필수다. 기본 동작은 그대로다. 구독 계정 driver 는 비용을 모르기 때문이다(Codex, Claude 는 estimated).
- canary 어댑터(`src/amplai_foundry/meta_harness/local_canary.py`)는 시험 결과를 canary 가 받는 형식으로 바꾼다. 답이 없는 시도는 어느 쪽으로도 세지 않고 canary 를 uncertain 으로 멈춘다.
- 운영자용 명령은 `amplai meta`(Work 030 S6, `src/amplai_foundry/runtime/meta_cli.py`)다. 제안, 검사, 실험 승인·실행, canary 승인·실행(승인은 시작하지 않고, 실행이 시작부터 시험까지 한 프로세스에서 한다), 승격, 되돌리기, 거절, 중단, 상태가 각각 하나의 명령이다. 단계를 묶는 명령은 없고, 순서에 맞지 않는 단계는 `META_STATE` 로 거절된다.
- 실험 시도의 참고 지표와 API 환산 비용은 `amplai meta report` 로 본다(D-094). 이미 저장된 기록에서 계산하고 판정에는 쓰지 않는다. 비용은 날짜별 단가표(`deployment/prices/`)로 계산하며 구독 청구액이 아니다.
- 실험 판정은 사전 등록한 비열등성이다. 성공률이 천장이므로 개선을 주장하지 않는다(D-093). 실제 진화 실행(S7)은 아직 하지 않았다. (Work 033 에서 대체: S7 은 이후 끝났고, 판정 방식과 corpus, 가변 표면은 §10 이 넓혔다. 대체 목록은 §10.14.)

## 7. 네트워크 없이 실제 파이프라인 재현

패키지를 설치한 Linux/WSL 환경에서 빈 출력 경로를 사용한다. 기존 경로를 덮어쓰지 않는다.

```bash
amplai ops version
amplai ops evolution-demo --output ./dev03-evolution
```

이 명령은 산술 recipe의 두 동결 구성을 **24개 문제 × baseline/candidate = 48회**, 카나리 2회 비교한다. 각 회차는 실제 Goal Contract → WorkGraph → grant → lease → local recipe worker → JSON 파일 → 독립 verifier → global verification을 거친다. 보고서는 `pipeline-evolution-report.json`이고 마지막에 서명된 승격과 fallback 롤백까지 검사한다. 각 평가 receipt는 실제 RunRecord/출력/검증 reference에 연결된다.

후보 알고리즘이 틀린 negative control도 자동시험에서 확인한다. 단, 이 예제는 **결정적인 로컬 산술 recipe**다. 외부 LLM 성능 개선, Claude/Codex/OpenCode 연결, 실제 컨테이너 방어, 회사 승인 체계가 qualification됐다는 뜻이 아니다. local recipe의 token/cost=0은 외부 모델 호출이 없다는 범위이며 CPU·인건비가 공짜라는 지표가 아니다. local_qualification 결과는 `demo-local` 범위를 벗어나 승격할 수 없다.

## 8. 재실행할 시험

```bash
# source checkout에서 적절한 dependencies 설치 후
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/v3 -q
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  tests/v3/test_dev03_observatory.py \
  tests/v3/test_dev03_evaluation.py \
  tests/v3/test_dev03_meta_harness.py -q
```

DEV-03 추가 142개, 이전 단계 포함 477개 V3 자동시험, 관련 기존 회귀시험 132개를 실행했다. 실제 결과/명령/시간은 전달 ZIP `validation/`와 `_v3_delivery/TEST_RESULTS.json`을 확인한다. 타임아웃과 수정 전 실패 로그도 history로 남기며 통과에 합산하지 않는다. pytest 제3자 plugin auto-load는 재현성을 위해 끈 조건을 명시한다.

설계의 **112개 규범 인수 시나리오**는 위 자동시험 수와 다르다. 전체 legacy regression, 사내 인증, 실제 provider/OS container, distributed durability, 완전한 install/update/rollback 통합은 RC/외부 qualification에서 확인한다. 이 단계의 partial evidence를 전체 설계 PASS로 승격하지 않는다.

## 9. 다음 단계로 넘기는 조건

DEV-03 ZIP SHA-256/manifest/두 패치를 검증한 뒤 RC 통합을 시작한다. 승인 설계와 기존 원본 baseline은 그대로 유지한다. Runtime/Foundry/Kit 승인 경로를 우회하는 shortcuts를 추가하지 않는다. 기능 변경으로 시험 결과가 달라지면 새 snapshot과 증거를 발행하며 이 스냅샷을 수정하지 않는다.

## 10. Work 033 — 메타하네스 시스템

상태: 구현은 repository 에 있고 실제 pilot 실행은 하지 않았다(`specs/033-harness-taxonomy/runs/pilot-runbook.md`). pilot 전의 AC 판정은 확인 필요다. 이 part 는 `specs/033-harness-taxonomy/{spec.md, plan.md, interfaces.md}`와 `src/amplai_foundry/meta_harness/`, `src/amplai_foundry/runtime/meta_commands/`를 기준으로 쓴다. 위 §1~§9 는 Work 030 까지의 내용이고, §10 에서 대체되는 부분은 §10.14 에 모은다. 3.0.0 wire schema 와 `design-reference/` 는 바꾸지 않는다. 새 구조는 내부 store record 에 있고 기존 composition·experiment·report ref 가 그것을 가리킨다.

**잠정 결정.** IC-15~IC-21, IC-23~IC-29, IC-31, IC-32 는 운영자에게 물었으나 확정 답이 없거나 구현 중에 정했다. 추천안으로 구현했고 provisional 이다. 야간 loop 를 켜기 전에 운영자가 명시적으로 확인해야 한다. IC-22 는 `interfaces.md` §0 결정이며 provisional 표에 없다. 확정 여부는 확인 필요. IC-30 은 **미결정**이며 구현하지 않았다(§10.10). 각 항목은 아래 해당 절에 표시한다. 단 IC-23(`TrialContext.domain`: trial 의 L1·L2 decision 이 읽는 과제 domain, 실제 goal 은 `unknown`)과 IC-28(turn 자신의 credential literal 검사: 걸리면 trace 전체를 버린다)은 아래 절에 따로 적지 않았고 여기서만 표시한다.

### 10.1 전체 구조

```text
cells (driver, model, effort)  ×  manifests (component versions)  =  compositions
        └── calibration ── corpus v2 (domain, 측정한 난이도, development / validation / holdout)
proposer (development trace + 점수) ─► component 후보 + 예측 ─► leak gate
screening ─► focused ─► ablation ─► holdout ─► canary ─► promote
                   저장된 record ─► amplai meta dashboard (정적 HTML)
```

모든 단계는 기존 gate(`meta_harness/service.py`), evaluation service(`evaluation/service.py`), budget(`meta_harness/budget.py`), release pointer(`runtime/execution/releases.py`)를 지난다. 새 구현은 `src/amplai_foundry/meta_harness/{components, manifest, composition, corpus_v2, leak_gate, tb2, deciders, judges, stages, trial_metrics, local_executor, traces, proposer, archive, nightly, quota, surrogate, dashboard}.py`와 `src/amplai_foundry/runtime/meta_commands/`에 있다.

### 10.2 Harness component 와 manifest

harness 는 **cell**(§10.3)과 **manifest**(component version 목록)의 조합이다. component 는 id, kind, version, class, content digest 를 가진 store record(`harness-component`)다. version 은 store revision 이다. 이전 version 은 모두 남아 있어 되돌리기는 이전 version 을 다시 가리키는 것이다. v1 content 는 오늘의 동작이며, baseline manifest 는 오늘의 실행 prompt 를 byte 단위로 같게 낸다(golden test, `tests/e2e/test_033_golden_prompt.py`).

manifest 는 composition 이 이미 가진 ref 가 가리키는 record 에 들어 있다. `prompt_bundle_ref`(role 줄), `context_policy_ref`(`context-policy`), `budget_policy_ref`(`budget-policy`), `router_policy_ref`(`router-policy`, shape `layered_v1`). `verification_policy_ref` 는 보호된 record 로 남는다.

| kind | layer | class | 담는 곳 |
|---|---|---|---|
| `role_prompt` | L4 | A | prompt bundle |
| `interpretation` | L1 | A | `router-policy` |
| `env_bootstrap`, `memory_notes` | L4 | A | `context-policy` |
| `retrieval` | L4 | B | `context-policy` |
| `feedback_form` | L6 | B | `context-policy` |
| `attempt_policy`, `execution_strategy`, `driver_options`, `fast_checks`, `limits` | L6, L2, L5, L7, L8 | B | `budget-policy` |
| `route_policy` | L2/L3 | B | `router-policy` |
| `decider`, `decision_method`, `judge_model` | L1~L8 | B | 각 policy 의 `deciders` 또는 참조 |
| `environment_image` | L9 | B | 정의만 있고 이번 round 에서 켜지 않는다(재자격 필요) |

class 의 의미는 design 16 §3 을 따른다. **A**: 고정 assembler 가 고르는 사실, 승인된 note, task prompt 발췌. **B**: repair heuristic, context retrieval, driver mapping, routing 처럼 동작을 바꾸는 부분. **C**: authority, verifier core, budget enforcement, signing, holdout 같은 보호 핵심이다. C 는 후보 표면이 아니다. 후보의 class 는 바꾼 component 중 가장 높은 class 다. class B 후보는 사람 운영자의 code review(`amplai meta review`)가 있어야 screen 이 통과한다. composition 의 ref 를 바꾸면 digest 가 바뀌므로 승인 대기 중인 plan 은 `COMPOSITION_CHANGED` 를 한 번 본다. 운영자는 `amplai meta component list|show|add` 로 component 를 보고 추가한다(`add` 는 source `operator` 로 등록한다).

### 10.3 Cell 과 effort probe

cell 은 `(driver, model, effort)` 하나이며 하나의 `model-profile`(`reasoning_profile` = effort)과 driver port 하나, 앱마다 baseline composition 하나를 가진다. `local.json` 의 `cells` 에 등록한다(`amplai ops local-cell add|remove|list|probe`).

- effort 는 argv 로 전달한다. Codex 는 `-c model_reasoning_effort=<e>`, Claude Code 는 `--effort <e>`(`runtime/execution/readonly_turn.py`, `cells.py`). 문서에 없는 effort 는 `EFFORT_UNSUPPORTED` 로 거절하고 조용히 바꾸지 않는다.
- 문서에 있는 effort 도 `ops local-cell probe CELL` 이 accepted probe 를 기록한 뒤에만 설치된다(`EFFORT_UNPROBED`, 거절된 probe 는 `EFFORT_REFUSED`). "accepted" 는 provider 가 그 flag 로 turn 을 끝냈다는 것만 증명한다.
- probe 는 cell 과 image 에 묶인다. `--environment <environment_id>` 를 주면 그 task environment image 에서 probe 하고 (cell, environment) 쌍으로 기록한다. image 나 driver version 이 바뀌면 새 probe 가 필요하다. 한계: environment id 는 앱 안에서만 유일해서 두 앱이 같은 id 를 쓰면 probe 를 공유한다.
- 같은 model 의 effort 변형은 그 model 의 qualification report 를 공유한다. report 가 없는 model 은 `DRIVER_UNQUALIFIED` 이다. OpenCode cell 은 effort 축이 없다(`provider-default` 외에는 `EFFORT_UNSUPPORTED`).
- 첫 round 의 cell 은 운영자 결정 OD-12 의 Codex gpt-5.6-sol {medium, high}, Claude {claude-sonnet-5, claude-opus-5-5} high 이다.

### 10.4 Corpus v2

과제는 domain 을 가진다. `bug`, `feature`, `refactor`, `cli_ops`, `data`, `ambiguity`, `terminal`, `regression`(`meta_harness/corpus_v2.py`). split 은 development / validation / holdout 이고 `CorpusService.freeze` 로 동결한다. 난이도는 선언하지 않고 calibration 이 cell 마다 측정한다. 모든 cell 이 모든 repeat 을 통과하는 과제는 saturated 로 regression set 으로 옮긴다. Work 030 의 20개 과제는 `amplai meta corpus import-work030` 으로 regression 과제가 된다. 모든 과제는 공정해야 한다(reference 가 hidden test 를 통과하고 base 는 실패. `amplai meta corpus check --repeats N`). Terminal-Bench 2.0 과제는 adapter(`scripts/tb2_adapter.py`, `meta_harness/tb2.py`)로 들이며 과제마다 environment 와 effort probe 가 따로 필요하다. 채점 `tb2_tests` 는 test 실행 명령이 확인되기 전까지 `success: null` 이다(§14 Q7, 확인 필요).

**leak gate**(`meta_harness/leak_gate.py`): component content 가 validation·holdout 과제 id, hidden test 이름, reference 에만 있는 식별자를 담으면 `contamination_findings` 로 기록하고 screening 이 거절한다.

split 현황: 자체 과제만으로 holdout 22(≥ 16), validation 약 13(< 24)이다. `splits.json` 은 TB2 채택 뒤 pilot 에서 고정한다(`interfaces.md` §10.2). corpus 는 앱 하나씩 쓴다(IC-31, provisional): calibration plan 과 stage plan 은 앱 하나의 과제만 고른다.

### 10.5 Layer L1~L8 과 decider

한 번에 하나의 조합을 정하지 않는다. layer 마다 자기 decider 가 있고, 그 layer 의 입력이 생기는 지점에서 호출된다.

| layer | decision point | 선택 | v1 prior |
|---|---|---|---|
| L1 interpretation | intake | `proceed`, `ask_back`, `replan_ask_first` | planner 가 질문을 내면 묻는다 |
| L2 structure | plan 뒤 | 열 가지 strategy | `repair_loop` |
| L3 roles | plan 뒤 | role(planner, executor, reviewer, proposer)별 cell | router order, 모든 role 에 같은 cell |
| L8 limits | plan 뒤 | `limits` version | deployment `Budget` |
| L4 context | dispatch | env bootstrap, memory notes, retrieval 의 on/off | manifest 그대로(v1 은 모두 off) |
| L5 agent options | dispatch | `driver_options` version | driver 기본값 |
| L6 on failure | 실패 시 | `retry_feedback`, `retry_fresh`, `escalate`, `stop` | feedback form 으로 attempt cap 까지 retry |
| L7 fast checks | 최종 검증 전 | `none`, `quick_checks` | `none` |

- **decision method 는 component** 다(`decision_method`). features, estimator, selection rule, fallback 네 부분을 version 으로 바꿀 수 있고, 바꾸면 다른 후보처럼 평가한다. v1 은 `pooled_beta_binomial_v1` / `noninferior_then_cheapest_v1` / `prior_v1` 이다.
- **partial pooling**: 전역 선택에서 시작해, 더 세밀한 bucket(domain → task class → planned size → app)의 자체 증거가 credible 한 차이를 보일 때만 specialise 한다. 아니면 coarse 선택으로 shrink back 한다. 기본 pooling strength 4, Wilson 구간은 effective count 위의 근사이고 table 에 그렇게 기록한다.
- 선택 규칙: 표본이 충분한 option 중 최고 posterior 보다 credibly 나쁘지 않은 것을 남기고, 성공 1건당 token 이 가장 낮은 것을 고른다. 동률은 단순한 쪽이다. 표본이 모자라면 v1 prior 다. 실제 goal 은 탐색하지 않는다.
- 고정 규칙: decider 는 development split 의 trial metrics 만 읽는다(`DECIDER_SPLIT`). validation·holdout 결과를 읽지 못한다. decider 의 평가는 evaluator 에서 하고 decider 안에서 하지 않는다. dispatch 뒤의 결정은 실행 중인 composition 을 바꾸지 않고 새 attempt 나 revision 을 시작한다.
- 명령: `amplai meta decider fit|show|regret|decisions`. layer 효과는 더해지지 않으므로(AHE) decider 변경도 promote 전에 전체 goal 결과로 확인한다.
- **judge**: decider 는 rule table 대신 typed 질문(yes/no, choice, score)에 답하는 judge 를 쓸 수 있다. `judge_model` 도 component 이며 option 은 `none`(v1), `llm_cell`(Claude, Codex cell), `jev` 이다. (judge, version, question type) 쌍은 qualification(`amplai meta judge label|qualify`) 전에는 쓰지 않는다. 기본 통과 기준은 accuracy 하한 ≥ 0.8, repeat agreement ≥ 0.9, ECE ≤ 0.1 이다. **Jev 는 connector 만 있다.** `JevJudge` 는 비활성 stub 이고 `enabled` 가 아니면 `JUDGE_NOT_CONFIGURED` 이다. 접근 방법·API·가격·데이터 정책은 확인 필요이고, 접근을 확인한 뒤에만 연결한다. 지금 judge 는 LLM cell 이다. goal 문장을 기계 밖으로 보내는 judge 는 data class 로 제한한다(기본은 synthetic corpus).

### 10.6 열 가지 execution strategy

`execution_strategy` component 의 `enabled` 목록과 `params` 로 켠다(`interfaces.md` §5.2, `runtime/execution/strategy_runner.py`).

| # | strategy | 요지 |
|---|---|---|
| 1 | `single` | agent 하나, attempt 하나 |
| 2 | `repair_loop` | verifier 피드백으로 이전 patch 위에서 최대 3 attempt (v1) |
| 3 | `workgraph_split` | planner 가 한 repo 의 goal 을 2~4 node 로 나눈다 |
| 4 | `plan_execute` | planner cell 이 step 을 내고 executor cell 이 구현한다 |
| 5 | `best_of_n` | 새 base 위 독립 시도 n(2~3), suite 를 통과한 첫 시도가 이긴다 |
| 6 | `generator_reviewer` | read-only reviewer cell 이 diff 를 보고 수정 요청, 같은 session 의 follow-up 으로 반영 |
| 7 | `cascade` | 싼 cell 먼저, 실패하면 강한 cell 로 revision |
| 8 | `orchestrator` | lead 가 2~4 part 로 나누고 part 마다 executor, 이어서 integration turn |
| 9 | `parallel_readonly` | 최대 4개의 investigator 가 attempt 전에 병렬로 조사 |
| 10 | `vote` | 한 attempt 에서 후보 k(2~3)개, app 의 `quick_verifiers` 로 후보를 고른 뒤 suite 한 번 |

- 한 repo 의 write 동시성은 1 이다(design 07 §4). read-only step 은 4개까지다. 병렬 writer 와 Claude native sub-agent, judge 채점 영역의 vote, n > 3 은 이번 round 에서 미룬다(`interfaces.md` §5.4).
- `workgraph_split` 과 `orchestrator` 의 node 는 integration queue(`integration_queue.py`)에서 합친다. `vote` 는 `fast_checks` 와 같이 쓸 수 없다.
- 보조 turn(reviewer, investigator, 추가 planner, judge)의 token 은 `limits.aux_max_tokens` 안에서만 쓴다. 기본값 0 이라 보조 turn 이 있는 strategy 는 운영자가 한도를 정하기 전에는 실제 goal 에서 쓰지 못한다(IC-21, provisional).
- strategy 선택은 L2 decider 가 한다. `route_policy` 는 class B 이고, 바뀐 정책은 후보로 같은 단계를 지난다. 온라인 학습은 없다.

### 10.7 Evaluator v2 (D-099)

evaluator 변경은 후보 실험과 따로 먼저 qualification 을 받는다. 저장된 experiment 는 기존 verdict 를 유지한다. 새 동작은 analysis plan 의 `evaluator_version_ref` 가 있을 때만 켜지고, 없는 legacy plan 은 결과가 바뀌지 않는다(golden test).

- **endpoint**: `noninferiority`(기본) 또는 `superiority`(`minimum_effect` 필요). 성공률로 개선을 주장할 수 있는 길이 생겼다.
- **decision class**: `improvement`, `efficiency`, `non_inferior`, `regression`, `tradeoff`, `inconclusive`. 성공률 구간(축 S)과 비용 축(C)에서 정한다. 후보 쪽 safety failure 는 `regression`, baseline 쪽만 있으면 `inconclusive`(`baseline_safety_failure`, IC-18, provisional)다. 누락·unknown 비용·drift·오염·표본 부족은 `inconclusive` 다. report verdict 는 `noninferiority` 에서 improvement, efficiency, non_inferior 가 pass 이고, `superiority` 에서는 improvement 만 pass 이며, regression 은 fail, 나머지는 inconclusive 다. class 는 analysis artifact 에 있고 report schema 에는 자리가 없다.
- **pass^k 와 과제별 비율**: repeat 이 여럿이면 "모든 repeat 통과"가 단위다. 결과에 arm 별 `pass_k` 와 `per_task` 가 추가된다(설명용).
- **calibration**: cell 마다 과제 1회씩 측정하고 cell 이 엇갈리거나 경계인 과제만 최대 5회까지 반복한다(OD-11, 적응형). 과제는 informative(통과율 0.2~0.9), saturated, flaky 로 분류한다. A/A discordance 로 noise band 를 정한다. noise band 안의 차이는 개선도 악화도 아닌 `unresolved` 다. calibration 은 앱 하나씩 돈다(IC-31, provisional).
- **MDE 와 최소 표본**: confirmatory 계획의 `sample_rationale` 에 `MDE: <값> at n=<과제 수>` 줄이 있어야 하고, 과제 수가 margin 에 필요한 최소(`min_tasks_for_margin`)보다 적으면 `SAMPLE_UNDERPOWERED` 이다. 탐색 단계(screening, ablation)는 검사하지 않고 MDE 를 설명용으로만 적는다.
- **budget 맞춘 best-of-n 기준 arm**: focused 에서 `reference` arm 을 함께 돌린다. 후보가 같은 비용의 best-of-n 에 dominated 이면 verdict 는 `inconclusive`(`dominated_by_budget_matched_reference`)다.
- **e-process**: 밤을 넘겨 누적하는 always-valid 검정(`sequential_rule: e_process_accumulating`). 다음 밤의 plan 이 이전 상태를 고정해 받는다. "어느 과제에서 더 낫다"는 방향만 검정하고 효과 크기는 검정하지 않으며, 그 한계를 artifact 에 적는다. IC-16(provisional): 같은 arm 의 새 proposal 은 e-process 를 이어 간다.
- **표본 부분집합**(IC-15, provisional): 버전이 붙은 plan 은 `select_cases` 로 정한 부분집합을 허용하고 freeze 와 run 에서 다시 계산한다.
- **병렬 trial**: dispatch 순서는 결정적이고 `parallel` 개까지 동시에 돈다. 첫 stop reason 뒤에는 새 trial 을 내보내지 않는다.
- **Q-suite**: Q-01~Q-15(`interfaces.md` §7.8: `tests/v3/test_033_golden_analysis.py`, `test_033_s1_analysis.py`, `test_033_s1_sequential.py`, `test_033_s2_service.py`, `test_033_s2_calibration.py`, `test_033_s2_requalify.py`, `scripts/evaluator_requalify.py`)가 통과해야 evaluator-version 이 쓰인다. 코드 digest 가 다르면 `EVALUATOR_CHANGED` 다.

### 10.8 Experiment stage 와 canary, promote

한 proposal 은 하나의 budget 아래 여러 동결 실험을 가진다(`meta_harness/stages.py`, template `default_v1`, margin 0.25, 신뢰도 0.95).

| stage | split | arm | repeat | 목적 | gate |
|---|---|---|---|---|---|
| screening | development, informative 과제 최대 12 | baseline, candidate | 1 | 탐색용 걸러내기 | 자동(회귀·safety failure 없을 때) |
| focused | validation, informative 과제 | baseline, candidate, reference | 2 | confirmatory | 운영자 |
| ablation | development, informative 최대 12 | leave-one-out 변형(최대 3) | 1 | component 기여도 | 자동 |
| holdout | holdout 전체 | baseline, winner | 1 | 확인, 한 번 사용 | 운영자(evaluator 가 holdout 을 소비) |

- `amplai meta search` 가 계획하고 gate 없는 단계를 돌린 뒤 gate 마다 멈춘다. gate 는 `approve-stage` 가 연다. `amplai meta stages` 가 위치를 보여 준다.
- 결정 규칙: focused 는 `max(16, n_min)` 미만의 informative 과제에서는 `NO_INFORMATIVE_TASKS` 로 보류한다(IC-20, provisional). 파생 ablation proposal 은 screening 도 promote 도 하지 않는다(IC-19, provisional). 제거 후보(removal sweep 변형)는 focused 의 decision class 가 `efficiency` 이거나 token 이 더 적은 `non_inferior` 가 아니면 holdout 에서 `NOT_A_REMOVAL` 로 멈춘다(IC-24, provisional).
- hack guard 는 trial 마다 기록한다(ask-back rate, edit 크기, 깨진 tool call, 시험 파일 수정, verified-but-hidden-fail). screening 에서 baseline 과 크게 다르면 `HACK_GUARD` 로 실패한다. guard 신호는 거절에만 쓰고 점수에 더하지 않는다.
- 이 경로의 development trial 은 trace 를 남긴다(§10.9). trial 의 `safety_failures` 와 `unknown_effects` 는 상수 0 이 아니라 run 의 receipt 에서 오는 것이 설계다(`plan.md` §1.4).
- **canary**(AC-11): corpus v2 proposal 은 `stageplan-<id>` 가 있으면 v2 경로로 `approve-canary`, `run-canary`, `promote`, `rollback` 이 열린다. 과제는 그 plan 앱의 main set 중 development 나 validation 과제이며 각 한 번이다. holdout 과제는 `CANARY_TASKS` 로 거절한다. holdout stage 가 `passed` 이고 evolution head 의 report 에 묶여 있어야 한다(`EVAL_NOT_PASSING`). 네 명령 모두 사람이 아니면 `APPROVAL_HUMAN` 으로 거절한다. 실패하면 canary 가 멈추고 release 는 그대로이며, 그때는 `reject` 가 정상 결과다. 롤백은 Work 030 과 같이 이전 signed release 만 되돌린다. promote 후 새 plan 이 그 composition 을 쓰고 실행 중 goal 은 그대로다.
- **executor 자격**(IC-29, provisional): `executor-qualification` record 가 `per_trial_tokens`, `basis`, `evidence` 와 자격을 낸 사람을 저장한다. 이후 `run-canary`, 야간 runner, launchd 시작은 그 cell 의 가장 새 사람 운영자 기록으로 복원한다. 기록이 없으면 preflight 에서 멈춘다.
- **task environment 고정**(IC-32, provisional): task environment 마다 sibling composition id 는 `<설치 id>:env-<env12>` 이다. stage 실험 중 환경 digest 가 고정 값과 다르면 trial 은 실행 없이 `environment_drift` receipt 를 낸다. 설치되지 않은 환경의 trial 은 `ENVIRONMENT_UNQUALIFIED` 로 멈춘다.
- 중단된 stage 실험이나 unknown 할당은 사람 운영자의 `amplai meta reconcile`(`experiment.reconcile`, IC-18 provisional)로만 정리한다.

### 10.9 Proposer

proposer 는 `amplai-meta-proposer` 신원이고 `harness.propose` 만 가진다. 검토, 승인, 실행, promote 는 못 한다(`meta_harness/service.py`).

- **정제된 development trace**(D-100, OD-13): corpus 의 meta-harness trial 에서만 trace 를 남긴다. 실제 goal 은 남기지 않는다. sanitizer `trace-sanitizer-v1` 은 default-deny 이고 reasoning item 과 열거하지 않은 event 를 버린다. tool 입출력은 2,000자, message 는 4,000자, trace 는 256 KiB 로 자른다. secret scan 에 걸리면 저장하지 않고 `trace-drop` 만 남긴다. `harness.propose` 권한은 development split 의 trace 만 읽는다. trace 는 export 하지 않고 dashboard 에는 개수만 나온다. Claude 의 `tool_result`, `thinking` 등 block 이름과 Codex 의 command 항목 이름은 확인 필요이며(§14 Q14) 확인 전에는 버린다. 운영자는 `amplai meta trace list|show` 로 본다.
- **제안 ensemble**(`amplai meta proposer run --cell C`): 싼 cell 이 기본 6개(`--drafts`)의 수정을 초안하고, 중복(정규화 digest 동일 또는 단어 집합 Jaccard ≥ 0.9)을 버리고, leak gate 를 통과시킨 뒤, 강한 cell 이 기본 2개(`--refine`)를 다듬는다. 입력은 빈 scratch 디렉터리에 read-only 로 둔다. 제안은 `draft` 로 저장되고 screen 전에는 움직이지 않는다.
- **예측과 점수**: 모든 수정은 어떤 development 과제가 좋아지고 나빠질지 예측한다. screening 후 precision 과 recall 을 `prediction-score` 로 저장한다(`amplai meta proposer score`). focused·holdout 점수는 validation·holdout 결과를 요약하므로 운영자만 보고 proposer 입력에서는 뺀다.
- **dreaming**(`amplai meta proposer dream --cell C --night D`): 그날 밤의 development trace 와 현재 `memory_notes` 로 add/merge/delete delta 를 낸다. note 는 (앱, task class) 마다 40개 이하이고 trace id 근거와 절대 날짜가 필요하며 leak gate 를 거친 뒤 class A 제안으로 일반 stage 를 지난다.
- **removal sweep**(`amplai meta proposer sweep --cell C --reason R`): champion 의 v1 이 아닌 component 마다 v1 로 되돌린 변형을 낸다. model snapshot 이 바뀌었거나 운영자가 요청할 때 쓴다. 변형은 보통 draft proposal 이고 `origin: "removal_sweep"` 이다(IC-24, provisional).
- **elite archive**(`meta_harness/archive.py`): champion 외에 (domain × 비용 대) bucket 별 최고 composition 을 development 결과(n ≥ 4)로 보관하고 lineage 를 잇는다. proposer 는 parent 를 archive 에서 고른다. focused·holdout 의 verdict 는 lineage 에만 남고 proposer 에게 보이지 않는다.

### 10.10 야간 loop

사람이 standing approval 을 발급하기 전에는 아무것도 돌지 않는다. 운영자가 `nightly approve` 로 발급해야 한다.

- **standing approval**(IC-17, provisional): action `nightly.explore`, 권한 `nightly.approve`(사람 운영자만), 최대 7밤. 허용 범위는 development split 의 exploratory 실험, calibration, regression set 의 drift 점검이다. 정책에 cell, 밤당 trial 수 B, 단계별 비율, 최대 budget, 유효 기간이 묶인다.
- **야간 신원**: `amplai-meta-nightly`. 가진 권한은 `experiment.approve`, `experiment.run`, `corpus.read`, `execution.approve` 뿐이다. 이 신원이 쓰는 승인은 standing approval 에서 유도되며(`issue_standing`) 매 trial guard 에서 standing approval 이 아직 유효한지 다시 확인한다. confirmatory, holdout, canary, promote, review, reconcile 은 이 신원이 가질 수 없다.
- **별도 배포**: 야간은 별도 meta deployment 에서만 돈다. 서버의 설정이나 runtime root 를 주면 `NIGHT_DEPLOYMENT` 로 거절한다. store 는 소유자가 하나다.
- **phase 순서**: preflight(kill switch, cell 과 evaluator 자격, IC-29 executor 자격, 종료 시각) → drift(약 10%) → screening design(운영자가 `shares.screening_design` 을 0 보다 크게 둘 때 PB12) → search(약 60%: proposer 초안, surrogate 순위, successive halving, 탐색 단계 실험) → confirmation(약 30%: 낮에 운영자가 승인한 실험만, 없으면 몫을 search 로 돌린다) → dreaming → dashboard 갱신.
- **drift**: 각 cell 의 champion 을 `drift_tasks` 에서 돌려 통과 수 k/n 이 calibration 의 95% Wilson 구간 양끝에서 낸 양측 99% 이항 예측 범위 밖이면 drift 다. driver version, image, model id 변화도 stop 이다. drift 밤에는 search, screening, confirmation 이 멈추고 dreaming 과 dashboard 는 돈다. 재calibration 과 removal sweep 이 예약되며, 그 밤 이후 운영자의 calibration 이 나올 때까지 `recalibration_pending` 이다.
- **정지 조건**: budget 소진(정상 종료), `stop_at`(밤 길이는 `min(stop_at, 시작 + meta.nightly.max_hours)`, 기본 8시간), quota 신호, standing approval 철회·만료, kill switch, preflight 실패, drift, 첫 unknown effect(`reconcile_pending`). budget 정지를 뺀 정지는 기록을 쓴 뒤 `Hold NIGHT_STOPPED` 로 끝난다.
- **focused 대기열**(IC-10): 밤이 끝나면 screening 을 통과한 후보의 focused 실험이 동결되어 dashboard 의 approvals 에 올라간다. 운영자가 `approve-stage --stage focused --queue` 로 정확한 digest 를 승인하고(실행하지 않는다), 다음 밤 confirmation 이 한 번 돌린 뒤 ablation 을 이어 가고 holdout gate 에서 멈춘다. holdout 은 운영자가 직접 돌린다.
- **알려진 한계(IC-30 미결정)**: draft 의 `screen` 에는 `harness.review` 가 필요하고 야간 신원은 그 권한이 없다. 그래서 하룻밤에 운영자가 낮에 screen 한 제안만 screening, ablation 한다. 선택지는 (A) 야간 신원이 class A 초안에 한해 mechanical screen 을 새 권한 `harness.screen` 으로 돌리는 안, (B) 현재 규칙 유지다. 운영자는 아직 정하지 않았다. 추천은 (A)이나 class C 권한을 바꾸므로 결정 전에는 구현하지 않았다.
- quota 는 observe 만 한다. 구독 window 의 크기를 알 수 없어 rate-limit 신호는 §14 Q5 를 pilot 에서 측정하기 전까지 0 으로 기록하고 `signal_source` 가 `none_stored_v1` 이다. 숫자를 추정하지 않는다. `amplai meta quota` 가 pilot 의 여유와 제안 B 를 보인다.
- 기록: `nightly-plan`, `nightly-run`, `quota-observation`. launchd 템플릿은 `deployment/launchd/ai.amplai.meta-nightly.plist.template` 이며 `nightly print-agent` 가 채워서 출력한다. 자동 설치는 없다. doc gardening 같은 계획 항목은 이번 round 에서 미뤘다(`interfaces.md` §8.11).

### 10.11 Dashboard

`amplai meta dashboard --out DIR` 는 저장된 record 에서 정적 HTML 을 만든다. 읽기 전용이다. script 가 없고 CSP 가 있으며 모든 문자열을 escape 하고 오프라인에서 열린다. 페이지는 다음과 같다.

| 페이지 | 내용 |
|---|---|
| `index.html` | model × effort matrix: 보정된 통과율과 구간, pass^k, informative 과제 수, 중앙 시간, 성공 1건당 token, API 환산 비용(설명용, D-094), 최선 composition 과 차이 |
| `cell-<id>.html` | cell 의 composition 과 manifest, 성공 대 token 도표, ablation 기여도, stage 이력 |
| `lineage.html` | proposal → 후보 → 실험 → verdict → release |
| `approvals.html` | 운영자를 기다리는 모든 것: stage gate, class B review, canary, promote, evaluator 변경, 대기 중인 야간 confirmation, "reconcile pending" proposal, 현재 standing approval 과 날짜 |
| `corpus.html`, `experiments.html` | domain × 측정한 난이도, saturated·flaky 목록, split 수 / 실험별 verdict, class, endpoint, 구간, 과제별 비교 |
| `layers.html`, `strategies.html`, `judges.html` | layer 별 specialised 또는 pooled 와 n, regret, coverage / strategy × cell 지표 / judge 자격과 호출 |
| `budget.html`, `evaluation.html` | 야간 B 와 관측한 quota, 밤별 사용량 / §10.12 지표 |

development 밖의 과제는 `<split> task #N` 으로 표시하고 trace 본문, validation·holdout prompt, hidden test 는 담지 않는다. 최선 composition 은 development 과제 4개 이상이 있어야 표시한다. `--feed-json` 은 같은 feed 를 JSON 으로 쓴다. 야간 runner 는 밤 끝에 `<runtime_root>/dashboard` 를 갱신한다.

### 10.12 평가 품질 track (D-105)

평가 자체를 측정하고 개선한다. 측정(`amplai meta evaluator quality`)은 다음과 같다. discrimination(champion 통과율 − 부정 대조군 통과율, 부정 대조군은 일부러 나쁘게 만든 `role_prompt`), saturation, grader flakiness, contamination(leak gate 와 holdout 사용), dev-vs-holdout gap, 실제 결과와의 일치(canary 와 promote 뒤 merge 비율, 설명용 작은 n), domain coverage(`TASK_CLASS_TO_DOMAIN` 의 total variation distance. IC-27 provisional: bug_fix→bug, new_feature→feature, refactor→refactor, operations→cli_ops, 나머지는 unmapped 로 보고하고 거리에서 뺀다), 판정당 비용.

과제 폐기·추가·가중치, repeat 수, stage template, 분석 코드의 변경은 **evaluator change** 이며 후보 실험 안에서 하지 않는다. 별도 track 으로 `propose-change` → `qualify-change` → `approve-change`(또는 `reject-change`)를 지난다. `qualify-change` 는 §10.7 의 Q-suite 를 이 interpreter 의 subprocess 로 돌려 결과(`{files, passed, failed, errors, junit_digest, code_digests}`)를 change head 에 저장하고(IC-25 provisional), approve 가 코드 digest 를 다시 확인한다. propose, qualify, approve, reject 와 `requalify` 는 사람만 가진 `evaluator.approve` 권한이다(IC-26 provisional). 저장된 report 가 하나도 없는 범위의 Q-02 는 허용하되 `vacuous` 로 표시한다. 한 proposal 안에서 evaluator version 을 섞으면 `EVALUATOR_CHANGED` 다.

### 10.13 권한 표

| 행동 | 사람 운영자 | 야간 신원 `amplai-meta-nightly` | proposer `amplai-meta-proposer` |
|---|---|---|---|
| component 추가(`component add`) | 운영자가 CLI 로 실행하며, 기록은 proposer 신원(`harness.propose`)으로 source `operator` 로 들어간다 | 불가 | 신원으로서 등록(같은 경로) |
| 후보 제안·초안(`proposer run`, `propose-components`) | 명령 실행 가능(제출은 `amplai-meta-proposer` 신원으로 기록, 운영자 자신은 `harness.propose` 없음) | 불가 | 가능(`harness.propose`) |
| draft screen, class B review(`harness.review`) | 가능 | 불가(IC-30 미결정) | 불가 |
| calibrate | 가능 | standing approval 범위 안의 calibration 만 | 불가 |
| screening, ablation(development, exploratory) | 가능 | standing approval 에서 유도한 승인으로 가능 | 불가 |
| focused 승인(`approve-stage`, `--queue`) | 가능 | 불가(대기열의 승인된 실험만 다음 밤에 실행) | 불가 |
| holdout 실행(`corpus.holdout.evaluate`) | 가능 | 불가 | 불가 |
| canary 승인·실행, promote, rollback | 가능 | 불가 | 불가 |
| corpus freeze, evaluator change 와 requalify | 가능(`corpus.manage`, `evaluator.approve`) | 불가 | 불가 |
| reconcile(`experiment.reconcile`) | 가능 | 불가 | 불가 |
| standing approval 발급·철회(`nightly.approve`) | 가능 | 불가 | 불가 |
| trace 읽기 | 가능(전체 split) | development·validation split (holdout 불가, `traces.py:769-780`) | development split 만 |
| 자기 제안 승인 | — | — | 불가(`SELF_APPROVAL`) |

주: 운영자 권한 목록(`META_OPERATOR_PERMISSIONS`)에는 `harness.propose` 가 없다(`runtime/execution/meta_local.py`). `component add` 는 운영자가 쓴 내용을 proposer 신원으로 등록한다(`runtime/meta_commands/components.py`, `meta_harness/components.py` 의 `register` 가 source 가 baseline 이 아니면 `harness.propose` 를 요구한다). 야간 신원은 `corpus.read` 를 가진다(`runtime/execution/meta_local.py:94-95` 의 `NIGHTLY_PERMISSIONS`). 그래서 trace 는 development 와 validation split 을 읽고, holdout 은 `corpus.holdout.evaluate` 가 없어 읽지 못한다(`meta_harness/traces.py:769-780`). 승인은 action 과 대상 digest 에 묶인 durable 기록이고, 철회하면 이후 검사가 거절한다. credential 사본은 운영자가 직접 만든다(D-091). agent 는 사람 승인을 대신 쓰지 않는다.

### 10.14 Work 030 에서 대체된 것

| Work 030 의 서술 | 지금 |
|---|---|
| 가변 표면은 IMPLEMENTER role 줄(`prompt_bundle_ref`) 하나이고 `propose` 가 그것만 만든다 | component 단위로 확장됐다(§10.2). 후보 제안은 `propose-components` 가 한다. `propose --prompt-file` 이 `propose_components` 에 위임한다는 점은 `interfaces.md` §12.1 기준이다 |
| corpus 는 demo-app 과제 20개, split 하나 | corpus v2 는 domain 과 세 split 을 가진다(§10.4). Work 030 과제 20개는 regression set 으로 들어온다 |
| 모델·effort 는 driver 마다 하나, effort 는 전달되지 않는다 | cell(driver × model × effort)이 argv 로 effort 를 전달한다(§10.3) |
| 판정은 사전 등록한 비열등성뿐이고 개선을 주장하지 않는다 | evaluator v2 가 superiority endpoint 와 decision class 를 더한다. 버전이 붙지 않은 plan 은 이전 동작 그대로다(§10.7) |
| 실험은 proposal 당 하나, 반복 1회, 순차 | 한 proposal 이 stage 별 여러 실험을 갖고 병렬로 돈다(§10.8) |
| 평가 trial 의 planner 는 고정 contract 대용이라 task 해석(A1)을 시험하지 못한다 | real-planner 모드는 `local_executor.py`/`trial_metrics.py` 에 있다. `ambiguity` 과제 채점에 쓰이는 범위는 확인 필요 |
| "실제 진화 실행(S7)은 아직 하지 않았다" | 이 문장은 낡았다. Work 030 S7 은 끝났고 그 결과가 D-093 이다(`spec.md`). Work 033 의 실제 pilot 은 아직 하지 않았다 |
| 운영자용 명령은 `propose`~`status` | 같은 명령에 `calibrate`, `search`, `approve-stage`, `nightly`, `dashboard` 등이 더해졌다(`docs/v3/USING_AMPLAI_WORK.ko.md` 참조) |

그대로 유효한 것: §1~§5 의 평가 경로와 budget 규칙, §6 의 class 와 canary 와 승격 규칙(canary 정책 `cost_basis` D-092, 사람 승인 기록, `reject`, 서명된 release), §7 의 오프라인 재현(`ops evolution-demo`, 아직 repository 에 있다). `meta_harness/reference.py` 와 `pipeline_reference.py` 의 제거는 계획(D-101, legacy 정리 단계)이며 이 문서 시점에는 남아 있다.

### 10.15 확인하지 못한 것

- 실제 provider 를 쓴 pilot(cell 4개 calibration, 제안 한 바퀴, 3일 밤)의 결과. 모든 수치(통과율, token, 비용, quota)는 pilot 후 채운다.
- 사용자 환경에서의 launchd 동작.
- §14 Q4, Q5, Q7, Q14, Q15, Q17(Codex 재개 turn 의 usage 합산, quota 신호 기록, TB2 test 실행 명령, trace event 이름, 추론강도 probe, base commit 재현). 근거는 `specs/033-harness-taxonomy/interfaces.md` §14 와 pilot runbook 의 "확인 필요"다.
- Jev 의 접근·API·가격·데이터 정책.
- 전체 AC 판정. `specs/033-harness-taxonomy/report/index.html` 의 내용은 이 문서에서 검증하지 않았다(확인 필요).
