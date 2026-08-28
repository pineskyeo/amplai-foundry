---
doc_id: amplai-local-supervisor
title: AMPLAI Local Supervisor Operations
status: canonical
runtime_protocol: amplai.async-cross-app.v1
---

# AMPLAI Local Supervisor

Local Supervisor는 중앙 Project Store의 READY Work를 각 앱의 Claude Code 또는 명령형 worker에
연결하는 작은 deterministic process다. LLM이 아니며 의미 판단을 하지 않는다.

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

# 실행할 Work만 확인; claim/attempt 변화 없음
python3 scripts/amplai_supervisor.py --dry-run

# 자동 시작이 허용된 앱의 Work를 quiescent할 때까지 처리
python3 scripts/amplai_supervisor.py --once

# 계속 감시
python3 scripts/amplai_supervisor.py --run

# auto_start=false인 앱도 명시적으로 포함
python3 scripts/amplai_supervisor.py --once --include-manual
```

`--include-manual`은 명시적 foreground 실행 경계다. `--dry-run`은 Work를 claim하지 않고 attempt도
증가시키지 않으며, retry backoff 때문에 아직 실행하지 않는 Work를 `deferred_by_backoff`로 보여준다.

Supervisor는 worker가 끝나거나 `poll_seconds`가 지날 때까지 대기한 뒤에만 Store를 다시 훑는다.
`--once`는 한 pass에서 아무것도 띄우지 못하면 종료한다. backoff가 풀리기를 기다리지 않으므로,
backoff 대상이 남아 있으면 다시 실행하거나 `--run`을 쓴다.

## Claude Code worker

앱 local binding의 runner가 `claude-code`이면 Supervisor는 다음 의미로 실행한다.

```text
claude <configured args> --output-format json [--resume <session-id>] -p <durable work prompt>
```

Prompt에는 secret lease token을 넣지 않는다. token은 `AMPLAI_LEASE_TOKEN` 환경변수로 전달하며
`scripts/amplai.py`가 이 변수를 직접 읽으므로 `--lease-token`을 명령줄에 쓸 필요가 없다.
run log의 command에는 prompt를 `<prompt>`로 마스킹한다.

worker 권한은 runner args가 정한다. `--dangerously-skip-permissions`를 `auto_start=true`와
함께 쓰면 사람이 보지 않는 동안 그 repository에서 무엇이든 실행될 수 있고, Runtime은 이를 막지
못한다. `project verify`가 이 조합을 WARNING으로 보고한다. Work가 WAITING 후 다시 READY가 되면 저장된
session ID를 사용할 수 있지만, 진짜 상태는 언제나 Project Store다.

Worker 환경:

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
- 재시도는 `retry_not_before` backoff를 지나야 시작한다. 실패한 worker를 즉시 다시 띄우지 않는다.
- `FAILED`로 굳은 Work는 `work reset-attempts`로 예산을 다시 주거나 `work cancel`로 포기시킨다.
  포기시키면 그에 의존하던 Work도 `--cascade` 또는 `work retarget`으로 함께 정리해야 한다.
- lease와 lock은 호스트 로컬이다. 다른 호스트가 claim한 Work는 회수하지 않는다.
- 같은 질문/같은 revision은 ID와 상태/hash로 중복 처리하지 않는다.
- 동일 원인의 app ping-pong이 반복되면 새 Work를 계속 만들지 말고 Question을 CHALLENGE/HUMAN으로
  올려 coordination loop를 끊는다.

## 권장 운영

초기에는 각 앱 `auto_start=false`로 등록하고 `--include-manual`로 관찰하면서 사용한다. golden
scenario가 안정화된 앱만 `auto_start=true`로 바꾼다. 한 앱 한 active Work에서 시작하고, 병렬
workspace/merge scheduling은 미래 Global AMPLAI 단계로 남긴다.
