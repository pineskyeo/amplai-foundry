# Work 030 Runs

Real trials of the offline executor (S4). Each file is what `scripts/meta_smoke.py` wrote.

| File | Driver | Result |
|---|---|---|
| `smoke-1.json` | codex-cli | 4 of 4 trials `held` (`DRIVER_BOUNDARY`, about 5 s each). The Codex session started and the provider answered `unauthorized`: the refresh token of the operator's scoped credential copy was already used. No verdict about any candidate; the executor recorded `success=null`. |
| `smoke-claude-1.json` | claude-cli | 4 of 4 trials `verified` and the hidden tests passed (about 25 s each, usage `estimated`). |
| `smoke-codex-2.json` | codex-cli | 4 of 4 `held`, `unauthorized`: the operator had signed in again, which revoked the old token, but the scoped copy was not yet refreshed (its file time was still Sep 28). |
| `calibration-codex-1.json` | codex-cli | All 20 corpus tasks on the baseline composition, one run each: 20 of 20 `verified` with the hidden tests passed (34 to 86 s per trial, 17 minutes in all, 2.03M input tokens, usage `measured`). |
| `smoke-codex-3.json` | codex-cli | 4 of 4 `verified` and the hidden tests passed (37 to 44 s each, usage `measured`, about 87k to 101k input tokens and 1.0k to 1.3k output tokens per trial). |

Each smoke runs two tasks (`s01-semver-parse`, `s05-truncate`), each on the installed baseline
composition and on a control candidate whose prompt text is identical under another bundle id.
Both arms are the same text, so a smoke proves the plumbing (fixed contract, pinned composition,
pinned base commit, hidden judge, receipts), not any prompt effect.

The Codex failures were an operator action, not a code fault: sign in again and create a new scoped
copy with `~/.amplai/server/amplai-foundry/scripts/sandbox_up.sh --codex-home <dir>` (the agent
never copies credentials). The smoke deployment is separate from the operator's product
(`~/.amplai/local-smoke`, its own keys and store), so these trials are not among the real goals.

What the calibration says about the corpus: the baseline passes every task (20 of 20, small,
medium and large alike). The success rate is at its ceiling, so this corpus can show that a
candidate does not make things worse (non-inferiority), but no candidate can show a higher success
rate. Any "improvement" would have to be on another axis (for example tokens or time per solved
task, both measured for Codex) or on harder tasks. One run per task is a single sample, not a
pass rate per task.

## S7: the first real evolution run (2026-09-30)

Run in the separate deployment `~/.amplai/local-smoke` with the real Codex driver, one `amplai meta`
command per gate. The operator's real product and its active release were not touched. The claim
is the one D-093 fixed: non-inferiority on the success rate, no improvement.

| Candidate | Gates it went through | Result |
|---|---|---|
| `shorter` (`candidates/shorter.txt`, 65 to 44 words) | propose, screen, approve-experiment, run-experiment | Verdict `pass`: 20 tasks, success difference 0, confidence interval -0.201 to +0.201 (margin 0.25), no missing trial, no safety failure. Mean time difference +0.1 s. 4.02M tokens. |
| `shorter`, canary | approve-canary, run-canary | Stopped with `CANARY_RECOVERY_REQUIRED` before any canary trial ran. The canary is owned by the process that starts it, and `approve-canary` started it while `run-canary` was another process. The candidate was ended with `abort` (active release unchanged). Fixed in `meta_ops.py`: `approve-canary` only approves and `run-canary` starts and runs in one process. |
| `negcontrol` (`candidates/negative-control.txt`, forbids any file change) | propose, screen, approve-experiment, run-experiment, reject | Verdict `fail`, reason `inferiority`: the candidate failed all 20 tasks the baseline passed (difference -1, interval -1.0 to -0.60). 2.29M tokens. Codex follows the role text, so the experiment does react to the prompt. |

Not done: a canary, a promotion and a rollback of a real candidate. Those need a passing
candidate to be proposed again (the passed experiment belonged to the aborted candidate) and are
covered so far only by the lifecycle rehearsal with a stand-in agent
(`tests/e2e/test_rc12_meta_rehearsal.py`).

The active release of the smoke deployment stayed `local-baseline-0df61fa84b06` throughout.

