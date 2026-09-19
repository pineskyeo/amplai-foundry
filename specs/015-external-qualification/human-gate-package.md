# Human Gate Package — V3-062 Authorized Cutover

이 문서는 사람이 결정해야 하는 마지막 관문을 **정확히 어떤 입력으로** 여는지 적는다.
자동화가 대신 승인하지 않는다. 코드가 그것을 막는다:

- `src/amplai_foundry/distribution/cutover.py` — `human_decision_ref is None` 이면 `CUTOVER_HUMAN_GATE`.
- `src/amplai_foundry/runtime/contracts/foundry_authority.py:197` — 승인은 `action == "release.cutover"` 이고
  `subject_digest` 가 요청과 정확히 같아야 한다 (`APPROVAL_BINDING`).

## 승인 subject 를 계산하는 법

subject 는 세 값의 canonical digest 다. 값 중 하나라도 다르면 승인이 붙지 않는다.

```python
from amplai_foundry.runtime.contracts.identity import digest
subject = digest({
    "release_ref": <candidate release ref>,        # release-set 객체의 ref
    "targets": [{"id": "...", "binding_ref": <app binding ref>, ...}],
    "expected_active_ref": <현재 active release-pointer 의 release_ref 또는 None>,
})
```

## 사람이 하는 3단계

1. **Foundry governance 에 결정을 남긴다.** 기존 governance 승인 경로(`amplai-foundry governance ...`,
   `.ai-team/policy/approvals.jsonl` 은 사람만 쓴다) 로 `action = release.cutover`,
   `subject_digest = <위 값>` 인 decision 을 만든다. LLM 이나 runtime 은 이 객체를 만들 수 없다.
2. **결정을 V3 runtime 에 연결한다.** `POST /api/v3/authority/link` (runtime.admin 필요) 가
   `foundry-decision-link` 를 만든다. 이 링크의 ref 가 `human_decision_ref` 다.
3. **cutover 를 실행한다.** `CutoverService.cutover(actor, release_ref, targets=..., human_decision_ref=<링크 ref>,
   expected_active_ref=...)`. 성공하면 `release-pointer/active` 가 CAS 로 바뀌고 `cutover-receipt` 가 남는다.
   그 뒤 `release/rc01-conformance-closure.json` 의 `release_status` 를 FINAL 로 올리는 것은 별도 사람 결정이다.

## 아직 없는 것 (후속)

- `amplai ops` CLI 에 `cutover` 명령이 없다 (`src/amplai_foundry/runtime/cli.py` 의 ops 명령: version, status,
  demo, execution-demo, meta-demo, evolution-demo, observatory, validate, schemas, keygen, serve, doctor, restore).
  현재는 Python 호출로만 실행한다.
- 승인 뒤에도 남는 외부 항목은 `external-qualification-status.json` 의 partial/unavailable 8건이다. FINAL 선언 전에
  조직이 그 8건을 수용할지 결정한다.

## 이 패키지가 하지 않는 것

승인 객체를 만들거나 `approvals.jsonl` 을 쓰지 않는다. subject 계산 코드만 제공한다.
