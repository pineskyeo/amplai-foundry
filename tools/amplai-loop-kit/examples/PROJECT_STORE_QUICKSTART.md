# Project Store quick start

```bash
APP=/path/to/app
PROJECT=$HOME/workspace/amplai-project

cd "$APP"
python3 scripts/amplai.py project init --home "$PROJECT" --id ai-platform --name "AI Platform"
export AMPLAI_PROJECT_HOME="$PROJECT"
python3 scripts/amplai.py --project-home "$PROJECT" app register \
  --id <app-id> --repo "$APP" --runner claude-code --command claude
python3 scripts/amplai.py project verify
```

다른 앱도 같은 `PROJECT`에 등록한다. CR/Work가 만들어진 뒤에는
`python3 scripts/amplai_supervisor.py --dry-run`으로 실행 계획부터 확인한다.
