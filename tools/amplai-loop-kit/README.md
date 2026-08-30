# AMPLAI Loop Kit 2.4.0

AMPLAI Loop V2 앱을 위한 **Decision & Async Cross-App Runtime** 배포 패키지다.
정본은 `amplai-foundry`가 소유하고 등록된 앱들에 배포한다 (`PROVENANCE.md`).
각 앱에 같은 Kit을 설치하면 Claude Code와 Codex에서 같은 Work protocol을 사용할 수 있고,
이후 Hermes/Global AMPLAI가 동일한 Project Store를 통해 App Runtime들을 오케스트레이션할 수 있다.

## 제품 경계

- **AMPLAI Platform / Foundry**: 장기 제품. 지식·거버넌스·제안·증거·프로젝트 운영을 제공한다.
- **AMPLAI Loop Kit**: 그 제품과 다른 앱을 개발하기 위한 repository-local runtime이다.
- 의존 방향은 `Kit → Platform을 개발·검증`만 허용한다. Platform domain code가 Kit, `.ai-team`,
  Claude Code, Codex에 의존하면 안 된다.
- Platform과 Kit은 version 및 release train을 별도로 운용한다.

## 포함 기능

- 동일한 공개 의미: Claude Code `/design`, `/work`; Codex `$design`, `$work`
- Question → Evidence(DIRECT/LOCAL/PARALLEL) → Decision(AUTO/CHALLENGE/HUMAN)
- 중앙 Git Project Store: CR, Work, Contract, Decision, Evidence, event hash chain
- WAITING/READY dependency, atomic claim, lease, heartbeat, crash recovery
- deterministic Local Supervisor와 앱 사이 비동기 재개
- HostAdapter로 분리된 Claude Code non-interactive worker와 native Codex `exec --json` worker
- Claude/Codex session continuation 및 additive lifecycle hooks
- Decision/Evidence federation lifecycle: `LOCAL → CANDIDATE → SUBMITTED → ACCEPTED/REJECTED`
- handoff를 SSOT가 아닌 target-app Context view로 생성
- 신규 설치와 기존 Kit 업데이트를 위한 non-destructive installer

## 요구사항

- Python 3.6 이상
- Git
- 기존 AMPLAI Loop V2 앱 (`.agents/skills/work/SKILL.md`, `design/SKILL.md`)
- 선택한 worker의 CLI: `claude` 또는 `codex`

### 앱마다 있을 수도, 없을 수도 있는 것

`manifest.json`의 marker 대상 중 일부는 `required: false`다. 어떤 앱에는 있고 어떤 앱에는
없기 때문이고, 없어도 설치가 진행된다.

| marker 대상 | 없을 때 |
|---|---|
| `.agents/skills/handoff/SKILL.md` | 건너뛴다. 해당 skill이 있는 앱에서만 병합한다 |
| `.ai-team/skills/handoff/SKILL.md` | 같다 |
| `.ai-team/AUTONOMY_POLICY.md` | 헤더와 함께 새로 만든다 |

## 1. 패키지 검증

```bash
python3 selftest.py
python3 seal.py --verify
```

## 2. 앱에 설치 또는 업데이트

Project Store를 연결하지 않고 repository runtime만 설치한다.

```bash
python3 install.py \
  --target /path/to/app \
  --app-id <app-id> \
  --project-id ai-platform
```

동일 명령을 다시 실행하면 idempotent update가 된다. 설치 기록은
`.ai-team/install/amplai-loop-kit.json`에 남는다.

중앙 저장소까지 초기화하고 Claude Code 앱으로 등록한다.

```bash
python3 install.py \
  --target /path/to/app \
  --app-id <app-id> \
  --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project" \
  --runner claude-code \
  --runner-arg=--permission-mode \
  --runner-arg=acceptEdits
```

Codex 앱으로 등록한다. Installer는 sandbox나 approval을 자동 완화하지 않는다.

```bash
python3 install.py \
  --target /path/to/app \
  --app-id <app-id> \
  --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project" \
  --runner codex \
  --runner-arg=--sandbox \
  --runner-arg=workspace-write
```

`--runner-command`를 생략하면 native runner는 각각 `claude`, `codex`를 사용한다.
Project-local Codex hook은 처음 또는 정의 변경 후 Codex `/hooks`에서 검토·신뢰해야 실행된다.
Installer는 hook trust를 우회하지 않는다.
기본은 `auto_start=false`다. 실제 CLI로 foreground smoke test를 통과한 앱에서만
`--auto-start`를 사용한다.

## 3. 다른 앱 업데이트

```bash
python3 install.py --target /path/to/cortex \
  --app-id cortex --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project" --runner codex \
  --runner-arg=--sandbox --runner-arg=workspace-write

python3 install.py --target /path/to/recipe-studio \
  --app-id recipe-studio --project-id ai-platform \
  --project-home "$HOME/workspace/amplai-project" --runner claude-code
```

## Skill 정본과 host 표기

- `.agents/skills/`가 Claude/Codex 공통 workflow의 유일한 정본이다.
- `.claude/skills/*`는 해당 shared skill을 가리키는 exact symlink mirror다.
- 내부 capability는 Codex metadata에서 `allow_implicit_invocation: false`로 닫아
  `$work`/`$design` controller 우회를 막는다.
- 문서와 Contract에는 `work`/`design`이라는 의미를 저장하고, slash/dollar 표기는 host adapter가 맡는다.

## 충돌 정책

- Kit이 소유하는 파일을 사용자가 수정했다면 기본 설치는 중단한다.
- Skill/policy/workflow는 marker 내부만 Kit이 소유하며 marker 밖 내용은 보존한다.
- `.claude/settings.json`과 `.codex/hooks.json`의 기존 필드·permissions·hooks는 보존한다.
- uninstall은 install record에 적힌 AMPLAI hook만 제거한다.
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

수동 편집이나 중단된 쓰기로 `content_hash`가 어긋나면 Runtime은 읽기를 멈춘다. 다시 기록한다.

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

`/tmp`를 사용하지 않는다.

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
