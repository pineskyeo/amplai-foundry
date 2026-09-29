# 031 OpenCode Driver — Design

Status: Phase 2. 운영자 결정(2026-09-29) 반영. auth 방식은 아직 선택 전이다(Auth Options).
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
- `OpenCodeDriver(..., transport=<exec transport>, allow_local=True, subscribe=True)`. 비밀번호는 Auth
  Options 의 선택을 따른다(`OpenCodeServer.password`). D-087(비밀번호 없음)은 보류다.
- SSE 를 받으려면 exec transport 가 streaming 이어야 한다(Event Stream 참고).
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
- server password 는 Auth Options 참고(선택 전).

## Qualification Report Shape

`measured_qualification` 을 그대로 쓰려면 opencode report 가 다음을 만족해야 한다(`codex.py:197-216`).

| 필요 | 현재 opencode_qualify.py | 필요한 변경 |
| --- | --- | --- |
| `reports[<driver_id>]` | 이전: key `opencode-server-container` | 반영: container 모드 key `opencode-server` (D2) |
| top-level `container_image == profile.image` | 이전: 쓰지 않음 | 반영: container 모드에서 기록. `--out` 은 image 별 파일이어야 하고 공유 파일(`specs/015-…/driver-qualification.json`)은 거부 |
| `driver_version == tools["opencode"]` | `a.version` 1.17.13, profile tools.opencode 1.17.13 | `provider="opencode"` 이면 그대로 맞는다 |
| `model` | `opencode-go/glm-5.3-flash` | config model 과 같아야 한다 |
| `tool_use.outcome == pass` | 있음 (`:966`) | 없음 |
| `qualification_id`, `checks`, `checked_at` | 있음 | 없음 |

반영: `--container-profile`(기본 `deployment/local-container-opencode.json`, D3 에서는
`deployment/local-container-app-<app>.json`)과 image 별 `--out`. 파일이 없으면 빈 report 로 시작한다.
`tool_use`(long_command 포함)는 container 모드에서 이미 측정한다.

## Planner

- codex 는 `--output-schema`(`planner_codex.py:222-224`), claude 는 `--json-schema` 의 `structured_output`
  (`planner_codex.py:315-348, 397`)로 구조화 plan 을 받는다.
- **모른다:** `opencode serve` 1.17.13 의 `prompt_async`/`message` API 가 JSON schema 강제 출력을 지원하는지
  이 repo 에 증거가 없다(grep 결과 없음). 
- 가능한 형태: (a) read-only 권한 agent 로 한 turn 을 돌려 마지막 assistant text 를 strict JSON 으로 parse +
  `plan_schema` 검증(schema 강제 아님, 실패 시 Hold). (b) OpenCode 는 planner 를 두지 않고 codex/claude planner 가
  plan 한다. 운영자 결정 D5: (b), OpenCode planner 없음. Open O1 참고.

## Config / CLI Changes

- `LocalConfig.opencode: OpenCodeEntry | None` (credential_home, model, provider_id, egress, enabled).
  `AppEntry.opencode_qualification_report` / `opencode_container_profile` (optional). local config 는 wire
  schema 가 아니다(`schema_version "local-1"`, `local_deployment.py:158`).
- `ops local-driver opencode --enable/--disable`.
- `ops local-opencode ...`(설정 추가) 는 launcher·auth 이후에 만든다.
- `DRIVER_IDS`/`ACCOUNTS`/`TRANSPORTS` 확장과 `ROUTER_ORDER` append 는 반영했다.

## Decisions (Operator 2026-09-29)

이전 draft 의 "Decisions Needed" D1-D6 에 대한 운영자 결정이다.

| # | 결정 | 반영 |
| --- | --- | --- |
| D1 | design 을 따른다. `design-reference/design/11_DRIVERS_SESSIONS.md:36` 이 OpenCode Server 자격을 "password/auth explicitly on, async 204 + event correlation, SSE recovery, permissions" 로 정한다. driver 는 server 의 SSE event stream 을 소비하고 profile 은 `transport: "http_sse"`(3.0.0 enum 에 있음)를 기록한다 | `OpenCodeEvents`, `OpenCodeDriver(subscribe=True)`, `codex.py` `TRANSPORTS` |
| D2 | driver_id `opencode-server` | `DRIVER_IDS["opencode"]`, qualifier report key |
| D3 | opencode 를 app image(`deployment/worker-app/Dockerfile`)에 설치한다. 실행과 검증이 한 environment 를 쓴다 | Dockerfile, `scripts/app_image_up.sh` 의 `tools.opencode`, qualifier `--container-profile`. image build/push 는 하지 않았다 |
| D4 | router 순서 `codex, claude, opencode` (append) | `ROUTER_ORDER` |
| D5 | OpenCode planner 없음 | 변경 없음. 아래 Open O1 참고 |
| D6 | model-profile `provider` = `opencode-go` | `ACCOUNTS["opencode"]` |
| Auth | "정식으로" = design 의 "password/auth explicitly on" 유지. PR #28(D-087, 비밀번호 제거)은 보류 | 아래 Auth Options. 선택 전이라 구현은 stub 뿐이다 |

### Polling 이 fallback 으로 남는 근거

- design 은 driver interface 를 `poll/subscribe` 한 행으로 두고 입력을 "exact run handle, after cursor",
  출력을 "dedupe 가능한 event ID" 로 정한다(`design-reference/design/11_DRIVERS_SESSIONS.md:19`). 요구
  자격은 "SSE recovery" 다(`:36`).
- pinned server 는 cursor 로 이어받을 수 없다. 사실(1.17.13 binary, 아래 Event Stream):
  SSE frame 에 `id:` 가 없고(`id:void 0`), stream 은 살아 있는 bus 구독뿐이라 replay buffer 가 없다.
- 그래서 recovery 는 "재연결 시 REST 로 상태를 다시 읽고 이후 event 를 그 위에 접는다" 로 구현한다. stream 이
  없거나 끊긴 동안 `poll()` 은 REST read(`session/status`, `session/{id}/message`)로 답한다. 연결이
  살아 있고 reconcile 이 끝난 session 은 event 만으로 답하고 REST 를 부르지 않는다
  (`tests/v3/test_031_opencode_events.py::test_204_is_acceptance_and_completion_comes_from_the_correlated_event`
  의 `rest_reads` 검사).

## Event Stream

근거: pinned image `localhost:5000/amplai-worker-opencode@sha256:773c1b7a…` 의
`/usr/local/lib/node_modules/opencode-ai/bin/opencode.exe`(bun 단일 binary, sha256
`97cec34266f1fb21752755c1539a9accc1b5a1b8b3d1642046db9c15f424da54`)를 `docker run --rm --network none
--entrypoint cat` 으로 꺼내 문자열을 읽었다. 아래 offset 은 그 파일의 byte offset 이다.

| 사실 | 근거 |
| --- | --- |
| instance stream `/event` 는 `{id, type, properties}` 를 `data` 로 보낸다. 첫 event 는 `server.connected`, 10초마다 `server.heartbeat` | binary 의 `event connected` handler 부근(`"server.heartbeat"` 첫 출현) |
| global stream `/global/event` 는 `{payload:{id,type,properties}}` 를 보낸다 | `function TX()` 부근 |
| SSE frame 은 `{_tag:"Event",event:"message",id:void 0,data:JSON.stringify(n)}` 이다. `id:` 줄이 없다 | `function iX(` / `function PX(` |
| `/event` 는 요청한 instance directory 의 event 만 보낸다 | `function rX(` 의 `G.location?.directory===i.directory` |
| event type: `session.status {sessionID,status:{type}}`, `session.idle {sessionID}`, `message.updated {info}`, `message.part.updated`, `session.error`, `permission.asked` | `"session.idle"` 정의(`wn=`, `cn=`), TUI/CLI event switch |
| `Last-Event-ID` 처리 흔적은 bun 런타임 문자열뿐이고 server replay 는 찾지 못했다 | 모른다 수준의 negative evidence. 측정 필요(M2) |

구현(`src/amplai_foundry/agent_drivers/http.py`):

- `BoundHttp.sse(path)` — 같은 path 검사, status 검사, 줄 길이 상한(기본 1 MiB, 넘으면 `HTTP_BODY_LIMIT`),
  빈 줄로 frame 구분, 여러 `data:` 줄 결합, 연결 오류면 iteration 종료.
- `OpenCodeEvents` — tracked session(session → request message id)만 접는다. event `id` dedupe(최근 4096).
  `server.connected` 마다 tracked session 을 REST 로 reconcile. completion 은 session 당 첫 번째 것만 보관.
  같은 turn(`parentID`)에 다른 `sessionID` 의 assistant 응답이 오면 `SESSION_CORRELATION` Hold.
  background thread 가 재연결한다(0.5초 → 최대 5초 backoff).
- `OpenCodeDriver(subscribe=True)` — `start`/`resume` 은 prompt 전에 stream 을 연다(최대 10초 대기, 못 열면
  REST recovery). 204 는 `accepted` 일 뿐 완료가 아니다. 완료는 correlated `message.updated`(completed) +
  `idle` + boundary probe 가 모두 있어야 한다. `probe()["transport"]` 는 `http_sse`.
- exec transport 주의: `scripts/opencode_qualify.py` 의 `DockerExecTransport` 는 `subprocess.run` 으로 응답을
  한 번에 받는다(`:218-275`). SSE 를 흘리려면 launcher 의 transport 가 `curl -N` 을 `Popen` 으로 streaming
  해야 한다. launcher 는 아직 stub 이다.

## Auth Options

질문: server 비밀번호를 켜되 agent 의 bash/tool subprocess 가 그 값을 상속하지 않게 할 수 있는가.

기준 사실(1.17.13 binary):

| 사실 | 근거(offset) |
| --- | --- |
| `serve` 의 option 은 `port, hostname, mdns, mdns-domain, cors` 뿐이다. `--password` 는 `run --attach` 같은 client 쪽 option 이다 | `g6={port:…}` 정의, `--password` 의 describe "basic auth password (defaults to OPENCODE_SERVER_PASSWORD)" |
| config file 의 `server` 는 `port, hostname, mdns, mdnsDomain, cors` 만 읽는다 | `function K0(D,u)` |
| server 인증 설정은 env `OPENCODE_SERVER_PASSWORD`/`OPENCODE_SERVER_USERNAME` 에서만 읽는다(`@opencode/ServerAuthConfig`). 비밀번호가 없거나 빈 문자열이면 인증이 꺼진다(`required = isSome && value!==""`) | offset 96086713 |
| bash tool env = `{...process.env, ...shell.env hook 결과}` | `ShellTool.shellEnv`, offset 96551656 |
| session shell 명령 env = `extendEnv:true` + `shell.env` hook | offset 96493731 |
| PTY env = payload env + `shell.env` hook (process.env 상속 여부는 모른다) | offset 96218736 |
| formatter 실행 `extendEnv:true`, hook 없음 | offset 96628958 |
| git 실행 `extendEnv:true`, hook 없음 | offset 96660913 |
| plugin 은 config directory 의 `{plugin,plugins}/*.{ts,js}` 에서 읽는다 | offset 101247694 |
| bash tool shell 은 `process.env.SHELL` 로 고른다 | `n8(_)` 의 `cA(process.env.SHELL)` |

| 옵션 | 내용 | 사실 / 모름 | 판정 |
| --- | --- | --- | --- |
| A | config file 또는 serve flag 로 비밀번호 전달 | 사실: 둘 다 없다(`g6`, `K0`, ServerAuthConfig) | 불가 |
| B | env 로 비밀번호 + read-only config dir 의 plugin 이 `shell.env` hook 에서 `OPENCODE_SERVER_PASSWORD: ""`(와 USERNAME)를 돌려준다 | 사실: bash tool·session shell·PTY 는 hook 결과가 process.env 를 덮는다(96551656, 96493731, 96218736). 사실: formatter·git 은 hook 없이 상속한다(96628958, 96660913). 모름: LSP/MCP subprocess. 추정: agent 는 같은 uid(65534)라 `/proc/<server pid>/environ` 을 읽을 수 있다(Linux 동작, 측정 필요 M1) | 부분 차단 |
| C | env 로 비밀번호 + `SHELL` 을 `env -u OPENCODE_SERVER_PASSWORD` wrapper 로 지정 | 사실: bash tool shell 은 `process.env.SHELL` 로 정한다. 효과는 B 와 같은 경로뿐이고 wrapper 앞의 process env 는 그대로다 | B 의 보강. 단독으로는 부족 |
| D | plugin 이 로드 시 `delete process.env.OPENCODE_SERVER_PASSWORD` | 모름: auth layer 가 plugin 로드 전에 값을 읽는지. 늦게 읽으면 인증이 꺼진다(fail-open, `required` 가 false). 추정: `/proc/<pid>/environ` 은 시작 시 env 를 그대로 보여 준다 | 권장 안 함 |
| E | uid 분리: server 는 uid A(비밀번호 보유), tool 은 uid B | 모름: OpenCode 에 tool 실행 uid 를 바꾸는 설정이 있다는 증거가 없다. C 의 wrapper 가 `setpriv`/`sudo` 로 uid 를 바꾸는 방식은 image 에 권한 도구와 정책이 필요하다 | agent 에 대한 실제 경계. 설계·측정 필요 |

권장: **B + C 를 기본으로 하고 M1 측정 결과로 판정한다.** B+C 는 design 의 "password/auth explicitly on" 을
지키고 D-087 이 측정한 bash `env` 누출(`specs/017-external-qualification-closure/spec.md:44-45`) 경로를 막는다.
M1 에서 `/proc/<server pid>/environ` 이나 formatter/git 경로로 값이 보이면, 같은 uid 에서는 비밀번호가 agent 에게
비밀이 아니다. 그때 비밀번호는 다른 주체(host·다른 container)에 대한 인증으로만 쓰고, agent 에 대한 경계는
E(uid 분리)로 따로 결정한다. 같은 network namespace 의 tool 이 loopback server 에 닿는 것은 비밀번호 유무와
무관하게 사실이다(tool 은 server 가 spawn 한 같은 container process 다).

구현 상태: `OpenCodeServer.password` 필드만 두었다(`opencode_port.py`). 값이 있으면 driver 가 Basic auth 로
붙고, 없으면 D-087 경로(`loopback_exec`)다. plugin/wrapper/launcher 는 만들지 않았다.

## Still Needs A Real Measurement

- M1 auth: 선택한 auth 방식으로 bash `env`, `/proc/*/environ`, formatter/git subprocess env 에 비밀번호가 보이는지,
  인증 없는 요청이 401 인지.
- M2 SSE: 실제 `opencode serve` 에서 event 순서(`message.updated` completed 와 `session.idle` 의 순서), 재연결
  시 replay 가 정말 없는지, heartbeat 10초.
- M3 exec transport streaming: `docker exec curl -N` 이 SSE 를 끊김 없이 흘리는지, 끊긴 뒤 재연결.
- M4 resume: server 재시작 후 같은 home 에서 session 이 복원되는지(R3).
- M5 app image: opencode 를 넣은 app image build 후 qualifier(`--container --container-profile
  deployment/local-container-app-<app>.json --out <per-image file>`) 9 check + `tool_use`(long_command 포함).
- M6 startup 비용(R1), 시계 drift(R2).

## Open

- O1 (D5 결과): planner 는 선택된 driver id 로 찾는다(`product.py` `_planner`, `installed.planners.get(driver_id)`).
  OpenCode 가 선택되면(codex·claude 가 모두 빠진 경우) planning 은 `PLANNER_UNAVAILABLE` 로 Hold 한다.
  "OpenCode 실행 + 다른 driver planner" 조합을 허용할지는 결정이 필요하다.
- O2: launcher(container 시작/health/stop, streaming exec transport)는 auth 선택 뒤 구현한다.
- O3: `_compose` 는 opencode 가 켜져 있으면 여전히 `OPENCODE_NOT_WIRED` 로 Hold 한다(launcher 와 auth 대기).

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

Done (이 branch):
1. `PerDispatchOpenCodePort` + `OpenCodeServerLauncher`/`OpenCodeServer` + `ScopedOpenCodeHome`
   (`src/amplai_foundry/agent_drivers/opencode_port.py`). driver 는 `subscribe=True`.
2. SSE 소비: `BoundHttp.sse`, `OpenCodeEvents`, `OpenCodeDriver(subscribe=...)` (`http.py`).
3. `DRIVER_IDS`/`ACCOUNTS`/`TRANSPORTS`(`codex.py`), model-profile id 의 `/` → `.`(`common.schema.json` id
   pattern 에 `/` 가 없다), `ROUTER_ORDER` append(`product.py`).
4. `OpenCodeEntry` config, `ops local-driver opencode`.
5. app image Dockerfile + `app_image_up.sh` 의 `tools.opencode`, qualifier 의 `--container-profile`, report key
   `opencode-server`, top-level `container_image`, container 모드 전용 `--out`.

Stub: `PendingDockerLauncher`(`OPENCODE_LAUNCHER_PENDING`), `_compose` 의 `OPENCODE_NOT_WIRED`, auth 방식.

## Sources

위 표의 path:line.
