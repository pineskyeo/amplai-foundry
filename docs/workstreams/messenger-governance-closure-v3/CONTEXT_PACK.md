# Messenger Governance Closure V3 Context Pack

## Read First

1. `AGENTS.md`
2. External `SPEC.md`
3. `CURRENT_ITEM.md`
4. `QUALITY_GATES.md`
5. `PROGRESSION_QUEUE.md`

## External Spec

```text
/Users/pinesky/Documents/Codex/2026-07-29/prior-conversation-with-codex-conversation-role/outputs/SPEC.md
```

Expected SHA-256:

```text
e20876c8f51e1b155a54404c64c7bca3b4cf2498319c8116ed8f12d75ca975e3
```

Spec hash가 다르면 구현을 중단하고 contract 변경을 검토한다.

## Baseline

- Branch: `main`
- HEAD: `c3d635fdf20d5759cab803eaaa5922a905ab9233`
- Existing core: MGC-001–011 Governance, ingress, Outbox, Apply/Publish와 v2 migration
- Current risk: Slack raw-body 인증과 durable ack 사이의 3-second failure boundary

## Working Rule

작은 slice를 구현하고 test 후 subagent review를 실행한다. Review finding은 Main Codex가 triage한다.
