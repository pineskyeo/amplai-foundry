# Work 019 — Plan

## Architecture Decisions

| ID | Decision |
|---|---|
| D-078 | PR outcomes are read with the operator's `gh` (read-only) and recorded once per transition as `publication.opened/merged/closed/revised` goal events, in the plan-record transaction. Unknown stays unknown; pre-tracking PRs are backfilled as opened (marked). |
| D-079 | The system selects the HarnessComposition: `CompositionService.select` filters (model enabled, data class, driver qualified, capabilities), then ranks by a stored task-class baseline router policy. Initial policy (operator, 2026-09-28): Codex first, Claude fallback, every class. Planning uses the same selection; the execution choice is fixed in the plan, shown at approval and re-checked before any approval record is written. Claude CLI is registered only from a passing in-image qualification with a real tool-use turn. |
| D-080 | Design mode: contract `mode=design` with `workspace.design_write` only and a protected C-DESIGN constraint; the agent writes `specs/design/<goal>/design.md`; `DesignDocumentCheck` (host-side, never runs the change) requires design-path-only changes, the seven sections and ≥3 resolving `path:line` sources; output is a docs-only draft PR. |
| D-081 | Multi-app goals: one node per app with `depends_on`/`consumes` from the planner's work items; each node runs and is verified in its app's image; the downstream prompt carries the verified upstream patch; the goal-level global verification adds the operator-configured integration commands on all apps' base+patch copies; one linked draft PR per app. |
| D-082 | Steering and replanning on the real path. `amplai steer` = the design's pause → exact-session resume pair: the worker stops at a process boundary (checkpoint, snapshot), and the resumed turn receives the operator's message; the contract never changes. `amplai replan` = a new contract revision (protected constraints kept) and graph revision drafted by the planner with the reason; nothing runs until the operator approves the revision. |

## Slices

| Slice | Scope | Verifier |
|---|---|---|
| S1 (A) | `runtime/execution/outcomes.py`, loop idle tick, Observatory `publication_outcomes`, `ops pr-sync` | `tests/e2e/test_rc07_outcomes.py` + real PRs #12–#14 |
| S2 (E) | provider-generic profiles, `ClaudePlanner`, router policy, `select_composition`, `explain`, local config, `ops local-claude/local-driver`, qualify tool_use for Claude | `tests/e2e/test_rc07_selection.py` + Claude qualification + one real Claude goal |
| S3 (F) | `DesignDocumentCheck`, mode-aware plan/compile/approve/prompt/publish, `amplai design` | `tests/e2e/test_rc07_design.py` + one real design goal |
| S4 (D) | multi-app config/compile/loop/publish/tracker, integration check, steer, replan, demo repo | `tests/e2e/test_rc07_multiapp.py` + real two-app goal with one steer and one replan |
| S5 | real runs evidence, review (one reviewer at a time), docs cycle, verifier v2, push | A1–A5 |

## Regression Guard

- Every slice runs `tests/v3` and `tests/e2e` before commit; the full verifier v2 runs before
  push.
- Work 018 plan records (no `composition`, no `mode`) keep approving on Codex in work mode.
- The 3.0.0 wire schemas and `design-reference/` are unchanged (D-077 applies).

## Known Limits

- Claude reports `total_cost_usd` (an API-equivalent estimate); cost stays `unknown` in metrics
  until a pricing decision is made.
- Steering while a verifier runs (between attempts) is applied to the next attempt's prompt,
  not by pausing a process.
