# Work 030 Plan

Slices run in order. Each slice is a PR with its own tests, verifier v2 and docs cycle.

| Slice | What | Main files | Evidence |
|---|---|---|---|
| S0 (OD-4) | Analysis plan field `cost_basis: not_compared`. When set, unknown cost and non-measured usage are not inconclusive reasons. Evolution budget counts runs when cost is not compared. Qualified alone, before any candidate | `evaluation/analysis.py`, `evaluation/service.py`, `meta_harness/budget.py` | unit tests: same trials with and without the field; the default is unchanged |
| S1 | Prompt bundle: a versioned `prompt-bundle` record. The baseline is today's IMPLEMENTER text, byte-for-byte. The composition's `prompt_bundle_ref` points at it, and `ExecutionLoop.prompt` reads the role text from the goal's fixed composition; a legacy `policy` ref falls back to the built-in text | `runtime/execution/product.py`, `loop.py`, prompt bundle file | tests: the baseline prompt is identical; a candidate bundle changes only the role text |
| S2 | Release pointer: bootstrap a signed baseline release and the `release-pointer/active`. `select_composition` follows the pointer's components, and rollback moves it back | `runtime/local_deployment.py`, `product.py` | tests: promote/rollback change new goals only |
| S3 | Local meta wiring: MetaHarness + EvaluationService in the local product, a meta-proposer identity, operator approval_check, and `reject` | `local_deployment.py`, `meta_harness/service.py` | tests: self-approval refused; the lifecycle through reject |
| S4 | Offline executor: demo-app corpus of 20 tasks (fixed contracts, no planner), running each trial as a real goal through the loop. Receipts are admitted per the executor interface (`evaluation/service.py` freeze/run/recover_interrupted) | new `meta_harness/local_executor.py`, corpus data | real: a 2-task smoke, then the 40-run experiment |
| S5 | Canary routing: a canary assignment stored in the plan record; `select_composition` and approve agree; 3 goals; baseline fallback | `product.py`, `loop.py` | real canary |
| S6 | Operator surface: `amplai meta …` commands (propose via V3 design goal, screen, approve, evaluate, canary, promote, rollback, kill) | `cli.py`, local routes | CLI tests |
| S7 | Real evolution: V3 drafts a candidate from observations → experiment → canary → promote → rollback drill; plus a no-effect candidate ending inconclusive/fail | runs recorded in `specs/030-meta-harness-live/runs/` | design/16 §10 |

Decisions to record: D-088 (OD-4 evaluator cost basis), D-089 (live meta-harness wiring and roles, OD-1..OD-6).
