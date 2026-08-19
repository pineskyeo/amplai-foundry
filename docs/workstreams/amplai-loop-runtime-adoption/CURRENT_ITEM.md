# Current Item — AMPLAI Loop Runtime Adoption

## Goal

cortex 의 AMPLAI Loop Runtime V2.1 을 amplai-foundry 로 이식해 **이 저장소 개발 자체에**
쓴다. 절차 주도권을 `/work` controller 에 넘기고 user-invocable 을 `/work`·`/design` 둘로
줄인다. semantic runtime 은 제외한다.

근거: `D-046`. 출처: cortex main `3a3eb46b`.

## 상태 — 이식 끝, 시험 전

```text
LOOP DOCTOR: PASS
verifier --profile fast : PASS
verifier --profile full : pytest 하나만 FAIL (환경 문제, 아래 참고)
```

| | |
|---|---|
| 가져온 것 | skill 5개, `.ai-team/` runtime layer, `loopctl.py`·`loopv2.py`·`eval.sh` |
| 안 가져온 것 | semantic runtime (ontology·SHACL·CQ·MCP·project miner) |
| public skill | `/work`, `/design` 둘. internal 17개 |
| verifier | check 12개 / profile 6개. `verify` 의 7 check 전부 포함 |

세부는 [PORT-LOG.md](PORT-LOG.md), 조사 근거는 [FACTS.md](FACTS.md).

## 다음 할 일 — 순서대로

1. **`/work` 를 실제로 한 번 돌린다.** 이식 후 아직 실행한 적이 없다. **이게 제일 중요하다** —
   doctor 가 PASS 라는 것은 파일이 제자리에 있다는 뜻이지 loop 가 돈다는 뜻이 아니다.
   작은 목표 하나로 시험한다. 예: `/work loopctl status 출력에 feature 이름을 넣는다`
2. `.ai-team/knowledge/map.json` 과 `claims.jsonl` 이 **cortex 내용 그대로**다. 이 저장소
   것으로 바꿔야 Knowledge Readiness 가 의미를 갖는다.
3. `.ai-team/rules/` 7개를 안 읽었다. cortex 고유 규칙이 섞여 있을 수 있다.
4. `contracts/work-contract.template.json` 의 verifier profile 참조 확인. doctor 의
   `contract template: valid` 는 통과했지만 실제 Work 로 써 본 적은 없다.

## 알려진 실패 하나 — 이식과 무관

`tests/test_slack_http.py` 가 죽는다. localhost 연결이 막혔다.

```text
$ pytest --ignore=tests/test_slack_http.py
1136 passed

최소 재현: ThreadingHTTPServer 를 127.0.0.1:0 에 띄우고 urlopen
        → URLError [Errno 49] Can't assign requested address
```

그 module 의 `_FakeSlack` 이 실제 loopback HTTP server 를 쓴다. 세션 초반에는 전 suite
1382건이 통과했으므로 sandbox network 정책이 도중에 바뀐 것으로 보인다 **(추정)**.
`src/` 와 `tests/` 는 무변경이다. **다음 세션에서 먼저 이 환경 문제인지 다시 확인한다.**

## Stop Rules

- **`/work` 를 돌려 보기 전에는 이식이 성공했다고 보고하지 않는다.** doctor PASS 는 배치
  확인이지 동작 확인이 아니다.
- cortex 를 다시 당겨올 때 `scripts/loopctl.py` 는 **이 저장소용으로 고쳐져 있다.** 통째로
  덮으면 semantic required path 와 skill 정본 방향이 되돌아간다. `PORT-LOG.md` 의
  "이 저장소에 맞춘 것" 절이 그 목록이다.
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
