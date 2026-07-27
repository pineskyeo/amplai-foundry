# Proposal Summary

## Source

`SRC-20260728-E0C88A4D`은 같은 사용자 요구가 다른 표현, 시점 또는 모델을 거치며 여러 canonical note로 흔들리는 위험과 `Semantic Identity & Comparison Kernel` 개선 방향을 정리한 ChatGPT 대화다.

## Existing Knowledge Comparison

- `QUE-0009`가 semantic duplicate/conflict 판정을 이미 열린 질문으로 보유한다. 새 질문은 만들지 않고 구체화한다.
- `CON-0008`이 structured candidate와 evidence metadata를 이미 보유하므로 비교용 semantic structure만 보강한다.
- `ARC-0004`가 broad Governed Knowledge Compiler pipeline을 보유한다. 새 `ARC-0006`은 그 안의 좁은 comparison component boundary다.
- `EXP-0002`가 Golden Set/replay 실험을 이미 보유하므로 새 실험 대신 pairwise cases와 metric을 추가한다.

## Proposed Changes

- provisional `SemanticAnchor` concept를 만든다.
- semantic identity 비교가 kind 선택과 canonical creation보다 선행한다는 principle 후보를 만든다.
- Candidate Validator와 Proposal Builder 사이의 Semantic Comparison Kernel architecture 후보를 만든다.
- `UNCERTAIN` 결과가 Canonical CREATE/UPDATE를 승인하지 않는 decision 후보를 만든다.
- Phase 1A gate 이후 Phase 1B의 첫 신규 slice로 semantic comparison 계약과 Golden Set을 검증하는 sequencing decision 후보를 만든다.
- `QUE-0009`, `CON-0008`, `EXP-0002`, `MAP-0001`을 보강한다.

## Roadmap Boundary

Phase 1A는 여전히 current focus이며 완료되지 않았다. Approved roadmap에는 Phase 1B subphase가 없다. 이전 미승인 제안의 `1B-1 = Immutable Source Intake`와 이번 Source의 `1B-1 = Semantic Identity & Comparison Kernel` 명칭도 충돌하므로, 이 Proposal은 roadmap 파일이나 current focus를 직접 수정하지 않는다.

## Evidence Boundary

Referenced conversation JSON은 Original Content line 4(Vault file line 32) 한 줄에 저장됐다. 현재 Proposal locator는 line range만 지원하므로 세부 section별 locator를 가장하지 않고 Original Content line 4를 공통 evidence로 사용한다. 세부 claim은 operation reason과 draft 범위로 제한한다.

## Non-Changes

- 구현 코드를 작성하지 않는다.
- 외부 embedding, vector DB, LLM 자동 판정, 자동 merge/apply를 추가하지 않는다.
- `REFINES`를 모든 kind에 UPDATE로 고정하지 않는다.
- SemanticAnchor schema, comparison threshold와 Golden Set pass threshold를 확정하지 않는다.
- `QUE-0009`를 종료하지 않는다.

## Review Focus

- SemanticAnchor를 별도 runtime object로 둘지, 다른 이름과 lifecycle을 사용할지 검토한다.
- active와 candidate artifact를 retrieval corpus에서 어떻게 구분할지 검토한다.
- `UNCERTAIN` hold의 durable storage, review queue와 release condition을 검토한다.
- Phase 1B subphase 명칭과 기존 deferred numbering을 어떤 방식으로 정리할지 검토한다.
