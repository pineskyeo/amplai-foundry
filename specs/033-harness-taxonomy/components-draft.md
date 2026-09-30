# Harness Components — Draft (2026-09-30)

Draft for the next design (not a decision). Sources for measured effects:
`research-meta-harness.md` in this folder.

AMPLAI has two harness layers. The outer harness (contract, execution, verification, repair) is
AMPLAI's own and fully controllable. The inner harness (the model loop and tools inside Codex CLI,
Claude Code, OpenCode) is the vendor's; AMPLAI adjusts it only through the CLI's options and files.
Research harness taxonomies (R16: 7 subsystems; AHE: 7 components) mostly describe the inner layer.

## A. Outer Harness (AMPLAI controls)

| # | Area | Decides | Options | Current state |
|---|---|---|---|---|
| A1 | Specification | goal to contract (objective, scope, acceptance) | planner model, contract form, ask-back | planner; not variable |
| A2 | Decomposition | one step, several steps or apps | single node, node per app, staged | node per app only |
| A3 | Context assembly | what the agent is given first | role text, relevant files and docs, past decisions, prior failures, environment summary | only the role text is variable (`prompt_bundle_ref`) |
| A4 | Execution loop | what happens after a failure | attempts, resume vs fresh session, feedback form (full log, summary, failing tests) | fixed |
| A5 | Verification | what decides success | tests, lint, format, visual comparison, reviewer | fixed; the core is protected |
| A6 | Routing | which driver, model, reasoning effort | per task type | router order only, not per task type |
| A7 | Budgets and stopping | when to stop | wall time, tokens, attempts | in the contract; enforcement protected |
| A8 | Environment | where the agent runs | preinstalled tools and deps, egress allowlist | app image |
| A9 | Multi-attempt selection | several attempts, pick one | best-of-n, vote | none |
| A10 | Reviewer | another agent reviews and asks for fixes | on/off, other model | none |
| A11 | Memory | lessons from past runs to the next | failure summaries, per-task-type tips | none (knowledge store exists, not wired to execution) |

## B. Inner Harness (CLI options)

| # | Area | Means of adjustment | Measured effect in research |
|---|---|---|---|
| B1 | System prompt | vendor default fixed; AGENTS.md / CLAUDE.md, Claude system-prompt append | AHE prompt only -2.3 pp |
| B2 | Tools | allowed tools (Claude `--allowedTools`), MCP servers, skills | AHE tools +3.3 pp |
| B3 | Inner loop | max turns (Claude `--max-turns`), compaction | AHE middleware +2.2 pp |
| B4 | Model and reasoning effort | `--model`; Codex reasoning effort via config (확인 필요) | not measured separately |
| B5 | Long-term memory | notes file in the workspace, skills | AHE memory +5.6 pp (largest) |
| B6 | Permissions and sandbox | Codex: the container is the sandbox (D-073); Claude allowed tools; OpenCode permission rules | safety, effect 미확인 |

## C. Protected (not varied)

Verifier core, hidden tests, authority, budget enforcement, signing (design 16 §3 class C).

## Observations

- The only variable part today is A3's role text, the weakest component in the research.
- The components with the largest measured effects (memory, tools, loop control, context
  assembly) are absent or fixed here.
- Effects do not add (AHE: single-component sum +11.1 pp, all together +7.3 pp), so each component
  needs on/off/version switches and combinations chosen by experiment.

## D. Judge Candidate: Jev (TypeSafe AI, 2026-09-15)

A "System One" model: takes a state and typed questions (yes/no probability, choice, score) and
returns typed answers with probabilities in one pass, without generating text. LangChain's test
(https://www.langchain.com/blog/jev-agent-evals-langsmith): 100% agreement with a human oracle on
500 binary decisions (Claude 80.0%), repeat-scoring variance 92x lower than Claude, $0.00035 per
call, 0.44 s. The authors call the result observational and say human review stays necessary.

Possible places (proposals, unverified here): A6 routing (cheap task typing and model choice), A4
loop control (retry / stop / hand to a human), and an auxiliary judge for what tests cannot decide
(scope adherence, review or doc quality, UI/UX). It must not replace deterministic verification
(hidden tests are protected, class C); changing a judge is its own baseline qualification
(design 16 §3). Whether it accepts images is 미확인.

## E. Operator UI Requirement (operator, 2026-09-30)

The operator wants a harness UI that shows performance per model x reasoning effort, and for each
(model, effort) the best harness composition (which components on, which versions), with its
success rate and cost. Today there is no UI: only `amplai meta ...` commands and JSON
(`src/amplai_foundry/runtime/meta_cli.py`); the local HTTP API has goal routes only
(`local_deployment.py:476`). Implication for the experiment design: the search is per
(model, effort) cell, not one global best harness (research: effects depend on the executor model,
https://arxiv.org/html/2609.01437v1).

## F. Verification Speed As A Component (operator, 2026-09-30)

Prompted by this repository's CI: 3,461 tests run serially, pytest alone 14-15 min per Python
version (main run 36687084112). The operator wants verification speed inside the next
meta-harness.

- A5 options: full suite every attempt; affected tests first and the full suite last (staged);
  parallel execution. The repair loop multiplies this time on every retry.
- Metric: verification wall time, recorded next to success rate and token cost.
- Guard: which tests run when may vary; the final verdict is always the full verification. Dropping
  tests to go faster changes the verifier core (class C, design 16 §3).
- It also sets the meta-loop's own experiment budget (design 16 §9): faster verification buys more
  trials for the same budget.
