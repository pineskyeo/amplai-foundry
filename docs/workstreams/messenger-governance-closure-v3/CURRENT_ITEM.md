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
  구현 `done`(T001/T002/T003), review **미완**. round 10 three-lens 진행 중. gate 없음
- Frozen source/test evidence:
  `0009be7bc702705f776ee057bb717d607db0168e11c8d86feb3623e428c1f20f`
- Tests: targeted `189 passed, 1 deselected`; full `1087 passed, 1 deselected`
- Verification: Ruff check/format, mypy, Vault knowledge lint, `amplai-foundry verify` 7/7,
  task manifest validator 8/8와 `git diff --check` 모두 PASS
- Review: Package 3는 wave 4개로 나눠 wave마다 three-lens review를 돌렸고 전부 blocker 0
  으로 닫았다. wave 4는 2 round 진행 — round 1 고유 blocker 6건, round 2 5건 해소.
  round 2의 5건 중 둘은 round 1 수정이 만든 새 결함이다. mutation 16종 survivor 0
- Review: Wave 6R final contract/failure-recovery blocker와 advisory 0. Isolated regression
  mutation 10종 killed/0 survived. Wave 6R 전후 source/test combined hash는 동일하다.
- Remaining: Package 4와 MGC-012는 `ACTIVE`다. `MGC-012-T010`(live Slack target와 네
  환경값), `MGC-012-T012`(Python 3.12 clean clone 및 dependencies), `MGC-012-T013`
  (production entrypoint/provider recovery/governed recovery/lifecycle schema 승인)은 모두
  `blocked`다.
- Selected next item: **Package 5 wave 5** — round 11의 P1 셋을 닫는다. `R-1`(`slack_http.py`의
  고쳐지지 않은 사본)이 먼저다.
- Selected phase: wave 4는 끝났다. T004~T008 여덟 task 전부 `done`이고 round 11 three-lens를
  돌렸다. **round 11도 FAIL**이지만 P0는 0이고 mutation kill rate가 33% → 67%로 올랐다.
  기록은 `evidence/3lens-review-round-11.md`다.
- Sequence: `P5 wave 4 → P5 round 11 review → T010 → T013 → T012`. T013 configured startup
  trace가 T010의 실제 target/config evidence를 재사용하고, T012는 T010과 T013 완료 뒤
  closure를 수행한다.
- Stop rule: Package 5 review가 닫히기 전에는 Package 4 구현을 재개하지 않는다. round 9를
  assessed로 세지 않는다. drift 이전에 쓰인 P5 evidence 세 건의 command 결과를 round 10
  근거로 재사용하지 않는다.
- Stop rule: 결함을 고칠 때 **같은 형태가 저장소에 몇 개 있는지 먼저 센다.** wave 4의 `R-1`이
  이 규칙이 없어서 생겼다. `slack_projection.py`의 bare `BaseException` 을 고치면서 같은
  코드가 `slack_http.py`에도 있는 것을 놓쳤고, negative verification도 고친 사본만 확인했다.
- Stop rule: review 지시를 그대로 구현하기 전에 그 지시가 다른 축을 무너뜨리는지 본다. `R-2`가
  그 사례다. round 10 `C-1`이 "dead letter + operator hold"를 선언했고 그대로 따랐는데, 그
  지시가 transient와 terminal을 구분하지 않아 일시 장애가 되돌릴 수 없는 hold가 됐다.
- Stop rule: test가 무엇을 고정하는지 스스로 증명한다. `R-4`가 동어반복 test였다. 겨냥한
  guard를 실제로 깨뜨려 실패를 확인하지 않은 test는 evidence에 "고정한다"고 적지 않는다.
- Stop rule: 계약·spec을 코드에 맞출 때 **양쪽 집합의 크기를 센다.** `R-5`가 4 대 5 불일치를
  남기고 일치했다고 기록한 사례다.
- Stop rule: 재freeze → 재review 순서를 지킨다. round 11 aggregate는
  `36e3923f66599997f2e4eb56d535a63276b7d6b8f8ee93a1bf3d555cdb7ec247`이고 wave 5 완료 후
  round 12를 새로 얼린다. 이전 aggregate를 baseline으로 재사용하지 않는다.
- Freeze rule (D-035, round 12부터): frozen target에 **모든 per-task manifest YAML**을 넣고,
  `task-manifests/index.yaml`·`tasks.md`·`evidence/**`를 뺀다. 얼리기 전에 manifest header의
  제외 문구와 실제 line 목록을 대조한다.

  경위는 이렇다. round 10과 round 11이 같은 방식으로 drift했다 — review가 끝난 뒤 task status를
  `done`으로 바꾸면 `index.yaml`과 `tasks.md`가 재생성되어 aggregate가 어긋난다. 둘 다 무해했고
  둘 다 기록했지만 같은 실수가 두 번 났다. 더 큰 문제는 반대쪽이었다. `T004`~`T008.yaml`은 wave 4
  가 한 일의 계약 전부인데 target에 없었다. 자주 바뀌는 사무 기록은 얼려두고 검토 기준인
  계약서는 안 얼린 구성이었다.
- Stop rule: E-3/E-4가 확인되기 전에는 T010 구현을 시작하지 않는다. 설정 확인용 harness의
  성공을 live Slack E2E PASS로 보고하지 않는다. P5 `T003`의 live E2E 통과는 T010 acceptance가
  아니다.

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
