"""Work 033 S3 (D-096, interfaces.md §3.2): `CompositionService.classify` is component-aware.

Real: the installed local product compositions (rc06 rig), `ManifestService.materialize`, the
composition classifier the meta-harness gates use (`MetaHarness.submit`/`screen`). The rules:
protected field -> C; a field outside MUTABLE_A/B -> D; only carrier fields -> the highest class
among the changed components (env bootstrap, memory notes, role prompt, interpretation are A;
feedback form, attempt policy, route policy are B); any other field -> B. A prompt-only change
stays class A, as the Work 030 proposals were (`runtime/execution/meta_ops.py:115`).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_rc06_local_product import product  # noqa: F401  (fixture)

from amplai_foundry.meta_harness.components import ComponentService
from amplai_foundry.meta_harness.composition import CompositionService
from amplai_foundry.meta_harness.manifest import ManifestService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.identity import canonical, new_id
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import policies, prompts
from rc06_rig import build_rig, claude_inputs, install_codex_profile

PROPOSER_PERMISSIONS = frozenset({"harness.propose"})


class Classifier:
    def __init__(self, deployment: Any, tmp_path: Path) -> None:
        self.rig = build_rig(deployment, tmp_path)
        d = self.d = self.rig.d
        self.proposer = Actor("meta-proposer", d.scope, PROPOSER_PERMISSIONS, "service")
        self.manifests = ManifestService(
            d.store, d.scope, d.contracts, ComponentService(d.store, d.scope)
        )
        self.compositions = CompositionService(d.store, d.contracts)
        self.base_ref = self.rig.service.apps["app"].compositions["codex-cli"]
        self.base = d.store.get(d.scope, "harness-composition", self.base_ref)

    def version(self, kind: str, **fields: Any) -> Any:
        return self.manifests.components.register(
            self.proposer,
            component_id=f"{kind}.{new_id('v')[-8:]}",
            kind=kind,
            content={**copy.deepcopy(policies.V1[kind]), **fields},
            source="proposer",
            rationale="classify test",
        )

    def candidate(self, **slots: Any) -> Any:
        manifest = self.manifests.change(self.manifests.of_composition(self.base_ref), **slots)
        return self.manifests.materialize(
            self.proposer,
            base_composition_ref=self.base_ref,
            manifest=manifest,
            suffix=new_id("c")[-10:],
        )

    def raw(self, **fields: Any) -> Any:
        """A composition put directly (classify reads records; register checks are not the
        subject here)."""
        name = "app-codex__" + new_id("r")[-10:]
        return self.rig.service._put(
            "harness-composition", name, {**self.base, "composition_id": name, **fields}
        )

    def classify(self, ref: Any) -> dict[str, Any]:
        result: dict[str, Any] = self.compositions.classify(self.d.scope, self.base_ref, ref)
        return result


@pytest.fixture
def c(deployment: Any, tmp_path: Path) -> Classifier:
    return Classifier(deployment, tmp_path)


def _kinds(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(x["kind"], x["surface_class"]) for x in result["changed_components"]]


def test_a_prompt_only_change_stays_class_a(c: Classifier) -> None:
    bundle = c.rig.service._put(
        prompts.KIND,
        "implementer-cand",
        prompts.bundle("implementer-cand", ["Implement it in {app_id}."], "test"),
    )
    result = c.classify(c.raw(prompt_bundle_ref=bundle))
    assert result["surface_class"] == "A"
    assert result["changed_fields"] == ["prompt_bundle_ref"]
    assert _kinds(result) == [("role_prompt", "A")]
    change = result["changed_components"][0]
    assert change["before"] == c.base["prompt_bundle_ref"] and change["after"] == bundle


@pytest.mark.parametrize(
    ("slot", "fields", "surface"),
    [
        ("env_bootstrap", {"enabled": True}, "A"),
        ("memory_notes", {"enabled": True}, "A"),
        ("interpretation", {"planner_instruction": "ask_first"}, "A"),
        ("feedback_form", {"tail_chars": 500}, "B"),
        ("attempt_policy", {"max_attempts": 1}, "B"),
        ("route_policy", {"order": {"*": ["claude-cli", "codex-cli"]}}, "B"),
        ("retrieval", {"max_items": 5}, "B"),
        ("fast_checks", {"max_followups": 1}, "B"),
        ("limits", {"max_wall_seconds": 900}, "B"),
    ],
)
def test_a_carrier_change_takes_the_class_of_its_component(
    c: Classifier, slot: str, fields: dict[str, Any], surface: str
) -> None:
    result = c.classify(c.candidate(**{slot: c.version(slot, **fields)}))
    assert result["surface_class"] == surface
    assert _kinds(result) == [(slot, surface)]
    assert len(result["changed_fields"]) == 1  # only the carrier that holds the slot


def test_the_highest_class_among_the_changed_components_wins(c: Classifier) -> None:
    result = c.classify(
        c.candidate(
            env_bootstrap=c.version("env_bootstrap", enabled=True),
            attempt_policy=c.version("attempt_policy", max_attempts=2),
        )
    )
    assert result["surface_class"] == "B"
    assert sorted(_kinds(result)) == [("attempt_policy", "B"), ("env_bootstrap", "A")]
    assert result["changed_fields"] == ["budget_policy_ref", "context_policy_ref"]


def test_non_carrier_and_protected_fields_keep_their_rules(c: Classifier, tmp_path: Path) -> None:
    d = c.d
    claude = install_codex_profile(
        d.store, d.scope, claude_inputs(tmp_path), c.rig.service.apps["app"].capabilities
    )
    model = c.classify(
        c.raw(model_profile_ref=claude["model"], driver_profile_ref=claude["driver"])
    )
    assert model["surface_class"] == "B" and model["changed_components"] == []
    # a model change together with an A component is still B (rule 4)
    mixed = c.raw(
        model_profile_ref=claude["model"],
        driver_profile_ref=claude["driver"],
        prompt_bundle_ref=c.rig.service._put(
            prompts.KIND,
            "implementer-m",
            prompts.bundle("implementer-m", ["Do it in {app_id}."], "t"),
        ),
    )
    assert c.classify(mixed)["surface_class"] == "B"
    protected = c.rig.service._put("policy", "other-policy", {"policy_id": "other-policy"})
    result = c.classify(c.raw(verification_policy_ref=protected))
    assert result["surface_class"] == "C" and result["protected"] == ["verification_policy_ref"]


def test_legacy_carriers_with_the_same_components_are_no_component_change(c: Classifier) -> None:
    installed = c.rig.service.apps["app"]
    legacy = c.raw(context_policy_ref=installed.policy_ref, budget_policy_ref=installed.policy_ref)
    result = c.classify(legacy)
    assert result["changed_fields"] == ["budget_policy_ref", "context_policy_ref"]
    assert result["changed_components"] == [] and result["surface_class"] == "A"


def test_an_unreadable_carrier_is_class_b_never_a(c: Classifier) -> None:
    # a record of the carrier kind but not the §2.3 shape (tests/v3/test_dev03_meta_harness.py)
    budget = c.rig.service._put(
        "budget-policy",
        "budget-free-form",
        {"budget_id": "budget-free-form", "scope": c.d.scope.wire(), "intent": "lower limits"},
    )
    assert c.classify(c.raw(budget_policy_ref=budget))["surface_class"] == "B"
    context = c.rig.service._put(
        "context-policy",
        "context-free-form",
        {"scope": c.d.scope.wire(), "notes": "not a carrier"},
    )
    assert c.classify(c.raw(context_policy_ref=context))["surface_class"] == "B"


def _submit(dep: Any, kind: str, fields: dict[str, Any], declared: str) -> str:
    """A component proposal from the meta-proposer of the local product (as rc09 `draft`)."""
    d, service, meta = dep, dep.service, dep.meta_local
    proposer = meta.proposer
    base_ref = service.apps["app"].compositions["codex-cli"]
    manifests = ManifestService(
        d.store, d.scope, d.runtime.contracts, ComponentService(d.store, d.scope)
    )
    version = manifests.components.register(
        proposer,
        component_id=f"{kind}.s3",
        kind=kind,
        content={**copy.deepcopy(policies.V1[kind]), **fields},
        source="proposer",
        rationale="submit test",
    )
    candidate = manifests.materialize(
        proposer,
        base_composition_ref=base_ref,
        suffix="s3" + kind[:3],
        manifest=manifests.change(manifests.of_composition(base_ref), **{kind: version}),
    )
    observation = d.artifacts.admit(
        d.scope, b'{"issue": "x"}', "application/json", trust="verifier"
    )
    change = d.artifacts.admit(
        d.scope,
        canonical(
            {
                "changed_paths": [f"components/{kind}/{kind}.s3@1"],
                "baseline_ref": base_ref,
                "candidate_ref": candidate,
            }
        ),
        "application/json",
        trust="operator",
    )
    proposal = {
        "schema_version": "3.0.0",
        "proposal_id": new_id("harness-proposal"),
        "scope": d.scope.wire(),
        "baseline_ref": base_ref,
        "candidate_ref": candidate,
        "surface_class": declared,
        "hypothesis": "The component helps.",
        "observation_refs": [
            service._put(
                "harness-observation",
                new_id("observation"),
                {"observation_id": new_id("o"), "artifact": observation},
            )
        ],
        "change_artifact": change,
        "expected_benefit": "More passes",
        "risks": ["none known"],
        "protected_surface_findings": [],
        "experiment_plan_ref": service._put(
            "experiment-draft",
            new_id("exp-draft"),
            {"draft_id": new_id("d"), "comparison": "two arms"},
        ),
        "rollback_plan_ref": service._put(
            "rollback-plan",
            new_id("rollback"),
            {"rollback_id": new_id("r"), "target_release_ref": base_ref},
        ),
        "proposer": proposer.wire(),
        "status": "draft",
    }
    meta.meta.submit(proposer, proposal)
    return str(proposal["proposal_id"])


def test_the_meta_harness_submit_checks_the_declared_class(product: Any) -> None:  # noqa: F811
    """`MetaHarness.submit` compares the declared class with this classifier (service.py:124)."""
    dep, _client, _token = product
    assert _submit(dep, "env_bootstrap", {"enabled": True}, "A")
    assert _submit(dep, "feedback_form", {"tail_chars": 100}, "B")
    with pytest.raises(Hold) as wrong:
        _submit(dep, "attempt_policy", {"max_attempts": 2}, "A")  # a repair heuristic is B
    assert wrong.value.code == "SURFACE_CLASSIFICATION"
