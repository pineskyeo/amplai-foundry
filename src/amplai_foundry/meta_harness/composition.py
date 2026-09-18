"""Immutable component composition and policy-filtered routing before ranking."""

from __future__ import annotations

from amplai_foundry.runtime.contracts.semantics import check_refs
from amplai_foundry.runtime.errors import Hold, RuntimeFault

PROTECTED_FIELDS = frozenset({"verification_policy_ref", "protocol_major"})
MUTABLE_A = frozenset({"prompt_bundle_ref", "router_policy_ref", "context_policy_ref"})
MUTABLE_B = frozenset(
    {
        "model_profile_ref",
        "driver_profile_ref",
        "sandbox_profile_ref",
        "pack_refs",
        "budget_policy_ref",
        "qualification_ref",
    }
)


class CompositionService:
    def __init__(self, store, contracts):
        self.store, self.contracts = store, contracts

    def register(self, actor, value: dict) -> dict:
        actor.require("harness.propose")
        self.contracts.validate("harness-composition", value)
        check_refs(self.store, actor.scope, value)
        model = self.store.get(actor.scope, "model-profile", value["model_profile_ref"])
        driver = self.store.get(actor.scope, "driver-capabilities", value["driver_profile_ref"])
        if model["driver_profile_ref"] != value["driver_profile_ref"]:
            raise Hold("COMPOSITION_DRIVER", "Model is qualified for a different driver")
        if model["model_version_policy"] != "pinned":
            raise Hold(
                "MODEL_VERSION_POLICY", "Autonomous composition requires a pinned model snapshot"
            )
        if not model["enabled"] or driver["maturity"] != "qualified":
            raise Hold(
                "COMPOSITION_UNQUALIFIED",
                "Composition contains a disabled model or unqualified driver",
            )
        with self.store.tx() as db:
            return self.store.put(
                db,
                actor.scope,
                "harness-composition",
                value["composition_id"],
                value["revision"],
                value,
            )

    def classify(self, scope, baseline_ref: dict, candidate_ref: dict) -> dict:
        baseline = self.store.get(scope, "harness-composition", baseline_ref)
        candidate = self.store.get(scope, "harness-composition", candidate_ref)
        ignored = {"composition_id", "revision", "created_at"}
        changed = {
            k
            for k in set(baseline) | set(candidate)
            if k not in ignored and baseline.get(k) != candidate.get(k)
        }
        if changed & PROTECTED_FIELDS:
            surface = "C"
        elif changed - MUTABLE_A - MUTABLE_B:
            surface = "D"
        elif changed & MUTABLE_B:
            surface = "B"
        else:
            surface = "A"
        return {
            "surface_class": surface,
            "changed_fields": sorted(changed),
            "protected": sorted(changed & PROTECTED_FIELDS),
        }

    def select(
        self,
        scope,
        candidates: list[dict],
        *,
        classification: str,
        required_actions: set[str],
        scores: dict[str, float] | None = None,
    ) -> dict:
        import math

        if scores is not None and (
            not isinstance(scores, dict)
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in scores.values())
        ):
            raise RuntimeFault(
                "COMPOSITION_SCORE",
                "Ranking scores must be finite numbers; security eligibility is evaluated first",
            )
        eligible = []
        for ref in candidates:
            composition = self.store.get(scope, "harness-composition", ref)
            model = self.store.get(scope, "model-profile", composition["model_profile_ref"])
            driver = self.store.get(scope, "driver-capabilities", composition["driver_profile_ref"])
            if (
                model["enabled"]
                and classification in model["data_classes_allowed"]
                and driver["maturity"] == "qualified"
                and required_actions <= set(driver["qualified"])
            ):
                eligible.append(ref)
        if not eligible:
            raise Hold(
                "NO_COMPOSITION",
                "No qualified composition satisfies security and capability requirements",
            )
        return sorted(
            eligible, key=lambda ref: (-(scores or {}).get(ref["digest"], 0), ref["digest"])
        )[0]
