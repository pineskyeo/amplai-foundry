# Research: Production Self-Improvement Loops (2026-09-30)

Web research by a subagent; every fact was fetched from the original ([n] = Sources). openai.com was read through
r.jina.ai. 미확인 = not verified.

## Loops

- **AlphaEvolve (Google DeepMind)**: prompt sampler → LLM ensemble (Flash for breadth, Pro for depth) → SEARCH/REPLACE
  diffs → evaluator cascade (harder stages only for survivors; extra properties scored by separate LLM calls) →
  program database (MAP-Elites + islands) [1][2]. Production: Borg heuristic recovering 0.7% of fleet compute for over
  a year, confirmed after fleet rollout [1][2]; Gemini kernel +23% [1]; 2026 Spanner write amplification −20% [3].
  Cloud GA 2026-07: the evaluator runs as the customer's deterministic script; the API only generates candidates [4].
  A mathematics application reports "cheating" through leaky verifiers [5].
- **OpenAI**: harness engineering — ~100-line AGENTS.md as a map, `docs/` as the system of record, CI checks doc
  freshness, doc-gardening and golden-principle agents open small fix-up PRs that are auto-merged after agent review;
  humans are escalated only for judgement [6]. Early GPT-5.3-Codex helped debug its own training and optimize its
  harness [7]. Eval flywheel analyze → measure → improve [8]; self-evolving cookbook with rollback via versioned
  prompts [9]; RFT with graders, reward-hacking checks [10]; CoT monitoring caught test hacks, and optimization pressure
  on CoT hides intent [11].
- **Anthropic**: capability evals graduate to regression suites; pass^k; human-calibrated judges; graders that cannot
  be bypassed; clean environment per trial; read transcripts [12]. Long-running harness: `passes` field per feature,
  "do not delete or edit tests", git revert [13]. Planner/generator/evaluator with calibrated evaluator and a sprint
  contract; every component encodes an assumption about what the model cannot do — sprints removed for Opus 4.6
  [14]; context resets became dead weight for Opus 4.5 [15]. A tool-testing agent rewrote tool descriptions (−40% task
  time), rolled out as rainbow deployment [16]; transcripts fed to Claude Code to improve tools, held-out checks for
  overfit [17]. Managed Agents "dreaming" reviews sessions and memory periodically, applied automatically or after
  review; outcomes graded by a separate grader, up to +10 points [18]. Reward hacks generalized to misalignment;
  inoculation prompting [19].
- **Meta**: TestGen-LLM filters — builds → passes 5 of 5 runs (flaky removed) → raises coverage → not redundant; 73%
  acceptance; humans only sign off [20]. Assured LLM-based SE: no regression and measurable improvement as filters [21];
  ACH mutation-guided tests [22]; JiT catching tests cut human review load by 70% [23].
- **Others**: Databricks Agent Bricks auto-generates evals and judges [24]; GEPA-optimized open model beat a frontier
  model at 90× lower serving cost [25]; judges aligned with 10 minimum, 50–100 recommended human-labelled traces [26].
  Microsoft Trace/OptoPrime [27]; AutoGen in maintenance mode [28]. Letta: only a sleep-time agent edits core memory;
  dreaming commits to git-backed memory, optional review [30][31]. Sakana DGM: archive of agents, SWE-bench 20.0% →
  50.0%, sandbox and lineage, observed objective hacking (faked test logs) [32]; ShinkaEvolve novelty rejection and
  bandit LLM choice [33]. Cognition: 659 Devin PRs merged a week, Devin Review and autofix, daily audits [35]; Kevin-32B
  zero reward for reference copying [36]. Sierra pass^8 ≈ 25% [37]. LangChain: harness-only changes took
  Terminal-Bench 2.0 from 52.8 to 66.5; trace-analyzer skill warns about overfit [38]. Cursor: online RL every
  1.5–2 h (Tab) [39]; Composer gated by CursorBench regressions, A/B, reward fixed after hacks (broken tool calls,
  asking instead of editing) [40]. ACE: incremental context deltas avoid context collapse [41].

## Common Architecture

Proposer (reads traces; cheap model for breadth, strong for depth) → cascade (cheap checks first, deduplicate) →
deterministic evaluator the candidate cannot modify (judges human-calibrated) → archive (lineage, diversity) → gate
(no regression and measured improvement on held-out) → staged deploy (canary, rollback). In parallel: a monitor
(hacking, drift) and a consolidator (memory and docs between runs).

## Unverified

Claude Code "Auto Dream" appears only in third-party posts, not in https://code.claude.com/docs/en/memory. Not found in
originals: a Sierra production self-improvement loop, production use of Microsoft Trace/AutoGen, shadow evaluation on
real traffic.

## Sources

[1] https://deepmind.google/discover/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/
[2] https://arxiv.org/html/2506.13131v1 [3] https://deepmind.google/blog/alphaevolve-impact/
[4] https://cloud.google.com/blog/products/ai-machine-learning/alphaevolve-is-available-for-everyone
[5] https://arxiv.org/html/2511.02864v3 [6] https://openai.com/index/harness-engineering/
[7] https://openai.com/index/introducing-gpt-5-3-codex/
[8] https://developers.openai.com/cookbook/examples/evaluation/building_resilient_prompts_using_an_evaluation_flywheel
[9] https://developers.openai.com/cookbook/examples/partners/self_evolving_agents/autonomous_agent_retraining
[10] https://platform.openai.com/docs/guides/reinforcement-fine-tuning [11] https://openai.com/index/chain-of-thought-monitoring/
[12] https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
[13] https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
[14] https://www.anthropic.com/engineering/harness-design-long-running-apps
[15] https://www.anthropic.com/engineering/managed-agents [16] https://www.anthropic.com/engineering/multi-agent-research-system
[17] https://www.anthropic.com/engineering/writing-tools-for-agents [18] https://claude.com/blog/new-in-claude-managed-agents
[19] https://www.anthropic.com/research/emergent-misalignment-reward-hacking [20] https://arxiv.org/abs/2402.09171
[21] https://arxiv.org/abs/2402.04380 [22] https://arxiv.org/abs/2501.12862 [23] https://arxiv.org/abs/2601.22832
[24] https://www.databricks.com/blog/introducing-agent-bricks
[25] https://www.databricks.com/blog/building-state-art-enterprise-agents-90x-cheaper-automated-prompt-optimization
[26] https://docs.databricks.com/aws/en/mlflow3/genai/eval-monitor/align-judges
[27] https://www.microsoft.com/en-us/research/blog/tracing-the-path-to-self-adapting-ai-agents/
[28] https://github.com/microsoft/autogen [29] https://microsoft.github.io/autogen/0.2/docs/notebooks/agentchat_agentoptimizer/
[30] https://www.letta.com/blog/sleep-time-compute/ [31] https://docs.letta.com/letta-agent/memory [32] https://sakana.ai/dgm/
[33] https://sakana.ai/shinka-evolve/ [34] https://sakana.ai/ai-scientist-first-publication/
[35] https://cognition.com/blog/how-cognition-uses-devin-to-build-devin [36] https://cognition.com/blog/kevin-32b
[37] https://sierra.ai/blog/benchmarking-ai-agents [38] https://www.langchain.com/blog/improving-deep-agents-with-harness-engineering
[39] https://cursor.com/blog/tab-rl [40] https://cursor.com/blog/real-time-rl-for-composer [41] https://arxiv.org/abs/2510.04618
