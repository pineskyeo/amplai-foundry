# Portable Work and Design

Work: scope/contract → evidence-backed readiness → Context Pack/environment →
approved plan → taskify → consistency analysis → implement and deterministic eval →
converge → document freshness → independent review → report-only gardening → handoff.
Design stops after approved requirements, boundaries, evidence and executable acceptance;
it does not implement source changes.

READY/BYPASS and resolved approval gates are prerequisites, not implementation evidence.
Use the existing scripts/loopctl.py and scripts/eval.sh. A Slice contains real Eval
commands. Empty, failing or unavailable validation never means PASS. Preserve failed
attempts and classify a repeated failure after three attempts before further repair.

The generic Context Pack requires AGENTS.md, this workflow and the verifier registry.
Their explicit governing-input roles are separate from reviewed release guides and
still respect source security and retired-instruction metadata. Only applicable active
indexed Decisions and typed Decision references enter from an explicitly configured
ledger; never select its whole historical body. Validation reconstructs all expected
sources, claims and Decisions, so a rehashed but incomplete pack is invalid.

Contract, failure/recovery and regression reviews are independent and sequential. P0,
P1 and Blocking-P2 findings prevent closure. Record advisory dispositions explicitly.
Review follows document freshness; scoped report-only gardening follows review.
Actual approved changes after review require proportional re-verification.

Full scan requires an explicit high-risk repository_gardening Work. Compatibility
garden apply stays report-only; backups, tracked documents and historical ADR/incident
sources are not automatically removed. Incomplete scans cannot pass the gardening gate.
Document split/merge requires exact source-section preservation, not a summary. Retirement
planning is read-only. Its internal apply/recovery seam requires existing trusted authority
and verifier adapters; no destructive CLI or new approval authority is provided.

Canonical knowledge, decisions, Work memory and evidence have separate ownership. Keep
historical provenance and flag unknown/conflicting meaning. Repository-local permissions
and production boundaries apply. Never copy credentials or private trace to deliverables.
