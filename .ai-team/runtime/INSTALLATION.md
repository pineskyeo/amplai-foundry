---
doc_id: amplai-async-installation
title: AMPLAI Decision & Async Runtime Installation
status: canonical
---

# 설치와 업데이트

배포 단위는 `amplai-loop-kit` 하나다. 기존 AMPLAI Loop V2 앱에 안전하게 설치·업데이트한다.
공개 의미는 `work`와 `design` 둘이며 host별 표기는 Claude Code `/work`, `/design`; Codex
`$work`, `$design`이다.

## 중앙 저장소 초기화

```bash
python3 scripts/amplai.py project init \
  --home "$HOME/workspace/amplai-project" \
  --id ai-platform \
  --name "AI Platform"

export AMPLAI_PROJECT_HOME="$HOME/workspace/amplai-project"
```

`project init`은 현재 repository의 `.ai-team/runtime/async-policy.json`을 기본 policy로 사용하고
Git repository를 초기화한다. `/tmp`는 canonical Project Store로 사용하지 않는다.

## Claude Code 앱 등록

```bash
python3 scripts/amplai.py --project-home "$AMPLAI_PROJECT_HOME" app register \
  --id <app-id> \
  --repo "$PWD" \
  --runner claude-code \
  --runner-arg=--permission-mode --runner-arg=acceptEdits \
  --max-concurrency 1
```

`--command`를 생략하면 `claude`를 사용한다. 기존 Claude permissions를 자동으로 높이지 않으며,
worker args는 조직의 보안 정책에 맞게 직접 정한다.

## Codex 앱 등록

```bash
python3 scripts/amplai.py --project-home "$AMPLAI_PROJECT_HOME" app register \
  --id <app-id> \
  --repo "$PWD" \
  --runner codex \
  --runner-arg=--sandbox --runner-arg=workspace-write \
  --max-concurrency 1
```

`--command`를 생략하면 `codex`를 사용한다. Runtime은 `codex exec --json`을 호출하고 저장된
thread id가 있으면 `resume`으로 이어간다. Installer는 Codex sandbox나 approval policy를
자동 완화하지 않는다. 파일 변경이 필요한 Work에는 검토된 `--sandbox workspace-write` 같은
명시적 최소 권한을 runner args로 준다.

여러 host profile을 한 app binding에 둘 때는 `--runner-profiles-json`과
`--default-runner-profile`을 사용한다. 기존 singular `--runner` 설정도 계속 읽는다. Slack이나
Hermes는 app runner를 activate할 수 없고, DRAFT Work의 activation은 별도 human-authority path다.

`--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `danger-full-access`는 unattended worker에서
강한 우회다. `auto_start=true`와 조합하면 `project verify`가 WARNING으로 보고한다.

`--auto-start`를 생략하면 수동 앱이다. native CLI를 foreground에서 검증하기 전에는 자동 시작하지
않는다. Runtime이 실제로 강제하는 `policy.forbidden_automatic_actions` 항목은
`python3 scripts/amplai.py project verify`의 `policy_enforcement`에서 확인한다.

## Kit installer 원칙

Kit 2.5.0의 generic baseline은 신규 개발 저장소에서 명시적으로 opt-in한다.
`--bootstrap-baseline --repo-profile generic --dry-run`으로 먼저 변경 계획을 확인한다.
기존 파일·규칙·host trust와 Project Store 연결은 자동으로 교체하지 않는다.
Package 전체 checksum을 검증하며 같은 버전명도 무결성 검사를 생략하지 않는다.

첫 문서 작업에서는 실제 app identity에 맞는 repository_id와 guide의 단일 metadata owner를
확정한다. 기존 guide의 보안·audience·version을 읽고 명시적 Markdown-file roots와
adjacent .amplai.json 또는 JSON frontmatter를 등록한다. ACTIVE라는 예전 label이나
baseline 설치 성공은 문서 검토 승인이 아니다. 기능 작업의 같은 work 흐름에서 현재
source/dependency evidence를 검토하고 configured offline view를 검증한다.
`document_source_routes`는 개발용 원본을 담당 guide에 연결하거나 과거 baseline을
그대로 보존한다. 필수 guide, 미분류 원본, 변경된 이력은 이 경로로 우회하지 않는다.

- 모든 파일을 preflight한 뒤 쓰기 시작한다.
- Kit-owned 파일은 이전 설치 hash가 유지된 경우만 자동 업데이트한다.
- 로컬 수정은 기본적으로 conflict이며 덮어쓰지 않는다.
- `--force`일 때만 timestamp backup 후 교체한다.
- `work`, `design`, autonomy/workflow 문서는 marker section으로 additive merge한다.
- `.claude/settings.json`과 `.codex/hooks.json`은 기존 필드와 hook을 보존하고 AMPLAI hook만 dedupe한다.
- `.agents/skills`가 공통 정본이고 `.claude/skills`는 exact symlink mirror다.
- 동일 버전 재설치는 idempotent하다.
- install record는 `.ai-team/install/amplai-loop-kit.json`에 남긴다.

## Agent lifecycle hooks

Claude Code와 Codex의 SessionStart는 현재 App identity와 Work Context를 compact하게 주입한다.
SessionEnd는 host-local session/thread checkpoint만 기록한다. 중요한 상태는 hook에 의존하지 않고
`work` 실행 중 Project Store에 즉시 기록한다. hook 오류는 agent session 자체를 막지 않는다.

- Claude 설정: `.claude/settings.json`
- Codex 설정: `.codex/hooks.json`
- Codex hook command는 repository root에서 실행되며 `--host codex` adapter를 사용한다.
- Project-local hook은 최초 설치 또는 definition hash 변경 후 Codex `/hooks`에서 검토·신뢰한다.
  Installer는 `--dangerously-bypass-hook-trust`를 자동 추가하지 않는다.
