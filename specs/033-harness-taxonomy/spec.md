# Work 033 — The V3 Meta-Harness System

**Work ID**: 033-meta-harness-system · **Created**: 2026-09-30 · **Type**: architecture · **Risk**: high
**Status**: design, awaiting operator decisions (OD-9 … OD-13)

Inputs in this folder: `components-draft.md` (operator requirements A–F), `research-meta-harness.md`,
`research-benchmark-domains.md`, `research-dashboards.md`. Normative design:
`design-reference/design/16_META_HARNESS.md`.

## Why

Work 030 made the meta-harness run on real runs (D-089), with one real evolution that claimed
non-inferiority only (D-093). What it can do today, checked in code:

| Area | Today | Evidence |
|---|---|---|
| Variable surface | Only the IMPLEMENTER role lines (`prompt_bundle_ref`). `propose` builds nothing else | `src/amplai_foundry/runtime/execution/meta_ops.py:61-139`, `runtime/execution/loop.py:602-616` |
| Other composition refs | context, verification and budget refs all point to one `policy` record that nothing reads | `runtime/execution/product.py:394-396` |
| Surface classes | By composition field: prompt/router/context = A; model/driver/sandbox/packs/budget/qualification = B; verification/protocol = C | `meta_harness/composition.py:12-23,57-80` |
| Routing | Selection reads the app's installed router policy (task class → driver order), not the composition's | `product.py:443-494` (`order` at 457-460) |
| Model and effort | One model per driver from `local.json`; ports bound at boot by driver-profile digest; reasoning effort is never passed (`reasoning_profile: "provider-default"` has no reader) | `runtime/local_deployment.py:116-138,266-325`, `agent_drivers/ports.py:84-111`, `runtime/execution/codex.py:316`, `agent_drivers/cli.py:119-151` |
| Corpus | 20 demo-app tasks (7 small, 7 medium, 6 large), one split; the Codex baseline passed 20 of 20, so success rate is at its ceiling | `specs/030-meta-harness-live/corpus/manifest.json`, `meta_ops.py:170-181`, D-093 |
| Splits | `CorpusService` supports development / validation / holdout, proposer ACL and holdout exposure limits, but the local path freezes every case into one split | `evaluation/corpus.py:19-264`, `meta_ops.py:168-181` |
| Analysis | Paired binary, conservative Wilson bounds; verdict is non-inferiority only. An improvement can be claimed only for cost. The unit is a task whose repeats all pass | `evaluation/analysis.py:28-101,112-268` |
| Experiments | One experiment per proposal (`max_attempts` 1), one repeat, sequential trials, at most 256 trials | `meta_ops.py:246`, `evaluation/service.py:148-153,250-253` |
| Trials | The planner is a deterministic stand-in, so task interpretation (A1) is never exercised; `safety_failures` and `unknown_effects` are written as 0 | `meta_harness/local_executor.py:35-73,140-150` |
| Proposer | Candidate text is a file the operator passes; no raw trace is kept (drivers keep digests, ids and usage only) | `runtime/meta_cli.py:60-82`, `agent_drivers/codex_app_server.py:299` |
| Operator view | `amplai meta …` prints JSON; no dashboard | `runtime/meta_cli.py` |
| Legacy | `meta_harness/reference.py` (arithmetic stand-in, usage 0) and `pipeline_reference.py` (deterministic recipe) with `ops meta-demo` / `ops evolution-demo` | `reference.py:220-229,411-435`, `pipeline_reference.py:1-7`, `runtime/cli.py:340,354` |

What the research says a meta-harness needs (`research-meta-harness.md`): components as
separately switchable parts (AHE: memory +5.6 pp, tools +3.3, middleware +2.2, prompt −2.3), effects
that do not add (+11.1 pp summed vs +7.3 pp together), search / validation / held-out splits
(evolution score and held-out agreed in 34 of 64 cases), a budget-matched baseline, a proposer that
reads raw traces (50.0% vs 34.6% with scores only) and states a prediction for every edit.

What the operator asked for (2026-09-30): harness parts added or removed by measured effect, not
only removed; performance and the best harness per model × reasoning effort, shown in a UI; tasks
split by domain like public benchmarks; verification speed as part of the harness; legacy removed
along the way; the final result as an easy HTML page; implementation by a workflow of Opus 5.5 and
Sonnet 5.5 agents with individually assigned reasoning effort.

## Goal

The operator runs the V3 meta-harness end to end on real executions:

1. A harness is a **cell** (driver, model, reasoning effort) plus a **manifest** of versioned
   **components** that switch on, off or to another version. The baseline manifest reproduces
   today's execution prompt byte for byte.
2. Each cell is **calibrated** on a measured corpus (domains × measured difficulty; development,
   validation and holdout splits; a leak gate).
3. A **proposer** reads development-split traces and scores, and drafts component edits, each with a
   prediction of which tasks improve or regress.
4. Candidates go through **screening → focused paired comparison → ablation → holdout → canary →
   promote**, against the cell baseline and a **budget-matched best-of-n baseline**, under a
   pre-declared decision rule that can claim improvement, non-inferiority with a benefit, regression
   or inconclusive.
5. A read-only **HTML dashboard** shows the model × effort matrix, the best composition per cell,
   ablation contributions, candidate lineage and pending approvals.
6. Legacy paths that the new system replaces are removed, and the final result is an HTML report.
7. **Strategy is chosen per task, and the chooser is itself evaluated** (operator, 2026-09-30):
   a router policy maps observable task features (task class, domain, planned size, apps) to a
   strategy (single agent, workgraph decomposition, parallel read-only steps) and a cell. The router
   policy is a class B component: a changed policy is a candidate that goes through the same stages,
   and the dashboard shows per-strategy results.

## Terms

| Term | Meaning |
|---|---|
| Cell | One `model-profile` (3.0.0): `driver_profile_ref`, `provider_model_id`, `reasoning_profile` (the effort, e.g. `high`) |
| Component | A named, versioned, content-addressed harness part with a declared surface class (design 16 §3) |
| Manifest | The component versions a composition uses, carried in the records its existing refs point to |
| Stage | calibration, screening, focused, ablation, holdout, canary |
| Informative task | A task whose calibrated baseline pass rate in the cell is between 0.2 and 0.9 |

## Operator Decisions

Decided in conversation (2026-09-30):

| # | Decision |
|---|---|
| OD-1 | Components are fine-grained and added or removed by measured effect ("넣을때는 넣고 뺄땐 빼는") |
| OD-2 | The best harness is found per model × reasoning effort, and the UI shows that matrix |
| OD-3 | Tasks are split by domain like public benchmarks; easy tasks stay only for regression and cheap-routing checks |
| OD-4 | Verification speed is part of the harness (components-draft F) |
| OD-5 | Implementation runs as a workflow: Opus 5.5 and Sonnet 5.5 agents with assigned effort |
| OD-6 | Legacy the new system replaces is removed along the way |
| OD-7 | The final result is an easy HTML page |
| OD-8 | PRs that pass CI are merged by the agent |

Open (asked 2026-09-30; recommendation in bold):

| # | Question | Options |
|---|---|---|
| OD-9 | What "develop with AMPLAI V3" means | (a) workflow agents write code; V3 runs all experiments; (b) every slice is an `amplai work` goal run by V3's drivers; **(c) (a) plus a few small slices dogfooded through `amplai work`, their runs recorded as observations** |
| OD-10 | Legacy scope | **(1) the meta-harness path only**; (2) every superseded V3 path; (3) also the V1/V2 loop (`.ai-team`, loop kit, skills) |
| OD-11 | Real-run budget for this Work | S ≈ 355 trials (2 cells, ~6 h); **M ≈ 710 trials (4 cells, ~12 h)**; L ≈ 1,065 trials (6 cells, ~18 h) — plan §4 |
| OD-12 | Cells in the first round | **codex gpt-5.6-sol {medium, high} and claude {claude-sonnet-5, claude-opus-5-5} at high**, each cell qualified first; opencode stays a routing candidate without effort variants until its variant control is measured |
| OD-14 | Strategy selection and experiment parallelism | **Add an `execution_strategy` component (single / workgraph split / parallel read-only steps) chosen per task by the router policy, which is evaluated like any candidate; write concurrency stays 1 per repo (design 07 §4); run experiment trials concurrently (separate containers and workspaces)** |
| OD-13 | Trace capture for the proposer | **Sanitized transcripts of meta-harness trials on the synthetic corpus only (no reasoning items, secret-scanned, proposer-readable, never exported)**; or no capture (proposer gets scores and verdict details only) |

## Proposed Decisions (recorded after approval)

- **D-096 Components and manifests.** Components are store records with id, kind, version, class
  and content digest. A composition's manifest lives in the records its existing refs point to:
  `prompt_bundle_ref` (role lines, unchanged), `context_policy_ref` (a `context-policy` record:
  environment bootstrap, memory notes, feedback form), `budget_policy_ref` (a `budget-policy` record:
  attempt policy). `verification_policy_ref` stays the protected record. The surface class of a
  candidate is the highest class among its changed components, following design 16 §3 (task prompt
  excerpts and approved notes are A; repair heuristic, context retrieval, driver mapping and
  routing are B). No 3.0.0 schema changes.
- **D-097 Cells.** Operator-registered cells; effort reaches the drivers (Codex
  `-c model_reasoning_effort=<e>`, Claude Code `--effort <e>`). A driver qualification binds image,
  driver version and model; effort variants of a qualified model share its report.
- **D-098 Corpus v2.** Domain-tagged tasks with development / validation / holdout splits frozen
  through `CorpusService`; difficulty is measured per cell by calibration, not declared; tasks every
  cell always passes move to a regression set; a leak gate rejects component content that names
  task ids, hidden test names or reference-only identifiers.
- **D-099 Analysis and protocol.** A superiority endpoint for success rate, a decision class
  (improvement, efficiency, non-inferior, regression, tradeoff, inconclusive), pass^k through
  repeats, a budget-matched best-of-n baseline arm, and stage-specific sampling plans. This is an
  evaluator change: it lands and is qualified first (stored experiments keep their verdicts), never
  together with a candidate experiment (design 16 §3).
- **D-100 Proposer traces** (per OD-13).
- **D-101 Legacy removal** (per OD-10).

## Constraints (verified)

- 3.0.0 wire schemas and `design-reference/` stay unchanged. The copies under `contracts/`,
  `src/amplai_foundry/runtime/contracts/data` and `design-reference/contracts` are identical.
- Class C (authority, verifier core, budget enforcement, signing, holdout) is not a candidate
  surface (design 16 §3); this Work changes the meta-harness itself as governed engineering work.
- An evaluator change is qualified alone before any candidate experiment uses it (design 16 §3,
  line 27; D-088 precedent).
- A proposer cannot review, approve, execute or promote (`meta_harness/service.py:101-109`).
- Changing an installed composition's refs changes its digest: plans awaiting approval see
  `COMPOSITION_CHANGED` once (`product.py` approve).
- Subscription drivers: Codex cost is unknown (D-085), Claude usage is estimated (D-084); API
  equivalents are descriptive only (D-094). Budgets count runs and tokens.
- Driver credentials stay operator-copied, scoped files (D-091); agents never copy credentials.
- A reasoning level a model does not support is refused, not silently replaced (Claude Code:
  "Available levels depend on the model", https://code.claude.com/docs/en/cli-reference; Codex:
  `model_reasoning_effort`, https://learn.chatgpt.com/docs/config-file/config-reference).

## Non-goals

- Judge-graded domains (UI quality, visual fidelity, documents). The judge qualification protocol is
  a later Work: without a judge-graded domain there is no reference to qualify a judge against.
- Computer use, open web search and simulated-user domains (deferred with the operator,
  2026-09-30).
- Online bandit or reinforcement-learning routing (design 16 §2).
- Switching the running server's image or profile.

## Acceptance

| ID | Statement | Evidence |
|---|---|---|
| AC-01 | Components are versioned records with a class; a manifest maps onto the existing composition refs; the baseline manifest renders today's prompt byte for byte | unit tests; golden prompt test |
| AC-02 | Environment bootstrap, memory notes, feedback form and attempt policy change the execution prompt and loop as declared, each switchable on and off | tests per component |
| AC-03 | Cells: the operator registers a cell; Codex and Claude receive the effort; an unsupported effort is refused; qualification binding per D-097 | argv-capture tests; one qualified real cell per driver |
| AC-04 | Corpus v2: ≥ 40 tasks over ≥ 5 domains, every task fair (reference passes hidden tests, base fails), three splits frozen, leak gate active | `corpus_check`, tests |
| AC-05 | Calibration measures per-cell pass rates with repeats and marks informative, saturated and flaky tasks | calibration records |
| AC-06 | The evaluator change (D-099) is qualified: every stored experiment re-analyzes to its recorded verdict; the new endpoints have tests including negative controls | tests |
| AC-07 | The proposer drafts component edits with predictions from development data only; the leak gate refuses a planted leak; prediction precision is recorded after evaluation | tests; real proposal |
| AC-08 | `amplai meta search` runs the stages for one cell under a declared budget and stops at every human gate | tests; real run |
| AC-09 | The dashboard is static HTML with no script and a CSP, built from stored records, showing the matrix, best composition per cell, ablation, lineage and pending approvals | tests; real dashboard |
| AC-10 | Legacy in the approved scope is removed; no reference remains; migrated tests pass | grep; tests |
| AC-11 | Real round within the approved budget: calibration of the approved cells, at least one proposer candidate through screening → focused → holdout, then canary and promote or a correct reject; the dashboard shows it | run records; dashboard |
| AC-12 | Verifier `--profile v2` PASS; docs RESOLVED; decisions recorded; final HTML report | logs; report |
