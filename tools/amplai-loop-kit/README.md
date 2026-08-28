# AMPLAI Loop Kit 2.3.0

AMPLAI Loop V2 앱을 위한 **Decision & Async Cross-App Runtime** 배포 패키지다.
정본은 `amplai-foundry` 가 소유하고 등록된 앱들에 배포한다 (`PROVENANCE.md`).
각 앱에 같은 Kit을 설치하면 지금은 사람이 각 App Agent를 사용할 수 있고, 이후 Hermes/Global
AMPLAI가 동일한 Project Store/Work protocol로 앱 Runtime을 오케스트레이션할 수 있다.

## 포함 기능

- 공개 entry point 유지: `/design`, `/work`
- Question → Evidence(DIRECT/LOCAL/PARALLEL) → Decision(AUTO/CHALLENGE/HUMAN)
- 중앙 Git Project Store: CR, Work, Contract, Decision, Evidence, event hash chain
- WAITING/READY dependency, atomic claim, lease, heartbeat, crash recovery
- deterministic Local Supervisor와 앱 사이 비동기 재개
- Claude Code `-p` worker, optional session resume, additive hooks
- handoff를 SSOT가 아닌 target-app Context view로 생성
- 신규 설치와 기존 Kit 업데이트를 위한 non-destructive installer

## 요구사항

- Python 3.6 이상
- Git
- 기존 AMPLAI Loop V2 앱 (`.agents/skills/work/SKILL.md`, `design/SKILL.md`)
- Claude Code worker를 쓸 경우 `claude` CLI

### 앱마다 있을 수도, 없을 수도 있는 것

`manifest.json` 의 marker 대상 중 셋은 `required: false` 다. **어떤 앱에는 있고 어떤 앱에는
없기 때문이고, 없어도 설치가 진행된다.**

| marker 대상 | 없을 때 |
|---|---|
| `.agents/skills/handoff/SKILL.md` | 건너뛴다. `handoff` skill 이 있는 앱에서만 병합한다 |
| `.ai-team/skills/handoff/SKILL.md` | 같다 |
| `.ai-team/AUTONOMY_POLICY.md` | 헤더와 함께 새로 만든다 (2.2.0 부터) |

`handoff` skill 은 모든 Loop V2 앱이 갖는 것이 아니다. **kit 은 그 skill 이 있는 앱을
전제하지 않는다** — 있으면 marker 절을 넣고 없으면 그냥 넘어간다. 설치 결과의 action 수가
앱마다 다른 것은 이 때문이다.

## 1. 패키지 검증

```bash
python3 selftest.py
```

## 2. 앱에 설치 또는 업데이트

```bash
python3 install.py \
  --target /path/to/app \
  --app-id <app-id> \
  --project-id ai-platform
```

동일 명령을 다시 실행하면 idempotent update가 된다. 설치 기록은
`.ai-team/install/amplai-loop-kit.json`에 남는다.

중앙 저장소까지 초기화하고 앱을 등록하려면:

```bash
python3 install.py \
  --target /path/to/app \
  --app-id <app-id> \
  --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project" \
  --runner claude-code \
  --runner-command claude
```

기본은 `auto_start=false`다. 안정화 후에만 `--auto-start`를 사용한다.

## 3. 다른 앱 업데이트

```bash
python3 install.py --target /path/to/cortex \
  --app-id cortex --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project"

python3 install.py --target /path/to/recipe-studio \
  --app-id recipe-studio --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project"
```

## 충돌 정책

- Kit이 소유하는 파일을 사용자가 수정했다면 기본 설치는 중단한다.
- Skill/policy/workflow는 marker 내부만 Kit이 소유하며 marker 밖 내용은 보존한다.
- `.claude/settings.json`의 기존 permissions와 hooks는 보존한다.
- `--force`는 충돌 파일을 `.ai-team/backups/amplai-loop-kit/<timestamp>/`에 백업한 뒤 교체한다.
- 모든 preflight가 끝난 뒤 쓰며, 쓰기 실패 시 target repository 변경을 rollback한다.

먼저 확인만 하려면:

```bash
python3 install.py ... --dry-run
```

## 제거

```bash
python3 install.py --target /path/to/app --uninstall --dry-run
python3 install.py --target /path/to/app --uninstall
```

Kit이 설치한 파일, marker section, hook, JSON rule만 되돌린다. 중앙 Project Store는 건드리지
않는다.

## 손상 복구

수동 편집이나 중단된 쓰기로 `content_hash`가 어긋나면 Runtime은 읽기를 멈춘다. 다시 기록한다:

```bash
python3 scripts/amplai.py project reseal --all
```

막힌 Work를 푸는 명령:

```bash
python3 scripts/amplai.py work cancel --id CR-0001-W002 --reason "설계 변경" --cascade
python3 scripts/amplai.py work retarget --id CR-0001-W003 --depends-on CR-0001-W001
python3 scripts/amplai.py work reset-attempts --id CR-0001-W002
```

## 중앙 Project Store

`/tmp`를 사용하지 않는다. 예:

```bash
export AMPLAI_PROJECT_HOME="$HOME/workspace/amplai-project"
python3 /path/to/app/scripts/amplai.py project status
python3 /path/to/app/scripts/amplai_supervisor.py --dry-run
python3 /path/to/app/scripts/amplai_supervisor.py --once --include-manual
```

상세 규약은 설치 후 다음 문서를 본다.

- `.ai-team/runtime/DECISION_ASYNC_PROTOCOL.md`
- `.ai-team/runtime/LOCAL_SUPERVISOR.md`
- `.ai-team/runtime/INSTALLATION.md`
