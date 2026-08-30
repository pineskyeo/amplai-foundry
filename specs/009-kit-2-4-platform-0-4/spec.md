# ALR-008 — Loop Kit 2.4.0 과 Platform 0.4.0 을 결함을 고쳐서 들인다

## Summary

외부 번들 `AMPLAI_2.3.1_to_Kit_2.4.0_Platform_0.4.0.zip` 을 이 저장소에 적용한다. 번들은
`ced83af` 기준 직접 패치이고 우리 HEAD `b191edf` 에도 그대로 적용된다. **다만 그대로 넣으면
이 저장소의 block gate 셋이 깨지고, control plane 에 재현되는 결함 넷이 함께 들어온다.**
이 Work 는 패치를 적용하고 그 결함을 전부 닫은 뒤 gate 를 초록으로 만든다.

```text
번들 sha256   be425436100802f0c8bc997456935ee22361157c34bc31b1ea3c558ef5f7a127
baseline      ced83af53f9d625f5d91edb069a8e877ed3bb3b5
규모          122 files, +7714 / -3047.  신규 45, 삭제 11
대상          Loop Kit 2.3.1 → 2.4.0,  Platform 0.2.0 → 0.4.0
```

## Why

Kit 2.4.0 은 Claude Code 와 Codex 를 같은 Work protocol 의 host 로 만든다 (`D-055`).
Platform 0.4.0 은 Governance Store 와 분리된 control plane bounded context 를 더한다.
`D-053` 으로 **이 저장소가 kit 정본**이므로, kit 결함을 여기서 고치지 않으면 fleet 셋이
같은 자리에서 깨진다.

## Evidence — 전부 실행해서 얻은 값이다

검증은 `HEAD` 에서 뜬 `git worktree` 에 패치를 적용해 수행했다. 저장소 작업본은 건드리지
않았다.

### 적용 자체는 문제없다

```text
git apply --check (HEAD 기준)   PASS
kit seal --verify               PASS
kit selftest                    PASS   2.4.0, check 10개
loopctl doctor                  PASS
pytest                          PASS   1542 passed, 4 deselected  (main 기록 1519)
```

### Gate 셋이 깨진다

```text
verifier --profile v2           FAIL
  ruff-check   [block]          FAIL   46 errors
  ruff-format  [block]          FAIL   9 files would be reformatted
  mypy         [block]          FAIL   2 errors
  나머지 8 check                PASS

docs validate --repo            STALE  (main 은 FRESH, errors [])
  broken reference: AGENTS.md:135 -> agents/openai.yaml
```

`.github/workflows/ci.yml:20-22` 가 `ruff check .`, `ruff format --check .`, `mypy src` 를
돌린다. **Actions 가 복구되는 즉시 세 step 이 빨간불이 된다.**

> `AGENTS.md:41 -> .amplai/tmp/` 도 처음에 떴으나 패치 탓이 아니다. `.amplai/tmp/` 는
> gitignore 된 런타임 디렉토리라 본 저장소에는 있고 새 worktree 에는 없다. 만들어 주니
> 사라졌다. 이 Work 의 결함 목록에서 뺀다.

### 원인 — kit 이 설치하는 새 script 가 lint 예외에 없다 (foundry 한정)

번들은 `scripts/amplai_hosts.py` 를 새로 추가하고 `tools/amplai-loop-kit/manifest.json:175`
에서 **배포 대상 저장소에도 설치**한다. 이 파일은 kit 규약대로 Python 3.6 호환으로 쓰였다 —
`from __future__ import print_function`, `codecs.open`, `super(__class__, self)`, `%` 포맷.

`pyproject.toml:60-69` 의 `extend-exclude` 는 kit 이 설치하는 script 를 **이름으로 열거**한다.

```text
"scripts/amplai.py", "scripts/amplai_hook.py",
"scripts/amplai_runtime.py", "scripts/amplai_supervisor.py",
```

번들이 건드린 `pyproject.toml` 은 version 한 줄뿐이다 (`0.2.0` → `0.4.0`). 다섯 번째 파일이
빠져 py311 규칙으로 검사되고 11건이 뜬다. `D-051`·`D-053` 이 세운 "kit 은 3.6 호환을 지키고
이 저장소는 그것을 lint 대상에서 뺀다" 는 경계를 번들이 자기 파일 하나로 깼다. `D-053` 으로
이 저장소가 kit 정본이므로 여기서 닫는 것이 곧 배포 원본을 닫는 것이다.

**배포 대상 두 곳에는 지금 영향이 없다 — 실측했다.** `synapse` 와 `cortex` 의 `origin/main`
에는 `pyproject.toml` 도 `ruff.toml` 도 없고, 두 저장소 tracked 파일 전체에서 `ruff`·`mypy`
문자열이 한 번도 나오지 않는다. 두 곳은 Python lint 를 돌리지 않는다. 이 결함은 **foundry
한정으로 활성이고 대상 저장소에서는 잠복**이다.

### ruff/mypy 위반 분포

```text
ruff-check 46건
  scripts/amplai_hosts.py                11   ← 위 원인. 예외 목록에 넣으면 사라진다
  src/amplai_foundry/control_plane/*      21   ← src 는 strict. 실제 위반이다
  tests/test_control_plane.py              3
  scripts/loopctl.py                       2   UP032. per-file-ignores 에 UP032 가 없다
  규칙별  E501 27 / UP035 3 / UP031 3 / RUF005 3 / UP032 2 / UP020 2
          F841 2 / UP010 1 / UP008 1 / UP004 1 / F401 1

ruff-format 9 files
mypy strict 2   outbox.py:133, worker.py:189  Returning Any from function declared to return "Row"
```

전부 기계적이다. 로직 결함은 여기 없다.

### 확인한 control plane 결함 넷

`CP-1`·`CP-2` 는 실행해서 재현했다. 둘 다 WSGI app 의 except 사슬을 빠져나가 `start_response`
가 호출되지 않는다 — JSON 오류 응답조차 못 만든다.

**`CP-1` canonical_ref 충돌.** `service.py:119` 가 `canonical_ref` 를
`{kind}:{project_id}:{content_digest[:24]}` 로 만든다. 중복 검사는 `origin_ref` 기준
(`service.py:96-105`) 이라 **같은 내용을 다른 `origin_ref` 로 올리면** 중복 검사를 통과하고
`cp_objects` PK 에서 터진다.

```text
1st : 201 Created
2nd : UNCAUGHT  sqlite3.IntegrityError: UNIQUE constraint failed: cp_objects.canonical_ref
```

`store.py:72` 의 PK 가 `canonical_ref` 단독이고 `tenant_id` 가 식별자에 없다. **다른 tenant 가
같은 `project_id` 문자열을 쓰면 tenant 간에도 충돌한다.**

**`CP-2` 타입 검증 없는 `int()`.** `service.py:196,215` 가 요청 JSON 의 `limit`·`max_attempts`
를 그대로 `int()` 에 넣는다.

```text
POST /v1/projects/p1/context-requests  {"query":"q","limit":[1]}
  → UNCAUGHT  TypeError: int() argument must be ... not 'list'
```

`"abc"` 면 `ValueError` 라 400 으로 잡히는데 `[1]` 이나 `{"a":1}` 은 `TypeError` 라 안 잡힌다.
상한도 없다.

**`CP-3` 요청 body 크기 상한 없음.** `http_api.py:113-115` 가 클라이언트가 선언한
`CONTENT_LENGTH` 를 그대로 `stream.read(length)` 에 넘긴다. 음수면 `read(-1)` 로 EOF 까지
읽는다.

**`CP-4` 예외 분류가 불완전하다.** `http_api.py:36-48` 의 except 목록에 `TypeError` 도
`sqlite3.Error` 도 없다. 그 계열이 뜨면 WSGI 계약을 깬 채 밖으로 나간다.

### 함께 고칠 것 — 인증 문맥 재조회

`service.py:99-101` 이 이미 인증된 principal 을 두고 `cp_objects` 조회 안에서
`(SELECT tenant_id FROM cp_api_tokens WHERE token_digest=?)` 서브쿼리로 tenant 를 다시 찾는다.
`service.py:111-115`·`200-204` 도 같은 재조회를 한다. 셋 다 `status='active'` 를 안 본다.
서브쿼리가 `NULL` 이면 `WHERE` 가 영원히 안 맞아 조용히 중복을 만든다. `CP-1` 을 고치는 자리와
같은 함수이므로 함께 닫는다.

## 함께 하는 것 — `feynman` skill 제거

사용자가 `feynman` 을 쓰지 않기로 했다. 흔적을 전부 찾았다.

```text
.agents/skills/feynman/      SKILL.md, commands/{explain,feynman,simplify}.md,
                             references/examples.md  — 정본 실물
.claude/skills/feynman       symlink (패치 후에도 symlink)
scripts/loopctl.py           expected_skills 집합, allowed_public 집합,
                             그리고 그 위 주석이 이름을 셋 다 든다
skills-lock.json             vendored 출처 기록 — neurofoo/agent-skills,
                             computedHash ac3da8f0c609...
CLAUDE.md                    보조 skill 목록
AGENTS.md                    보조 skill 목록 (패치가 같은 줄을 고친다)
docs/workstreams/amplai-loop-runtime-adoption/{FACTS.md,PORT-LOG.md}   이력 기록
```

**순서 제약이 있다.** 번들 패치가 `scripts/loopctl.py` 의 `expected_skills`·`allowed_public`
영역과 `AGENTS.md` 의 보조 skill 줄을 함께 고친다. 삭제를 먼저 하면 patch hunk 가 깨진다.
**패치를 먼저 적용하고 그 위에서 지운다.**

`tests/` 는 skill 집합을 단언하지 않는다 — grep 으로 확인했다. 강제는 `loopctl doctor` 하나뿐이고,
`expected_skills` 와 `allowed_public` 양쪽에서 빼면 통과한다. kit 은 skill 을 배포하지 않으므로
(`manifest.json#owned_files` 23개 중 skill 0개) **대상 저장소에는 영향이 없다.** `cortex` 의
`test_shared_skill_surface_is_exact_and_only_two_are_public` 은 cortex 자신의 `EXPECTED_SKILLS`
를 보므로 무관하다.

`FACTS.md` 와 `PORT-LOG.md` 는 이력 문서다. 지우지 않고 제거 사실을 덧붙인다.

## 배포 대상 두 곳 — 실측한 사전 조건

`synapse` 와 `cortex` 에 kit 2.4.0 을 넣는 것은 **별도 Work (`ALR-009`)** 로 뗀다. 다만 그
설계가 성립하는지 지금 확인했다.

```text
required_paths                2.3.1 과 동일하다 —
                              .agents/skills/{work,design}/SKILL.md, .ai-team/runtime/WORKFLOW.md
                              두 저장소 origin/main 에 셋 다 있다
owned_files                   20 → 23.  추가는 scripts/amplai_hosts.py 와
                              tests/ai/test_amplai_{host_adapters,federation}.py 셋뿐이다
                              그 test 는 amplai_hosts 만 import 하고 tempdir 로 끝난다 — 이식 가능
.ai-team 최상위               runtime 하나뿐이다.  install/local/backups 는 installer 가 만들고
                              D-054 가 이미 예외로 열어 뒀다.  cortex 의 완화된 guard 를 안 깬다
                              synapse 의 guard 는 expected - actual 만 보는 단방향이라 무관하다
.codex/hooks.json             2.4.0 이 새로 additive merge 한다
                              synapse 는 이미 이 파일이 있다 (SessionStart matcher + PostToolUse 3개)
                              cortex 는 .codex/config.toml 만 있고 hooks.json 은 없다
```

**merge 안전성을 synapse 의 실제 파일로 돌려서 확인했다.** kit 의 `merge_hooks` 에
`origin/main:.codex/hooks.json` 을 넣으니 기존 `SessionStart` 항목(matcher 포함)과 `PostToolUse`
셋이 그대로 남고 kit hook 이 새 그룹으로 붙었으며 `SessionEnd` 가 추가됐다.

**`--project-home` 없이 설치하면 Project Store 를 건드리지 않는다.** `install.py:935-936` 이
그 인자가 없으면 `configure_project_store` 를 즉시 반환한다. 그러면 `store.register_app(...,
repo_path=self.target, ...)` (`install.py:953-955`) 이 안 불린다 — `ALR-007` 이 겪은 repo_path
덮어쓰기 사고의 원인이 그 줄이다. 건너뛰는 것은 host-local `.ai-team/local/project.json`
(`plan_local_binding`, `install.py:596-597`) 뿐이고 그 파일은 두 저장소 모두 gitignore 돼 있다.
committed 산출물인 `.ai-team/app.json` 은 `--project-home` 과 무관하게 쓰인다.

따라서 **대상 저장소의 작업 사본을 동기화하지 않아도 배포할 수 있다.** `origin/main` 에서 뜬
worktree 에 `--project-home` 없이 설치하고 거기서 commit·PR 하면 된다 — `ALR-007` 이 쓴 방식과
같고, Store 사고 경로가 구조적으로 닫힌다. 사용자의 dirty 로컬 브랜치(synapse 27, cortex 2)를
건드릴 이유가 없다.

**다만 `kit_distribute.py --verify` 는 로컬 디스크를 본다.** 그것이 일치를 보고하게 하려면
사용자의 실제 체크아웃이 `main` 을 받아야 한다. 배포 성공과 `--verify` 통과는 별개다.

## Non-goals

- **token 만료(`expires_at`) 추가.** 결함이 아니라 정책 기능이다. operator rotation 절차 없이
  넣으면 추측이 된다. 별도 Work 로 남긴다.
- **`"*"` wildcard permission 제거** (`auth.py:85`). CLI `token-issue` 가 `--permission` 을
  명시로 받으므로 operator 가 요청할 때만 생긴다. 결함이 아니다.
- **project scope 불일치를 401 대신 403 으로 바꾸는 것** (`auth.py:78`). 동작에 영향이 없다.
- **connector HMAC 검증 helper 추가.** 서명은 `signature` key 를 뺀 canonical form 으로
  계산되는데 검증 helper 가 코드에 없다. 상호운용 문제이고 이 Work 의 결함 목록 밖이다.
- **실제 Claude/Codex 계정 E2E.** 번들도 `real_claude_codex_account_e2e_performed: false` 로
  밝혔다. release gate 로 남기고 `auto_start` 는 계속 끈다.
- **control plane 을 실제 network 에 띄우는 것.** in-process WSGI 로만 검증한다.
- **CI 초록불.** Actions 가 계정 결제로 멈춰 있다. 이 Work 가 풀 수 있는 것이 아니다.

## Acceptance

`work-contract.json#acceptance` 의 `AC-001` ~ `AC-011` 이 정본이다.

## Decisions

- `D-055` — 번들이 가져오는 host 대칭 결정. **`Status: APPROVED` 로 적혀 오지만 번들은
  `.ai-team/policy/approvals.jsonl` 을 건드리지 않는다** (패치 전체에서 언급 0회). skill 정본을
  `.claude/skills` 에서 `.agents/skills` 로 뒤집는 `architecture_change` 다. 선례인 `D-054` 는
  ledger 에 사람 승인을 남겼다. **사람이 ledger 에 넣어야 한다.**
- `D-056` — 이 Work 가 새로 쓴다. Platform 0.4.0 도입과 control plane 결함 수정 방침. 번들에는
  Platform 0.4.0 에 대응하는 Decision 이 하나도 없다.

## Skill SSOT 뒤집기가 Claude 에 미치는 영향 — 실측

승인 조건이 "Claude 환경 사용에 전혀 지장 없을 것" 이므로 19개 skill 전부를 대조했다.
비교 대상은 **Claude 가 실제로 읽는 파일** — 패치 전후로 `.claude/skills/<name>/SKILL.md` 를
symlink 까지 따라가 읽은 내용이다.

```text
동일 6개        dev-loop, eli12, feynman, grill-me, grilling, systematic-debugging, taskify
speckit 9개     순수 추가 2줄 (## Host Invocation 문단).  기존 workflow 삭제 0
work            없는 파일 호출을 제거 — tools/ontology/semanticctl.py 는 존재하지 않는다
                (D-046 이 semantic runtime 을 이식 제외했다)
design          없는 파일 참조를 제거 — docs/decisions/cortex-decisions.md 도 없다
code-review     C99/RHEL/HP-UX 를 project runtime 으로 일반화
frontmatter     user-invocable 19개 전부 보존.  내부 capability 14개는 계속 false
symlink         19/19 생성, 깨진 link 0
```

**지금 Claude 가 읽는 `work` skill 은 없는 파일을 부르라고 지시하고 있었다.** 이 변경은
무지장이 아니라 그 결함을 닫는다. 승인 조건을 충족한다.

## Rollback

패치가 만지는 파일을 그 뒤에 수정하지 않았다면 `patch/rollback_patch.py` 가
`git apply --reverse` 로 되돌린다. 이 Work 는 그 위에 수정을 얹으므로 **rollback 은 branch
단위로 한다** — 브랜치를 버리면 `main` 은 그대로다. control plane 은 신규 bounded context 라
기존 데이터를 건드리지 않고, `cp_*` table 은 이 저장소에 아직 배포된 적이 없어 되돌릴 상태가
없다.

## 적용 도구 주의

`patch/apply_patch.py:44-47` 은 **HEAD 가 정확히 `ced83af` 가 아니면 거부한다.** 우리 HEAD 는
`b191edf` 라 그 script 는 못 쓴다. `git apply` 를 직접 쓴다 — 깨끗이 적용됨을 확인했다.

`--repair-zip-baseline` 은 `git reset --hard HEAD` 를 fail-closed 검사보다 **먼저** 실행한다
(`apply_patch.py:48-50`). untracked 는 안 지우므로 tracked 수정을 날리고도 중단될 수 있다.
**쓰지 않는다.**
