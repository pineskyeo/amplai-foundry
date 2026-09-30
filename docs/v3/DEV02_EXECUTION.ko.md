# AMPLAI V3 DEV-02 — 실행 계층 사용·검증 안내

## 위치와 범위

이 문서는 `3.0.0.dev2` 개발 스냅샷의 실행 계층 안내다. 승인된 V3 설계 원본과 30개 규범 스키마의 버전은 `3.0.0`으로 유지한다. 모델 이름을 transport 이름으로 사용하지 않는다. Astra 등 모델은 `ModelProfile`이고, Claude CLI·Codex CLI·OpenCode 서버·Responses는 서로 다른 실행/통신 경계다.

DEV-02는 로컬 실행·프로토콜·실패 경계를 구현하고 시험한 개발 단계이다. 실제 계정, 사내 인증, 컨테이너 탈출/네트워크 격리, 모든 V3 인수 시나리오까지 검증된 운영 릴리스가 아니다. 전체 설계의 원본 요구·작업·시나리오는 전달 ZIP의 `_v3_delivery/*TRACE.json`과 `design-reference/`에 보존한다.

## 설치와 실제 파일 기반 실행 예제

Python 3.11 이상과 Git을 사용한다. 아래는 전달 패키지 최상위에서 실행한다.

```bash
python tools/verify_delivery.py --root . --patches
python -m venv .venv
. .venv/bin/activate
python -m pip install dist/amplai_foundry-3.0.0.dev2-py3-none-any.whl
amplai ops version
amplai ops schemas
amplai ops execution-demo --output /tmp/amplai-dev02-new-run
```

`--output`에는 비어 있는 새 경로를 지정한다. 이 예제는 별도 데모 권한으로 두 앱의 계약·DAG를 만들고, WorkCoordinator가 각각의 작업 폴더에 `result.json`을 실제로 생성한다. Worker는 `verifying`까지만 이동시킨다. 별도 verifier가 출력 바이트를 검사하고, 마지막 통합 verifier가 두 앱의 결과를 확인해야 `verified`가 된다. 결과는 출력 폴더의 `execution-report.json`에 저장된다.

예제는 실제 데이터 파일 작업이며 LLM 응답을 흉내 내서 모델 연동 성공이라고 표시하지 않는다. `external_provider_qualified=false`, `arbitrary_code_containment_qualified=false`가 명시된다. 이 데모의 권한·서명 키를 운영 서비스에 사용하지 않는다.

## 실행 구성과 진입점

| 책임 | 구현 모듈 |
|---|---|
| 전략 선택 / 위험도 상향 / 필수 Gate | `src/amplai_foundry/runtime/execution/strategies.py` |
| 승인된 사실을 이용한 그래프 전략 지정 | `src/amplai_foundry/runtime/graphs/compiler.py` (`compile_adaptive`) |
| 실시간 권한·정책·환경·예산 교집합 | `src/amplai_foundry/runtime/execution/envelope.py` |
| 디스패치·폴링·파일 수집·검증 인계 | `src/amplai_foundry/runtime/execution/worker.py` (`WorkCoordinator`) |
| lease·fence·예산·공유 자원 | `src/amplai_foundry/runtime/execution/service.py`, `src/amplai_foundry/runtime/budgets/service.py` |
| 모델과 분리된 Driver port / 관리자 registry | `src/amplai_foundry/agent_drivers/ports.py` |
| 네이티브 journal / exact-session 저장 | `src/amplai_foundry/agent_drivers/protocol.py`, `src/amplai_foundry/agent_drivers/sessions.py` |
| CLI lifecycle / 서버·API 전송 | `src/amplai_foundry/agent_drivers/cli.py`, `src/amplai_foundry/agent_drivers/http.py` |
| 실제 작업 폴더의 snapshot/restore/collect | `src/amplai_foundry/sandbox/workspace.py` |
| OS 격리 레시피 / 제한된 로컬 데이터 작업 | `src/amplai_foundry/sandbox/container.py`, `src/amplai_foundry/sandbox/local.py` |
| native call-id ↔ effect-id 연결 | `src/amplai_foundry/tool_broker/native.py`, `src/amplai_foundry/runtime/effects/service.py` |
| pause/cancel/resume/checkpoint | `src/amplai_foundry/runtime/execution/steering.py` |

`DriverRegistry.register()`는 `runtime.admin` 권한이 필요하다. 등록되는 port의 driver ID·버전이 저장된 profile과 같아야 하며, `qualified` profile이어야 한다. Worker는 등록하거나 스스로 qualification을 부여할 수 없다. registry의 설치 여부, profile의 서명·권한, environment qualification은 각각 별도로 검사한다. 생성자에 `qualified=True`를 넣는 행위만으로 운영 qualification이 생기는 것은 아니다.

사용자 진입점 `/work`, `/design`을 추가 대량 명령으로 대체하지 않는다. 자동화 서버는 기존 Foundry 권한으로 계약·그래프를 동결한 뒤 Runtime의 `claim()`으로 받은 dispatch를 WorkCoordinator에 전달한다. 데모 외 실제 서비스 설치는 해당 기관의 인증·경계 qualification과 RC 단계의 통합 인수시험을 거친다.

## 전략 의미

- `direct`: 낮은 위험, 국소 변경, 결정적 verifier가 있는 경우의 단일 시도. 독립 검증은 생략하지 않는다.
- `bounded_loop`: 실패 후 verifier가 재작업을 허용한 경우에만 Runtime이 다음 attempt를 admit한다. 첫 실행도 시도 수에 포함되며 Worker가 임의로 재시도하지 않는다.
- `deliberative`: 불확실성에 대해 별도 검토된 계획 receipt를 요구한다. Worker 자신의 승인, 다른 contract/graph의 receipt는 거절한다. 계획 작성 주체와 검토 서비스는 배포 시 명시적으로 연결한다.
- `discovery`: 목표 해소를 위한 읽기 전용 탐색. 쓰기 권한을 가진 미해결 목표를 탐색이라는 이름으로 실행하지 않는다.

여러 앱은 WorkGraph로 병렬 연결한다. Graph 자체를 실행 전략이라고 부르거나 반복을 위해 DAG에 cycle을 추가하지 않는다. 보호된 정책·CI·계약 파일 변경이면 위험도 하한을 높인다. 이미 동결된 저위험 계약과 충돌하면 계약 재검토로 HOLD한다. 지원하지 않는 strategy를 조용히 `direct`로 낮추지 않는다.

## 중복·재시작·일시정지 규칙

디스패치는 native I/O 전에 durable journal과 scoped 실행 head에 기록한다. 같은 dispatch/prompt/workspace/output-port의 재요청은 이미 저장된 검증 인계 결과를 반환하거나 reconciliation을 요구한다. `starting`, `launching`, `unknown` 상태에서 native 프로세스를 다시 만드는 동작은 금지한다.

네이티브 `latest`, `continue`, 다른 세션 ID, 다른 모델·driver 버전·workspace는 exact resume가 아니다. private native HOME은 실행 서버가 관리하는 세션별 경로이며 사용자의 실제 HOME을 mount하지 않는다. portable checkpoint에는 실제 workspace 내용과 공개 실행 상태만 포함한다. 인증정보와 private native 세션 파일을 전역 evidence archive로 복사하지 않는다.

pause/cancel 명령 수신, transport ACK, 프로세스 종료 확인은 다른 사건이다. 종료가 입증되고 미해결 effect가 없어야 checkpoint를 만든다. pause는 compute slot과 쓰기 resource claim을 반환하지만 root token/cost 예약은 남긴다. resume는 권한·정책·context·snapshot·profile을 재검사하고 자원을 먼저 재획득한다. 그 후 exact session 재개를 보낸다. ACK 손실은 `resume_pending`으로 남으며 자동 재전송하지 않는다. 성공 ACK 이후 새 lease/fence를 발급한다. 이전 fence의 출력·heartbeat는 거절한다.

WorkCoordinator 연결 순서는 다음과 같다.

```python
# actor / worker는 서버에서 인증한 객체이며 요청 JSON에서 생성하지 않는다.
steering.quiesce(actor, pause_id,
    lambda run_id, kind: coordinator.stop_and_snapshot(worker, run_id, kind))
steering.resume(actor, resume_id, worker,
    lambda run_id, checkpoint: coordinator.resume_exact(worker, run_id, checkpoint))
coordinator.continue_resumed(worker, run_id)
```

`continue_resumed`는 재개가 확정된 새 lease에서만 수집한다. 중간 권한 철회, 종료 미확인, session 변경, budget 경과는 HOLD다. pause 또는 네트워크 대기가 root/run wall-clock을 초기화하지 않는다. 데이터 전용 RecipePort는 native resume를 구현한 모델이 아니므로 `NEW_SESSION_REQUIRED`를 반환한다.

## ToolBroker와 외부 부작용

native session/turn/call ID, 도구 이름, 인자 digest, 계약·그래프·grant를 묶어 하나의 effect ID를 만든다. 인자 JSON의 중복 키와 NaN/Infinity는 거절한다. 같은 call에 다른 인자를 보내면 충돌이다. grant가 넓어도 현재 work의 capability 밖 도구는 사용할 수 없다.

도구 시간초과는 외부 시스템의 쓰기를 취소했다는 뜻이 아니다. 호출이 늦게 성공할 수 있으므로 상태는 `unknown`이고 자동 재전송하지 않는다. 늦은 응답으로 성공을 덮어쓰지 않는다. trusted read-only reconciler로 실제 상태를 확인한 뒤 분류한다. 결과가 불명확한 effect가 있으면 work/global 검증을 완료하지 않는다. 필요시 보상 작업도 새로운 승인된 effect이며 마술적인 원상복구로 처리하지 않는다.

SecretBroker는 scope·host·HTTPS endpoint·TTL에 묶인 handle을 사용한다. HOME/PATH/LD_PRELOAD/PYTHONPATH/NODE_OPTIONS/DOCKER_HOST 등 실행 권한을 바꾸는 환경 주입은 거절한다. 비밀값이나 예외의 원문을 관측 기록에 남기지 않는다.

## 드라이버별 확인 범위

| 드라이버 | DEV-02 확인 | 실제 배포에서 남은 확인 |
|---|---|---|
| RecipePort | 실제 독립 폴더에서 데이터 작업·증거·통합 검증 | 임의 코드/외부 모델에 사용 금지 |
| Claude/Codex CLI | 알려진 테스트 subprocess의 JSONL·종료·정확한 resume·credential HOME·timeout·orphan HOLD | Codex CLI 0.155.1(`gpt-5.6-sol`)는 app image 안에서 9 probe + 실제 셸·파일 쓰기 turn(`tool_use`)을 pass했고 로컬 실행 경로(`amplai work` → 승인 → container → 검증 → draft PR)에 연결됐다 (Work 018, D-073). Claude CLI 2.1.278(`claude-sonnet-5`)도 같은 app image 에서 9 probe + `tool_use` 를 pass 했고 시스템이 고르는 fallback composition 으로 연결됐다 (Work 019, D-079). 회사 계정·여러 운영자·원격 worker 는 남아 있다 |
| OpenCode | HTTP fixture로 health/version·인증·204 ACK·turn/session correlation·idle/stop boundary·exact resume. 실제 `opencode serve` 1.17.13 실제 turn: host-side 9 probe 중 6 pass, sandbox(`amplai-worker-opencode` image + egress profile, `scripts/opencode_qualify.py --container`) 에서 8 pass (Work 017) ; app image(`amplai-worker-app-amplai-foundry@sha256:2ab06021…`)에서 9 check + `tool_use` pass, 실행 경로에 세 번째 후보로 연결, corpus 과제 1개 verified (Work 031, D-095) | 서버 비밀번호는 env 로 전달되지만 env guard(`shell.env` plugin + `SHELL` wrapper)가 agent shell 에서 `OPENCODE_SERVER_*` 를 지운다. 같은 uid 는 `/proc/<pid>/environ` 으로 읽을 수 있다(D-091 로 수용); crash resume 은 raw HTTP prompt 로만 측정; token 사용량은 보고하지 않음(usage `unknown`) |
| Responses | HTTP fixture로 store=false·bounded tokens·native call ID·structured plan·idempotency | 실제 계정·정확한 모델·배포 도구 정책; 모든 async/steering API가 지원된다는 뜻 아님 |
| Managed/API/app-server 확장 | 미승인 경로가 fail-closed인 경계 | 공개 API/배포 qualification 후 별도 profile |

컨테이너 레시피는 digest-pinned image, non-root, cap-drop, read-only root, pids/memory/cpu 제한, 기본 network=none이다. network 를 붙이려면 실측 pass 로 만든 egress qualification ref 가 있어야 한다 (`src/amplai_foundry/sandbox/egress.py`: `--internal` network + allowlist CONNECT sidecar, deny-by-default). 로컬 colima/docker 에서 `scripts/sandbox_up.sh --egress` 로 sidecar 를 올리고 `scripts/container_qualify.py` 로 컨테이너 안 실제 turn 을 자격화한다 (Work 016). memory/pids/cpu 제한은 `scripts/limits_qualify.py` 가 같은 argv 로 실측한다 — 제한 초과 할당은 OOMKilled, 제한 초과 fork 는 EAGAIN 으로 거절되고 engine 은 살아 있다 (Work 017, `deployment/local-limits-qualification.json`). 부하 중 CPU throttling 과 podman 은 아직 측정하지 않았다. 초기 빌드 환경에는 docker/podman이 없어 실제 격리를 인수시험하지 않았다. CLI 테스트의 FakeContainer는 프로토콜을 검사하는 신뢰된 subprocess fixture이며 보안 sandbox가 아니다. qualification이 없을 때 host shell로 대체하지 않는다.

## 시험·재현과 다음 단계

`python -m pytest tests/v3 -q`는 기존 DEV-01 시험과 DEV-02 추가 시험을 함께 실행한다. 다중 프로세스 journal 경합, lease/stale worker, 중복 생성, 실제 파일 증거, native exact resume, 늦은 effect 결과, 예산 초과 완료 거절 등을 포함한다. 실제 실행한 케이스 수와 결과는 전달 ZIP의 JUnit과 TEST_RESULTS가 기준이다. 테스트 개수가 규범 인수 시나리오 112개의 완료 개수와 같다고 계산하지 않는다.

설치 검사는 새 venv에 새 wheel을 설치했으나 현재 환경에 이미 설치된 의존성을 명시적으로 공유한다. 완전히 빈 환경에서의 dependency resolution, 플랫폼 전체 호환성, 온라인 계정 연결은 별도다. 전체 legacy 회귀와 V3 운영 인수 closure는 RC의 필수 항목이다.

다음 개발은 DEV-03의 관측·메타하네스이다. DEV-02의 모든 코드·검사 기록을 보존하고, 승인된 설계에 따라 RunRecord/지표, protected holdout, 비교 실험, canary, 승격·롤백과 self-approval 차단을 강화한다. 실제 외부 드라이버 qualification 미완료 항목을 성공으로 승격하지 않는다.
