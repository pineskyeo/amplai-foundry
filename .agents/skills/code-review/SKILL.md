---
name: code-review
description: Internal independent V2 quality gate. Review a completed diff against contract, Context Pack, active evidence, Cortex rules, verifier environment, safety policy, and semantic regression, then emit a machine-readable verdict.
user-invocable: false
disable-model-invocation: false
---

# Independent V2 Review

구현 agent와 가능한 한 분리된 context에서 수행한다.

- converge: 만들기로 한 것이 모두 구현됐는가
- review: 구현이 정확하고 안전한가
- semantic review: 지식/ontology 의미가 근거와 일치하는가

## 반드시 읽을 evidence

- work contract, spec, plan, task manifests
- Knowledge Readiness, Domain Discovery, Context Pack, handoff
- actual diff와 관련 코드
- evaluator report와 environment fingerprint
- evidence trace
- active decisions/claims, 관련 ontology/binding/evidence
- 변경 경로에 해당하는 `.ai-team/rules/`와 permission policy

실행하지 않은 테스트를 PASS라고 쓰지 않는다. superseded 지식을 current truth로 사용하지
않는다. secret/PII는 값 없이 종류와 위치만 보고한다.

## Review order

1. scope/contract/Context Pack 이탈과 누락
2. Knowledge Readiness가 실제 evidence로 READY였는지
3. correctness, error/failure path, resource lifetime
4. public API/schema/ABI/registry와 semantic compatibility
5. concurrency, retry/idempotency, partial recovery
6. C99/RHEL/HP-UX/Python compatibility 및 environment 차이
7. verifier의 판정력, RED→GREEN/회귀 evidence
8. active/superseded conflict, provenance, ontology candidate lifecycle
9. permission/containment/security/privacy
10. maintainability와 불필요한 framework/Skill/V3 scope creep

## Verdict contract

마지막 블록은 둘 중 하나다.

```text
VERDICT: PASS
```

```text
VERDICT: CHANGES_REQUIRED

FINDINGS:
- R001 [blocking] path:line — 결함, 위반 근거, 영향, 최소 수정 방향.
- R002 [should-fix] path:line — 위험, 근거, 수정 방향.
```

blocking이 하나라도 있으면 `CHANGES_REQUIRED`다. nit만으로 loop를 막지 않는다.
