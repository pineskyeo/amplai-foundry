# V2 Work Contract

normal/high Work는 `specs/<feature>/work-contract.json`을 implementation 전에 만든다. Contract는 spec 전체가 아니라 **이번 Work의 Done, knowledge route, verifier, environment, provenance, gate**를 고정한다.

## Required V2 fields

- `goal`, `work_type`, `risk`
- `scope.include/exclude`, `non_goals`
- `acceptance[].statement/evidence`
- `knowledge.readiness/context_pack/domain_discovery`
- `verification.profile`
- `environment.fingerprint`
- `provenance.evidence_trace`
- `human_gates`
- `retry.max_attempts_per_slice`
- `status`

Domain-heavy/architecture/operations/repository-gardening 또는 high-risk Contract는 Knowledge Readiness와 Context Pack이 `required`다. LOW tiny change만 명시적으로 bypass할 수 있다.

`work_type`은 `tiny_change`, `bug_fix`, `logic_change`, `refactor`, `new_feature`, `domain_heavy`, `architecture`, `operations`, `repository_gardening` 중 하나다. repository 전체 정리는 `repository_gardening`으로 분류하고 별도 command를 만들지 않는다.

```bash
python3 scripts/loopctl.py contract validate specs/<feature>/work-contract.json
```

`tasks.md`는 Slice 실행 순서이고 `work-contract.json`은 feature-level Done 계약이다. 같은 내용을 중복 SSOT로 만들지 않는다.
