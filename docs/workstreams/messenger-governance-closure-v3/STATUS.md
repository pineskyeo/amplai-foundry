# Messenger Governance Closure V3 Status

## Summary

| Field | Value |
|---|---|
| Workstream | `messenger-governance-closure-v3` |
| Status | in-progress |
| Current Item | `MGC-012` |
| Completed | `11/16` |
| Baseline | `e78858cba73c70307385458ad37fe2c41e34173b` |
| Spec | `AMP-SPEC-MGC-003` |

## Gate

- Planning: PASS
- Implementation: MGC-001–011 PASS / MGC-012 ACTIVE
- Subagent Review: MGC-012 Package 1 PASS at `ef35201` — P0/P1/Blocking-P2 0
- Subagent Review: MGC-012 Package 2 — round 4 three-lens blocker 0 at `f3f7a98`,
  post-gate delta regression review blocker 0 at `2dcf663`. Round 1–3에서 고유
  blocker 16건, round 5에서 1건을 해소했다
- Subagent Review: MGC-012 Package 3 PASS at `5b2024b` — wave 1–4 전부 blocker 0.
  wave 4는 round 1에서 고유 blocker 6건, round 2에서 5건을 해소했다. round 2의 5건 중
  둘은 round 1 수정이 만든 새 결함이다. mutation 16종 survivor 0
- Subagent Review: MGC-012 Package 4 wave 5 PASS at `dfbcd15` — 2 라운드. round 1에서
  고유 blocker 8건(P0 1건 포함), round 2에서 2건을 해소했다. round 2의 둘은 round 1
  수정이 미완이었음을 드러낸 test gap이다. mutation 37종 중 survivor 3은 전부 닫았다
- Repository Verification: PASS — all seven verify stages at `dfbcd15`, 987 tests

## Next

`MGC-012 — Slack Reference Adapter` Package 4 wave 6으로 간다 — T008 readback 자가검사,
T009 E2E harness, T011 Provider 격리. 셋 다 network 없이 돌고 같은 test file을 쓰므로
순차로 돈다. wave 5(T006 transport, T007 credential)는 `dfbcd15`에서 gate PASS다.

wave 5 이전에 "아직 없다"고 적었던 셋은 전부 닫혔다. Slack workspace와 app은 만들어졌고
(`auth.test` 확인, scope가 `chat:write`·`channels:history`로 H-1.1과 일치), credential
경로는 T007이 만들었고(`AMPLAI_SLACK_BOT_TOKEN`, `AMPLAI_SLACK_SIGNING_SECRET`),
`conversations.history`의 OAuth scope는 research R-009가 공식 문서로 고정했다.

**새로 열린 것 하나.** T010이 쓸 channel ID(`C...`)와 app ID(`A...`)가 계약에 없다.
`auth.test`가 주는 `bot_id`는 `app_id`와 다른 값이라 대체할 수 없고, `reconcile`이
`app_id`로 남의 message를 배제하므로 필수다. T009 또는 T010이 H-4.1에 변수를 더한다.

D-014의 provider outbox destination granularity는 Package 4에도 넣지 않는다.
`destination_ref` 형식을 바꾸면 `reconcile_connection`이 기존 durable row를 digest
불일치로 거부하므로 evidence migration이 필요하고, 그건 별도 item이다.
