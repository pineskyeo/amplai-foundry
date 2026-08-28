# AMPLAI Verifier Registry V2

`registry.json`은 무엇을 PASS로 볼지 정의하는 executable SSOT다. Skill prompt에 숨은 check를 두지 않는다.

```bash
.venv/bin/python .ai-team/verifiers/run.py --list
.venv/bin/python .ai-team/verifiers/run.py --profile fast
.venv/bin/python .ai-team/verifiers/run.py --profile v2 --json-report /tmp/v2-report.json
```

## Profiles

- `fast`: ruff check/format, mypy, loop runtime doctor, task manifest validator
- `commit`: fast + `git diff --check`
- `standard`: fast + 전체 pytest
- `runtime`: standard + loop 가 부르는 shell script 문법
- `full`: standard + `amplai-foundry` schema/vault lint/project pack
- `v2`: runtime + full — **gate 판정용**

이름은 `registry.json` 의 `profiles` 가 정본이다. `policy.json` 의 `profile_rank`,
`work-contract.schema.json` 의 `verification.profile` enum, `loopctl.py` 의
`PROFILE_RANK_FALLBACK` 셋이 그것과 어긋나면 `loopctl doctor` 가 block 한다.

V2 report에는 commit, host/platform/machine, Python/cc/make/git version, registry hash, target assumptions가 포함된다. 이는 **어떤 환경에서 무엇을 증명했는지 경계를 남기는 것**이다.

Block check가 현재 환경에서 실행 불가능하면 PASS가 아니라 `UNAVAILABLE`(exit 2)다. warn check는 기록하되 block verdict를 바꾸지 않는다.
