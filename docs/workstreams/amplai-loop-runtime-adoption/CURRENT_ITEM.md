# Current Item — AMPLAI Loop Runtime Adoption

## Goal

cortex 의 AMPLAI Loop Runtime V2.1 을 amplai-foundry 로 이식해 **이 저장소 개발 자체에**
쓴다. 절차 주도권을 `/work` controller 에 넘기고 user-invocable 을 `/work`·`/design` 둘로
줄인다. semantic runtime 은 제외한다.

근거: `D-046`. 출처: cortex main `3a3eb46b`.

## 현재 갱신 — Kit 2.3.2와 Codex 실행 호환 (2026-08-29)

`D-055`가 Claude Code와 Codex를 같은 Loop Runtime의 host adapter로 정의했다. 이전
`ALR-002`~`ALR-007` 절은 당시 이식·2.3.1 배포의 역사 기록이고, **현재 정본은 다음**이다.

```text
kit 정본         tools/amplai-loop-kit/   2.3.2
공통 skill 정본  .agents/skills/
Claude mirror    .claude/skills/* -> ../../.agents/skills/*
공개 진입        Claude /work·/design, Codex $work·$design
worker           claude-code | codex | command
hook             .claude/settings.json + .codex/hooks.json (additive merge)
```

2.3.2는 Codex 문서 노출만 추가한 버전이 아니다. `codex exec --json`, thread ID 회수,
`codex exec resume`, SessionStart/SessionEnd adapter, internal skill의 implicit invocation 차단,
권한 우회 경고와 uninstall 소유권까지 포함한다. amplai-foundry에는 설치를 끝냈지만 synapse와
cortex의 2.3.2 배포는 이 작업 범위에서 실행하지 않았다. 두 저장소의 마지막 확인 기록은
2.3.1이며, 배포 전 각 저장소 자체 gate를 다시 통과해야 한다.

장기 Platform(`src/amplai_foundry`)과 Loop Kit은 같은 저장소에 있어도 별도 제품이다. Platform
domain/application은 Kit·`.ai-team`·Claude/Codex runtime을 import하지 않는다. 상세 점검과 후속
구조는 `docs/reviews/2026-08-29-platform-kit-audit.md`를 따른다.

## 상태 — 이식이 닫혔다

```text
LOOP DOCTOR: PASS
verifier --profile v2     : PASS (WARN 0건)
pytest                    : 1382 passed, 4 deselected
context validate          : valid, MATCH
docs validate --repo      : FRESH
```

| | |
|---|---|
| 가져온 것 | skill 5개, `.ai-team/` runtime layer, `loopctl.py`·`loopv2.py`·`eval.sh` |
| 안 가져온 것 | semantic runtime (ontology·SHACL·CQ·MCP·project miner). 호출부도 없앴다 |
| public skill | `/work`, `/design` 둘. internal 17개 |
| verifier | check 11개 / profile 6개. `verify` 의 7 check 전부 포함 |
| rule | 3개. cortex 고유 4개는 지웠다 |

세부는 [PORT-LOG.md](PORT-LOG.md), 조사 근거는 [FACTS.md](FACTS.md).

## 첫 완주가 찾아낸 것 — `ALR-002`

**`/work` 를 돌리자마자 loop 가 자기 발에 걸렸다.** contract → readiness → context 순서에서
Context Pack 이 `verdict: MISSING` 으로 멈췄다. `.ai-team/knowledge/map.json` 의 source 20개 중
16개가 cortex 경로였다. `context_pack: required` 인 한 **이 저장소의 어떤 Work 도 DONE 에
도달하지 못하는 상태**였고, 이식 후 한 번도 안 돌려 봐서 안 드러났다.

고친 것은 셋이다. Work artifact 는 `specs/004-amplai-loop-runtime/` 에 있다.

1. **profile 이름이 네 곳에서 달랐다.** `registry.json` 의 profiles 는 6개, `policy.json` 의
   `profile_rank`·`work-contract.schema.json` 의 enum·`loopctl.py` 의 fallback 은 7개였다.
   `commit` 이 rank 에 없고 `semantic`·`rhel` 은 registry 에 없었다. rank 에 없는 이름은
   `classify` 에서 -1 로 밀려 **조용히 선택되지 않는다.** registry 를 정본으로 통일했다.
2. **죽은 check 하나.** `loop-skill-surface` 가 `doctor --skills` 를 불렀는데 그 flag 는
   upstream cortex 에도 없다. 항상 `exit 2` 였고 severity 가 `warn` 이라 `VERIFIER: PASS` 에
   가려져 있었다. skill surface 검사는 `doctor` 가 이미 block 으로 한다.
3. **knowledge plane 이 cortex 그대로였다.** map 24 source, claim 6건, decision index 3건을
   이 저장소 자산으로 갈아 끼웠다. `project` 도 `cortex` → `amplai-foundry`.

**doctor 가 이제 이 부류를 block 으로 잡는다.** registry 를 정본으로 policy·schema·fallback
셋과 이름 집합을 대조하고, rank 순서·`defaults.profile`·`risk_profiles`·path_rule 의 profile
참조·claims 파싱·map source 존재·project 일치까지 본다. 일곱 가지 어긋남을 각각 만들어
FAIL 을 확인했다.

`review` 는 세 lens 를 순차로 돌렸다. contract 와 failure/recovery 가 `FAIL` 을 냈고
(P1 1건, P2-Blocking 2건), regression 은 `PASS` 였다. 지적은 전부 반영했다.

### 남긴 기록 하나

`decisions.index.json` 에서 `ADR-AMPLAI-V1`·`ADR-AMPLAI-V2` 를 `superseded` 표시 없이 지웠다.
둘 다 cortex 의 ADR 이고 path(`specs/005-…`, `specs/006-…`)가 이 저장소에 **존재한 적이 없다.**
그 파일은 index 이지 decision 원본이 아니다 — 원본은 `docs/workstreams/*/DECISIONS.md` 가 갖고
그것은 무변경이다. 역사 덮어쓰기가 아니라 dangling index entry 제거다. 흔적을 여기 남긴다.

## 두 번째 완주가 닫은 것 — `ALR-003`

`ALR-002` 가 남긴 다섯 부류를 닫았다. 전부 cortex 를 가리키던 것들이다.

1. **rule 7개가 없는 build system 을 규정했다.** `c99-rhel8-build`·`dist-ui`·`memory-safety`·
   `production-debug-map` 넷은 파일 전체가 C99/RHEL/React/C daemon 규칙이라 지웠다. 남은 셋
   (`commit-release-gate`·`contract-registry`·`test-discipline`)은 개념이 유효해서 enforcement
   만 이 저장소 실물로 바꿨다 — `make hooks`·`.alg`·`ui/dist` 대신 verifier profile 과
   `amplai_foundry.cli` 를 인용한다.
2. **path_rule 7개 중 둘은 대상 경로가 하나도 없었다.** `core-runtime`(`src/runtime/**`,
   `plugins/**`)과 `build-system`(`Makefile`, `mk/**`)을 뺐다. 나머지의 dead pattern
   (`include/cortex/**`, `sdk/**`, `ui/src/**`, `specs/005-…`, `specs/006-…`)도 정리했다.
   `CODEX.md`·`.codex/config.toml`·`.mcp.json`·`.claude/settings.json` 은 **남겼다** — 아직
   없지만 생기면 high 로 잡혀야 하는 adapter 설정이다.
3. **documentation policy 가 없는 경로를 정본이라 선언했다.** `canonical_roots` 10개 중 8개가
   부재였다. `impact_rules` 7개도 `src/runtime/**`·`plugins/**`·`ui/src/**` 를 trigger 로 삼아
   전부 죽어 있었다. 다섯 축을 이 저장소 package 이름으로 다시 썼다.
4. **discovery 가 매번 없는 module 을 import 했다.** `ontology_candidates` 가 `tools/ontology`
   를 `sys.path` 에 넣고 실패한 뒤 `semantic-runtime-unavailable` 을 결과에 넣었다.
   `load_ontology_bindings` 도 같은 부류다. `D-046` 이 미이식을 정했으므로 호출부를 없앴다.
5. **environment fingerprint 가 증명한 적 없는 환경을 주장했다.** `loopv2.py` 와
   `verifiers/run.py` 두 곳이 `C99`·`RHEL5/7/8`·`HP-UX`·`x86-32`·`Python 3.6` 을 하드코딩했고,
   tool 목록은 system `python3` 3.9.6 만 기록했다. **이게 제일 나빴다** — fingerprint 는
   "무엇을 증명하지 않았는지" 경계를 남기는 물건인데 거짓 경계를 남겼다. 실측값으로 바꿨다.

```json
"target_assumptions": {
  "verified_on": "macOS-26.2-arm64-arm-64bit",
  "python_requirement": ">=3.11",
  "verifier_interpreter": ".venv/bin/python",
  "not_verified": ["다른 OS·architecture 에서의 동작", "requires-python 아래 버전에서의 동작"]
}
```

## Kit 도입 — `ALR-004`·`ALR-005`·`ALR-006` (2026-08-28)

이식 workstream 이 닫힌 뒤, 외부에서 받은 **AMPLAI Loop Kit** 을 이 저장소가 소유하고
다른 앱에 배포하는 데까지 갔다. Decision 셋이 그 경로다.

```text
D-051   kit 을 제거 가능한 층으로 설치한다          (ALR-004 / specs/005 — superseded)
D-052   Store 가 supervisor 의 코드와 실행 권한을 갖는다  (ALR-005 / specs/006 — superseded)
D-053   amplai-foundry 가 kit 정본을 소유하고 배포한다    (ALR-006 / specs/007 — done)
```

`ALR-004`·`ALR-005` 는 `ALR-006` 이 흡수했다. **Decision 셋은 전부 유효하고** 각
spec.md 머리에 경위가 적혀 있다.

### 지금 상태

```text
kit 정본     tools/amplai-loop-kit/   2.3.0
             출처는 synapse main:1c05001b (PR #81). PROVENANCE.md 가 경위를 담는다
설치         amplai-foundry 2.3.0 / synapse 2.3.0 / cortex 미설치
Store        ~/workspace/amplai-project — 앱 등록, supervisor 진입점
배포 명령    scripts/kit_distribute.py  (--dry-run / --all / --app / --verify / --uninstall)
운영 문서    docs/workstreams/amplai-loop-runtime-adoption/KIT-DISTRIBUTION.md
```

**supervisor 는 켜지 않았다.** `auto_start` 는 `false` 이고, `HANDOFF §3` 의 활성 세션
라우팅 확인이 아직 남아 있다.

### 무엇을 얻었나

**supervisor 가 하나만 산다는 것이 규약이 아니라 코드로 보장된다.** 락이
`amplai_supervisor.py` 안에 있으므로 Store 진입점을 쓰든 앱 사본을 직접 실행하든 두 번째는
거부된다. `D-052` 가 한계로 인정했던 우회 경로가 닫혔다.

```text
두 번째 supervisor        exit=3
앱 사본 직접 실행         exit=3      다른 저장소 사본도 마찬가지다
SIGKILL 직후(stale 전)    exit=3
stale 회수 후             exit=0
--dry-run                 exit=0      계획은 claim 하지 않으므로 락 밖이다
```

배포 경로는 두 층이다 — `distribution/targets.json`(커밋, 절대경로 0건)과
`.ai-team/local/kit-targets.json`(host-local, gitignore). **경로를 못 정하면 추측하지 않고
멈춘다.**

### review 가 잡은 것 — blocker 19

세 lens 를 순차로 돌렸고 P1 여덟과 Blocking-P2 열하나를 전부 닫았다. 기록은
`specs/007-kit-source-and-distribution/evidence/review-{contract,failure,regression}.md` 다.

각 lens 가 다른 층을 잡았다.

| lens | 건수 | 대표 |
|---|---|---|
| contract | 6 | 배포된 Store 진입점이 옛 코드라 **버전 대조가 무력화**돼 있었다 |
| failure | 6 | heartbeat 실패를 무시해 **supervisor 둘이 사는 것**을 재현했다 |
| regression | 7 | **코드는 맞는데 test 가 못 잡았다.** Store 진입점에 test 가 하나도 없었다 |

**`CHECKSUMS` 봉인이 두 번 깨진 채 commit 됐고 아무도 읽지 않았다.** `seal.py --verify` 를
만들어 verifier 에 `kit-seal` block check 로 등록했다 — 이제 깨지면 자동으로 잡힌다.

### cortex 를 되돌린 이유

cortex 에는 `.ai-team` 최상위를 **정확히 일곱으로 못박은 test** 가 있고 kit 의
`install`·`local`·`backups` 가 그것을 깨뜨렸다. 세 앱 중 cortex 에만 있는 규약이고, 그 test 를
고치는 것은 contract 의 `non_goals` 가 막는다.

제거 후 원상을 확인했다 — kit 흔적 0, `tests/ai` 99 passed, `doctor` PASS.

**그 저장소가 규약을 갱신하면 `--app cortex --all` 로 바로 배포된다.** `targets.json` 항목은
사유와 함께 남겨 뒀다.

**`D-054` 가 그 방향을 정했다 (2026-08-28).** 규약을 버리는 것이 아니라 kit 3개
(`install`·`local`·`backups`)만 예외로 허용하도록 **완화한다.** 삭제하면 cortex 의 규약
문장은 남고 강제도 기록도 없어져 foundry 보다 느슨해진다 — foundry 는 test 를 안 가지는
대신 `D-051` 각주로 예외를 문서화했다.

배포 전에 kit 결함 하나를 먼저 고친다. `AmplaiHookTest` 가 `AMPLAI_PROJECT_HOME`·
`AMPLAI_APP_ID` 를 안 지워 실제 Store 를 읽는데, **그 변수를 심는 것이 kit 자신의
SessionStart hook 이다** (`amplai_hook.py:75-76`). 고치지 않고 배포하면 cortex 의
`loop-runtime-tests` block check 가 Claude 세션 안에서 항상 FAIL 한다. **지금 foundry
에서도 재현된다.** 설계는 `specs/008-cortex-redeploy/` 다.

### 그 과정이 드러낸 kit 결함 둘

```text
빈 디렉토리     uninstall 이 파일만 지우고 .ai-team/install 같은 빈 디렉토리를 남겼다.
                git 이 빈 디렉토리를 추적하지 않아 이 저장소 시험에서는 안 보였고
                cortex 의 규약 test 가 드러냈다. 고쳤고 회귀 test 둘을 넣었다
created_paths   kit 이 만든 파일을 내용 확인 없이 지웠다. 앱이 그 아래 쓴 내용이
                사라진다. marker 밖에 우리 헤더 말고 다른 것이 있으면 이제 남긴다
```

## `ALR-007` 이 1·2 를 닫았다 (2026-08-28)

`D-054` 가 cortex 의 `.ai-team` 최상위 guard 를 **삭제가 아니라 kit 3개 예외로 완화**
하기로 정했고, 그것을 실행해 fleet 셋이 전부 2.3.1 이 됐다.

```text
amplai-foundry   PR #2     merge   kit 2.3.1 정본 + 설치.  main 에 들어갔다
cortex           PR #146   squash  규약 완화 + 각주 + 설치.  CI 셋 전부 PASS 확인 후
synapse          PR #82    squash  설치 갱신 + 벤더링 정본 삭제 (D-053 남은 것 2)
```

### 그 과정이 드러낸 것 셋

**hook test 가 실제 Store 를 읽고 있었다.** `discover_project_home()` 이
`AMPLAI_PROJECT_HOME` 을 repo-local binding 보다 먼저 보는데, 그 변수를 심는 것이 kit 의
SessionStart hook 자신이다. kit 이 설치된 저장소의 agent 세션에서는 그 test 가 **항상**
실패했고, 사람이 맨 터미널에서 돌리면 통과해서 안 보였다. 2.3.1 이 고쳤다.

```text
사람이 터미널에서   1515 passed
agent 세션 안에서   1 failed, 1514 passed
```

**로컬 gate 와 CI gate 가 다른 도구를 쓰고 있었다.** `ruff>=0.6,<1` 이라 CI 는 매번 최신을
받는다. 0.16 이 markdown 코드 블록 포맷을 켜면서 문서 여섯이 걸렸고 그중 하나가
append-only 인 `DECISIONS.md` 였다. 역사 기록을 포맷터에 맞춰 다시 쓰는 대신
`>=0.15.21,<0.16` 으로 고정했다. **올릴 때는 별도 Work 로 diff 를 보고 올린다.**

**정본을 안 지우면 실제로 깨진다.** synapse 에서 2.2.0 소스와 2.3.1 설치본이 공존하면
installer test 가 둘을 대조하다 실패한다 (실패 5 + 오류 6). 삭제가 그것을 없앤다.

### CI 없이 머지한 구간

머지 시점에 계정 전체의 GitHub Actions 가 과금 문제로 멈춰 있었다. 세 저장소 모든 job 이
`steps=0` 으로 3초 만에 죽었다. **cortex 만 중단 직전에 CI 셋을 통과했고**,
amplai-foundry 와 synapse 는 로컬 검증만으로 admin merge 했다.

```text
amplai-foundry   verifier --profile v2 PASS (block check 11개), pytest 1519 passed
synapse          181 tests, 실패 2 — main baseline 에도 있는 무관한 둘
```

**`PR #2` 의 나머지 41 commit 은 CI 로 확인된 적이 없다.** 생성 이후 여섯 번 전부
`RUF036` 으로 실패했고 그것을 고쳤지만, 고친 뒤의 전체 실행을 인프라 때문에 못 봤다.
Actions 가 복구되면 `main` 에서 한 번 돌려 보는 것이 남는다.

## 다음 할 일

```text
1. CI 복구 확인      Actions 가 살아나면 main 을 한 번 돌린다. PR #2 의 41 commit 은
                     CI 로 확인된 적이 없다
2. policy.json 대칭  제거가 표기를 원복하지 못한다. 내용은 정확히 같다.
                     append_to_json_array 의 역함수가 필요하다
3. supervisor 켜기   HANDOFF §3 의 활성 세션 라우팅 확인이 선행이다.
                     확인 결과에 따라 worktree 인식·알림 설계가 갈린다
4. ruff 0.16         핀을 올릴지. markdown 포맷이 문서 여섯을 건드리고 그중 하나가
                     DECISIONS.md 다
5. A-F1              --all 과 --app 조합이 경고 없이 좁혀진다. help 문구와 실제가 다르다
```

그리고 다른 저장소에 남긴 것 둘.

```text
cortex    high-risk-ack 이 kit 의 async schema 여덟을 equipment 계약으로 잡는다.
          경로 조건을 좁힐지는 그 저장소가 정한다
synapse   .ai-team/runtime/policy.json 의 path_rule 이 tools/amplai-loop-kit/** 를
          가리켜 죽은 rule 이 됐다. 그 pattern 은 kit fragment 소유라 정본에서 고쳐야 한다
```

`.ai-team/README.md`의 Cortex 잔여 제목은 2.3.2 점검에서 amplai-foundry 기준으로
정리했다.

## 해소된 실패 하나 — interpreter 문제였다

`tests/test_slack_http.py` 는 죽지 않는다. **2026-08-19 재확인에서 전 suite 가 통과했다.**

```text
$ .venv/bin/python -m pytest
1382 passed, 4 deselected in 128.04s
```

원인은 sandbox network 정책이 아니라 **interpreter 선택**이다. `python3` 는 system 3.9 로
붙고 그 module 의 `import tomllib` 가 `ModuleNotFoundError` 로 죽는다. 이 저장소는
`requires-python = ">=3.11"` 이다 (`pyproject.toml`).

```text
$ python3 -m pytest tests/test_slack_http.py     # system 3.9
E   ModuleNotFoundError: No module named 'tomllib'

$ .venv/bin/python -m pytest tests/test_slack_http.py
246 passed
```

**test 와 verifier 는 `.venv/bin/python` 으로 돌린다.** 맨 `python3` 를 쓰면 이 저장소
dependency 가 없는 interpreter 에 붙는다. registry 의 `loop-runtime-doctor` 와
`manifest-validator` 두 check 만 `python3` 를 쓰는데, 그 둘은 stdlib 만 써서 3.9 에서도 돈다.

## Stop Rules

- **`doctor` PASS 는 배치 확인이지 동작 확인이 아니다.** 이 저장소가 그 대가를 실측했다 —
  `LOOP DOCTOR: PASS` 와 `VERIFIER: PASS` 가 동시에 나오는 상태에서 Context Pack 은
  한 번도 valid 를 낸 적이 없었다. 새 검사를 넣으면 **어긋난 상태를 만들어 FAIL 을 확인**한다.
- **verifier profile 이름의 정본은 `.ai-team/verifiers/registry.json` 의 `profiles` 다.**
  `policy.json` 의 `profile_rank`, `work-contract.schema.json` 의 enum, `loopctl.py` 의
  `PROFILE_RANK_FALLBACK` 셋은 그것을 따라간다. `doctor` 가 block 으로 잡는다.
- cortex 를 다시 당겨올 때 `scripts/loopctl.py` 는 **이 저장소용으로 고쳐져 있다.** 통째로
  덮으면 semantic required path 와 skill 정본 방향이 되돌아가고, `ALR-002` 가 넣은 정합
  검사도 함께 사라진다. `PORT-LOG.md` 의 "이 저장소에 맞춘 것" 절이 그 목록이다.
- **현재 skill 정본은 `.agents/skills/` 하나다 (`D-055`).** `.claude/skills/`는 exact symlink
  mirror이며 `doctor`가 집합·방향·visibility를 block으로 검사한다. 이 항목은 2026-08-19
  port 당시의 반대 방향 결정을 2.3.2가 supersede한 결과다.
- `.ai-team/` 은 canonical 지식을 복사하지 않는다. `vault/` 가 정본이고 Decision 은
  `docs/workstreams/*/DECISIONS.md` 가 갖는다.

- Stop rule: **무결성 자리가 둘이면 둘 다 갱신하고, 검사하는 코드가 있는지 확인한다.**
  kit 은 `manifest.json` 의 파일별 sha256 과 `CHECKSUMS.sha256` 을 따로 갖는데 selftest 는
  앞의 것만 읽었다. **봉인이 두 번 깨진 채 commit 됐다.** `seal.py --verify` 를 만들고
  verifier 에 `kit-seal` 로 등록해 자동으로 잡히게 했다
- Stop rule: **mutation 을 돌릴 때 원복이 timeout 으로 건너뛰어질 수 있다.** payload 에 넣은
  mutation 이 test hang 으로 원복 단계에 도달하지 못해 정본과 설치본 양쪽에 `if False:` 가
  남았다. **설치본만 치고**, 끝나면 전수 검색으로 mutation 흔적을 확인한다
- Stop rule: **mutation 이 hang 을 만들면 test 를 고친다.** `persistent=True` 루프에서
  중단 조건을 무력화하면 영원히 돈다. hang 은 실패보다 나쁜 신호다 — 즉시 실패하도록
  test 를 바꾼다
- Stop rule: **다른 저장소에 설치했으면 그 저장소의 test 를 돌린다.** `doctor` 만 보고
  넘겼다가 cortex 의 규약 test 를 깨뜨렸다. `.ai-team` 최상위를 일곱으로 못박은 test 가
  있었고 kit 이 셋을 더했다. **각 저장소는 자기 규약을 갖는다**
- Stop rule: **`git status` 로만 확인하면 빈 디렉토리를 놓친다.** git 은 빈 디렉토리를
  추적하지 않는다. uninstall 이 파일만 지우고 디렉토리를 남긴 것을 이 저장소 시험에서는
  못 봤고 cortex 의 규약 test 가 드러냈다
- Stop rule: **test 의 mock 이 실제 형식과 다르면 결함을 숨긴다.** install record 의 필드는
  `package_version` 인데 mock 이 `version` 으로 형식을 지어냈고, 그 탓에 `--verify` 가 전
  대상을 mismatch 로 보고하는 결함이 test 를 통과했다. **실제 산출물을 읽는 test 를 함께
  둔다**
- Stop rule: **시간에 의존하는 test 는 해상도를 먼저 본다.** `utc_now()` 가 초 단위라
  1초 미만 stale 창은 시험할 수 없다. 그리고 **두 필드를 다 늙히면 어느 쪽이 우선인지
  시험하지 못한다** — 고치려던 것과 다른 것을 재게 된다

## ALR-002~ALR-007 당시 Out Of Scope

- semantic runtime (ontology, SHACL, competency question, MCP, project miner)
- AMPLAI V3 (분산 실행) — cortex 도 의도적으로 미구현
- 제품 코드 `src/amplai_foundry/` 변경

## 다른 workstream

`MGC-012 Package 5` 는 **round 17 이 열린 채로 멈춰 있다.** wave 10(T031~T034)을 구현했으나
review 하지 않았다. 재개하려면
`docs/workstreams/messenger-governance-closure-v3/CURRENT_ITEM.md` 를 읽는다.
