# Portable Loop Runtime

The only development entry points are work and design. Use the repository's AGENTS.md
and approval rules first; this baseline does not replace domain rules or host trust.
The same scripts/loopctl.py and scripts/loopv2.py run contract, readiness, context,
evidence and documentation checks. The registry owns executable verifier profiles.

## Execute

Run `python3 scripts/loopctl.py doctor`. For a feature, validate its work contract,
readiness and Context Pack, then generate tasks from task-manifests and execute
`bash scripts/eval.sh --feature specs/<feature> --slice S01`.
Generic profiles verify the baseline and selected task contracts, not undeclared product
behavior. Add actual product checks to the same verifier registry and task acceptance.

## Preserve

Knowledge indexes reference owner sources; they do not copy another project's knowledge.
Document changes before independent review. Afterwards, incremental gardening is
report-only. No install, publish, host trust change, credential disclosure or deletion is
implied by a successful local verifier. Unavailable native/target checks remain unverified.

Compatibility garden apply is also report-only. User backups, tracked documents, ADRs and
incidents are not automatic garbage. Full scanning requires an explicit high-risk
repository_gardening Work: garden full <feature> --report-only. Missing reads or scan
limits cannot pass as a complete result.

Document split/merge uses docs preserve --input <request.json> for exact section coverage.
Retained originals or evidence-bound exact destinations preserve every source byte;
summary text and Git history alone are insufficient. docs plan-retirement is read-only.
Keep planning input/output outside the scanned checkout to avoid self references. No
destructive CLI or new approval authority is installed. Internal retirement/recovery
requires trusted adapters and is tested only in disposable repositories.

Skills are canonical under .agents/skills; .claude/skills contains exact relative mirrors.
No second controller, scheduler, state store or standalone docs entry point is required.
