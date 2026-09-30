# Work 033 Plan — The V3 Meta-Harness System

Spec: `spec.md`. Every "today" statement below is cited in `spec.md` §Why.

## 1. Architecture

```text
cells (driver, model, effort)  ×  manifests (component versions)  =  compositions
        │                                   │
        └── calibration ── corpus v2 (domains × measured difficulty, dev / validation / holdout)
                                            │
proposer (dev traces + scores) ─► candidate component edit + prediction ─► leak gate
                                            │
screening ─► focused (paired, repeats, best-of-n baseline) ─► ablation ─► holdout ─► canary ─► promote
                                            │
                          stored records ─► amplai meta dashboard (static HTML)
```

Everything runs through the existing gates (`meta_harness/service.py`), evaluation service
(`evaluation/service.py`), budget (`meta_harness/budget.py`) and release pointer
(`runtime/execution/releases.py`). No 3.0.0 schema changes: new structure lives in internal store
records that the existing composition, experiment and report refs point to.

### 1.1 Components (D-096)

A store record `harness-component`:

```json
{"component_id": "env-bootstrap", "kind": "env_bootstrap", "version": 2, "surface_class": "A",
 "content": {...}, "content_digest": "sha256:…", "parent": {"id": "…", "revision": 1, "digest": "…"},
 "source": "operator | proposer", "created_at": "…"}
```

| Kind | Carried by | Content (v1 = baseline) | Class (design 16 §3) | Research |
|---|---|---|---|---|
| `role_prompt` | `prompt_bundle_ref` (existing prompt bundle, unchanged) | IMPLEMENTER role lines | A (task prompt excerpt) | AHE prompt −2.3 pp |
| `env_bootstrap` | `context_policy_ref` → `context-policy` | off (v1); on: bounded repo tree, verifier commands, tool versions, the fastest relevant test command | A (fixed assembler, selected facts) | Meta-Harness environment bootstrapping |
| `memory_notes` | `context-policy` | off (v1); on: ≤ 40 curated lines per app / task class | A (approved notes) | AHE memory +5.6 pp |
| `feedback_form` | `context-policy` | today's form: failing acceptance + 3,000-char stdout/stderr tail (`loop.py:586-600`) | B (repair heuristic) | AHE middleware +2.2 pp |
| `attempt_policy` | `budget_policy_ref` → `budget-policy` | today's repair attempts, repair on the previous patch, best-of-n = 1 | B (repair heuristic; budget bound) | budget-matched baseline |
| `execution_strategy` | `budget-policy` | single node per app (v1, today's planner, `loop.py:3`); variants: workgraph split by file area, parallel read-only steps (≤ 4, design 07 §4); write concurrency 1 per repo | B (decomposition; routing) | AFlow graph search |
| `route_policy` | app router policy (`product.py:457-460`) | task features (task class, domain, planned size, apps) → (strategy, cell); v1 = today's order | B (routing threshold / driver mapping) | design 16 §2 |
| `driver_options` | `budget-policy` | none (v1); Claude `--max-turns`, `--append-system-prompt`; Codex config overrides from an allowlist | B (driver behaviour) | AHE tools +3.3 pp |

- A candidate's class is the highest class among changed components. Changing a cell (model or
  effort) stays class B through `model_profile_ref` (`composition.py:12-23`).
- `verification_policy_ref` keeps pointing at the protected record (class C).
- **Baseline guarantee.** The v1 manifest renders exactly today's execution prompt. A golden test
  compares bytes against the current `ExecutionLoop.prompt` output for the corpus tasks.
- File-level revert = pin the previous component version; every version stays in the store.
- Installed baseline compositions get revision + 1 when their context and budget refs move from the
  shared `policy` record to the new records. Plans awaiting approval see `COMPOSITION_CHANGED` once.

### 1.2 Cells (D-097)

- `local.json` gains `cells: [{"driver": "codex-cli", "model": "gpt-5.6-sol", "effort": "high"}, …]`.
  Each cell is one `model-profile` with `reasoning_profile` = effort, one driver port and one
  baseline composition per app, named `<app>-<driver>-<model>-<effort>`.
- Argv: Codex `exec … --model <m> -c model_reasoning_effort=<e>`; Claude Code `-p … --model <m>
  --effort <e>`. OpenCode: variant control is 확인 필요 (its docs describe config `variants`, not a
  per-prompt flag); until measured, OpenCode cells have no effort axis.
- Qualification: `measured_qualification` already binds image, driver version and model
  (`runtime/execution/codex.py` around line 203-224). Effort variants of a qualified model share its
  report; a model without a passing report is refused (`DRIVER_UNQUALIFIED`).
- The router policy's order lists cell ids per task class. A routing change is class B and goes
  through the gates, never online (design 16 §2).
- Prices: a new dated table is added only with official prices; otherwise `no_price` (D-094).
- Command: `amplai ops local-cell add|remove|list`.

### 1.3 Corpus v2 (D-098)

- Layout follows Work 030 (`meta_harness/local_corpus.py:3-10`): `base/` app plus
  `tasks/<id>/{task.json, hidden/, reference/}`; `task.json` gains `domain` and `split`.
- A richer base app so tasks can span several modules. Domains (all test-graded):
  `bug` (subtle defects, edge-case hidden tests), `feature` (multi-file), `refactor`
  (behaviour-preserving plus structure checks), `cli_ops` (scripts, packaging, config), `data`
  (parsing and formatting with property-style hidden tests), `ambiguity` (A1: the right outcome is a
  planner question; graded on the planner's `questions`, which needs the real planner in trials).
- ≥ 40 tasks. Every task passes `corpus_check` fairness: the reference passes the hidden tests and
  the base fails them.
- Splits frozen with `CorpusService.freeze` (development, validation, holdout;
  `evaluation/corpus.py:26-121`). The proposer reads development only (`corpus.py:131`).
- Difficulty is measured, never declared: calibration records per cell. Informative = pass rate in
  [0.2, 0.9]; saturated tasks (every cell passes every repeat) move to a regression set; flaky
  grading (`corpus_check --repeats`) blocks a task.
- Leak gate: component content is scanned for validation and holdout task ids, hidden test names and
  identifiers that occur only in reference solutions; a hit is a `contamination_findings` entry and
  the proposal is refused at screening.

### 1.4 Evaluator and protocol (D-099)

Evaluator (lands and is qualified first):

- `analysis.py` keeps `paired_binary_conservative` and adds `endpoint: noninferiority |
  superiority`, `minimum_effect` for superiority, and a decision class computed from the success
  interval plus descriptive cost and time deltas: `improvement` (lower bound > minimum effect),
  `efficiency` (non-inferior and cost benefit), `non_inferior`, `regression`, `tradeoff`,
  `inconclusive`.
- pass^k: `repeats_per_task` > 1 already means "every repeat passes" (`analysis.py:152-166`); the
  report adds per-task pass rates for calibration.
- Sample rationale must include a minimum-detectable-effect line computed from the planned task
  count (n = 20 gives a standard error near 11 pp; `research-meta-harness.md` §Power).
- Qualification: every stored experiment re-analyzed under the new code returns its recorded
  verdict; negative controls fail or stay inconclusive.

Protocol (one proposal, several frozen experiments under the proposal's one budget):

| Stage | Split | Arms | Repeats | Purpose | Gate |
|---|---|---|---|---|---|
| calibration | dev + validation | cell baseline | 2–3 | measure difficulty | operator approves budget |
| screening | dev informative | baseline, candidate | 1 | exploratory filter | automatic advance on no regression and no safety failure |
| focused | validation informative | baseline, candidate, best-of-n baseline (cost-matched) | 2 | confirmatory | operator approves experiment |
| ablation | dev informative | leave-one-out variants | 1 | component contribution | automatic |
| holdout | holdout | baseline, winner | 1 | confirm, one use | evaluator identity consumes holdout |
| canary | real goals | winner | — | existing (D-092) | operator approves, then promotes |

- `amplai meta calibrate --cell … --repeats …` and `amplai meta search --cell … --proposal …`
  orchestrate stages and stop at every human gate; `status` shows where.
- `max_attempts` of a proposal budget becomes the number of planned experiments.
- Trials: executor gains a real-planner mode (needed for `ambiguity`), and `safety_failures` /
  `unknown_effects` come from the run's receipts instead of constants.

### 1.5 Proposer (D-100, per OD-13)

- Trace capture (if approved): for meta-harness trials on the corpus only, a sanitized transcript
  artifact (visible messages, tool calls, tool output truncated; reasoning items dropped), passed
  through the secret scanner used by qualification, size-capped, readable by the proposer role,
  never exported.
- `amplai meta propose --from-traces --cell …`: a read-only driver turn (the pattern of
  `planner_codex.py`) given the component catalog, the current manifest, development traces and
  scores. Output schema: component kind, new content, rationale, predicted improving and regressing
  development task ids, risk.
- The leak gate runs on the output; screening refuses a hit.
- After focused evaluation, prediction precision and recall are stored with the proposal (AHE).

### 1.6 Dashboard

`amplai meta dashboard --out DIR` writes static files from stored records; no script, a CSP, all
text escaped, offline (the checks of `verification/runtime/render_acceptance.py:24-26`). Pages:

1. Matrix: rows = driver/model, columns = effort; each cell shows calibrated pass rate with its
   interval, pass^k, informative task count, median time, tokens per solved task, API-equivalent
   cost per solved task (descriptive, D-094), and the best composition with its delta.
2. Cell page: compositions with their manifests (component on/off/version), success vs cost
   frontier (inline SVG), ablation contributions, stage history.
3. Lineage: proposal → candidate → experiments → verdict → release.
4. Pending approvals: everything waiting for the operator, with the surface class.
5. Corpus: domains × difficulty, saturated and flaky lists.
6. Experiments: verdict, endpoint, interval, per-task baseline vs candidate with regressions marked.

### 1.7 Verification speed

- Repository CI: PR #45 (pytest-xdist). Local parallel run: 3,461 tests, 376 s, no conflicts.
- Product: `env_bootstrap` can include the fastest relevant test command (A4 fast feedback). The
  final verification stays the protected full verification (class C).

### 1.8 Legacy (D-101, per OD-10; scope (1) shown)

| Remove | Callers | Tests to migrate or delete |
|---|---|---|
| `meta_harness/reference.py` | `runtime/cli.py:340` (`ops meta-demo`), `pipeline_reference.py:22` | `test_meta.py`, `test_rc02_meta.py`, `test_dev03_meta_harness.py`, `test_dev03_evaluation.py`, rc11 tests through the `meta03` fixture (migrate these to the real service fixtures first) |
| `meta_harness/pipeline_reference.py` | `runtime/cli.py:354` (`ops evolution-demo`) | `test_dev03_meta_harness.py:487-488` |
| `demo-local` exception | `meta_harness/service.py:419-421` | with the above |
| duplicate `MetaReference.release` | `reference.py:119-211` | with the above |
| stale comment | `runtime/execution/meta_local.py:144` | — |

Each removal first shows no remaining reference (static and dynamic: entry points, CLI registry,
string imports). Tests that assert real evaluation semantics are ported, not deleted. `RecipePort`
is not legacy (used widely by runtime tests).

## 2. Slices

| Slice | Content | Depends on | Acceptance |
|---|---|---|---|
| S1 | Evaluator (D-099 evaluator part) + qualification | — | AC-06 |
| S2 | Components, manifests, runtime readers, golden prompt (D-096) | — | AC-01, AC-02 |
| S3 | Cells, effort argv, qualification binding, `ops local-cell` (D-097) | S2 | AC-03 |
| S4 | Corpus v2 base app and ≥ 40 tasks, splits, leak gate (D-098) | — | AC-04 |
| S5 | Protocol: calibrate, search, stages, best-of-n arm, real-planner trials, receipt counters | S1, S2, S3, S4 | AC-05, AC-08 |
| S6 | Proposer, trace capture (per OD-13), predictions | S5 | AC-07 |
| S7 | Dashboard | S5 | AC-09 |
| S8 | Legacy removal (per OD-10) | S2 | AC-10 |
| S9 | Docs and decisions (DEV03, usage guide, D-096 … D-101) | S1–S8 | AC-12 |
| S10 | Real round (per OD-11, OD-12) | S1–S7 | AC-11 |
| S11 | Final HTML report | S10 | AC-12 |

S1, S2 and S4 have no mutual dependency and can run in parallel when their files do not overlap
(S4 touches only `specs/033-harness-taxonomy/corpus/`). Each slice ends with a PR: tests, ruff,
mypy, the parallel suite, verifier `--profile v2`, the V3 docs cycle, CI, merge.

## 3. Execution (workflow, OD-5)

Per slice: implement → tests → verify → review → fix → verify. Reviews run one at a time
(memory: parallel reviewers hit session limits). At most 3 agents run at once; test writers own one
test file each and treat `src/` as read-only (memory: parallel test writers). No worktree isolation
unless two writers would touch the same file (it needs its own venv).

| Role | Model | Effort |
|---|---|---|
| Core implementer (S2 components, S5 protocol, S6 proposer) | Opus 5.5 | xhigh |
| Implementer (S1 evaluator, S3 cells, S8 legacy) | Opus 5.5 | high |
| Corpus task author (S4, one agent per domain) | Sonnet 5.5 | high |
| Dashboard implementer (S7) | Sonnet 5.5 | high |
| Test writer | Sonnet 5.5 | medium |
| Docs writer (S9) | Sonnet 5.5 | medium |
| Mechanical scans (reference sweeps, lint fixes) | Sonnet 5.5 | low |
| Reviewer (correctness, then security; sequential) | Opus 5.5 | high (xhigh for S2 and S6) |

The main session integrates, runs verifiers, docs cycles, PRs and merges, and runs S10 with the
operator's approvals. Under OD-9 (c), two small slices of S9 are also submitted as `amplai work`
goals on a separate deployment, and their runs are kept as observations.

## 4. Budget (OD-11)

Per-trial measurements: Codex ~51 s and ~100k input tokens (20 tasks, 17 min, ~2.03M input,
D-093), 89% of input cached in a measured run with an API-equivalent of $0.080 (D-094); OpenCode
46 s (`specs/031-opencode-driver/runs/trial-1.json`). Estimates below use ~1 min and ~100k input
tokens per trial; larger corpus v2 tasks will cost more (추정).

| Tier | Cells | Calibration (40 tasks × 2 repeats) | Search rounds (~195 trials each) | Trials | Wall time, sequential |
|---|---|---|---|---|---|
| S | 2 | 160 | 1 | ~355 | ~6 h |
| M | 4 | 320 | 2 | ~710 | ~12 h |
| L | 6 | 480 | 3 | ~1,065 | ~18 h |

A search round: screening 12 tasks × 2 arms (24), focused 16 tasks × (baseline + candidate +
best-of-2) × 2 repeats (128), ablation 2 variants × 12 (24), holdout 8 × 2 (16), canary 3.
Subscription quota per window is unknown; runs pause and resume under the proposal budget.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Hard but fair tasks are difficult to write (D-093 open) | calibration keeps only informative tasks; fairness check; Opus review of hidden tests |
| Small samples cannot show small effects | informative tasks, repeats, minimum-detectable-effect in every plan, honest `inconclusive` |
| Subscription limits stop a round | budgets, pause and resume, sequential trials |
| Trace capture leaks secrets or private data | OD-13; synthetic corpus only; secret scan; no export |
| A model does not support an effort | qualification per cell; refuse, never substitute |
| Composition digests change | revision bump; one `COMPOSITION_CHANGED` for waiting plans |
| Removing legacy breaks tests | migrate first, remove after green |
| The running server is disturbed | real rounds use a separate deployment; the running profile stays unchanged |

## 6. Rollback

Each slice is its own PR. The v1 manifest is today's behaviour; cells are optional configuration;
the release pointer keeps its signed rollback (`meta_harness/service.py:944-1024`).

## 7. Validation

- Per slice: targeted tests, `ruff check`, `ruff format --check`, `mypy src`, `pytest -n auto`.
- Per PR: `python3 .ai-team/verifiers/run.py --profile v2`, docs cycle on
  `specs/019-v3-completion`, CI.
- End: AC-11 real round records, dashboard, final report.

## 8. Execution Strategies And How They Are Chosen (operator, 2026-09-30: implement all)

Strategies (all implemented as `execution_strategy` versions):

| # | Strategy | Mechanism |
|---|---|---|
| 1 | single | one agent end to end (today) |
| 2 | repair loop | retry with verifier feedback (today; form = `feedback_form`) |
| 3 | workgraph split | planner splits one repo's goal into ordered nodes by file area |
| 4 | plan-then-execute | a planner cell plans, an executor cell implements |
| 5 | best-of-n | n independent attempts, the verifier picks a passing one |
| 6 | generator + reviewer | a second agent reviews the diff and asks for fixes before verification |
| 7 | cascade | cheap cell first, escalate to a stronger cell on failure |
| 8 | orchestrator + sub-agents | a lead agent delegates bounded parts; depth and count are budget-capped (design 02) |
| 9 | parallel read-only steps | investigation and review steps in parallel (≤ 4, design 07 §4) |
| 10 | vote / ensemble | several answers combined; for test-graded tasks the verifier decides |

Write concurrency stays 1 per repo write scope (design 07 §4); parallel writers work in separate
worktrees and go through an integration queue with re-verification.

Selection (`route_policy`), in order:

1. **Features** known before running: task class, domain, planned size (files and acceptance
   items from the plan), number of apps, risk.
2. **Measured table**: for each feature bucket × (strategy, cell), the success rate with its interval,
   pass^k, cost per solved task and time, from calibration and experiments.
3. **Rule**: among options whose success rate is not credibly lower than the best option's (the
   non-inferiority margin of the policy), pick the lowest expected cost per solved task; ties go to
   the simpler strategy (lower number above).
4. **Prior** when a bucket has too few samples: today's behaviour (single + repair, router order),
   never an untested strategy.
5. **Runtime signal**: cascade (7) and repair (2) react to failure within a goal; nothing else
   changes mid-goal (design 16 §2: composition fixed after dispatch).
6. The policy itself is a class B candidate: a new table/rule goes through screening → focused →
   holdout → canary against the current policy; no online learning (design 16 §2).

### 8.1 Metrics for strategy selection (in scope)

Per trial (stored with the trial receipt; existing ones marked *):

| Metric | Why |
|---|---|
| success*, hidden-test result*, verified-but-hidden-fail* | outcome (D-094) |
| tokens by class*, API-equivalent cost*, wall time* | cost (D-094) |
| strategy id, cell(s) used, agent calls, turns, attempts used | what the strategy actually did |
| escalations (cascade), reviewer rounds and fix requests (generator + reviewer) | whether the extra step was used and helped |
| best-of-n: attempts until the first pass | how much n is needed |
| workgraph / orchestrator: nodes, sub-agents, integration conflicts, re-verifications | cost of splitting |
| verification wall time | component F |

Per feature bucket × (strategy, cell), computed from the trials:
success rate with its interval, pass^k, cost and time per solved task, sample count.

Router quality (for evaluating a `route_policy` candidate):
- **regret**: on tasks where every option was measured, the gap between the chosen option and the
  best option in hindsight (success first, then cost);
- **coverage**: share of tasks decided by measured data vs the fallback prior;
- **escalation rate** and cost of escalations.

All are descriptive except the pre-declared endpoint of each experiment (D-093, D-099).

## 9. L10 Experiment Operations: The Nightly Loop (research: `research-continuous-search.md`)

A fixed nightly trial budget B (operator-set; subscription windows permitting). Evidence accumulates across
nights; nothing is re-run that is already measured for the same (config hash, task, model snapshot, corpus version).

| Share of B | Phase | What |
|---|---|---|
| ~10% | drift check | the champion on fixed canary tasks; a changed model snapshot or a canary outside its band stops search that night and re-baselines |
| first nights | screening | layers as factors in a screening design (Plackett–Burman / fold-over) to find the few layers that matter (HARBOR: ~5 of ~40); the rest are fixed |
| ~60% | search | a surrogate fitted on all runs (task type × configuration, IRT-style or block-additive) proposes candidates, plus proposer edits; successive halving on task-subset fidelity |
| ~30% | confirmation | top 1–2 candidates paired with the champion on the same tasks |

Guards (evaluator part; qualified alone first, D-099):
- promotion evidence as an e-process / always-valid test so stopping across nights keeps its error rate;
- paired differences with task-cluster standard errors; differences under the noise band are "unresolved";
- a sealed holdout queried through a budget (reusable-holdout rule) and rotated;
- always compared with the same-budget best-of-n baseline (Rethinking);
- a chance constraint against regression vs the champion (HARBOR);
- promotion still goes through canary and the operator (design 16).

Run log per trial: config hash and per-layer options, harness git SHA, resolved model snapshot, task id, type,
corpus version, split, seed, attempt index, fidelity, phase, outcome, cost, tokens, trace ref, e-value state,
holdout query counter.

Cheaper models as low fidelity are not used (no evidence; transfer is limited, HarnessDev).
Continuous nightly operation of this combination has no published evidence: the first nights are a pilot.
