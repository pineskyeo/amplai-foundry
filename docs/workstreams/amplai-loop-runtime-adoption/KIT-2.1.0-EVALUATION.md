# AMPLAI Loop Kit 2.1.0 — 도입 평가

/ design 산출물이다. 구현하지 않았다. 이 저장소 복제본에 **실제로 설치하고 제거해 본** 실측
기록이고, 판정은 사용자가 준 네 기준에 대한 답이다.

```text
평가 대상   ~/Downloads/amplai-loop-kit-2.1.0.tar.gz   56KB, VERSION 2.1.0
비교 기준   이 저장소 = cortex main 3a3eb46b 이식본 (D-046)
실측 환경   /Users/pinesky/.claude/jobs/65025e17/tmp/probe (이 저장소 복제본, 전용 venv)
verdict     DESIGN READY — `D-051` 이 승인했다. feature 는 `specs/005-amplai-loop-kit/`
```

**사용자가 요청한 2.2 는 존재하지 않는다.** `~/Downloads` 에 있는 것은 2.1.0 하나이고
`VERSION` 파일도 `2.1.0` 이다.

## 판정 요약

| 사용자 기준 | 답 | 근거 |
|---|---|---|
| 장기 방향에 걸리는가 | **걸리지 않는다** | `ARC-0003` 이 이미 같은 구조를 설계했다 |
| 지금 당장 쓸 수 있는가 | **Project Store 를 만들어야만** | Store 없이는 아무 기능도 안 돈다 |
| 겹쳐서 문제를 일으키는가 | **넷. 셋은 선례가 있고 하나는 선택이다** | 아래 실측 |
| 나중에 제거할 수 있는가 | **된다. 실측으로 확인했다** | 제거 후 `git status` 가 빈다 |

**Store 없이 설치하는 것은 무의미하다.** 효용이 0인데 lint 만 깨진다. 실질 선택지는 둘이다 —
Store 까지 만들어 제대로 쓰거나, 설치하지 않거나.

## 1. Kit 이 무엇인가

`README.md` 첫 줄이 스스로 말한다.

> Synapse의 AMPLAI Loop V2를 기준으로 만든 **Decision & Async Cross-App Runtime** 배포 패키지다.

프레임워크를 갈아끼우지 않는다. `reference/SYNAPSE_INTEGRATION.md` 가 "Existing capabilities
retained" 로 `/design`·`/work`, Knowledge Readiness, Context Pack, Evidence Trace, verifier 를
전부 유지한다고 적는다. **기존 Loop V2 앱 위에 additive 로 얹는다.**

가져오는 것은 넷이다.

```text
Question → Evidence(DIRECT|LOCAL|PARALLEL) → Decision(AUTO|CHALLENGE|HUMAN)
중앙 Git Project Store   CR/Work/Contract/Decision/Evidence + event hash chain
Local Supervisor         claim, lease, heartbeat, recovery, app capacity
Claude Code -p worker    SessionStart/SessionEnd hook 으로 붙는다
```

`selftest.py` 는 통과한다 — `ok: true`, check 7개.

### 계보가 다르다

이 저장소는 **cortex 판** AMPLAI Loop Runtime V2.1 이다 (`D-046`, 출처 cortex main
`3a3eb46b`). kit 은 **synapse 판** V2 위에 얹는 확장이다. 둘 다 "AMPLAI Loop V2.1" 을
자칭하지만 같은 코드가 아니다.

## 2. 장기 방향과의 정합 — 걸리지 않는다

`vault/projects/amplai/40-architecture/ARC-0003` 이 이 저장소의 역할 경계를 정한다.

> Knowledge Layer는 Project Memory의 도메인 계약, provenance와 retrieval 경계를 제공한다.
> Governor는 정책과 승인 조건을 집행하고, **Work Manager는 업무 상태를 모아 계획과 위임을
> 수행하며**, Implementation Agent는 위임된 변경을 구현하고 검증한다.
>
> **Hermes는 Work Manager 후보이고 Claude Code와 Codex는 Implementation Agent 역할을 수행할
> 수 있다.** 모든 Runtime은 project-owned knowledge를 공유하지만 각 실행의 session state와
> tool event를 canonical knowledge에 자동 편입하지 않는다.

kit 의 아키텍처 그림(`SYNAPSE_INTEGRATION.md`)과 대조하면 같은 것이다.

```text
Human → Hermes → Global AMPLAI → 같은 Project Store / Work protocol → App AMPLAI Runtimes
```

**kit 은 이 저장소가 이미 설계해 둔 방향의 선행 구현이다.** 우연이 아니라 같은 AMPLAI 계보에서
나왔다. 장기 방향과 충돌하지 않는다.

`ARC-0003` 이 그은 선 하나도 지켜진다 — kit 의 SessionEnd checkpoint 는 Project Store(개발 층)에
쓰이고 `vault/` 의 canonical knowledge(제품 층)를 건드리지 않는다.

### 다만 두 규약과는 실제로 어긋난다

**`.ai-team/README.md` 의 Scope freeze:**

> V2에는 parallel worker, fan-out/fan-in, **Work DAG scheduler**, remote worker,
> **resource scheduler**, multi-repo merge coordinator가 없다. 그것은 V3다.

kit 의 Local Supervisor 는 claim·lease·heartbeat·app capacity 를 다루고 WAITING/READY
dependency loop 을 돈다. **문구상 V3 로 미뤄 둔 것에 해당한다.** kit 자신은 방어한다 —
"The Local Supervisor is intentionally not the V3 distributed execution fabric. It does not
plan goals, coordinate merges, publish Git state, deploy" — 그리고 `default_app_concurrency: 1`
로 한 앱 한 Work 를 강제한다. **정면 충돌은 아니지만 문구는 손대야 한다.**

**`.ai-team/README.md` 의 Directory ownership:**

> `.ai-team`은 문서 내용도, Work별 report 이력도 쌓지 않는다. **정책과 schema만 둔다.**

설치 실측 결과 `.ai-team` 에 다음이 생긴다.

```text
정책·schema (규약에 맞음)   async-policy.json, schemas/*.json (7개), app.template.json
문서 (규약 위반)            DECISION_ASYNC_PROTOCOL.md, LOCAL_SUPERVISOR.md, INSTALLATION.md
이력 (규약 위반)            install/amplai-loop-kit.json, backups/
identity (새 개념)          app.json
```

**문서 3개와 이력 2종이 규약을 벗어난다.** 규약을 고치거나 예외를 명시해야 한다.

### Hermes 라는 이름이 두 층에서 다르다

- **제품 층** (`ARC-0007`): Hermes 는 "사용자의 자연어와 파일을 `IntentRequest` 로 구조화하고
  AMPLAI API 에 제출하는 Client Partner" 다. `MGC-014` 로 계획돼 있고 `CON-0010` 은 아직
  `candidate` 다
- **kit**: Hermes 는 여러 앱 Runtime 을 오케스트레이션하는 개발 층 상위 조정자다

**같은 이름이 다른 것을 가리킨다.** 설치하면 문서에서 두 뜻이 섞인다.

## 3. 지금 당장 쓸 수 있는가 — Store 가 없으면 안 돈다

`amplai_runtime.py:1825` 의 `discover_project_home` 이 결정적이다.

```python
def discover_project_home(repo_root=None, explicit=None):
    ...
    raise NotFoundError(
        "AMPLAI_PROJECT_HOME is not set and no usable project_home_hint exists"
    )
```

**기본값도 자동 생성도 없다.** `AMPLAI_PROJECT_HOME` 환경변수, `.ai-team/local/project.json`,
`app.json` 의 `project_home_hint` 중 하나가 실재해야 한다.

hook 은 그 예외를 잡아 넘어간다 (`amplai_hook.py` 주석: "Hooks are additive context/checkpoint
helpers. They must not prevent Claude Code from starting or ending when the Project Store is
absent"). 매 세션 `AMPLAI hook skipped: ...` 를 stderr 로 내고 `return 0` 한다.

`fragments/work.md` 마지막 줄도 같은 말을 한다.

> 중앙 Store나 app identity가 없는 단일 repo 작업은 기존 Loop V2 경로로 정상 동작한다.

**따라서 `--project-home` 없이 설치하면 파일 27개가 늘고 아무 기능도 안 돈다.**

### Store 를 만들면 얻는 것

앱이 amplai-foundry 하나뿐이면 cross-app 효용은 0이다. 남는 것은 둘이다.

1. **Local Supervisor 자동 실행.** READY Work 를 claim 해 `claude -p` worker 를 띄운다.
   사람이 자리를 비워도 진행한다. 기본은 `auto_start: false` 이고 README 가 "안정화 후에만
   `--auto-start`" 라 적는다
2. **Decision authority 기계 강제.** `async-policy.json` 이
   `human_requires_approval_evidence: true`, `challenge_requires_independent_review: true`,
   `done_requires_evidence: true` 를 강제한다

2번은 지금 이 저장소가 겪는 문제와 정확히 맞물린다. **round 21 의 blocker `C21-3` 이
"`D-050` 이 `approvals.jsonl` 을 안 탔다" 였다** — kit 의 강제는 그런 누락을 구조적으로 막는다.

## 4. 겹침 — 실측 넷

복제본에 실제로 설치하고 전후를 비교했다.

```text
설치 전   LOOP DOCTOR: PASS      VERIFIER v2: PASS
설치 후   LOOP DOCTOR: PASS      VERIFIER v2: FAIL
```

### (a) Ruff 223건 → verifier v2 FAIL. **선례가 있다**

깨진 것은 lint 하나다. 위반이 전부 kit 파일에서 나온다.

```text
ruff check scripts/amplai*.py tests/ai/     → Found 223 errors
ruff check . (kit 파일 제외)                 → All checks passed!
```

kit 은 Python 3.6 호환을 노려 `%` 포맷과 `from __future__ import print_function` 을 쓴다.
이 저장소는 `select = ["E","F","I","UP","B","SIM","RUF"]` 라 `UP031` 등이 걸린다.

**이 저장소는 같은 문제를 이미 겪고 해법을 남겼다.** `pyproject.toml:58-65`:

```toml
# cortex main 3a3eb46b 에서 이식한 loop runtime (D-046). ... upstream 과 diff 를 유지하기
# 위해 남긴다 — cortex 가 이 파일들을 계속 고치므로 재이식 때 충돌을 줄이는 쪽이 이득이다.
[tool.ruff.lint.per-file-ignores]
"scripts/loopctl.py" = ["UP031", "SIM115", "B904", "E402", "SIM102"]
"scripts/loopv2.py"  = ["UP031", "SIM115", "B904", "E402", "SIM102"]
```

kit 4파일에 같은 처리를 하면 된다. **제거할 때도 4줄만 지우면 된다.**

`.ai-team/` 은 이미 `extend-exclude` 에 있어 그쪽 산출물은 lint 대상이 아니다.

**mypy 는 영향이 없다.** `packages = ["amplai_foundry"]` 로 범위가 한정돼 `scripts/` 를 안 본다
— 설치 후에도 `Success: no issues found in 102 source files` 다.

### (b) `/work` SKILL.md 절 번호 중복

kit fragment 가 `## 11. Decision & Async Cross-App Runtime` 이라는 번호를 갖고 들어온다.
이 저장소 `/work` 는 이미 1~12절이다.

```text
244:  ## 11. Semantic change handling
260:  ## 12. Handoff / DONE
288:  ## 11. Decision & Async Cross-App Runtime   ← 중복, 그리고 12 뒤에 온다
```

marker(`<!-- AMPLAI-ASYNC-BEGIN/END -->`) 안쪽만 kit 소유이므로 번호를 바꾸려면 marker 안을
고쳐야 하고, 그러면 다음 kit 업데이트 때 충돌 대상이 된다. `/design` 쪽은 `## 7.` 로 들어오는데
이 저장소 `/design` 이 1~6절이라 **우연히 맞는다.**

### (c) test 수가 +16 움직인다

`pyproject.toml:39` 의 `testpaths = ["tests"]` 안에 kit 의 `tests/ai/` 가 들어간다. 실행하면
16개가 전부 통과한다.

```text
기준선   1409/1413 collected (4 deselected)     ← 복제본 실측, 본 저장소와 일치
설치 후  1425 예상 (1409 + 16)
```

Package 5 review 는 매 라운드 test 수를 두 방법으로 세어 대조해 왔으므로 **freeze target 과
review 기준선이 이동한다.**

### (d) 지시가 반대다 — 이건 선택이다

`amplai_hook.py` 의 SessionStart 가 **매 세션 context 를 주입한다.**

> CR/Work/Decision/Evidence in the Project Store are authoritative; **rendered handoff text is
> only a view.**

이 저장소는 반대다. `.ai-team/README.md` 의 Directory ownership 이 Work Memory 를
`specs/<feature>/contract|readiness|context|environment|trace|handoff|...` 로 못박고,
`/work` 12절이 `loopctl.py handoff write` 로 `handoff.json` 에 상태를 쓴다. **round 21 결과도
거기 기록했다.**

hook 은 `sessionTitle` 도 `project:app` 으로 덮는다.

**파일 충돌이 아니라 지시 충돌이다.** 두 SSOT 가 공존하면 어느 쪽이 정본인지 매 세션 흔들린다.
설치한다면 이 문장을 어떻게 다룰지 먼저 정해야 한다.

## 5. 제거 가능성 — 실측으로 확인했다

`install.py` 에 uninstall 경로가 **없다** (`grep -in "uninstall|remove|revert"` 0건). 그러나
설치가 남기는 것이 전부 git 추적 대상이고 경계가 명확하다.

```text
수정 5개 (marker/merge)     .agents/skills/{work,design}/SKILL.md
                            .ai-team/runtime/WORKFLOW.md, policy.json, .gitignore
새 파일·디렉토리            scripts/amplai{,_hook,_runtime,_supervisor}.py
                            .ai-team/runtime/{schemas/,async-policy.json,app.template.json}
                            .ai-team/runtime/{DECISION_ASYNC_PROTOCOL,INSTALLATION,LOCAL_SUPERVISOR}.md
                            .ai-team/{app.json,install/,backups/,local/}
                            .claude/settings.json, tests/ai/
```

복제본에서 아래를 실행하고 확인했다.

```bash
git checkout -- .agents .ai-team/runtime/WORKFLOW.md .ai-team/runtime/policy.json .gitignore
rm -rf <위의 새 파일·디렉토리>
```

```text
git status --short   → 빈 출력      (흔적 0)
LOOP DOCTOR          → PASS
ruff check .         → All checks passed!
```

**완전히 되돌아간다.** Project Store 는 저장소 밖이라 디렉토리를 지우면 끝이다.

`.claude/settings.json` 은 이 저장소에 **원래 없다** — kit 이 새로 만들므로 제거 시 파일째
지우면 된다. 기존 permissions/hooks 를 보존하는 merge 로직을 탈 일이 없다.

## 6. 결정 — `D-051`

세 가지를 함께 정했다. 전문은
`docs/workstreams/messenger-governance-closure-v3/DECISIONS.md` 의 `D-051` 이다.

**D-1. Store 까지 만들어 제대로 쓴다.**
`--project-home` 으로 저장소 밖에 Project Store 를 만들고 `--app-id amplai-foundry` 로
등록한다. `auto_start` 는 `false` 다. Store 없이 파일만 까는 안은 버렸다 — 얻는 것이 없는데
ruff 만 깨진다.

**D-2. handoff 는 두 층으로 나눈다.**
kit 의 Project Store Work 는 **앱 간 조율 단위**이고 `specs/<feature>/handoff.json` 은
**feature 내부 slice 진행 상태**다. 서로 다른 것을 다루므로 어느 쪽도 상대의 SSOT 가 아니다.
hook 이 주입하는 "rendered handoff text is only a view" 는 **Project Store Work 의 rendered
view 를 가리키는 문장**으로 읽는다. 경계는 `.ai-team/README.md` 각주에 명시한다.

**D-3. 규약 위반은 각주로 표시한다.**
Scope freeze 와 Directory ownership 을 **고치지 않고** "kit 2.1.0 이 설치된 동안의 예외" 를
각주로 단다. 장기 규약을 임시 설치 때문에 바꾸면 되돌리기 어렵다. **각주는 제거와 함께
사라진다.**

### 제거 절차 — 확정본

`install.py` 에 uninstall 이 없으므로 절차를 여기 확정한다. 복제본에서 실행해 `git status`
가 비는 것을 확인했다.

```bash
# 1. marker / merge 로 바뀐 것을 되돌린다
git checkout -- .agents/skills/work/SKILL.md .agents/skills/design/SKILL.md \
                .ai-team/runtime/WORKFLOW.md .ai-team/runtime/policy.json .gitignore

# 2. kit 이 새로 넣은 것을 지운다
rm -rf scripts/amplai.py scripts/amplai_hook.py scripts/amplai_runtime.py \
       scripts/amplai_supervisor.py \
       .ai-team/runtime/schemas .ai-team/runtime/async-policy.json \
       .ai-team/runtime/app.template.json \
       .ai-team/runtime/DECISION_ASYNC_PROTOCOL.md \
       .ai-team/runtime/INSTALLATION.md .ai-team/runtime/LOCAL_SUPERVISOR.md \
       .ai-team/app.json .ai-team/install .ai-team/backups .ai-team/local \
       .claude/settings.json tests/ai

# 3. 이 저장소가 kit 을 위해 만든 것을 되돌린다
#    - pyproject.toml 의 per-file-ignores 에서 kit 4파일 줄
#    - .ai-team/README.md 의 Scope freeze / Directory ownership 예외 각주
#    - .ai-team/AUTONOMY_POLICY.md (제거할지는 별도 판단 — kit 과 무관하게 쓸모가 있다)

# 4. Project Store 를 지운다 (저장소 밖)
rm -rf "$AMPLAI_PROJECT_HOME"

# 5. 확인
git status --short        # 비어야 한다
python3 scripts/loopctl.py doctor
.venv/bin/ruff check .
.venv/bin/python -m pytest --collect-only -q | tail -1   # 1409 로 돌아와야 한다
```

**주의 둘.**

- `.claude/settings.json` 은 이 저장소에 **원래 없다.** kit 이 새로 만들므로 파일째 지운다.
  나중에 다른 이유로 이 파일이 생기면 hook 항목만 지워야 한다
- **kit 재설치는 idempotent update 라 marker 와 `.claude/settings.json` 을 다시 쓴다.**
  각주와 `per-file-ignores` 는 kit 소유가 아니므로 살아남지만, marker 안쪽은 덮인다

## 7. 설치 절차

`specs/005-amplai-loop-kit/spec.md` 가 S01~S03 으로 나눴다. 실행은 `/work` 의 몫이다.

```text
S01  설치 전 준비
     - .ai-team/AUTONOMY_POLICY.md 를 쓴다. preflight 가 required_paths 로 요구한다.
       kit 은 내용을 읽지 않으므로(selftest.py:47 이 한 줄짜리로 check 7개를 통과시킨다)
       내용은 이 저장소 것으로 채운다 — policy.json 의 human_gates 7개와
       forbidden_automatic_actions 를 산문으로 푼다. 지어내지 않는다
     - pyproject.toml per-file-ignores 에 kit 4파일을 추가한다 (선례: loopctl/loopv2)
     - install.py --dry-run 으로 27 action 을 확인한다

S02  설치
     - Project Store 를 저장소 밖에 만들고 --app-id amplai-foundry 로 등록한다
       (auto_start 는 false)
     - 설치 후 doctor / verifier v2 / pytest 로 기준선을 갱신한다 (1409 → 1425)

S03  기록
     - .ai-team/README.md 에 Scope freeze·Directory ownership 예외 각주를 단다 (본문 무수정)
     - /work SKILL.md 의 절 번호 중복을 기록한다 (marker 안쪽이라 못 고친다)
     - 제거 절차(§6)를 복제본에서 다시 실행해 확인한다
     - test 기준선 이동을 messenger CURRENT_ITEM.md 에 적어 다음 라운드가 알게 한다
```

**Package 5 review 가 열려 있는 동안 설치하면 test 기준선이 움직인다.** round 22 를 먼저
닫고 설치하는 편이 review 를 단순하게 만든다 — 다만 이것은 순서 선호이지 제약은 아니다.

## 실측 근거 목록

```text
kit selftest                    ok: true, check 7개
dry-run (복제본)                27 action, 충돌 0
실제 설치 (복제본)              ok: true, project_store: null
설치 전 doctor / verifier v2    PASS / PASS
설치 후 doctor / verifier v2    PASS / FAIL (원인: ruff 223건, 전부 kit 파일)
설치 후 mypy                    Success (102 source files) — 영향 없음
kit test 실행                   16개 전부 통과
제거 후 git status              빈 출력
제거 후 doctor / ruff           PASS / All checks passed
```

작업물은 `/Users/pinesky/.claude/jobs/65025e17/tmp/` 에 있다 — `kit/` 압축 해제본,
`probe/` 복제본. **이 저장소 자체는 이 문서 외에 건드리지 않았다.**
