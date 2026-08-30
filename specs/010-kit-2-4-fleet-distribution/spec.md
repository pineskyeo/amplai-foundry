# ALR-009 — kit 2.4.0 을 synapse 와 cortex 에 배포한다

## Summary

`ALR-008` 이 foundry 에 kit 2.4.0 을 들이고 gate 를 초록으로 만든 뒤, 같은 kit 을 배포 대상
둘에 넣는다. `D-053` 의 fleet 셋을 2.4.0 으로 맞추는 것이 목표다.

**`ALR-008` 이 `main` 에 들어가기 전에는 시작하지 않는다.**

## Why

`D-053` 이 fleet 을 하나의 kit 버전으로 유지하기로 했다. 2.4.0 은 Codex 를 first-class host 로
만들고 (`D-055`) Decision/Evidence federation lifecycle 을 더한다. 대상 둘은 현재 2.3.1 이다.

## Evidence — 실측한 사전 조건

### kit 2.4.0 이 대상에 무엇을 하는가

```text
required_paths        2.3.1 과 동일하다
                      .agents/skills/work/SKILL.md, .agents/skills/design/SKILL.md,
                      .ai-team/runtime/WORKFLOW.md
                      → synapse·cortex origin/main 에 셋 다 있다

owned_files           20 → 23.  추가 셋:
                      scripts/amplai_hosts.py
                      tests/ai/test_amplai_host_adapters.py
                      tests/ai/test_amplai_federation.py

.ai-team 최상위       runtime 하나뿐이다.  install·local·backups 는 installer 가 만들고
                      D-054 가 cortex 에서 이미 예외로 열었다

json_merges           .ai-team/runtime/policy.json 에 path_rules 병합 (기존과 동일)

hooks                 .claude/settings.json  — 기존과 동일
                      .codex/hooks.json      — 2.4.0 이 새로 additive merge 한다
```

### `.codex/hooks.json` 병합은 안전하다 — 실행으로 확인했다

```text
synapse   origin/main 에 이미 있다.  SessionStart(matcher="startup|resume|clear|compact") 1개,
          PostToolUse(matcher="^Bash$") 안에 hook 3개
cortex    .codex/config.toml 만 있고 hooks.json 은 없다.  installer 가 새로 만든다
```

kit 의 `merge_hooks` 에 synapse 의 실제 파일을 넣어 돌렸다.

```text
SessionStart 그룹 2개
  [0] matcher='startup|resume|clear|compact'  hooks=1   ← 기존 그대로
  [1] matcher=None                            hooks=1   ← kit 이 붙인 것
PostToolUse 보존              True (3개 그대로)
SessionEnd 추가               True
기존 SessionStart 항목 동일   True
기존 PostToolUse 동일         True
```

**Codex hook 은 설치돼도 `/hooks` 로 trust 하기 전에는 돌지 않는다.** installer 가 그 note 를
남기고 trust 를 우회하지 않는다 (`install.py:469-475`).

### 대상 저장소의 guard 는 안 깨진다

```text
cortex   test_ai_team_has_no_new_top_level_directory   D-054 로 kit 3개를 예외 허용.
                                                       2.4.0 이 새 최상위를 안 만든다
cortex   test_shared_skill_surface_is_exact...         cortex 자신의 EXPECTED_SKILLS 를 본다.
                                                       kit 은 skill 을 배포하지 않는다 (owned 0개)
synapse  test_runtime_layer_owns_exactly_seven_...     expected - actual 만 보는 단방향이라
                                                       추가 디렉토리를 허용한다
```

### lint 은 문제가 아니다

`synapse`·`cortex` `origin/main` 에는 `pyproject.toml` 도 `ruff.toml` 도 없고, tracked 파일
전체에서 `ruff`·`mypy` 문자열이 나오지 않는다. 새로 설치되는
`scripts/amplai_hosts.py` 의 Python 3.6 문법이 두 곳에서 lint 에 걸리지 않는다.

### 새 payload test 는 이식 가능하다

`tests/ai/test_amplai_host_adapters.py` 는 `scripts/amplai_hosts.py` 만 import 하고
`tempfile.mkdtemp` 로 격리한다. 저장소 고유 fixture 에 의존하지 않는다.

## 핵심 설계 — `--project-home` 없이 설치한다

`install.py:935-936` 이 `--project-home` 이 없으면 `configure_project_store` 를 즉시 반환한다.
그러면 `store.register_app(..., repo_path=self.target, ...)` (`install.py:953-955`) 이 안 불린다.
**`ALR-007` 이 겪은 Project Store `repo_path` 덮어쓰기 사고의 원인이 정확히 그 줄이다.**

건너뛰는 것은 `plan_local_binding` 이 쓰는 `.ai-team/local/project.json` 하나뿐이고
(`install.py:596-597`), 그 파일은 **두 저장소 모두 `.gitignore` 에 있다** — host-local 이라
commit 대상이 아니다. committed 산출물인 `.ai-team/app.json` 은 `--project-home` 과 무관하게
쓰인다.

따라서 다음이 성립한다.

```text
origin/main 에서 worktree 를 뜬다
  → --project-home 없이 kit 2.4.0 을 설치한다
  → 그 저장소의 test 를 돌린다
  → commit, push, PR
```

**사용자의 로컬 작업 사본을 동기화하거나 정리할 필요가 없다.** `synapse` 는 dirty 27,
`cortex` 는 dirty 2 이고 각각 68·279 commit 뒤처져 있지만 이 절차는 그것을 건드리지 않는다.
`ALR-007` 이 쓴 방식과 같고 (`남의 저장소는 origin/main worktree 에서 만진다`), Store 사고
경로가 인자 하나로 구조적으로 닫힌다.

## Non-goals

- **사용자의 로컬 체크아웃 동기화.** 이 Work 는 필요로 하지 않는다. 별개로 하고 싶으면
  하되 dirty diff 를 먼저 보고 살릴 것을 정해야 한다 — `synapse` 의 27건은 낡은 kit 2.3.0
  설치가 대부분이지만 `.claude/settings.json`·`.gitignore` 같은 tracked 수정이 섞여 있다.
- **`--project-home` 을 주는 설치.** Store `repo_path` 를 덮는다. Store 바인딩이 필요하면
  배포와 분리해서 실제 체크아웃에서 한다.
- **cortex 를 Project Store 에 묶는 것.** 인계 문서가 말한 "설계된 휴지 상태" 를 유지한다.
- **`kit_distribute.py --verify` 가 일치를 보고하게 만드는 것.** `--verify` 는 로컬 디스크를
  본다. 사용자의 체크아웃이 `main` 을 받기 전에는 mismatch 로 나온다. 배포 성공과 별개다.
- **supervisor 를 켜는 것.** 실제 Claude/Codex E2E 가 선행이다.
- **Codex hook trust.** 사용자가 각 저장소에서 `/hooks` 로 직접 한다.

## Acceptance

`work-contract.json#acceptance` 가 정본이다.

## Dependency

```text
ALR-008 (specs/009-kit-2-4-platform-0-4/)  status=done, main 머지 완료
```
