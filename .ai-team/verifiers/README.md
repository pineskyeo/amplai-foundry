# Cortex Verifier Registry V2

`registry.json`은 무엇을 PASS로 볼지 정의하는 executable SSOT다. Skill prompt에 숨은 check를 두지 않는다.

```bash
python3 .ai-team/verifiers/run.py --list
python3 .ai-team/verifiers/run.py --profile fast
python3 .ai-team/verifiers/run.py --profile semantic
python3 .ai-team/verifiers/run.py --profile v2 --json-report /tmp/v2-report.json
```

## Profiles

- `fast`: 빠른 repository invariants
- `commit`: fast + commit acknowledgement
- `standard`: fast + schemas/contracts
- `runtime`: standard + Loop Runtime structure/policy/permission/quadrant
- `semantic`: fast + RDF/CQ/SHACL/MCP/proposal
- `v2`: runtime + semantic + mining/gardening
- `full`: product build/unit/functional/smoke
- `rhel`: full + RHEL compatibility

V2 report에는 commit, host/platform/machine, Python/cc/make/git version, registry hash, target assumptions가 포함된다. 이는 target RHEL/32-bit/Python 3.6을 실제로 실행했다는 뜻이 아니라 **어떤 환경에서 무엇을 증명했는지 경계를 남기는 것**이다.

Block check가 현재 환경에서 실행 불가능하면 PASS가 아니라 `UNAVAILABLE`(exit 2)다. warn check는 기록하되 block verdict를 바꾸지 않는다.
