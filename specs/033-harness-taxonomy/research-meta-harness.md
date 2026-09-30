# Research: Meta-Harness Methods And Harness Components (2026-09-30)

Web research by a subagent for the next design (harness components, experiment selection).
Every number carries its source URL. Items marked 미확인 could not be verified. The OpenAI
harness-engineering page returned 403; its numbers come from a Japanese translation
(https://note.com/npaka/n/nb4c5488e82fd), not checked against the original.

## Component Taxonomies

- R16 (Barbaste et al.) splits a coding harness into 7 subsystems: agent loop, LLM integration,
  tools and actions, memory and context, safety and permissions, orchestration, extensibility
  (https://arxiv.org/html/2609.00006v1).
- AHE (Lin et al.) keeps 7 components as files: system prompt, tool description, tool
  implementation, middleware, skill, sub-agent config, long-term memory
  (https://arxiv.org/html/2604.25850).

| Component | Measured effect |
|---|---|
| System/role prompt | AHE prompt only 67.4% vs seed 69.7% (-2.3 pp) (https://arxiv.org/html/2604.25850); OpenAI replaced one big AGENTS.md with a map-like table of contents (translation) |
| Tools (implementation and description) | AHE tools only 73.0% (+3.3 pp) (https://arxiv.org/html/2604.25850) |
| Context assembly / environment bootstrap | Meta-Harness found environment bootstrapping on TB2: Opus 4.6 76.4% vs 74.7%, Haiku 4.5 37.6% vs 33.7% (https://arxiv.org/html/2603.28052) |
| Long-term memory | AHE memory only 75.3% (+5.6 pp, the largest single component) (https://arxiv.org/html/2604.25850) |
| Middleware / loop control | AHE middleware only 71.9% (+2.2 pp) (https://arxiv.org/html/2604.25850) |
| Verification / evaluator | Overhead inside the model's capability, lift only at its edge (https://www.anthropic.com/engineering/harness-design-long-running-apps); no numbers |
| Planning | Anthropic removed sprints with Opus 4.6; SICA: reasoning scaffolds hurt a reasoning model (https://arxiv.org/html/2504.15228) |
| Stopping / budgets | Hard-coded step limits broke when the executor model changed (https://arxiv.org/html/2609.01437v1) |

## Search Methods

| Method | Search space | Search | Cost |
|---|---|---|---|
| Meta-Harness | harness code files | proposer reads all past code, scores and raw traces | about 60 harnesses over 20 iterations (https://arxiv.org/html/2603.28052) |
| AHE | one file per component | every edit carries a prediction checked next round; file-level revert | 10 iterations, k>=2 rollouts per task, about 32 h (https://arxiv.org/html/2604.25850) |
| HarnessCompass | structural and guidance tracks | parallel tracks, merged | 5 turns (https://arxiv.org/html/2608.01918) |
| ADAS | agent as Python code | meta agent with archive | 25-30 iterations (https://arxiv.org/html/2408.08435) |
| AFlow | workflow graph of operators | MCTS | 20 rounds (https://arxiv.org/html/2410.10762) |
| GEPA | prompts | reflective mutation, Pareto frontier | 1,839-7,051 rollouts (https://arxiv.org/html/2507.19457) |
| DGM | agent's own code | open-ended archive, cascade 10->50->200 tasks | about 2 weeks per run (https://arxiv.org/html/2505.22954) |
| SICA | agent's own code | self-edit | 15 iterations about $7,000 (https://arxiv.org/html/2504.15228) |

## Experiment Selection

- Split search / validation / held-out. Rethinking: held-out gain only +0.6 points
  (https://arxiv.org/html/2607.12227v1). Evolution score and held-out moved together in 34/64
  cases (https://arxiv.org/html/2609.01437v1).
- Screening cascade: cheap evaluation first, then expensive (DGM, AlphaEvolve).
- Budget-matched baseline: at equal cost, harness evolution did not consistently beat parallel
  sampling (https://arxiv.org/html/2607.12227v1).
- Stratify tasks by domain and difficulty: Scaffold Effect used 8 domains (BUG, BUILD, DATA, IMPL,
  ML, PUZZLE, SEC, SYS) (https://arxiv.org/html/2607.22585v1); AHE easy 4 / medium 55 / hard 30.
- Leak gates: reject edits naming task ids, test names or private symbols
  (https://arxiv.org/html/2608.01918).
- The proposer should see raw traces: median 50.0% with traces vs 34.6% with scores only
  (https://arxiv.org/html/2603.28052).

## Adding Vs Removing

- Anthropic removes components one at a time; a big one-shot cut failed. "Every component encodes
  an assumption about what the model can't do" (Anthropic post above).
- Effects do not add: AHE single-component sum +11.1 pp, full combination +7.3 pp; on hard tasks
  memory alone 63.3% beat the full combination 53.3% (https://arxiv.org/html/2604.25850).
- Harnesses differ more in cost than in pass rate: 0-8 pp pass-rate spread, up to 40x tokens per
  solved task (https://arxiv.org/html/2607.22585v1).

## Power Limit (own calculation)

n=20, p=0.5: the pass-rate standard error is about 11 pp. Single-component effects of 2-6 pp
cannot be told apart with 40 runs; prefer large-effect components and pool held-out results.
