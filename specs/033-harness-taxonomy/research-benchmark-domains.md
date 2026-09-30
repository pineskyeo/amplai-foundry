# Research: Benchmark Task Domains And Difficulty (2026-09-30)

Web research by a subagent. openai.com pages returned 403; numbers from secondary reports are
marked (2차). 미확인 = not verified; (추정) = own inference.

## Domains Real Benchmarks Cover

| Domain | Benchmarks | How it is judged |
|---|---|---|
| Small bug fix | SWE-bench Verified (500), Multilingual (300, 9 languages) | hidden unit tests |
| Multi-file feature | SWE-bench Pro (1,865; ref patch avg 107.4 LOC, 4.1 files), SWE-Lancer IC | tests + e2e |
| Greenfield / long build | Commit0 (54 libraries from spec), Web-Bench (50 projects x 20 dependent tasks, best 25.1%) | sequential tests |
| Frontend behaviour | FrontendBench (148), Web-Bench | browser e2e scripts |
| Visual fidelity / UI quality | Design2Code (484 screenshots to HTML), ArtifactsBench (1,825), WebDev Arena | screenshot similarity, MLLM checklist judge (94.4% agreement with WebDev Arena), human preference |
| Multimodal input (screenshot to patch) | SWE-bench Multimodal (617 JS tasks; best 12% at release) | tests |
| Terminal / ops | Terminal-Bench 2.0 (89), 3.0, 4.0 | environment-state tests, >=5 runs per pair |
| Computer / browser use | OSWorld / Verified (369), OSWorld 2.0 (108 workflows, median 1.6 h human), WebArena (812), VisualWebArena (910) | state scripts, checkpoint partial score |
| Tool use / MCP / user collaboration | tau2/tau3-bench, MCP-Universe, MCPMark (127) | final DB state, pass^k |
| Research / search | BrowseComp (1,266), Mind2Web 2 (130) | short answer, agent-judge rubric |
| Documents / data deliverables | GDPval (1,320 tasks, 44 occupations), CharXiv | expert pairwise; auto grader 66% vs human-human 71% |
| Ambiguity / asking back | no dedicated benchmark found; OSWorld 2.0 lists guessing instead of asking as a failure cause | rubric (추정) |
| Review / judgement | SWE-Lancer Manager | compared with the real manager's choice |

Sources: SWE-bench https://www.swebench.com, https://labs.scale.com/leaderboard/swe_bench_pro_public,
https://arxiv.org/abs/2502.12115, https://commit-0.github.io/, https://arxiv.org/abs/2505.07473,
https://arxiv.org/abs/2506.13832, https://salt-nlp.github.io/Design2Code/, https://arxiv.org/abs/2507.04952,
https://arena.ai/blog/webdev-arena, https://arxiv.org/abs/2410.03859, https://arxiv.org/abs/2601.11868,
https://xlang.ai/blog/osworld-verified, https://arxiv.org/abs/2606.29537, https://arxiv.org/abs/2307.13854,
https://jykoh.com/vwa, https://arxiv.org/abs/2506.07982, https://github.com/eval-sys/mcpmark,
https://arxiv.org/html/2504.12516, https://arxiv.org/abs/2506.21506, https://arxiv.org/html/2510.04374.

## How Difficulty Is Defined

- Human time: SWE-bench Verified (39% under 15 min, about 90% under 1 h), Terminal-Bench expert
  estimates, METR time horizon (50% success time; visual tasks 40-100x shorter horizon than
  software), OSWorld 2.0, GDPval (7-9 expert hours).
- Empirical model pass rate: Terminal-Bench re-labels Easy >= 66.7% frontier pass, Hard < 33.3%;
  human prediction correlated only r=0.436 (https://arxiv.org/abs/2601.11868).
- Filtered at creation: HLE, BrowseComp, Aider Polyglot (only problems 3 or fewer models solved),
  MMMU-Pro (drop text-only-solvable).

## Easy Tasks And Saturation

- Saturated tasks are retired: Terminal-Bench 4.0 removed tasks every current model solves 5/5
  (https://www.tbench.ai/news/terminal-bench-4-0); Aider replaced its set at 84.2%; Artificial
  Analysis moves saturated evals to legacy; OpenAI stopped reporting SWE-bench Verified
  (flawed tests, contamination) (2차) and later withdrew SWE-bench Pro public (2차).
- Easy suites are kept separately for quick checks: MCPMark keeps 10 easy tasks per service.
  (추정) For a product, the easy tier is for smoke/regression and cheap-routing checks, not ranking.
- Hard tasks get partial signal: checkpoint partial scores (OSWorld 2.0), pass^k (tau-bench).
