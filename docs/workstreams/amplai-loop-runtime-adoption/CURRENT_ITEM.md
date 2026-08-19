# Current Item — AMPLAI Loop Runtime Adoption

## Goal

cortex 의 AMPLAI Loop Runtime V2.1 을 amplai-foundry 로 이식해 **이 저장소 개발 자체에**
쓴다. 절차 주도권을 `/work` controller 에 넘기고 user-invocable 을 `/work`·`/design` 둘로
줄인다. semantic runtime 은 제외한다.

근거: `D-046`. 출처: cortex main `3a3eb46b`.

## 상태 — `/work` 를 한 번 완주했다

```text
LOOP DOCTOR: PASS
verifier --profile v2     : PASS (WARN 0건)
verifier --profile commit : PASS
pytest                    : 1382 passed, 4 deselected
```

| | |
|---|---|
| 가져온 것 | skill 5개, `.ai-team/` runtime layer, `loopctl.py`·`loopv2.py`·`eval.sh` |
| 안 가져온 것 | semantic runtime (ontology·SHACL·CQ·MCP·project miner) |
| public skill | `/work`, `/design` 둘. internal 17개 |
| verifier | check 11개 / profile 6개. `verify` 의 7 check 전부 포함 |

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

## 다음 할 일 — 순서대로

1. `.ai-team/rules/` 7개 중 넷이 cortex 고유다 — `c99-rhel8-build`, `dist-ui`,
   `production-debug-map`, `memory-safety`. 파일 전체가 C/RHEL/UI 빌드 규칙이라 이 저장소와
   무관하다. 파일 단위로 정리한다. `c99-rhel8-build.md:29` 는 없어진 `rhel` profile 도 계속
   언급한다.
2. `policy.json` 의 `path_rules` 에 대응 경로가 없는 rule 이 남아 있다 — `public-contract` 의
   `include/cortex/**`·`sdk/**`, `core-runtime` 의 `src/runtime/**`, `build-system` 의 `mk/**`.
   유효한 profile 을 가리켜서 doctor 는 통과하지만 죽은 rule 이다.
3. `.ai-team/policy/documentation.json` 의 `canonical_roots` 가 없는 cortex 경로를 선언한다.
   `historical_documents` 만 이번에 고쳤다.
4. `scripts/loopv2.py:422` 가 `from semantic_runtime import SemanticRuntime` 를 시도한다.
   이식하지 않은 module 이다. try/except 로 감싸져 있는지 확인한다.
5. `environment capture` 가 `Python 3.9.6` 을 기록한다 — system interpreter 다. verifier 는
   `.venv` 로 도는데 fingerprint 는 다른 interpreter 를 남긴다.

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
- **skill 정본은 `.claude/skills/` 다.** cortex 는 반대다. 이식 중 한 번 뒤집어 `taskify` 를
  cortex 판으로 덮었다가 되돌렸다.
- `.ai-team/` 은 canonical 지식을 복사하지 않는다. `vault/` 가 정본이고 Decision 은
  `docs/workstreams/*/DECISIONS.md` 가 갖는다.

## Out Of Scope

- semantic runtime (ontology, SHACL, competency question, MCP, project miner)
- AMPLAI V3 (분산 실행) — cortex 도 의도적으로 미구현
- 제품 코드 `src/amplai_foundry/` 변경

## 다른 workstream

`MGC-012 Package 5` 는 **round 17 이 열린 채로 멈춰 있다.** wave 10(T031~T034)을 구현했으나
review 하지 않았다. 재개하려면
`docs/workstreams/messenger-governance-closure-v3/CURRENT_ITEM.md` 를 읽는다.
