# Current Item — MGC-012

## Goal

Slack interaction을 raw body 상태에서 인증하고 verified credential만 기존 durable
ingress 계약으로 전달한다. 성공 ack는 ingress commit 이후 3초 이내 반환하며 decision,
message projection과 retry는 background path에서 수행한다.

## Frozen Acceptance

- A1: raw request body를 deserialize 전에 확보하고 검증 byte와 fingerprint byte가 동일함
- A2: `X-Slack-Signature` HMAC을 constant-time으로 검증하고 signing secret 또는 raw body를 저장·로그하지 않음
- A3: `X-Slack-Request-Timestamp`의 허용 clock skew를 검증하고 stale 또는 malformed request를 fail-closed함
- A4: verified payload의 `api_app_id`, workspace 또는 enterprise identity가 Project installation allowlist와 일치함
- A5: 공식 `block_actions` contract만 수락하고 범용 `interaction_payload_id`를 요구하지 않음
- A6: external actor, container/view, action ID, action timestamp와 raw body digest로 deterministic ingress fingerprint를 생성함
- A7: opaque ActionToken credential은 hash로만 durable ingress에 전달하고 raw credential과 raw body는 DB·Audit·log에 남기지 않음
- A8: durable ingress commit 전 success ack를 반환하지 않으며 DB busy 또는 commit 실패 시 non-success ack를 반환함
- A9: ingress connection timeout과 전체 synchronous path가 Slack 3-second ack budget을 침범하지 않음
- A10: ack 이후 decision 처리는 기존 authority, idempotency, token과 Proposal state 계약을 background worker에서 사용함
- A11: Provider message는 ordered Outbox를 사용하며 retry, supersession, DLQ와 operator hold 계약을 우회하지 않음
- A12: 동일 verified interaction replay는 최초 durable 결과로 수렴하고 같은 key의 다른 fingerprint는 conflict로 실패함
- A13: unsupported payload, invalid signature/timestamp/app/workspace/actor/action과 malformed form/JSON test가 fail-closed함
- A14: Slack adapter가 Telegram 또는 다른 Provider activation state를 변경하지 않음
- A15: Slack reference E2E, 전체 regression과 `amplai-foundry verify`가 통과하고 Contract·Evidence·Ops review blocker가 0건임

## Slices

1. Raw request verification and normalized Slack interaction contract
2. Durable ingress ack boundary and background decision handoff
3. Ordered Slack message projection, retry and recovery
4. Slack reference E2E, activation isolation and closure review

## Current Package

- Package 1 — raw-body signature/timestamp/allowlist contract and fail-closed fixtures:
  `PASS` at `ef35201`
- Tests: `649/649 PASS`
- Verification: `7/7 PASS`
- Review: Contract·Evidence·Ops blocker 0
- Next: Package 2 — durable ingress ack boundary and background decision handoff

## Out Of Scope

- Telegram webhook와 callback adapter
- Hermes natural-language Skill
- production Slack credential 발급 또는 secret rotation 실행
- Slack activation rollout
- Provider message를 authoritative Proposal state로 사용하는 기능

## Review Team

- Slack protocol, raw-body signature and normalized contract reviewer
- credential secrecy, durable ack and replay evidence reviewer
- timeout, retry, Outbox, DLQ and operational failure reviewer
