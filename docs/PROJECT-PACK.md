# Project Pack Contract

Project Pack은 한 프로젝트의 canonical 자산과 파생 runtime 경계를 이동 가능한 단위로 묶는다. 절대 clone 경로는 identity에 포함되지 않는다.

```text
project-root/
├── .amplai/
│   ├── project.yaml
│   ├── domain.lock.yaml
│   ├── intake/        # runs, roadmap reports/proposals, evaluation seed
│   ├── proposals/
│   ├── semantic/
│   ├── ontology/
│   ├── skills/
│   ├── policies/
│   ├── agents/
│   ├── evals/
│   └── runtime/       # derived, archive 제외
└── vault/projects/amplai/
```

`project.yaml`은 stable `project_id`, `/project/{project_id}`로 끝나는 namespace, alias, default 여부, canonical 상대경로와 runtime 상대경로를 선언한다. 모든 경로는 pack root 안에 있어야 한다. `domain.lock.yaml`은 import의 exact version을 고정하며 누락, 불일치, cycle은 validation error다.

## Commands

```bash
amplai-foundry project list --workspace .
amplai-foundry project show amplai
amplai-foundry project validate amplai
amplai-foundry project rebuild amplai
amplai-foundry project pack amplai --output dist/amplai.zip
```

`rebuild`는 canonical Memory에서 `.amplai/runtime/index/memory-index.json`을 재생성한다. `pack`은 timestamp를 고정한 deterministic ZIP을 만들며 runtime, `.env`, key/credential 파일과 외부를 가리키는 symlink를 포함하지 않는다.

여러 Pack은 같은 local ID를 가질 수 있다. lookup과 relation은 namespace로 한정한다.
Workspace 수준의 비교에서는 `target_namespace`로 cross-project 대상을 명시할 수
있지만, 독립 archive의 유효성을 보장하기 위해 한 Pack만 검증·패키징할 때는 bundle에
포함되지 않은 외부 Memory reference를 거부한다.

Project Pack은 runner profile, managed worktree, Slack activation ledger 또는 Slack status outbox를 canonical
Memory로 포함하지 않는다. 그것들은 Project Store/host-local runtime state이며 Pack의 source of truth가 아니다.
