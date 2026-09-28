# Work 019 — V3 Completion On The Mac (A, D, E, F)

**Work ID**: AMPLAI-V3-EXEC02 · **Created**: 2026-09-28 · **Type**: new_feature (architecture)

## Why

Work 018 made `amplai work` real for one app and one driver. Four parts of the approved V3 design
are still missing on that real path:

- **A — PR outcome**: the result of a published draft PR (merged, closed, edited by a human) is not
  recorded. The design counts rework and human intervention
  (`design-reference/design/15_EVAL_OBSERVATORY.md:20-31`); "was the result accepted" is the
  strongest quality signal and is absent.
- **E — composition selection**: the design selects a HarnessComposition by filtering on
  project/data/risk/required capabilities and then applying a task-class baseline policy
  (`design-reference/design/16_META_HARNESS.md:14-16`). `CompositionRegistry.select` exists
  (`src/amplai_foundry/meta_harness/composition.py:82-120`), but the local product hard-wires the
  Codex profile at approval (`src/amplai_foundry/runtime/execution/product.py`, `approve`). Claude
  CLI passed the 9 probes in Work 016 but its tool use in the container was never measured
  (D-073, D-075).
- **F — design mode**: `mode=design` produces design artifacts and review evidence and never
  dispatches implementation (`design-reference/design/03_INVARIANT_REGISTRY.md:59`,
  `05_GOAL_RESOLVER_CONTRACT.md:48`, `27_END_TO_END_SCENARIOS.md:8`). `amplai design` only stores
  the intent today (`src/amplai_foundry/runtime/cli.py`, `design`).
- **D — multi-app, steer, replan**: V3-057 and T-107 are closed only by fixture evidence (D-075).
  The real path has one app, one node, no steering and no replan.

## Goal

On this Mac, the same operator flow (`amplai work|design "…"` → contract → approve → container →
verify → draft PR) also:

1. records what happened to each draft PR and reports acceptance and human rework (A);
2. lets the system choose the driver/model composition, Codex first and Claude as fallback, and
   shows the choice and reason at approval (E);
3. runs design goals that produce reviewed design documents only (F);
4. runs one goal across two apps with a dependency, accepts operator steering of a running agent,
   and replans with a new contract revision and a new approval (D).

## Operator Decisions (2026-09-28)

- E: initial task-class baseline policy = **Codex first, Claude fallback**. Changes to the policy
  come from meta-harness evidence later, not ad-hoc.
- F: design output = **docs-only draft PR** (only design paths may change).
- D: second app = **new demo repo** `pineskyeo/amplai-demo-app` (GitHub private).
- Every goal is approved by the operator (Work 018 rule stays).

## Requirements

- **R1 (A)**: For every published draft PR, the product periodically reads its state with the
  operator's `gh` and records `publication.merged`, `publication.closed` or
  `publication.revised` (commits other than ours on the branch) once per transition, on the goal's
  audit trail. The Observatory reports counts, acceptance rate (merged / decided) and revised
  share. Unknown stays unknown (no PR state read → not counted).
- **R2 (E)**: Claude CLI is qualified in the app image with the production argv including a real
  tool-use turn. Both compositions are registered only from matching measured qualification.
- **R3 (E)**: Composition choice uses `CompositionRegistry.select`: eligibility filter first,
  then the stored baseline policy. The plan record and the approval screen show the chosen
  composition and why (rank, excluded candidates with reason). The operator approves the plan,
  not a driver. The composition is fixed after dispatch.
- **R4 (E)**: Planning follows the same selection (the planner runs on the selected composition's
  driver), so a Codex outage does not block planning when Claude is eligible.
- **R5 (F)**: `amplai design "…" --app X` drafts a design contract (mode=design, design
  capabilities only), the agent writes only under the design path, a deterministic design
  verifier checks path scope, required sections and source citations, and the result is a
  docs-only draft PR. No implementation node is dispatched.
- **R6 (D)**: A goal may target two installed apps. The compiled graph has one node per app with
  a dependency; the downstream node sees the upstream change; each node is verified by its app's
  suite on base+patch; the goal-level verification runs a cross-app integration check on both
  changes. Each app gets its own draft PR, linked to each other.
- **R7 (D)**: `amplai steer <goal> "…"` delivers operator guidance to the running attempt through
  the durable steering ledger; the real CLI agent is stopped at a process boundary and resumed on
  the exact session with the message. Steering never changes the contract.
- **R8 (D)**: `amplai replan <goal> "…"` drafts a new contract revision (protected constraints
  kept) and a new graph revision; nothing runs until the operator approves the revision.
- **R9**: Everything in Work 018 keeps working (no regression): existing tests, real `amplai work`
  path, metrics.

## Acceptance

- **A1**: Real PRs #12–#14 get their outcome recorded; the Observatory shows it.
- **A2**: Claude qualification report with tool_use pass; a real goal runs on Claude when Codex is
  made ineligible, and on Codex otherwise.
- **A3**: One real design goal ends in a docs-only draft PR; a patch touching code fails.
- **A4**: One real two-app goal ends in two linked draft PRs with integration verification; one
  real steer and one real replan are recorded on real runs.
- **A5**: verifier v2 PASS, docs validate RESOLVED, repository docs FRESH.

## Non-goals

- Windows/company environment, remote workers, multiple operators.
- Automatic online routing/bandits (design default off); changing the baseline policy from data (B).
- Auto-merge, deployment.
- Changing `design-reference/` or the 3.0.0 wire schemas.
