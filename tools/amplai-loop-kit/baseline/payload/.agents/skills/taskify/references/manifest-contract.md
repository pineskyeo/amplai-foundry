# Task Manifest Contract

## Purpose

A task manifest is an execution contract between an approved design and an
implementation agent. It is not a brainstorming note and not a substitute for the
product or architecture specification.

## Status lifecycle

```text
draft
  ├─> blocked
  └─> ready
        └─> in_progress
              └─> verification
                    ├─> ready
                    ├─> needs_revalidation
                    └─> done

Any non-terminal state may become superseded when the source design replaces it.
A done task becomes needs_revalidation rather than ready when its accepted behavior
must be checked against a changed source.
```

Allowed statuses:

- `draft`: Structure exists but is not executable yet.
- `blocked`: A material decision or dependency is unresolved.
- `ready`: Inputs, scope, dependencies, and acceptance are sufficient.
- `in_progress`: An implementation agent owns the task.
- `verification`: Implementation is complete and gates are running or under review.
- `needs_revalidation`: Previously accepted work was affected by a source change.
- `done`: All required gates and evidence passed.
- `superseded`: Replaced by a newer task or no longer required, with history retained.

## Mandatory fields

### Identity

- `schema_version`
- `id`
- `title`
- `type`
- `status`

Task IDs must remain stable after publication.

### Goal

`goal.outcome` states the externally observable result.  
`goal.user_value` states why the result matters.

### Source

`source.requirements` provides traceability to approved requirements.
`source.decisions` references ADRs or explicit decisions that constrain the task.

### Scope

- `include`: Work necessary for this outcome.
- `exclude`: Nearby work explicitly outside the task.
- `allowed_paths`: Paths the implementer may change without expanding scope.
- `forbidden_paths`: Paths the task must not change.

Empty path arrays mean the boundary could not be proven from the source. They do not
mean unrestricted permission. Repository permission policy still applies.

### Dependencies

- `dependencies.tasks`: Task IDs in the same task set.
- `dependencies.external`: Services, approvals, environments, data, or teams outside
  the task graph.

Internal dependencies must form a DAG.

### Implementation constraints

- `guidance`: Non-binding implementation guidance from the approved plan.
- `invariants`: Rules that must remain true.
- `non_goals`: Attractive but out-of-scope additions.

Guidance must not silently become a new requirement.

### Acceptance

`acceptance.behaviors` uses concrete Given/When/Then statements.

`acceptance.commands` contains commands already supported by the repository:

- `id`: Unique within the task.
- `kind`: `build`, `test`, `integration`, `lint`, `typecheck`, `eval`, or another
  project-defined machine gate.
- `run`: Exact command.
- `required`: Whether completion depends on it.

`acceptance.manual_checks` is allowed only when automation is unavailable. Every
manual check should state the observer, action, expected result, and why it is not
automated.

`acceptance.evidence` declares what must be retained or summarized.

### Risk

Levels:

- `low`: Local and easily reversible.
- `medium`: Multiple components or meaningful compatibility risk.
- `high`: Production state, data integrity, security, equipment movement, migration,
  or hard-to-reverse behavior.

High-risk tasks should state explicit approvals.

### Loop

- `max_attempts`: Hard cap on repeated implementation attempts.
- `replan_after_same_failure`: Escalate when the same failure class repeats.
- `stop_conditions`: Conditions that end implementation and return to planning,
  design, or an external owner.

A bounded retry loop is not permission to change the specification.

### Completion

A task is `done` only when:

```text
all_acceptance_passed == true
required_evidence_present == true
completed_at is populated
evidence_paths is non-empty
```

The validator enforces this consistency.

## Updating an existing task set

### Preserve

- IDs
- Status
- Completion evidence
- Human review notes
- Source traceability
- Update history

### Change safely

- Add a new ID for new work.
- Mark replaced work `superseded`.
- Mark completed work `needs_revalidation` when source behavior changes.
- Record the source revision and reason for material changes.
- Surface conflicts rather than rewriting history.

## Vertical slice examples

### Good

```text
A repeated start request for the same lot returns the existing run and proves that
only one active run can exist under concurrent requests.
```

This can contain state lookup, locking, API response, tests, and logs because all of
them prove one behavior.

### Bad

```text
Implement database layer.
Implement API layer.
Write tests.
```

These tasks cannot independently demonstrate user or system value and create a late
integration cliff.

## When a separate foundation task is justified

A foundation task is acceptable when all are true:

1. A near-term vertical slice immediately depends on it.
2. Its boundary is stable and approved.
3. It has independent machine-checkable acceptance.
4. It does not speculate about future generality.
5. It is smaller and safer than embedding the work in the first slice.
