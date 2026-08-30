# ALR-009 Plan — worktree 배포 절차

## Boundary

```text
바꾼다        synapse:  kit 2.3.1 → 2.4.0 설치 산출물 (owned 23 + hooks + install record)
              cortex:   같음
              foundry:  KIT-DISTRIBUTION.md, CURRENT_ITEM.md 기록

안 바꾼다     두 저장소의 로컬 작업 사본과 브랜치 — origin/main worktree 에서만 만진다
              Project Store — --project-home 을 주지 않는다
              .ai-team/local/project.json — gitignore 된 host-local 파일
              kit 정본 tools/amplai-loop-kit/ — ALR-008 이 확정한 것을 그대로 쓴다
```

## 절차 — 저장소마다 같다

```bash
# 1. origin/main 에서 worktree 를 뜬다.  작업 사본을 안 건드린다
git -C ~/workspace/<app> fetch origin
git -C ~/workspace/<app> worktree add --detach /tmp/kit24-<app> origin/main

# 2. --project-home 없이 설치한다.  Store 를 안 건드리는 것이 이 인자의 부재다
.venv/bin/python tools/amplai-loop-kit/install.py \
    --target /tmp/kit24-<app> --app-id <app> --project-id ai-platform

# 3. 그 저장소의 test 를 그 저장소의 방식으로 돌린다
cd /tmp/kit24-<app> && python3 -m unittest discover -s tests/ai -t .
python3 .ai-team/verifiers/run.py --profile v2
python3 scripts/loopctl.py doctor

# 4. commit, push, PR
# 5. 머지 후 worktree 를 지운다
git -C ~/workspace/<app> worktree remove /tmp/kit24-<app>
```

**install.py 의 실제 인자 이름은 실행 전에 `--help` 로 확인한다.** 위는 설계 의도이고
`--target`·`--app-id`·`--project-id` 표기는 검증 대상이다.

## Slices

### `S01` — synapse

먼저 한다. `.codex/hooks.json` 이 이미 있어 merge 경로를 실제로 타는 쪽이다. 병합 결과가
설계와 같은지 (기존 `SessionStart` matcher 항목과 `PostToolUse` 3개 보존) diff 로 확인한다.

검증: `tests/ai` 전체, verifier v2, doctor. 설치 전 수를 먼저 세고 대조한다.
`git diff` 에 `.ai-team/local/project.json` 이 **없어야 한다.**

### `S02` — cortex

`.codex/hooks.json` 을 새로 만드는 쪽이다. `D-054` 로 완화된
`test_ai_team_has_no_new_top_level_directory` 가 통과하는지가 핵심 관문이다.

검증: 같음. 추가로 `.ai-team` 최상위 목록을 설치 전후로 찍어 비교한다.

### `S03` — 기록

```text
1. KIT-DISTRIBUTION.md 에 2.4.0 배포 결과
2. CURRENT_ITEM.md 의 남은 것 갱신
3. kit_distribute.py --verify 를 돌리고 결과를 그대로 적는다 —
   로컬이 main 을 안 받았으면 mismatch 로 나오는 것이 정상이다.  그 사실을 적는다
```

## Failure path

```text
merge 가 기존 hook 을 지운다      diff 로 즉시 보인다.  PR 을 열지 않고 멈춘다
cortex guard 가 FAIL             .ai-team 최상위에 예상 밖 디렉토리가 생긴 것이다.
                                 D-054 예외 셋과 실제 목록을 대조한다
새 payload test 가 그 저장소에서  amplai_hosts import 경로 문제일 가능성이 높다.
FAIL                             scripts/ 가 sys.path 에 들어가는지 본다
Store repo_path 가 바뀐다        --project-home 을 실수로 준 것이다.
                                 ALR-007 의 복구 절차는 specs/008-cortex-redeploy/spec.md 에 있다
push 가 막힌다                   git_publish gate 다.  사람 승인을 받는다
```

## CI

두 저장소의 CI 도 같은 계정 아래 있다. Actions 가 복구되지 않았으면 초록불을 못 본다.
`ALR-007` 과 같은 조건이므로 merge 방식을 사람이 정한다.
