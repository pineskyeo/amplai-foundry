# Research: Harness And Eval Management UIs (2026-09-30)

Web research by a subagent. 미확인 = not verified.

| Tool | Views | Actions | Source |
|---|---|---|---|
| LangSmith | experiment compare with a baseline, regression red / improvement green with counts per column, compact/full/diff tables, side-by-side traces, chart axes from metadata; Align Evals compares human and LLM-judge scores | none found | https://docs.langchain.com/langsmith/compare-experiment-results |
| Braintrust | pinned baseline, diff mode, sort by regression, summary of score, cost, latency, error rate; per-experiment Improvement/Regression/Tradeoff/Tie; pairwise human preference | none found | https://www.braintrust.dev/docs/evaluate/compare-experiments |
| W&B Weave | baseline column, radar chart, per-metric bars, row-level results | none found | https://docs.wandb.ai/weave/guides/evaluation/compare_evals |
| Arize Phoenix | experiment compare with eval, cost and tokens side by side; trace links; ELv2, self-host | none found | https://arize.com/docs/phoenix/release-notes/08-2025/08-15-2025-enhance-experiment-comparison-views |
| Langfuse | baseline vs candidate side by side (score, cost, latency), regression, annotation queue, LLM-judge setup in the UI; self-host (Docker) | promote/rollback via the `production` label on prompt versions | https://langfuse.com/docs/evaluation/experiments/compare-experiments, https://langfuse.com/docs/prompt-management/features/prompt-version-control |
| OpenAI Evals | trace grading, datasets, graders, prompt optimizer; deprecated, read-only 2026-10-31, shut down 2026-11-30 | — | https://developers.openai.com/api/docs/deprecations |
| Harbor Hub | dataset explorer, jobs, trial viewer, trajectories, custom leaderboards, run comparison | — | https://hub.harborframework.com/ |
| HELM / Epoch Hub / Terminal-Bench leaderboard | read-only leaderboards, per-item logs, 95% CI | — | https://crfm-helm.readthedocs.io/en/latest/, https://epoch.ai/blog/benchmarking-hub-update, https://www.tbench.ai/leaderboard/terminal-bench/2.0 |

Meta-harness papers (Meta-Harness, AHE, HarnessCompass) ship no UI: they manage candidates with the
filesystem and git (https://github.com/stanford-iris-lab/meta-harness,
https://github.com/china-qijizhifeng/agentic-harness-engineering).

## Pattern

- Standard: experiment list, compare with a baseline, regression/improvement colouring, row diff,
  trace drill-down, LLM-judge configuration.
- Partial: cost/token/latency in the compare view (Braintrust, Phoenix, Langfuse).
- Rare: promote/rollback actions (Langfuse prompt labels only).
- Not found: candidate lineage trees; a matrix of best harness per model x reasoning effort
  (미확인 — no tool documented it).
