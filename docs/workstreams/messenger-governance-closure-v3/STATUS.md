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
- Repository Verification: PASS — all seven verify stages at `2dcf663`, 709 tests

## Next

`MGC-012 — Slack Reference Adapter` Package 3의 ordered Slack message projection,
retry와 recovery를 구현한다. Package 2 durable ack boundary와 background decision
handoff는 `2dcf663`에서 gate PASS다.

Package 3의 범위와 진행 방식은 D-018에 있다. D-014의 provider outbox destination
granularity는 Package 3에 넣지 않는다. `destination_ref` 형식을 바꾸면
`reconcile_connection`이 기존 durable row를 digest 불일치로 거부하므로 evidence
migration이 필요하고, 그건 별도 item이다.
