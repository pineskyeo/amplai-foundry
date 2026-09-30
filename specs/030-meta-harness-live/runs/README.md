# Work 030 Runs

Real trials of the offline executor (S4). Each file is what `scripts/meta_smoke.py` wrote.

| File | Driver | Result |
|---|---|---|
| `smoke-1.json` | codex-cli | 4 of 4 trials `held` (`DRIVER_BOUNDARY`, about 5 s each). The Codex session started and the provider answered `unauthorized`: the refresh token of the operator's scoped credential copy was already used. No verdict about any candidate; the executor recorded `success=null`. |
| `smoke-claude-1.json` | claude-cli | 4 of 4 trials `verified` and the hidden tests passed (about 25 s each). Two tasks, each on the baseline and on a control candidate whose prompt text is identical under another bundle id. |

The Codex failure is an operator action, not a code fault: sign in again and create a new scoped
copy (the agent never copies credentials). The smoke deployment is separate from the operator's
product (`~/.amplai/local-smoke`, its own keys and store), so these trials are not among the real
goals. Both arms in the Claude smoke are the same text: the run proves the plumbing, not any
prompt effect.
