# Proposal Summary

## Source

`SRC-20260726-FEA66458`은 AMPLAI v2 로드맵 이후 Hermes의 역할, AMPLAI Runtime과 Knowledge Steward의 실행 경계, LLM 비결정성 통제, Governed Knowledge Compiler, 회귀 평가와 구현 우선순위를 정리한 사용자 제공 대화 원문이다.

## Proposed Changes

- AMPLAI Runtime과 Hermes의 host/client boundary를 기존 candidate concept에 보강한다.
- Memory Candidate, provenance, proposal review, Governor와 Meta-loop에 immutable Source, evidence, deterministic validation, risk approval, replay의 의미를 보강한다.
- Governed Knowledge Compiler와 event-driven Knowledge Steward를 candidate architecture로 분리한다.
- Knowledge Steward Golden Set/replay regression을 candidate experiment로 만든다.
- 모델 라우팅, semantic dedup/conflict, auto-approval, Hermes memory intake trigger와 Source Store boundary를 open question으로 기록한다.

## Deferred

- Hermes First, Event-driven Steward, No Direct Canonical Write, Deterministic Renderer, Evidence-required Candidate는 Source가 명시한 Decision 후보다. 이 Proposal은 이를 active Decision으로 확정하지 않는다.
- Phase 1B-1부터 1B-6까지의 세분화와 Cortex 첫 E2E slice는 approved roadmap의 별도 review 대상이다. 이번 Proposal에서는 `IGNORE`로 분류한다.
- `roadmap_update`, `ignore` candidate type과 후보 자동 적용 범위는 현재 domain contract에 아직 존재하지 않는다.

## Review Focus

- evidence range와 extraction metadata를 canonical governance requirement로 승격할지 검토한다.
- Governor의 risk/approval 범위와 LLM direct-write 금지를 runtime policy로 구체화할지 검토한다.
- 새 architecture와 experiment가 현재 Markdown-first prototype의 범위를 넘는 장기 설계 후보임을 확인한다.
- open question 4건의 분리와 Source Store 질문의 기존 `QUE-0007` 보강 범위를 검토한다.
