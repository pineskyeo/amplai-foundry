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
- Package 2 — durable ingress ack boundary and background decision handoff:
  `PASS` at `2dcf663`
- Package 3 — ordered Slack message projection, retry and recovery:
  `PASS` at `5b2024b`
- Package 4 wave 5 — real Slack HTTP transport and credential path:
  `PASS` at `dfbcd15`
- Package 4 Wave 6R — typed readback, network-free E2E harness, Provider isolation:
  `PASS` for `MGC-012-T008`, `MGC-012-T009`, `MGC-012-T011` under `APR-017` / `D-032`
- Package 5 — Slack Proposal Cards (`specs/003-slack-proposal-card`, feature `MGC-012-P5`):
  T001~T019 `done`, T020 `ready`. **round 14 FAIL. gate 없음**
- Frozen source/test evidence: round 13 aggregate
  `9573d51c632f4806803e20ef92d229bf9a5a710727f778bb87f5bbe2b03b9349`
- Tests: full `1274 passed, 4 deselected`
- Verification: Ruff check/format, mypy, `verify` 7/7, manifest validator 14/14,
  `git diff --check` 모두 PASS
- Review: **round 10·11·12·13 전부 FAIL.** round 13 은 P1 2건 / Blocking-P2 7건
- Selected next item: **wave 8** — round 14 blocker 8건. `P0-1`(`body = None` 무방비)이 먼저다
- Sequence: `wave 8 → round 15 review → T020 → T010 → T013 → T012`
- Stop rule: Package 5 review 가 닫히기 전에는 Package 4 구현을 재개하지 않는다
- Stop rule: **형제 위치를 끝까지 센다.** 규칙은 "정의 사본 + **호출 지점** + **같은 검사의
  모든 성분**" 이다. 네 라운드 연속 이것을 어겼다 — round 11 `R-1`, 12 `F-2`, 12 `RL-3~5`,
  13 `F-1`. wave 6 은 이 규칙을 문서에 적어놓고 스스로 어겼다
- Stop rule: **test 를 쓸 때 fixture 가 검사 대상을 지우는지 본다.** wave 6 의 token 누출
  test 는 fake opener 가 `del request` 를 해서 guard 가 지우려는 바로 그 local 을 먼저 없앴다.
  세 라운드 연속 이런 test 가 나왔다
- Stop rule: **mutation 이 살아남으면 코드보다 test 를 먼저 의심한다.** wave 6 에서 그렇게
  진짜 누출을 찾았다
- Stop rule: review 지시를 그대로 구현하기 전에 다른 축이 무너지는지 본다. 한 방향을 고치며
  반대를 만든 것이 round 10 → 11 → 12 → 13 으로 이어졌다
- Stop rule: **`/taskify` 를 건너뛰지 않는다.** `CT-3` 이 wave 6 의 절차 생략을 잡았다.
  blocker 를 고치는 wave 도 Pre-Implement Procedure 를 탄다
- Stop rule: **계약 변경은 Decision 을 먼저 받는다.** `C-2`(구현자가 요구사항을 약화)와
  `CT-1`(코드가 요구사항에서 벗어남)이 같은 규칙의 양면이다
- Stop rule: 재freeze → 재review 순서를 지킨다. round 14 target 은 freeze 후 무손상이다
- Stop rule: **수치를 보고하기 전에 두 번 센다.** 세 라운드 연속 틀렸다 — round 12 "21종을
  19종", 13 "12종을 9종", 14 "8블록을 9블록"
- Stop rule: **"전수 셌다" 를 적기 전에 문구 변형을 함께 센다.** round 14 `BP2-3` 은
  `exhaust` 로 grep 해서 "runs out of retries" 를 놓친 것이다
- Stop rule: **가리는 것(masking)을 발견하면 그것이 무엇을 더 가리는지 전수로 센다.**
  round 14 `P0-1` 은 wave 7 이 masking 을 문서화해놓고 그 질문을 안 한 결과다

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
