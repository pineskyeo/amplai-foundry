# AMPLAI Loop Runtime V2.1 — Port Log

**출처:** cortex main `3a3eb46b` (PR #83 `worktree-amplai-v21-patch`)
**근거:** `D-046`
**작성:** 2026-08-19

> **현재 상태 주의 (2026-08-29):** 아래는 2026-08-19 port 당시의 사실 기록이다.
> `D-055` / Kit 2.3.2가 skill 방향을 supersede하여 현재 정본은 `.agents/skills/`,
> `.claude/skills/`는 exact symlink mirror다.

## 무엇을 가져왔나

| 대상 | 내용 |
|---|---|
| skill 5개 | `work`, `design`, `dev-loop`, `code-review`, `systematic-debugging` |
| `.ai-team/` | `runtime`, `policy`, `contracts`, `verifiers`, `knowledge`, `evidence`, `rules`, `README.md` |
| script 3개 | `scripts/loopctl.py`, `scripts/loopv2.py`, `scripts/eval.sh` |

## 무엇을 안 가져왔나

**semantic runtime** — ontology TTL, SHACL, competency question, 읽기 전용 MCP,
`project_miner.py`. 이 저장소에는 기반이 없고 Knowledge Vault 와 Proposal 모델이 그 자리를
대신한다. `loopctl doctor` 의 required path 와 semantic check 블록, `quadrant.json` 의 해당
자산을 함께 제거했다.

## 이 저장소에 맞춘 것

### 1. verifier registry — 접합부

cortex 의 check 30개를 버리고 이 저장소 것 12개로 갈아 끼웠다. `verify` 의 7 check 가
전부 들어간다.

| profile | 구성 |
|---|---|
| `fast` | ruff-check, ruff-format, mypy, loop-runtime-doctor, manifest-validator |
| `commit` | fast + diff-check |
| `standard` | fast + pytest |
| `runtime` | standard + loop-shell-syntax |
| `full` | standard + schema, vault-lint, project-pack |
| `v2` | runtime + full — **gate 판정용** |

**초안에서 두 command 가 틀렸다.** `project validate --all` 은 없는 option 이었고
`lint vault` 는 `lint vault/` 여야 했다. `runner.py:44-60` 을 읽고 고쳤다.

### 2. gardening 보호 경로

`human_gated_paths` 를 cortex 경로에서 이 저장소 것으로 바꿨다. **`vault/**` 를 최상단에
넣었다** — 헌법 원칙 I(공식 지식을 자동 수정하지 않는다)과 정렬된다.

자동 삭제 범위는 cortex 그대로다 — `__pycache__`, `*.pyc/bak/orig/swp`, `.DS_Store` 류뿐이고
나머지는 proposal only 다.

### 3. `loopctl doctor` — 이 저장소 사실에 맞춤

| 고친 것 | 이유 |
|---|---|
| semantic required path 8개 제거 | 이식 대상이 아니다 |
| `expected_skills` 에 7개 추가 | `eli12`·`feynman`·`grill-me`·`grilling`·`speckit-checklist`·`speckit-constitution`·`speckit-taskstoissues` 를 유지한다 |
| public skill 판정을 `.claude/skills/` 기준으로 | **정본 위치가 cortex 와 반대다** |
| `.claude/skills` 가 symlink 여야 한다는 요구 완화 | 같은 이유. 깨진 link 만 잡는다 |
| legacy 목록에서 `/grill-me`·`/feynman` 제거 | 이 저장소가 유지하는 보조 skill 이다 |
| forbidden 에서 `.specify/workflows` 제거 | `workflow.yml` 이 아직 three-lens gate 의 문서상 근거다 |
| semantic runtime check 블록 제거 | 이식 대상이 아니다 |

> 이 표는 이식 시점(2026-08)의 기록이다. 두 줄이 그 뒤에 뒤집혔다. **skill 정본 방향**은
> `D-055` 로 `.agents/skills/` 가 됐고 `.claude/skills/` 는 exact symlink mirror 가 됐다 —
> cortex 와 같은 방향이다. **`feynman`** 은 2026-08-30 `ALR-008` 에서 제거했다. 쓰지
> 않기로 했다. `expected_skills` 와 `allowed_public` 양쪽에서 뺐다.

### 4. `.ai-team/skills` 정리

`.ai-team/skills/taskify` 가 cortex 의 runtime layer 와 경로가 겹쳤다. `.agents/skills/`
로 옮겼다.

**중간에 실수했다.** 옮기는 과정에서 cortex 판 taskify(317줄)로 이 저장소 판(250줄)을 잠깐
덮었다. `git checkout` 으로 되돌렸고 지금은 250줄 이 저장소 판이다. taskify 는 이식 대상이
아니다.

### 5. lint

이식한 두 script 가 Python 2 호환 style 이라 171건이 걸렸다. 자동 수정으로 168건을 닫고
남은 23건은 `pyproject.toml` 의 `per-file-ignores` 로 처리했다 — upstream 과 diff 를 유지해
재이식 때 충돌을 줄이는 쪽이 이득이다. **그 예외는 `scripts/` 두 파일에만 걸린다.**

### 6. `AGENTS.md` / `CLAUDE.md` 분리

`CLAUDE.md` 가 `AGENTS.md` 로의 symlink 였다. cortex 처럼 분리했다.

- `AGENTS.md` — 실질 규칙. `Spec-Kit Adoption` 절을 `Public Workflow`·`Runtime Ownership`·
  `What Was Not Ported`·`Skill Layout`·`Review Before Gate` 로 교체했다. 앞의 네 절
  (Knowledge Safety, Change Scope, Completion Gate, Codex Curation Contract)은 제품 규칙이라
  그대로 뒀다.
- `CLAUDE.md` — Claude Code adapter. command 둘과 communication 규칙만.

### 7. speckit 을 internal 로

`.claude/skills/` 의 speckit 9개와 `taskify` 의 frontmatter 를 `user-invocable: false` 로
바꿨다. 이제 user-invocable 개발 skill 은 `work`·`design` 둘이다.

## 검증

```text
$ python3 scripts/loopctl.py doctor
LOOP DOCTOR: PASS
- public skills: /work, /design
- internal capabilities: 17
- legacy runtime directories: absent
- policy/contract/verifier JSON: valid
- Knowledge/Context/Evidence plane: valid
- Semantic Runtime: 이식 제외 (D-046). Vault lint 가 대신한다
- Harness quadrant coverage: complete
- contract template: valid
- Claude skills: exact shared-skill symlink set

$ python3 .ai-team/verifiers/run.py --profile fast
VERIFIER: PASS

$ python3 .ai-team/verifiers/run.py --profile full
ruff-check PASS / ruff-format PASS / mypy PASS / loop-runtime-doctor PASS
manifest-validator PASS / schema PASS / vault-lint PASS / project-pack PASS
pytest FAIL   ← 아래 참고
```

## 해소된 실패 하나 — interpreter 문제였다

이식 세션은 `tests/test_slack_http.py` 실패를 "sandbox network 정책이 바뀐 것 **(추정)**" 으로
기록했다. **틀렸다.** `ALR-002` 가 재확인했고 원인은 interpreter 선택이었다.

```text
$ python3 -m pytest tests/test_slack_http.py     # system 3.9
E   ModuleNotFoundError: No module named 'tomllib'

$ .venv/bin/python -m pytest
1382 passed, 4 deselected
```

`pyproject.toml` 이 `requires-python = ">=3.11"` 이다. test 는 `.venv/bin/python` 으로 돌린다.

## 남은 일

이식 이후의 진행 상태는 [CURRENT_ITEM.md](CURRENT_ITEM.md) 가 갖는다. 이 문서는 이식 시점의
기록이고 더 갱신하지 않는다.
