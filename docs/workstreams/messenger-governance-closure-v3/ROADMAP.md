# Messenger Governance Closure V3 Roadmap

## Goal

`AMP-SPEC-MGC-003`을 구현해 Messenger decision과 canonical apply를 crash-safe governance boundary 안에 둔다.

## Source Contract

- Spec: `/Users/pinesky/Documents/Codex/2026-07-29/prior-conversation-with-codex-conversation-role/outputs/SPEC.md`
- Spec SHA-256: `e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3`
- Repository baseline: `e78858cba73c70307385458ad37fe2c41e34173b`

## Phases

| Phase | Outcome | Items |
|---|---|---|
| Foundation | SQLite authority와 immutable definition 기반 | `MGC-001`–`MGC-003` |
| Decision | Token, replay, ingress, mutation closure | `MGC-004`–`MGC-007` |
| Projection | Ordered outbox와 read projection | `MGC-008` |
| Apply | ApplyGrant, Worker staging, fenced publish | `MGC-009`–`MGC-010` |
| Integration | Migration, Slack, Telegram, Hermes | `MGC-011`–`MGC-014` |
| Activation | Runtime gate와 full verification | `MGC-015`–`MGC-016` |

## Completion

다음 조건을 모두 만족하면 workstream을 완료한다.

- `SPEC.md` Acceptance Criteria `AC-01`–`AC-15` 통과
- Python 3.11/3.12 clean-clone 검증
- `amplai-foundry verify` 단일 CI entrypoint 통과
- Slack/Telegram reference E2E 통과
- Subagent code/contract/operations review blocker 0건
- 활성화 flag 기본값 `false`
