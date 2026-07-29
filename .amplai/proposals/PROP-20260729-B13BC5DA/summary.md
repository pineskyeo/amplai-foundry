# Proposal Summary

## Source

`SRC-20260729-B13BC5DA`는 Hermes를 Slack·Telegram 기반 비서로 사용하면서 AMPLAI가 Project, Proposal, authority와 apply 통제를 소유하는 역할 경계를 정리한 ChatGPT 대화다.

## Existing Knowledge Comparison

- `CON-0010`이 Hermes를 교체 가능한 Client Partner와 Work Manager로 정의한다.
- `ARC-0003`이 Knowledge Layer, Governor, Work Manager와 Implementation Agent 책임을 분리한다.
- `PRI-0003`이 LLM의 직접 canonical write를 금지하고 Proposal review를 요구한다.
- `CON-0012`가 approval boundary를 Governor 책임으로 둔다.

## Proposed Changes

- `CON-0010`에 메신저 접수, AMPLAI 전용 API 호출과 직접 Git/shell 금지 경계를 추가한다.
- `ARC-0003`에 채널과 AMPLAI authority 경계를 추가한다.
- 채널 독립 `ProposalAction`을 중심으로 한 Messenger Proposal Control architecture를 만든다.
- 메신저 action을 AMPLAI가 actor, project, version, digest, state와 replay 기준으로 재검증하고 approve/apply를 분리하는 Decision을 만든다.
- `MAP-0002`에 새 architecture와 Decision을 연결한다.

## Non-Changes

- Slack·Telegram adapter와 Hermes Skill을 canonical knowledge 적용과 함께 자동 배포하지 않는다.
- Hermes, Slack 또는 Telegram에 canonical authority를 주지 않는다.
- 현재 `phase-2-ontology-kernel` focus를 변경하지 않는다.
- 기존 Phase 0과 Phase 1A~1C 완료 상태를 되돌리지 않는다.

## Review Focus

- `ProposalAction` core가 channel SDK에 의존하지 않는지 검토한다.
- external adapter 연결 전 Action Token과 provider verification gate가 필요한지 검토한다.
