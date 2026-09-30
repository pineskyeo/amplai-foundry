# Research: Searching A Combinatorial Harness Space Continuously (2026-09-30)

Web research by a subagent. (요약만) = abstract or search summary only; 미확인 = not verified.

## Harness Search Papers

| Work | How it picks what to evaluate | Scale | Source |
|---|---|---|---|
| Meta-Harness | proposer reads all past code, scores, traces; results kept on disk (no re-evaluation); test only on the final Pareto frontier | ~60 harnesses / 20 iterations | https://arxiv.org/html/2603.28052 |
| AHE | each edit declares expected fixes and regression risk; checked next round; wrong → file rollback | 10 iterations, k≥2 | https://arxiv.org/html/2604.25850 |
| HarnessCompass | structural and guidance tracks in disjoint spaces, merged | 5 iterations; 50 evolve / 450 held-out | https://arxiv.org/html/2608.01918 |
| AutoSaddler | improved patches re-checked on dev; history in a DAG, recombination | best in 147 traces (Meta-Harness 1,400) | https://arxiv.org/html/2608.23041 |
| HarnessOpt-Bench | validation aggregate only; test hidden; differences under the noise band are "unresolved"; validation optimistic | 100 calls per partition | https://arxiv.org/html/2608.06301 |
| Rethinking | same budget: evolution −0.8 pp vs parallel sampling +4.1 pp; held-out gain +0.6 pp | — | https://arxiv.org/html/2607.12227 |
| HarnessDev | gains depend on the executor model; limited transfer | — | https://arxiv.org/abs/2609.01437 (요약만) |
| HARBOR | constrained noisy Bayesian optimization; block-additive surrogate (main effects + pairwise); task-subset fidelity 8/22/44/89; ~40 flags → ~5 matter; chance constraint blocks regressions; 2 flags matched manual best 19.1%, all 8 on gave 13.5% | ~3.5 full suites | https://arxiv.org/html/2604.20938 |
| HarnessEvo | strong sub-additivity; find high-credit components first | — | https://arxiv.org/abs/2609.02889 (요약만) |
| Harness design study | 176 matched settings; planning effect flips by model | — | https://arxiv.org/abs/2609.20804 (요약만) |

No paper found runs harness search continuously (nightly); all use fixed iterations (미확인 beyond that).

## Search Under Expensive Evaluations

- Successive halving / Hyperband: drop the weaker half; order-of-magnitude faster than BO
  (http://proceedings.mlr.press/v51/jamieson16.pdf, https://arxiv.org/abs/1603.06560).
- HbBoPs: number of validation instances as fidelity, Hyperband + GP (https://arxiv.org/abs/2412.07820).
- TRIPLE: prompt selection as fixed-budget best-arm identification (https://arxiv.org/abs/2402.09723).
- GittinsEval: Bayesian bandit with task-difficulty prior; near-zero regret at 1–2% of exhaustive cost
  (https://arxiv.org/html/2609.25645).
- Plackett–Burman: 11 factors in 12 runs, main effects confounded with 2-factor interactions
  (https://www.itl.nist.gov/div898/handbook/pri/section3/pri335.htm).
- DSPy MIPROv2: minibatch BO with periodic full validation
  (https://github.com/stanfordnlp/dspy/blob/main/docs/docs/api/optimizers/MIPROv2.md).
- Cheaper model as low fidelity for agent tuning: not found (미확인); HarnessDev's limited transfer argues against it.

## Continuous Evaluation Practice

- Anthropic: repeated isolated trials; saturated capability evals graduate to a regression suite
  (https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents).
- METR: 6 runs per task; versioned suites (https://metr.org/time-horizons/).
- Drift: same-named GPT-4 moved 84% → 51% in 3 months (https://arxiv.org/abs/2307.09009); Claude model IDs are
  pinned snapshots (https://platform.claude.com/docs/en/models/overview).
- Reusable holdout (Thresholdout): https://arxiv.org/abs/1506.02629, https://arxiv.org/abs/1411.2664.
- Paired differences, clustered SE, power: https://arxiv.org/abs/2411.00640.
- Valid sequential testing: always-valid p-values (https://arxiv.org/abs/1512.04922), e-processes and confidence
  sequences (https://arxiv.org/abs/2210.01948).

## Surrogates

IRT with task features and scaffold decomposition (https://arxiv.org/abs/2604.00594); difficulty without rollouts
(https://arxiv.org/abs/2608.05797); AgentSquare LLM surrogate (https://arxiv.org/abs/2410.06153); Agentic Predictor
(https://arxiv.org/abs/2505.19764).
