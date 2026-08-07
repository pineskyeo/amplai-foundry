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
- Repository Verification: PASS — all seven verify stages at `5b2024b`, 898 tests

## Next

`MGC-012 — Slack Reference Adapter` Package 4의 Slack reference E2E, activation
isolation과 closure review로 간다. Package 3 ordered Slack message projection은
`5b2024b`에서 gate PASS다.

다음 단계는 `/speckit-plan`이다. `/taskify`는 Package 4용 plan이 없어 돌릴 수 없다 —
기존 `plan.md`는 Package 3 범위다.

D-026이 D-019 항목 8을 닫았다. 테스트용 Slack workspace와 app을 새로 만들어 실제 Slack을
치는 reference E2E를 갖는다. activation isolation은 A14 격리 검증뿐이고 `ActivationEvidence`
와 4단계 Gate machinery는 MGC-015가 만든다.

**아직 없는 것 셋.** workspace 자체(생성 전), credential이 프로세스에 도달하는 경로(repo에
Slack 설정을 읽는 `os.environ`이 한 줄도 없다), `conversations.history`의 OAuth scope
(공식 문서 미확인). 앞의 둘은 plan이 설계하고 마지막은 plan Phase 0 research가 고정한다.

D-014의 provider outbox destination granularity는 Package 4에도 넣지 않는다.
`destination_ref` 형식을 바꾸면 `reconcile_connection`이 기존 durable row를 digest
불일치로 거부하므로 evidence migration이 필요하고, 그건 별도 item이다.
