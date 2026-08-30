---
doc_id: amplai-local-supervisor
title: AMPLAI Local Supervisor Operations
status: canonical
runtime_protocol: amplai.async-cross-app.v1
---

# AMPLAI Local Supervisor

Local Supervisor는 중앙 Project Store의 READY Work를 각 앱의 Claude Code, Codex 또는 명령형
worker에 연결하는 작은 deterministic process다. LLM이 아니며 의미 판단을 하지 않는다.

## 책임

- dependency reconciliation (`WAITING → READY`)
- app busy/idle 및 `max_concurrency` 확인
- atomic claim + lease + heartbeat
- configured worker process 시작
- worker가 남긴 durable 상태 확인
- lease timeout/worker crash 복구
- 독립 앱 Work의 병렬 실행

## 하지 않는 일

- 목표/affected app/Work를 스스로 발명
- architecture·contract·domain 결정
- code edit, merge, push, deploy, release
- prose handoff를 파싱해 state 추론
- `DRAFT|BLOCKED|HUMAN_REQUIRED|FAILED` Work 자동 실행

## 실행

```bash
export AMPLAI_PROJECT_HOME=$HOME/workspace/amplai-project

python3 scripts/amplai_supervisor.py --dry-run
python3 scripts/amplai_supervisor.py --once
python3 scripts/amplai_supervisor.py --run
python3 scripts/amplai_supervisor.py --once --include-manual
```

`--include-manual`은 명시적 foreground 실행 경계다. `--dry-run`은 Work를 claim하지 않고 attempt도
증가시키지 않으며, retry backoff 때문에 아직 실행하지 않는 Work를 `deferred_by_backoff`로 보여준다.

Supervisor는 worker가 끝나거나 `poll_seconds`가 지날 때까지 대기한 뒤에만 Store를 다시 훑는다.
`--once`는 한 pass에서 아무것도 띄우지 못하면 종료한다. backoff가 풀리기를 기다리지 않으므로,
backoff 대상이 남아 있으면 다시 실행하거나 `--run`을 쓴다.

## Claude Code worker

runner가 `claude-code`이면 다음 의미로 실행한다.

```text
claude <configured args> --output-format json [--resume <session-id>] -p <durable work prompt>
```

Prompt의 controller 진입점은 `/work`다. 결과 JSON에서 session id를 회수해 host-local checkpoint에
저장한다.

## Codex worker

runner가 `codex`이면 다음 의미로 실행한다.

```text
codex exec --json <configured args> [resume <thread-id>] <durable work prompt>
```

Prompt의 controller 진입점은 `$work`다. JSONL stream의 `thread.started.thread_id`를 우선 회수해
host-local checkpoint에 저장하고 다음 실행에서 resume한다. worker command log에서는 두 native
runner 모두 실제 prompt를 `<prompt>`로 마스킹한다.

Codex 권한은 runner args가 정한다. Installer는 sandbox나 approval을 높이지 않는다. 파일 변경이
필요하면 검토된 최소 sandbox를 명시하고, `--dangerously-bypass-approvals-and-sandbox`, `--yolo`,
`danger-full-access`를 unattended `auto_start=true`와 조합하지 않는다. `project verify`가 이 조합을
WARNING으로 보고한다.

## 공통 worker 환경

Lease token은 prompt나 argv에 넣지 않고 환경변수로 전달한다.

```text
AMPLAI_PROJECT_HOME
AMPLAI_APP_ID
AMPLAI_WORK_ID
AMPLAI_CHANGE_ID
AMPLAI_LEASE_TOKEN
AMPLAI_SUPERVISED=1
```

Worker는 종료 전에 같은 Work를 `DONE|WAITING|BLOCKED|HUMAN_REQUIRED|FAILED`로 전환해야 한다.
정상 종료했는데 상태가 `CLAIMED|RUNNING`이면 Supervisor는 오류로 기록하고 retry policy를 적용한다.

## 장애 복구

- 프로세스가 죽어도 CR/Work/Decision/Evidence는 중앙 Store에 남는다.
- heartbeat가 끊겨 lease가 만료되면 `reconcile`이 Work를 복구한다.
- attempt가 `max_attempts`에 도달하면 `FAILED`로 남겨 무한 loop를 막는다.
- 재시도는 `retry_not_before` backoff를 지나야 시작한다.
- `FAILED` Work는 `work reset-attempts`로 예산을 다시 주거나 `work cancel`로 정리한다.
- lease와 lock은 호스트 로컬이다. 다른 호스트가 claim한 Work는 회수하지 않는다.
- 같은 질문/같은 revision은 ID와 상태/hash로 중복 처리하지 않는다.
- 동일 원인의 app ping-pong이 반복되면 Question을 CHALLENGE/HUMAN으로 올린다.

## 권장 운영

초기에는 각 앱 `auto_start=false`로 등록하고 `--include-manual`로 관찰한다. host별 실제 CLI smoke,
work terminal transition, session/thread resume를 통과한 앱만 `auto_start=true`로 바꾼다. 한 앱 한
active Work에서 시작하고, 병렬 workspace/merge scheduling은 미래 Global AMPLAI 단계로 남긴다.
