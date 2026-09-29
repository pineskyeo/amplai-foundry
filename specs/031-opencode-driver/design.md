# 031 OpenCode Driver — Design

Status: draft (Phase 1). 계약 결정은 하지 않는다. 결정이 필요한 항목은 "Decisions Needed" 에 모았다.
Base: `origin/feat/029-opencode-loopback` (PR #28, D-087 포함; 작성 시점 main 미병합).

## Goal

OpenCode(`opencode serve` 1.17.13)를 AMPLAI V3 local product 의 세 번째 system-selected driver 로
연결한다. 3.0.0 wire schema(`src/amplai_foundry/runtime/contracts/data/schemas/`)와
`design-reference/` 는 바꾸지 않는다.

## Current State (Facts)

| 사실 | 근거 |
| --- | --- |
| `OpenCodeDriver` 는 HTTP polling 이다. `probe()` 가 `"transport": "http_poll"` 을 낸다 | `src/amplai_foundry/agent_drivers/http.py:198` |
| `driver-capabilities` transport enum 은 `cli, stdio_rpc, http_sse, websocket, managed_api` 뿐이다 | `src/amplai_foundry/runtime/contracts/data/schemas/driver-capabilities.schema.json:19` |
| `install_codex_profile` 은 `"transport": "cli"` 를 고정으로 쓴다 | `src/amplai_foundry/runtime/execution/codex.py:289` |
| `OpenCodePort` 는 생성 시 workspace 하나에 묶인다. `prepare` 가 다른 workspace 면 `OPENCODE_WORKSPACE` Hold | `src/amplai_foundry/agent_drivers/ports.py:278-294` |
| worker 는 run 마다 workspace 를 새로 만들고 같은 port 에 `prepare(dispatch, prompt, workspace)` 를 부른다 | `src/amplai_foundry/runtime/execution/worker.py:192-197` |
| `DriverRegistry` 는 profile 하나당 port instance 하나를 불변으로 등록한다 | `src/amplai_foundry/agent_drivers/ports.py:84-89` |
| pause/cancel/steer 는 `registry.resolve` 로 같은 port instance 를 다시 얻어 handle 로 호출한다 | `worker.py:330-338`, `worker.py:369-376` |
| container qualification 은 `opencode serve` 를 pinned image 에서 detached 로 띄우고 `docker exec curl` 로 부른다. loopback only, password 없음 | `scripts/opencode_qualify.py:218-275`, `:346-374`; D-087 `docs/workstreams/v3-real-execution/DECISIONS.md:345-365` |
| qualification 은 `reports["opencode-server-container"]` 에 쓰고 top-level `container_image` 는 쓰지 않는다 | `scripts/opencode_qualify.py:953-955`; `container_image` 는 `scripts/container_qualify.py:676` 만 씀 |
| `measured_qualification` 은 `reports[driver_id]`, `status=pass`, top-level `container_image == profile.image`, `driver_version == tools[provider]`, `model`, `tool_use.outcome=pass` 를 요구한다 | `codex.py:194-216` |
| `DRIVER_IDS`/`ACCOUNTS` 는 codex/claude 뿐이다 | `codex.py:37-38` |
| `OpenCodePort.driver_id = "opencode-server"` | `ports.py:276` |
| OpenCode worker image 는 app image 가 아닌 `amplai-worker` base 위에 opencode 만 더한 별도 image 다 | `deployment/local-container-opencode.json:4-5`, `deployment/worker-opencode/Dockerfile` |
| codex/claude 는 app image 를 공유하고 environment record 는 image 별이다. "execution and verification must name the same pinned environment" | `codex.py:266-271`, `local_deployment.py:222-243` |
| `ROUTER_ORDER = ("codex-cli", "claude-cli")` (D-079) | `src/amplai_foundry/runtime/execution/product.py:50` |
| `ops local-driver` 는 `codex`/`claude` 만 받는다 | `src/amplai_foundry/runtime/cli.py:735` |
| `OpenCodeDriver.collect` 는 `usage: None` 을 돌려준다(worker 가 `UNKNOWN_USAGE` 로 채움) | `http.py:441`, `worker.py:268` |
| `OpenCodeDriver.resume` 은 아직 측정되지 않았다 | D-087 Open (`DECISIONS.md:364-365`) |

## Per-Run Server Lifecycle

원칙: dispatch 하나 = `opencode serve` container 하나 = run workspace 하나. 공유 server 는 두지 않는다
(`OpenCodePort` docstring "An isolated server must own the exact workspace", `ports.py:273`).

- **Port contract 충족.** registry 에는 `PerDispatchOpenCodePort` 하나를 등록한다. 이 port 는 생성 시
  workspace 를 받지 않고 `OpenCodeServerLauncher` 를 받는다. `prepare(dispatch, prompt, workspace)` 에서
  dispatch 전용 native home 을 seed 하고, launcher 로 server 를 그 workspace 에 띄우고, 그 server 의
  transport/boundary probe 로 `OpenCodeDriver` 를 dispatch 마다 만든다. 기존 `OpenCodePort` 의 workspace
  일치 검사(`workspace_probe`)는 dispatch 단위로 그대로 한다. handle 은 dispatch_id 이고 port 는
  handle → (driver, server, home) 표를 가진다.
- **start/health.** launcher 는 `ContainerSandbox.command([...opencode serve --port 4096 --hostname 127.0.0.1],
  workspace, name, env_names=[], native_home=home)` 에 `-d` 를 넣은 argv 로 띄운다(qualifier 와 같은 형태,
  `opencode_qualify.py:346-354`). health 는 exec transport 로 `GET /global/health` 를 180초까지 기다린다
  (`opencode_qualify.py:363-370`). version 검사는 driver `probe()` 가 한다(`http.py:192-194`).
- **boundary probe.** `session/status` idle + server 의 비-opencode 자손 process 0 개
  (`opencode_qualify.py:421-446`, `Turns.boundary` `:513-517`). launcher 의 server 객체가 제공한다.
- **stop.** `collect` 성공 후, `destroy`, `pause`/`cancel` 이 `process_stopped=True` 를 돌려준 뒤 container 를
  `docker rm -f` 한다. `destroy` 는 driver 가 stopped 를 확인한 뒤에만 부른다(`http.py:444-447`).
- **cancel.** driver `cancel` → `/abort` ack → boundary 확인 → `cancelled`. 확인되면 server 를 멈추고
  credential 을 회수한다. 확인 전이면 server 를 유지한다(abort ack 는 종료 증거가 아니다, `http.py:340`).
- **pause.** driver `pause` 는 cancel 과 같고 state 를 `paused` 로 바꾼다. stopped 확인 뒤 server 를 멈춘다.
  이후 `checkpoint` 는 journal 만 읽으므로(`http.py:271-277` poll 의 early return) server 없이 동작한다.
  port 는 checkpoint 에 `native_home` 을 더한다(codex 의 `checkpoint["native_home"]` 과 같은 방식, `codex.py:128`).
- **resume.** 새 dispatch 에 대해 checkpoint 의 `native_home` 으로 server 를 다시 띄우고 driver `resume` 으로 같은
  native session 에 prompt 를 보낸다. **확인 필요:** `opencode serve` 재시작 뒤 이전 session 을 data dir 에서
  다시 읽는지는 모른다. 측정 전까지 resume 은 qualification probe 가 필요하다(Risks R3).
- **process 재시작.** handle 표는 memory 에만 있다. owner process 가 죽으면 port 는 그 handle 을
  `OPENCODE_SERVER_LOST` 로 Hold 한다. 남은 container 정리는 이름 규칙(`amplai-opencode-<dispatch_id>`)으로
  reconcile 한다(추정: 기존 CLI driver 의 orphan 처리와 같은 방향. `tests/v3/test_dev02_drivers.py:204` 확인 필요).

## Host → Container Transport

- `httpx.BaseTransport` 구현 `DockerExecTransport`(현재 `scripts/opencode_qualify.py:218-275`)를 src 로 옮겨
  launcher 가 쓴다. 요청은 curl config 로 stdin 에 넣는다(argv 에 body/인증 없음).
- `OpenCodeDriver(..., password="", loopback_exec=True, transport=<exec transport>, allow_local=True)`
  (`http.py:167-176`). 비밀번호는 쓰지 않는다(D-087).
- container 는 `--internal` egress network 위에 있어 host route 가 없다(`opencode_qualify.py:221-223`).

## Credential Handling

- operator 가 만든 scoped copy(`scripts/sandbox_up.sh --opencode-home DIR`)만 받는다. 실제 home 또는
  `$XDG_DATA_HOME` 과 같으면 Hold(qualifier 와 같은 규칙, `opencode_qualify.py:297-302`).
- dispatch 마다 `<native_root>/<dispatch_id>/` 를 home 으로 만들고 `.local/share/opencode/auth.json` 과
  `.cache/opencode/models.json`(egress allowlist 에 models.dev 가 없어 catalog seed 가 필요,
  `opencode_qualify.py:304-311`)을 복사한다.
- run 이 끝나거나 pause 되면 run home 의 `auth.json` 을 지운다(codex 와 같은 "no credential at rest",
  `codex.py:118-122`). **확인 필요:** OpenCode provider credential 이 refresh 로 바뀌는지는 모른다. 바뀐다면
  codex 처럼 write-back 이 필요하다. 이번 구현은 write-back 하지 않는다.
- server password 는 없다(D-087).

## Qualification Report Shape

`measured_qualification` 을 그대로 쓰려면 opencode report 가 다음을 만족해야 한다(`codex.py:197-216`).

| 필요 | 현재 opencode_qualify.py | 필요한 변경 |
| --- | --- | --- |
| `reports[<driver_id>]` | key `opencode-server-container` (`:954`) | driver_id 와 같은 key (Decision D2) |
| top-level `container_image == profile.image` | 쓰지 않음 | `--container` 일 때 `existing["container_image"]` 를 쓴다. 단 같은 파일을 codex/claude 가 공유하면 덮어쓴다 → 별도 파일 권장 |
| `driver_version == tools["opencode"]` | `a.version` 1.17.13, profile tools.opencode 1.17.13 | `provider="opencode"` 이면 그대로 맞는다 |
| `model` | `opencode-go/glm-5.3-flash` | config model 과 같아야 한다 |
| `tool_use.outcome == pass` | 있음 (`:966`) | 없음 |
| `qualification_id`, `checks`, `checked_at` | 있음 | 없음 |

권장: `--out` 을 app 별 새 파일(예: `deployment/opencode-qualification-<app>.json`)로 주고, 파일이 없으면
`{"schema_version", "reports": {}}` 로 시작하게 한다(현재는 기존 파일을 반드시 읽음, `:951`).

## Planner

- codex 는 `--output-schema`(`planner_codex.py:222-224`), claude 는 `--json-schema` 의 `structured_output`
  (`planner_codex.py:315-348, 397`)로 구조화 plan 을 받는다.
- **모른다:** `opencode serve` 1.17.13 의 `prompt_async`/`message` API 가 JSON schema 강제 출력을 지원하는지
  이 repo 에 증거가 없다(grep 결과 없음). 
- 가능한 형태: (a) read-only 권한 agent 로 한 turn 을 돌려 마지막 assistant text 를 strict JSON 으로 parse +
  `plan_schema` 검증(schema 강제 아님, 실패 시 Hold). (b) OpenCode 는 planner 를 두지 않고 codex/claude planner 가
  plan 한다. Decision D5.

## Config / CLI Changes

- `LocalConfig.opencode: OpenCodeEntry | None` (credential_home, model, provider_id, egress, enabled).
  `AppEntry.opencode_qualification_report` / `opencode_container_profile` (optional). local config 는 wire
  schema 가 아니다(`schema_version "local-1"`, `local_deployment.py:158`).
- `ops local-driver opencode --enable/--disable`.
- `ops local-opencode ...`(설정 추가) 는 image 결정(D3) 이후에 만든다.
- `DRIVER_IDS`/`ACCOUNTS` 확장, `install_driver_profile` 의 transport 값은 D1 이후.
- `ROUTER_ORDER` 에 opencode 추가는 D4 이후.

## Decisions Needed

평문 요약: OpenCode 를 실제로 고르게 하려면 여섯 가지를 운영자가 정해야 한다. 가장 급한 것은 D1(schema 를
안 바꾸고 transport 를 뭐라고 기록하나)과 D3(OpenCode 를 어느 image 에서 돌리나)이다.

### D1 — driver-capabilities `transport` 값 (schema 변경 불가)

| 옵션 | 내용 | trade-off |
| --- | --- | --- |
| A | `"cli"` | host 가 실제로 부르는 것은 `docker exec curl`(CLI process). 기존 install 코드 그대로. HTTP 의미가 기록에서 사라진다 |
| B | `"http_sse"` | HTTP 계열이라 가깝지만 driver 는 SSE 를 쓰지 않는다. 사실과 다른 기록 |
| C | `"managed_api"` | server 가 관리되는 API 라는 의미로 볼 수 있으나 enum 의 뜻이 design 에 정의돼 있는지 확인 필요 |

권장: **A `cli`**. host 쪽 실제 transport 가 exec(CLI) 이고 거짓 기록(SSE)을 피한다. 세부 `http_poll` 은
qualification report 의 `method` 와 probe artifact 에 남는다(`opencode-probe.json:7`). 다음 schema 개정 때
`http_poll` 을 추가하는 것을 Open 으로 남긴다.

### D2 — driver_id

| 옵션 | trade-off |
| --- | --- |
| A `opencode-server` (현 `OpenCodePort.driver_id`) | 코드 변경 없음. report key 를 바꿔야 함 |
| B `opencode-server-container` (현 report key) | report 그대로. port 의 driver_id 변경 필요, composition 이름 규칙 `driver_id.split('-')[0]` → `opencode` 로 동일 |

권장: **A**, qualifier 가 container 모드에서 `opencode-server` key 로 (새 파일에) 쓰게 한다.

### D3 — 실행 image

| 옵션 | trade-off |
| --- | --- |
| A 별도 opencode image (`local-container-opencode.json`) | 이미 측정됨. app 도구(venv, pytest)가 없어 agent 가 test 를 못 돌린다. environment record 가 image 별이라 실행/검증 environment 가 달라져 `codex.py:266-267` 불변식과 충돌 |
| B app image 에 opencode 설치 (`worker-app` 위에 layer) | 불변식 유지, agent 가 app 도구 사용. 새 image build + 재qualification 필요 |
| C app image 를 base 로 한 app 별 opencode image | B 와 같고 image 수만 늘어난다 |

권장: **B**. 재qualification 비용은 들지만 검증 environment 불변식을 지킨다.

### D4 — router 순서

A `codex, claude, opencode` (fallback 끝) / B 운영자 설정값으로 순서 지정 / C opencode 는 selection 에서 빠진
experimental 로 등록. 권장: **A** (D-079 순서 유지, 추가만).

### D5 — OpenCode planner

A planner 없음(다른 driver planner 사용) / B text→strict JSON parse + schema 검증 / C OpenCode JSON 강제 기능
측정 후 결정. 권장: **A** 로 시작하고 C 를 측정 항목으로 둔다.

### D6 — model-profile `provider` 값

`ACCOUNTS` 에 들어갈 id. 예 `opencode-go-account` / `opencode-zen`. provider 는 `common#/$defs/id` 형식만 요구한다
(`model-profile.schema.json`). 권장: 운영자가 실제 계정 종류 이름을 준다(추정 금지).

## Risks

- R1 per-dispatch startup 비용: 측정값은 report 의 `startup_seconds` 가 아니라 qualifier 에만 있다
  (`opencode_qualify.py:369`). egress 가 막힌 npm/models.dev fetch 를 기다려 느릴 수 있다(주석 `:365`). dispatch
  당 지연으로 붙는다.
- R2 `ascending_message_id` 는 server 시계가 host 보다 뒤지지 않는다고 가정한다(`http.py:27-28`). colima VM
  시계 drift 는 측정되지 않았다.
- R3 resume: server 재시작 뒤 session 복원 여부 모름. 측정 전 resume 을 쓰면 `RESUME_*` Hold 가 날 수 있다.
- R4 owner process crash 시 handle 표 유실 → container 가 남을 수 있다(reconcile 필요).
- R5 usage 는 `UNKNOWN_USAGE` 다. budget 계산이 unknown 으로 남는다.
- R6 credential refresh 여부 모름(write-back 없음).
- R7 D3-A 를 고르면 실행/검증 environment 불일치.

## Implementation Plan

Phase 2 (결정 불필요, 이 branch):
1. `PerDispatchOpenCodePort` + `OpenCodeServerLauncher`/`OpenCodeServer` Protocol + `ScopedOpenCodeHome`
   (`src/amplai_foundry/agent_drivers/opencode_port.py`), fake launcher test.
2. `OpenCodeEntry` config parsing, `AppEntry.opencode_*` optional field. 설정되면 `_compose` 는
   `OPENCODE_NOT_WIRED` Hold (D1/D3/D4 대기 stub).
3. `ops local-driver opencode`.

Stub (결정 대기): docker launcher(D3), install profile transport(D1), driver_id/report key(D2), router(D4),
planner(D5), ACCOUNTS(D6).

## Sources

위 표의 path:line.
