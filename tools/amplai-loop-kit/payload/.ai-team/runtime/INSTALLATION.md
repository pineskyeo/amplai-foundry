---
doc_id: amplai-async-installation
title: AMPLAI Decision & Async Runtime Installation
status: canonical
---

# 설치와 업데이트

배포 단위는 `amplai-loop-kit` 하나다. 기존 AMPLAI Loop V2 앱에 안전하게 설치/업데이트하며 공개
entry point는 `/design`, `/work`만 유지한다.

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

## 앱 등록

```bash
python3 scripts/amplai.py --project-home "$AMPLAI_PROJECT_HOME" app register \
  --id <app-id> \
  --repo "$PWD" \
  --runner claude-code \
  --command claude \
  --runner-arg=--permission-mode --runner-arg=acceptEdits \
  --max-concurrency 1
```

`--auto-start`를 생략하면 수동 앱이다. installer나 Runtime은 기존 Claude permissions를 자동으로
높이지 않으며, worker args는 각 조직의 보안 정책에 맞게 직접 정한다.

worker 권한은 **필요한 최소**로 준다. 권장 순서는 다음과 같다.

1. `--permission-mode acceptEdits` — 파일 편집은 허용하고 그 외에는 확인을 받는다.
2. 필요하면 `--allowedTools` 로 쓸 도구를 명시한다.
3. `--dangerously-skip-permissions` 는 모든 확인을 무력화한다. `auto_start=true` 와 함께 쓰면
   사람이 보지 않는 동안 그 repository 에서 무엇이든 실행될 수 있다. 격리된 환경이 아니면 쓰지
   않는다. 이 조합은 `amplai.py project verify` 가 WARNING 으로 보고한다.

Runtime 은 worker 가 무엇을 하는지 강제하지 못한다. `policy.forbidden_automatic_actions` 중
Runtime 이 실제로 거부하는 항목은 `project verify` 의 `policy_enforcement` 에서 확인한다.

## Kit installer 원칙

- 모든 파일을 preflight한 뒤 쓰기 시작한다.
- Kit-owned 파일은 이전 설치 hash가 유지된 경우만 자동 업데이트한다.
- 로컬 수정은 기본적으로 conflict이며 덮어쓰지 않는다.
- `--force`일 때만 timestamp backup 후 교체한다.
- `/design`, `/work`, autonomy/workflow 문서는 marker section으로 additive merge한다.
- `.claude/settings.json`은 기존 permission과 hook을 보존하고 AMPLAI hook만 dedupe하여 추가한다.
- 동일 버전 재설치는 idempotent하다.
- install record는 `.ai-team/install/amplai-loop-kit.json`에 남긴다.

## Claude hooks

SessionStart는 현재 App identity와 Work Context를 compact하게 주입한다. SessionEnd는 host-local
session checkpoint만 기록한다. 중요한 상태는 hook에 의존하지 않고 `/work` 실행 중 Project Store에
즉시 기록한다. hook 오류는 Claude Code session 자체를 막지 않는다.
