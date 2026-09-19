"""Conservative result reuse (design/06 §6, T-028).

A prior node success may stand in for a new run only when every identity that shaped it
still matches. Deploy/publish receipts are never reused as compute results. An LLM
statement of compatibility is not evidence.
"""

from __future__ import annotations

from typing import Any

from amplai_foundry.runtime.errors import Hold

REUSE_IDENTITY = (
    "node_digest",
    "input_artifact_digests",
    "acceptance_subset_digest",
    "verifier_version",
    "policy_epoch",
    "environment_fingerprint",
    "knowledge_pin",
)
NEVER_REUSED = frozenset({"deploy", "publish", "production_control"})


def reuse_decision(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"decision": "reuse"|"reverify", "mismatched": [...]}``; never raises."""
    mismatched = [key for key in REUSE_IDENTITY if prior.get(key) != current.get(key)]
    if prior.get("receipt_kind") in NEVER_REUSED:
        mismatched.append("receipt_kind")
    return {"decision": "reuse" if not mismatched else "reverify", "mismatched": mismatched}


def require_reusable(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Raise ``REUSE_REVERIFY`` instead of letting a stale success pass."""
    verdict = reuse_decision(prior, current)
    if verdict["decision"] != "reuse":
        raise Hold(
            "REUSE_REVERIFY",
            "Prior node result does not match the current identity; verify again",
            details={"mismatched": verdict["mismatched"]},
        )
    return verdict
