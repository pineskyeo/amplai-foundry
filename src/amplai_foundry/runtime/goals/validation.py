"""Cross-object admission for a frozen goal; model output cannot supply these guards.

The normative schemas remain unchanged. This module enforces the semantic bindings
from design/05 and design/14 against the scoped immutable object store.
"""

from __future__ import annotations

from typing import Any

from ..contracts.identity import digest
from ..contracts.registry import Contracts
from ..contracts.semantics import check_context, check_refs
from ..errors import Hold
from ..storage.store import Scope, Store


def validate_bindings(
    store: Store, contracts: Contracts, scope: Scope, contract: dict[str, Any]
) -> None:
    plan = store.get(scope, "verification-plan", contract["verification_plan_ref"])
    contracts.validate("verification-plan", plan)
    check_refs(store, scope, plan)
    if plan["scope"] != scope.wire() or plan["policy_ref"] != contract["policy_ref"]:
        raise Hold("VERIFICATION_SCOPE", "Verification plan must use this scope and policy")
    # Definition ownership is deliberately acyclic (design/33).
    if plan["contract_ref"] is not None:
        raise Hold(
            "VERIFICATION_OWNER_CYCLE", "A root verification definition has no contract backlink"
        )
    ids = [b["acceptance_id"] for b in plan["bindings"]]
    if len(ids) != len(set(ids)):
        raise Hold("VERIFICATION_DUPLICATE", "Duplicate acceptance bindings are ambiguous")
    bindings = {b["acceptance_id"]: b for b in plan["bindings"]}
    if set(bindings) != {a["id"] for a in contract["acceptance"]}:
        raise Hold(
            "VERIFICATION_COVERAGE", "Bind every acceptance exactly once, with no invented IDs"
        )

    apps = [store.get(scope, "app-binding", r) for r in contract["targets"]]
    installed = {digest(r) for app in apps for r in app["verifier_profile_refs"]}
    environments = {digest(r) for app in apps for r in app["environment_refs"]}
    governing = [contract["policy_ref"], *(r for app in apps for r in app["invariant_refs"])]
    bundle = store.get(scope, "context-bundle", contract["context_bundle_ref"])
    contracts.validate("context-bundle", bundle)
    check_refs(store, scope, bundle)
    check_context(bundle, governing)

    for criterion in contract["acceptance"]:
        binding = bindings[criterion["id"]]
        if binding["verifier_ref"] != criterion["verifier_ref"]:
            raise Hold("VERIFICATION_BINDING", "Contract and plan name different verifiers")
        if digest(criterion["verifier_ref"]) not in installed:
            raise Hold("VERIFIER_NOT_INSTALLED", "A target registry must pin the verifier revision")
        profile = store.get(scope, "verifier-profile", criterion["verifier_ref"])
        contracts.validate("verifier-profile", profile)
        if (
            binding["environment_ref"] != profile["environment_ref"]
            or digest(binding["environment_ref"]) not in environments
        ):
            raise Hold(
                "VERIFIER_ENVIRONMENT", "Plan, installed verifier and app environment must agree"
            )
        if not binding["subject_selector"].strip() or not binding["decision_rule"].strip():
            raise Hold(
                "VERIFIER_RULE", "A verifier requires an observable subject and decision rule"
            )
        if not set(criterion["required_evidence_types"]) <= set(binding["required_evidence_types"]):
            raise Hold("VERIFICATION_EVIDENCE", "A plan cannot weaken required evidence types")
        if criterion["mandatory"] and (
            not binding["independent_review"] or not profile["protected"]
        ):
            raise Hold(
                "VERIFIER_INDEPENDENCE",
                "Mandatory criteria require protected independent verification",
            )
        if criterion["facet"] == "safety" and profile["kind"] in {
            "model_assisted",
            "artifact_visual",
        }:
            raise Hold("SAFETY_SCORER", "A subjective scorer cannot be the sole safety gate")
        if criterion["human_acceptance_required"] and profile["kind"] != "human":
            raise Hold(
                "HUMAN_BINDING", "Human acceptance needs an explicitly installed human verifier"
            )
