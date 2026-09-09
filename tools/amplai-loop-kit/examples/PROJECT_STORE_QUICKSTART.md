# Project Store quick start

## Claude Code app

```bash
APP=/path/to/app
PROJECT=$HOME/workspace/amplai-project

cd "$APP"
python3 scripts/amplai.py project init --home "$PROJECT" --id ai-platform --name "AI Platform"
export AMPLAI_PROJECT_HOME="$PROJECT"
python3 scripts/amplai.py --project-home "$PROJECT" app register \
  --id <app-id> --repo "$APP" --runner claude-code
python3 scripts/amplai.py project verify
```

## Codex app

```bash
python3 scripts/amplai.py --project-home "$PROJECT" app register \
  --id <app-id> --repo "$APP" --runner codex \
  --runner-arg=--sandbox --runner-arg=workspace-write
python3 scripts/amplai.py project verify
```

Native runner의 `--command`를 생략하면 각각 `claude`, `codex`가 기본값이다. 다른 앱도 같은
`PROJECT`에 등록한다. CR/Work가 만들어진 뒤에는
`python3 scripts/amplai_supervisor.py --dry-run`으로 실행 계획부터 확인한다.

한 앱에 여러 runner를 허용하려면 `--runner-profiles-json`과 `--default-runner-profile`을 등록한다.
각 Work는 `controller`, `runner_profile`, `base_ref`를 고정하고 Supervisor가 managed worktree에서 실행한다.
