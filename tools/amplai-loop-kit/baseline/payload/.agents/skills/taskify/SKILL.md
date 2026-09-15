---
name: taskify
description: Convert an approved specification, architecture plan, PRD, ADR set, or GitHub Spec Kit outputs into small implementation-ready vertical task manifests with stable IDs, dependencies, scope boundaries, executable acceptance checks, and bounded retry or replan rules. Use before implementation when asked to split work, create implementation steps, transform spec.md/plan.md/tasks.md into executable goals, or prepare an implementation loop. Do not use to implement code or silently decide unresolved product or architecture questions.
user-invocable: false
disable-model-invocation: false
---

# Taskify

Portable manifests use JSON syntax inside `.yaml` files. The bundled validator and
generator parse JSON with the Python standard library; existing YAML is optional and
requires an already installed PyYAML adapter. Do not install dependencies implicitly.

Transform approved intent into execution-ready task manifests. Do not implement the tasks.

Use the user-provided target when present:

```text
$ARGUMENTS
```

## Required outcome

Produce:

```text
<output-directory>/
├── index.yaml
├── <FEATURE>-T001.yaml
├── <FEATURE>-T002.yaml
└── ...
```

Each task must be a bounded, independently verifiable vertical slice that an
implementation agent can execute without reopening settled design decisions.

## 1. Discover the governing context

Read, in this order when available:

1. User instruction for this invocation.
2. Repository `AGENTS.md`, `CLAUDE.md`, and applicable nested instructions.
3. Approved decisions and ADRs.
4. `spec.md` or equivalent product specification.
5. `plan.md` or architecture/design plan.
6. Existing `tasks.md`.
7. Relevant implementation and tests, only to confirm repository facts.
8. CI, Makefile, package scripts, or test runners to discover real verification commands.

Treat product intent and implementation facts differently:

- Specifications and approved decisions define intended behavior.
- Existing code reveals interfaces, constraints, and available verification.
- Existing code does not override an explicit approved requirement.
- Never ask the user for information that can be reliably discovered in the repository.

If the design source is missing or still contains a decision that materially changes
task boundaries, do not invent the decision. Record it as an open question and mark
affected tasks `blocked`.

## 2. Choose the output directory

Use:

- `<feature-directory>/task-manifests/` when the source is inside
  `specs/<feature-directory>/`.
- `.amplai/tasks/<feature-slug>/` otherwise.
- An explicit user-specified output path takes precedence.

Do not overwrite an existing task set blindly. Apply the update rules below.

## 3. Build requirement traceability first

Create a requirement catalog before creating tasks.

- Preserve requirement IDs from the source.
- When a source has no IDs, assign local IDs such as `REQ-001` in `index.yaml`.
- Map every in-scope requirement to at least one task.
- Mark intentionally deferred or excluded requirements explicitly.
- Do not count an architecture component, file, or generic activity as a requirement.

A requirement is uncovered when no task acceptance behavior proves it.

## 4. Decompose into vertical slices

Prefer a task that completes one observable capability across the necessary layers.

Good:

```text
A duplicate lot-start request returns the existing active run, including persistence,
API behavior, concurrency protection, tests, and operational evidence.
```

Avoid:

```text
Create database tables.
Create API layer.
Create frontend.
Write all tests.
```

### Task boundary rules

A task must:

- Have one primary outcome.
- Be executable in one focused implementation session.
- Have one coherent rollback boundary.
- Be testable without waiting for unrelated future tasks.
- Include its own tests unless it creates shared test infrastructure.
- Identify explicit dependencies.
- Avoid mixing implementation, deployment, data migration, and production rollout
  when those have different risk or approval boundaries.

Split a task when:

- It has multiple independently useful outcomes.
- Different parts require different approvals or rollback procedures.
- Acceptance cannot be expressed with one coherent evidence set.
- It spans unrelated subsystems.
- A failed implementation would leave an ambiguous partial state.

Foundation tasks are allowed only when they are immediately consumed by a vertical
slice and have independent acceptance checks. Do not create speculative platform work.

## 5. Create stable IDs and a dependency DAG

Use IDs:

```text
<FEATURE>-T001
<FEATURE>-T002
```

Rules:

- Derive `<FEATURE>` from an established project or feature identifier when possible.
- IDs are immutable after first publication.
- Never renumber surviving tasks to close a gap.
- Dependencies must form a directed acyclic graph.
- A task must not depend on itself.
- Prefer the smallest valid dependency set.
- Put third-party, environment, or organizational blockers under
  `dependencies.external`, not as fake internal tasks.

## 6. Write each task manifest

Start from [assets/task-manifest.yaml](assets/task-manifest.yaml).

Mandatory content:

- Stable identity and status.
- Observable goal.
- Source requirement and decision links.
- Included and excluded scope.
- Allowed and forbidden path boundaries when they can be determined reliably.
- Internal and external dependencies.
- Invariants and non-goals.
- Given/When/Then acceptance behaviors.
- Real build, test, lint, integration, or eval commands.
- Required evidence.
- Risk level and approval requirements.
- Retry, replan, and stop conditions.
- Completion conditions.

### Acceptance rules

Acceptance must be externally checkable.

Good:

```yaml
- id: AC-01
  given: an active run already exists for LOT123
  when: the lot-start endpoint receives a second request for LOT123
  then: it returns the existing run_id and creates no additional active run
```

Avoid:

```yaml
- code is clean
- implementation looks correct
- task is complete
```

Use commands already supported by the repository. Do not invent a command solely
because its name sounds plausible. When no automated command exists:

- Define the missing harness as a task or dependency.
- Or define a precise manual check with an owner and reason.
- Never represent a manual observation as an automated gate.

## 7. Write `index.yaml`

Start from [assets/task-index.yaml](assets/task-index.yaml).

The index must contain:

- Source files and revision information when available.
- Task list and dependency summary.
- Requirement coverage.
- Deferred and excluded requirements.
- Open questions.
- Critical path or execution waves.
- Generation and update notes.

Execution waves may contain tasks with no dependency path between them. Do not claim
parallel safety merely because tasks are in the same wave; shared file ownership and
shared state must also be checked.

## 8. Preserve history when updating

When task manifests already exist:

- Preserve IDs, status, evidence, and human-authored notes for surviving tasks.
- Preserve completed tasks unless the new design invalidates their acceptance.
- Use `needs_revalidation` when a completed task must be checked again.
- Mark removed work `superseded`; do not silently delete history.
- Add new tasks with new IDs.
- Record why dependencies, scope, or acceptance changed.
- Do not reset `in_progress`, `verification`, or `done` to `ready`.
- Surface conflicts between the new source and completed implementation.

## 9. Validate

Resolve and run the bundled validator:

```text
scripts/validate_task_manifest.py <output-directory>
```

Install its dependency only with the user's normal project dependency process.
Do not automatically modify the environment.

Validation must pass before reporting the task set as ready. If the validator cannot
run, report that as a tooling blocker rather than claiming success.

Read [references/manifest-contract.md](references/manifest-contract.md) when field
semantics or update behavior needs clarification.

## 10. Report

Return a compact summary:

- Output directory.
- Number of ready, blocked, and superseded tasks.
- Execution waves and critical dependencies.
- Requirements not covered.
- Open decisions.
- Validator result.

Do not start implementation. End by naming the first ready task or stating why no
task is ready.
