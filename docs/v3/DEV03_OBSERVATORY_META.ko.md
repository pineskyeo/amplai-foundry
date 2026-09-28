# DEV-03 — Observatory & Meta-Harness

상태: **3.0.0.dev3 개발 스냅샷**. 승인 설계의 관측·평가·개선 경계를 구현하고 로컬 시험으로 검증한 단계이며 전체 V3 또는 운영 자격 인증이 아니다. DEV-01/02 기능과 승인 스키마 3.0.0을 유지한다. 현재 요구사항의 완전한 인수 판정은 배포 ZIP의 `_v3_delivery/REQUIREMENT_TRACE.json`을 따른다.

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
- 누락·unknown 비용·환경 drift·오염은 inconclusive, safety 위반은 fail이다. 결과를 보고 유리한 중단 시점을 고르는 adaptive peeking은 허용하지 않는다.
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
