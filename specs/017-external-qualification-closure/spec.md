# Work 017 — External Qualification Closure (locally closable items)

**Work ID**: AMPLAI-V3-RC05 · **Created**: 2026-09-27 · **Type**: operations + bug_fix

## Goal

`specs/015-external-qualification/external-qualification-status.json` 의 미해결 항목 중 이
workstation 에서 실측으로 진전시킬 수 있는 것을 측정하고, 측정으로 드러난 driver 결함을 고친다.
외부 계정·장비가 필요한 항목은 건드리지 않고 남은 목록으로 둔다.

## Scope

1. main CI 실패 (`test_append_cost_does_not_grow_with_history`, run 35563398959) 원인 확인
2. OpenCode server 실제 모델 turn + workspace boundary probe (`scripts/opencode_qualify.py`)
3. 컨테이너 memory/pids/cpus 제한 실측 (`scripts/limits_qualify.py`)
4. ledger note/evidence 갱신 (status 값은 측정이 바꾼 경우에만 바꾼다)

## Non-goals

podman 설치, opencode 버전 업그레이드, OpenAI API key·OTLP collector·두 번째 host 준비,
ReleaseSet 생성과 migration drill, push/merge.

## Results (measured 2026-09-27)

| 항목 | 결과 | Evidence |
|---|---|---|
| CI flaky | rerun 녹색 (validate 3.11/3.12). 로컬 5/5 pass. 단일 wall-clock 표본 → 각 2회 min 으로 보강 | `tests/ai/test_amplai_kit_regressions.py` |
| OpenCode | 6/9 pass, fail 2 (secret_isolation, crash_recovery), egress inconclusive | `driver-qualification.json#reports.opencode-server` |
| Container limits | 필수 5/5 pass, CPU throttling under load inconclusive | `deployment/local-limits-qualification.json` |

### Driver defects found with the real server (fixed, regression tests first)

- **Non-ascending `messageID`**: `OpenCodeDriver` 가 `"msg_" + digest` 를 보냈다. opencode 1.17.13 은
  session message 를 ID 순으로 정렬하므로 server 가 만든 reply ID 보다 뒤에 정렬되는 user ID 는
  turn 을 미응답으로 보이게 해 reply 를 끝없이 생성했다 (60 s 에 31 개, abort 로 정지) 또는 전혀
  생성하지 않았다. `ascending_message_id()` 가 server 형식(`ms * 0x1000` 12 hex + 14 base62)으로
  만들고 전송 전에 journal 에 저장한다. RED: `tests/v3/test_rc05_opencode_message_id.py`.
- **gzip 이중 decode**: `BoundHttp.request` 가 decode 된 bytes 에 원래 `content-encoding` header 를
  붙여 Response 를 재구성해 두 번째 decode 에서 실패했다 (`PROVIDER_UNREACHABLE`). encoding/length
  header 를 제거한다. byte budget 은 decode 된 크기에 계속 적용된다.

### OpenCode failures (host-side process model, not fixed here)

- `secret_isolation`: `opencode serve` 는 비밀번호를 `OPENCODE_SERVER_PASSWORD` env 로만 받고
  (`opencode serve --help` 에 다른 경로 없음) bash tool 이 이를 상속한다.
- `crash_recovery`: SIGKILL + restart 뒤 exact-session resume 과 no-replay 는 성립하지만 죽은
  server 의 tool process 가 고아로 남는다.

두 항목과 egress 는 OpenCode 를 container egress profile 위에서 돌리는 것이 다음 단계다 (추정:
container 종료가 process tree 를 함께 끝내고 env 는 profile 이 통제한다 — 측정 전).

## Remaining (outside this Work)

OpenCode in-container run, CPU throttling under load, podman, ReleaseSet 기반
migration/upgrade/rollback drill, OpenAI Responses (API key), OTLP collector, physical holdout +
independent approver, statistical population. `reuse.py` scheduler wiring 과 R404 cutover resume
verb 은 이 Work 범위 밖의 코드 후속이다.
