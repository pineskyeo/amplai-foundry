# Portable Baseline Inventory

This is the finite design inventory, not a copied payload or installation receipt.
Canonical package owner remains `tools/amplai-loop-kit`. Existing app-local files are
preserved or three-way merged; this list does not grant overwrite authority.

## Runtime Inputs

- `scripts/loopctl.py`, `scripts/loopv2.py`, `scripts/eval.sh` and planned `scripts/amplai_docs.py`.
- `.ai-team/verifiers/run.py`, generic `registry.json`, and `README.md`.
- `.ai-team/README.md`; `.ai-team/runtime/WORKFLOW.md`, `policy.json`, `escalation.md`.
- `.ai-team/policy/knowledge-readiness.json`, `permissions.json`, `tdd.json`, `quadrant.json`,
  `documentation.json`, `gardening.json`.
- `.ai-team/contracts/README.md`, `work-contract.schema.json`, `work-contract.template.json`.
- `.ai-team/knowledge/README.md`, `map.json`, `claims.jsonl`, `decisions.index.json`,
  `knowledge-readiness.schema.json`, `knowledge-readiness.template.json`,
  `domain-discovery.schema.json`, `context-pack.schema.json`, `doc-impact.schema.json`,
  `garden-report.schema.json`, `handoff.schema.json`.
- `.ai-team/evidence/README.md`, `provenance.schema.json`.
- Local document/profile schemas and defaults consumed by the planned document engine.

## Capability Closure

Each of the following has one canonical `.agents/skills/<name>/SKILL.md`:
`work`, `design`, `dev-loop`, `speckit-specify`, `speckit-clarify`, `speckit-plan`,
`taskify`, `speckit-analyze`, `speckit-implement`, `speckit-converge`, `code-review`,
`systematic-debugging`. The ten internal capabilities also require their
`agents/openai.yaml`. Claude mirrors are exact relative symlinks, not copied instructions.

Taskify additionally requires `assets/task-index.yaml`, `assets/task-manifest.yaml`,
`references/manifest-contract.md`, `scripts/validate_task_manifest.py`; its dependency
guide is retained for optional legacy YAML support. New portable tasks use JSON-first loading.

Spec-Kit requires `.specify/scripts/bash/common.sh`, `check-prerequisites.sh`, `setup-plan.sh`,
`.specify/scripts/taskify_to_tasks_md.py`, `.specify/templates/spec-template.md`,
`plan-template.md` and a generic `.specify/memory/constitution.md`.
Feature marker and per-Work/evaluator state are generated locally, never seeded from Foundry.

## Preserve And Adapt

`AGENTS.md`/`CLAUDE.md` receive scoped bootstrap instructions, not whole-file ownership.
Baseline and async extension edits to one path are composed into one transaction action.
Symlinks need explicit typed actions and recovery/uninstall records. Never replace a user
directory with a mirror or follow an unvalidated link.

Generic profile has no Vault, `.venv`, specific feature, Cortex entry point, private approval
or model/provider identity. Foundry keeps its full verifier and additional six skills.
An app profile may add actual build/test/ontology-binding commands and protected paths;
it may not remove mandatory runtime checks or broaden trust/approval automatically.

The installed inventory excludes `.ai-team/app.json` source values, local/install/backups,
host settings, Project Store content, real Work artifacts and source repository history.
The existing async installer still owns its declared identity/hook creation and records it.

## Acceptance Closure

Fresh canary executes doctor, contract, readiness, context, task validation/generation,
Slice evaluator, docs and report-only garden using only installed files plus declared
Python/Git/Bash utilities. Remove each required family and require a deterministic failure.
Schema distribution is not full JSON Schema validation: current runtime uses explicit
stdlib validators, which must enforce the contract fields exercised by the canary.

All package files, including installer-read store supervisor payload outside owned_files,
are checked by the complete seal before mutation. Version equality is not content equality.
No real installation or fleet update is performed by this design record.
