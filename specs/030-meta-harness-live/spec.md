# Work 030 — The Meta-Harness On Real Runs

**Work ID**: AMPLAI-V3-META01 · **Created**: 2026-09-29 · **Type**: new_feature (architecture)

## Why

The V3 completion criterion for the Meta-Harness asks for one real, safe improvement candidate that
goes through the V3 execution pipeline, not a fake simulator. It must produce an immutable
experiment, a baseline comparison, a guarded canary, an explicit promote and a rollback drill
evidence, and reject/inconclusive must work when there is no effect
(`design-reference/design/16_META_HARNESS.md:76-78`).

The V3 FINAL drill compared "a deterministic recipe algorithm, not an LLM", with no provider
credentials (`src/amplai_foundry/meta_harness/pipeline_reference.py:1-6`). The local product uses
only `CompositionService` for driver selection (`src/amplai_foundry/runtime/execution/product.py`,
`select_composition`). The evolution lifecycle exists (`src/amplai_foundry/meta_harness/service.py`:
submit … kill_switch) but no real Codex/Claude run has ever gone through it.

## Goal

On this Mac, V3 improves its own harness on real runs:

1. V3 (a meta-proposer service identity) drafts a class-A change to the IMPLEMENTER prompt from
   observations (`design-reference/design/16_META_HARNESS.md:20-22`).
2. The operator screens it and approves an offline experiment. Baseline and candidate prompts then
   run on a fixed corpus of demo-app tasks with real drivers and deterministic verifiers, and the
   pre-registered analysis judges the result.
3. On pass, the operator approves a canary of 3 real low-risk goals routed to the candidate, then
   promotes. A rollback drill returns the active pointer to the baseline.
4. A candidate without effect ends `inconclusive` or `fail`, never promoted.

## Operator Decisions (2026-09-29)

| # | Decision | Choice |
|---|---|---|
| OD-1 | First class-A surface | The IMPLEMENTER prompt (`ExecutionLoop.prompt`) |
| OD-2 | Eval corpus | Tasks in the demo app (github pineskyeo/amplai-demo-app) with deterministic verifiers |
| OD-3 | Budget | Offline experiment 40 runs; canary 3 goals |
| OD-4 | Cost basis | Success rate only. Subscription accounts give no known cost (D-085; Claude is `estimated`, D-084), so the analysis plan declares cost not compared and the budget counts runs |
| OD-5 | Statistics | 20 tasks × 1 repeat per arm, non-inferiority margin 0.25, confidence 0.95, purpose `confirmatory` |
| OD-6 | Roles | V3's meta-proposer identity proposes; the human operator screens, approves the experiment and canary, and promotes (proposer ≠ reviewer, `src/amplai_foundry/meta_harness/service.py:101-109`) |

## Constraints (verified in code)

- An analysis marks the verdict `inconclusive` when any trial has unknown cost or non-`measured`
  usage (`src/amplai_foundry/evaluation/analysis.py:170-173,190-191`). OD-4 changes the evaluator,
  so it lands and is qualified before any candidate experiment, never together with one
  (`design-reference/design/16_META_HARNESS.md:27`).
- Promotion needs purpose `confirmatory` outside the demo tenant
  (`src/amplai_foundry/meta_harness/service.py:419-425`).
- A proposer, or any actor holding `harness.propose`, cannot review, approve, execute or promote
  (`service.py:101-109`).
- Changing a composition's prompt bundle changes its digest and revision, so plans awaiting
  approval get `COMPOSITION_CHANGED` once (`product.py` `approve`).
- 3.0.0 wire schemas stay unchanged (standing constraint).

## Non-goals

- Class B/C surfaces (routing thresholds, verifier core, budget enforcement, authority).
- Online bandit or automatic promotion. A human promotes.
- OpenCode as a driver (PR #28 qualifies it; wiring is separate).
- Cost-based comparison (OD-4).

## Acceptance

- AC-1: a promoted and a rolled-back release pointer change which prompt new goals get, and
  running goals keep their composition.
- AC-2: an offline experiment of 20 demo tasks × 2 arms runs through real drivers with
  deterministic verifiers and immutable trial receipts, and the frozen analysis gives
  pass/fail/inconclusive.
- AC-3: a canary of 3 real goals runs on the candidate with the baseline as fallback, within the
  budget.
- AC-4: one real candidate reaches promote followed by a rollback drill, and a no-effect or worse
  candidate ends inconclusive/fail (the design/16 §10 evidence).
- AC-5: every gate is the operator's explicit command, and self-approval is refused.
