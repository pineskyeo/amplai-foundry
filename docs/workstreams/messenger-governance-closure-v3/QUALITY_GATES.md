# Messenger Governance Closure V3 Quality Gates

## Item Gate

- [ ] Frozen acceptance 충족
- [ ] Item scope test 통과
- [ ] 기존 regression test 통과
- [ ] Ruff 통과
- [ ] mypy 통과
- [ ] Knowledge lint 통과
- [ ] Subagent contract review blocker 0건
- [ ] Subagent failure/recovery review blocker 0건
- [ ] Subagent regression review blocker 0건
- [ ] Main Codex finding triage 완료

## Review Severity

| Severity | Gate |
|---|---|
| P0 | 차단 |
| P1 | 차단 |
| Blocking-P2 | 차단 |
| Advisory | 기록 후 item별 판단 |

## Completion Gate

- [ ] Python 3.11 clean clone
- [ ] Python 3.12 clean clone
- [ ] `amplai-foundry verify`
- [ ] Hard-kill suite
- [ ] Storage failure suite
- [ ] Slack reference E2E
- [ ] Telegram reference E2E
- [ ] Final subagent review
