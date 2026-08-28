# Cortex AMPLAI Loop Runtime V2.1 — Fact Collection

**목적:** 이식 가능성 판단의 근거. 조사 기록이지 spec 이 아니다.
**대상:** `~/workspace/cortex` main `3a3eb46b` (PR #83 `worktree-amplai-v21-patch`)
**작성:** 2026-08-19. 전부 실측이고 추정에는 "(추정)" 을 붙였다.

## 1. 버전 확인

사용자는 "2.2" 라 했으나 **cortex main 최신은 V2.1 이다.**

| 근거 | 값 |
|---|---|
| spec 디렉터리 | `specs/007-amplai-v21-doc-freshness-gardening` (005=v1, 006=v2) |
| main HEAD | `3a3eb46b` Merge PR #83 `worktree-amplai-v21-patch` |
| 직전 | PR #82 `worktree-amplai-v21-hardening` |
| roadmap | `docs/ai/AMPLAI_LOOP_RUNTIME_V1_V2_AND_ROADMAP.md` 313줄. V1/V2/V2.1 Addendum 까지. **V3 는 "의도적으로 구현하지 않는다"** |

`grep -rl "2\.2\|V2\.2"` 는 무관한 파일 둘만 잡았다. **2.2 는 없다.** 사용자가 V2.1 로 확정했다.

## 2. AMPLAI 가 무엇인가

**개발 loop runtime 이다.** 이 저장소의 amplai-foundry(지식 거버넌스 제품)와 이름만 같다.

```text
V1 = Reliable Single-Agent Loop
V2 = Knowledge-Aware Evidence-Governed Loop
V2.1 = V2 + Documentation Freshness + Repository Gardening (P6 확장)
V3 = Distributed Multi-Agent Execution Fabric — 미구현, 의도적
```

공개 표면은 **둘뿐**이다 — `/work`, `/design`.

```text
/work
→ classify / contract
→ Knowledge Readiness (READY|BYPASS|DISCOVER|BLOCKED)
→ Context Pack + Environment
→ V1 Slice Loop (implement → verify → diagnose → repair ↺)
→ Evidence Trace
→ DOCUMENTATION FRESHNESS → review → INCREMENTAL GARDENING
→ semantic/evidence finalize → DONE
```

## 3. 물리적 구성과 규모

| 위치 | 규모 |
|---|---|
| `.ai-team/contracts` | 3 files |
| `.ai-team/evidence` | 2 |
| `.ai-team/knowledge` | 11 (map/claims/decision index/schema 6종) |
| `.ai-team/policy` | 7 (readiness/permission/tdd/quadrant/documentation/gardening/approvals) |
| `.ai-team/rules` | 7 |
| `.ai-team/runtime` | 3 (WORKFLOW.md, escalation.md, policy.json) |
| `.ai-team/verifiers` | 3 (README, registry.json, run.py 360줄) |
| `scripts/loopctl.py` | 884줄 |
| `scripts/loopv2.py` | 1,988줄 |
| `.claude/skills/` | work 285줄, dev-loop 125, design 116, code-review 58, systematic-debugging |

**합계 약 3,232줄 python + 36개 markdown/json.**

## 4. 이식성 — 실측

engine 이 프로젝트 중립이다.

| 파일 | 줄 | `"cortex"` 문자열 |
|---|---|---|
| `scripts/loopctl.py` | 884 | **0** |
| `.ai-team/verifiers/run.py` | 360 | 1 |
| `scripts/loopv2.py` | 1,988 | 4 |

**외부 의존이 없다.** `loopctl.py` 의 import 는 `argparse fnmatch io json loopv2 os re subprocess sys` 뿐이다. `from __future__ import print_function` 이 있어 Python 2 호환 style 이다.

경로 가정은 저장소 루트 기준 상수 둘이다.

```python
POLICY   = os.path.join(".ai-team", "runtime", "policy.json")
REGISTRY = os.path.join(".ai-team", "verifiers", "registry.json")
```

`loopctl doctor` 가 요구하는 파일 목록이 `scripts/loopctl.py:319-328` 에 하드코딩돼 있다.

### loopctl subcommand 30개

```text
append apply audit build capture check classify context contract discovery
docs doctor environment evaluate full garden handoff impact incremental init
mine permission quadrant readiness scan status trace validate verify write
```

## 5. Verifier 모델 — 이식의 접합부

`registry.json` = `{schema_version, project, profiles, checks}`.

check 는 **shell 명령 한 줄**이다.

```json
{"id": "loop-runtime-tests",
 "command": "python3 -m unittest discover -s tests/ai -p 'test_*.py'",
 "severity": "block",
 "source": ".ai-team/runtime/WORKFLOW.md",
 "description": "..."}
```

profile 8개: `fast standard commit runtime semantic v2 full rhel`. `extends` 로 상속한다.
cortex 는 check 30개를 등록했다.

**이 저장소는 이미 꽂을 것이 있다.** `src/amplai_foundry/verification/runner.py:37-50` 의 7 check.

| id | command |
|---|---|
| `pytest` | `python -m pytest` |
| `ruff-check` | `python -m ruff check .` |
| `ruff-format` | `python -m ruff format --check .` |
| `mypy` | `python -m mypy src` |
| `schema` | `python -m amplai_foundry.cli schema check` |
| `vault-lint` | (동일 CLI) |
| `project-pack` | (동일 CLI) |

형식이 같다. **registry 에 그대로 옮겨진다.**

## 6. 이 저장소와의 겹침

| skill | 양쪽 |
|---|---|
| `speckit-analyze` `speckit-clarify` `speckit-converge` `speckit-implement` `speckit-plan` `speckit-specify` `taskify` | **공유** — 같은 계보 |
| `work` `design` `dev-loop` `code-review` `systematic-debugging` | **cortex 에만** |
| `eli12` `feynman` `grill-me` `grilling` `speckit-checklist` `speckit-constitution` `speckit-taskstoissues` | **이쪽에만** |

즉 가져올 신규는 다섯이고, 그중 `work`/`dev-loop` 가 나머지를 지휘하는 controller 다.

## 7. 충돌 넷

### 7.1 절차 주도권 (정면충돌)

이 저장소 `CLAUDE.md` 의 **Pre-Implement Procedure** 는 사람이 고정 순서를 밟게 못박았고
`D-033`·`D-036`·`D-037` 이 떠받친다. `/work` 는 "Skill 순서를 사용자에게 떠넘기지 않는다" 로
runtime 이 고르게 한다. **둘 중 하나가 이겨야 한다. Decision 이 필요하다.**

### 7.2 `.ai-team/` 이름 충돌

| | 이 저장소 | cortex |
|---|---|---|
| 내용 | `.ai-team/skills/taskify` (skill 원본) | runtime state 7개 디렉터리 |

같은 경로, 다른 용도다.

### 7.3 review 층 중복

이쪽 3-lens subagent review(contract/failure-recovery/regression) ↔ cortex `code-review` skill.
어느 쪽이 gate 를 여는지 정해야 한다.

### 7.4 Knowledge Safety — **양립 가능성이 높다**

이 저장소 헌법 원칙 I: "AI 가 공식 지식을 자동 승인하거나 자동 수정하게 만들지 않는다".

cortex 도 같은 방향이다. `.ai-team/policy/permissions.json` 실측:

```text
read_repo/edit_repo/run_test/git_diff  → allowed
architecture_change/public_contract_change/ontology_promotion/
destructive_migration/production_operation/commit → gated
push → prohibited
```

`/work` 절대원칙에도 "ontology candidate 를 canonical active graph 에 직접 append 하지
않는다", "LLM 자기평가를 PASS 로 쓰지 않는다" 가 있다. roadmap 의 V2 safety rule 12개도
같은 축이다.

**policy 6개(`documentation`·`gardening`·`knowledge-readiness`·`permissions`·`quadrant`·`tdd`)의
top-level key 를 전부 확인했고 `gardening.json` 은 안전 모델을 정독했다.**
(`approvals.jsonl` 은 json 이 아니라 append log 다.)

`gardening.json` 실측 — **자동 삭제 범위가 매우 좁다.**

| | 값 |
|---|---|
| `safe_auto_patterns` | `__pycache__`, `*.pyc/pyo/bak/orig/rej/swp`, `*~`, `.DS_Store`, `.fuse_hidden*` **뿐** |
| `EVIDENCE_REQUIRED` | reference/build/test/packaging scan 넷이 다 모여야 제거 |
| `HUMAN_GATED` | candidate/proposal 까지만. 명시적 사람 승인 없이 삭제 안 함 |
| `no_static_reference_is_not_dead` | 텍스트 참조 부재를 dead 근거로 쓰지 않는다 |
| `rule` | "candidate 는 삭제 제안이지 삭제가 아니다. **proposal only**" |
| `modes.incremental` | `/work` 기본값. changed scope 주변만. `repository_wide: false` |
| `modes.full` | explicit Work 에서만. `default_apply: false` |
| `pass_is_not` | "garbage candidate 가 0 개" — 0 을 통과 조건으로 안 씀 |

`human_gated_paths` 는 cortex 경로 목록이다 (`include/**`, `sdk/**`, `docs/ontology/model/active/**`,
`docs/decisions/**` 등). **이 저장소로 옮기면 `vault/**` 를 여기 넣는다** — 그러면 헌법 원칙 I
과 정렬된다.

즉 Knowledge Safety 충돌은 **설정 문제이지 설계 충돌이 아니다.** (`human_gated_paths` 와
`safe_auto_patterns` 를 이 저장소 것으로 바꾸는 일)

## 8. 이 저장소가 이 runtime 을 필요로 하는 근거

2026-08-18~19 세션에서 wave 8 → round 15 → wave 9 → round 16 → wave 10 을 손으로 돌렸다.
그때 난 실패가 runtime 이 겨냥하는 것과 일치한다.

| 겪은 것 | runtime 의 대응 |
|---|---|
| 수치를 여섯 라운드 연속 틀렸다 | "LLM 자기평가를 PASS 로 쓰지 않는다" + deterministic verifier |
| 수정이 새 blocker 를 만들었다 (round 16 의 9건 중 다수가 wave 9 산물) | Contract → Slice → verify → diagnose → repair 를 작은 단위로 |
| reviewer 가 세 번 세션 한도로 죽어 판정을 잃었다 | "evidence/handoff recoverable" 이 V1 Done 조건 |
| 절차를 사람이 순서대로 조작 | `/work` controller |

## 9. 확인하지 않은 것

- `.ai-team/policy/` 의 `documentation`·`knowledge-readiness`·`quadrant`·`tdd` 본문
  (top-level key 만 봤다)
- `.ai-team/rules/` 7개 전부
- `loopv2.py` 1,988줄의 내용 (subcommand 를 노출하지 않는다 — loopctl 이 import 해서 쓴다)
- `tools/knowledge/project_miner.py`, `tools/ontology/*` — cortex 의 semantic runtime.
  이 저장소에 rdflib/ontology 가 없으므로 **가져올지 자체가 미정**이다
- cortex 의 `tests/ai` — runtime 자체의 test. 이식하면 함께 와야 하는지 미확인
- cortex `tests/ai` 를 이식 대상에 넣을지
