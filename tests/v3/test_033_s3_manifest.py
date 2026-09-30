"""Work 033 S3 (D-096, interfaces.md §2.3, §3.1-3.2): manifests map onto the composition refs.

Real: the store, `ManifestService`, `ComponentService`, the runtime readers of
`runtime/execution/policies.py`, and (rig tests) the installed local product compositions. A
manifest is carried by the records the existing 3.0.0 refs point to; a legacy composition whose
context and budget refs name the shared `policy` record reads as the v1 manifest.
"""

from __future__ import annotations

import copy
import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness.components import ComponentService
from amplai_foundry.meta_harness.manifest import Manifest, ManifestService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution import policies, prompts, releases
from amplai_foundry.runtime.execution.codex import put_record
from amplai_foundry.runtime.execution.product import ROUTER_ORDER, Budget
from amplai_foundry.runtime.storage.store import Scope, Store
from rc06_rig import build_rig

SCOPE = Scope("tenant-s3", "project-s3")
ADMIN = Actor("installer", SCOPE, frozenset({"runtime.admin"}), "service")
PROPOSER = Actor("meta-proposer", SCOPE, frozenset({"harness.propose"}), "service")
CEILING = Budget().wire()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    value = Store(tmp_path / "state")
    yield value
    value.close()


@pytest.fixture
def manifests(store: Store) -> ManifestService:
    return ManifestService(store, SCOPE, Contracts(), ComponentService(store, SCOPE))


def prompt_ref(store: Store) -> dict[str, Any]:
    return put_record(
        store,
        SCOPE,
        Contracts(),
        prompts.KIND,
        prompts.BASELINE_ID,
        prompts.bundle(prompts.BASELINE_ID, prompts.IMPLEMENTER_BASELINE, "test"),
    )


def baseline(manifests: ManifestService) -> Manifest:
    return manifests.baseline(
        ADMIN, "app", prompt_bundle_ref=prompt_ref(manifests.store), route_order=list(ROUTER_ORDER)
    )


def version(manifests: ManifestService, kind: str, name: str = "t", **fields: Any) -> Any:
    """A proposer version of ``kind`` = v1 content with ``fields`` changed."""
    return manifests.components.register(
        PROPOSER,
        component_id=f"{kind}.{name}",
        kind=kind,
        content={**copy.deepcopy(policies.V1[kind]), **fields},
        source="proposer",
        rationale="test version",
    )


# ---------------------------------------------------------------- write


def test_write_puts_content_addressed_carriers_and_is_idempotent(
    manifests: ManifestService, store: Store
) -> None:
    manifest = baseline(manifests)
    refs = manifests.write(ADMIN, manifest, app_id="app")
    assert set(refs) == {
        "context_policy_ref",
        "budget_policy_ref",
        "router_policy_ref",
        "manifest_ref",
    }
    assert refs["context_policy_ref"]["id"].startswith("ctx-")
    assert refs["budget_policy_ref"]["id"].startswith("bud-")
    assert refs["manifest_ref"]["id"].startswith("manifest-")
    assert len(refs["context_policy_ref"]["id"]) == len("ctx-") + 24
    kind, ctx = resolve_ref(store, SCOPE, refs["context_policy_ref"])
    assert kind == "context-policy" and ctx["schema"] == "amplai.context-policy.v1"
    assert ctx["components"] == {s: manifest.context[s] for s in policies.CONTEXT_SLOTS}
    assert ctx["deciders"] == {"L4": None}
    _, bud = resolve_ref(store, SCOPE, refs["budget_policy_ref"])
    assert bud["components"]["driver_options"] is None  # v1: argv unchanged
    assert bud["deciders"] == {"L5": None, "L6": None, "L7": None, "L8": None}
    _, index = resolve_ref(store, SCOPE, refs["manifest_ref"])
    assert index["components"] == manifest.flat() and list(index["components"]) == sorted(
        index["components"]
    )
    assert "decider.L4" in index["components"] and "role_prompt" in index["components"]
    # idempotent: the same manifest gives the same records, nothing re-appended
    assert manifests.write(ADMIN, baseline(manifests), app_id="app") == refs


def test_the_installed_router_keeps_its_id_and_candidates_are_content_addressed(
    manifests: ManifestService, store: Store
) -> None:
    base = baseline(manifests)
    router = manifests.write(ADMIN, base, app_id="app")["router_policy_ref"]
    assert router["id"] == "app-router" and router["revision"] == 1
    _, value = resolve_ref(store, SCOPE, router)
    assert value["kind"] == "layered_v1" and value["policy_id"] == "app-router"
    assert value["deciders"] == {"L1": None, "L2": None, "L3": None}
    ask = version(manifests, "interpretation", planner_instruction="ask_first")
    candidate = manifests.write(PROPOSER, manifests.change(base, interpretation=ask), app_id="app")
    assert candidate["router_policy_ref"]["id"].startswith("router-")
    # the baseline router was not re-appended by the candidate
    assert [
        r["revision"]
        for r, _ in store.list_objects(SCOPE, "router-policy")
        if r["id"] == "app-router"
    ] == [1]


def test_writing_needs_an_installer_or_a_proposer(manifests: ManifestService) -> None:
    base = baseline(manifests)
    with pytest.raises(RuntimeFault) as denied:
        manifests.write(Actor("x", SCOPE, frozenset(), "service"), base, app_id="app")
    assert denied.value.code == "FORBIDDEN"


# ---------------------------------------------------------------- combination rules (§2.3)


@pytest.mark.parametrize(
    ("attempt", "header", "limit_attempts", "ok"),
    [
        ({"max_attempts": 3}, "v1", 2, False),  # attempts above limits
        ({"max_attempts": 2}, "v1", 2, True),
        ({"repair_base": "fresh_base"}, "v1", 3, False),  # fresh base + feedback + v1 header
        ({"repair_base": "fresh_base"}, "v1_fresh", 3, True),
        ({"repair_base": "fresh_base", "feedback": False}, "v1", 3, True),  # header never shown
        ({"repair_base": "previous_patch"}, "v1_fresh", 3, False),
    ],
)
def test_manifest_combination(
    manifests: ManifestService, attempt: dict[str, Any], header: str, limit_attempts: int, ok: bool
) -> None:
    base = baseline(manifests)
    changed = manifests.change(
        base,
        attempt_policy=version(manifests, "attempt_policy", **attempt),
        feedback_form=version(manifests, "feedback_form", header=header),
        limits=version(manifests, "limits", max_attempts=limit_attempts),
    )
    if ok:
        manifests.write(PROPOSER, changed, app_id="app")
        return
    with pytest.raises(RuntimeFault) as bad:
        manifests.write(PROPOSER, changed, app_id="app")
    assert bad.value.code == "MANIFEST_COMBINATION"


# ---------------------------------------------------------------- change, diff, class


def test_change_refuses_unknown_slots_and_kind_mismatches(manifests: ManifestService) -> None:
    base = baseline(manifests)
    notes = version(manifests, "memory_notes", enabled=True)
    method = manifests.components.register(
        PROPOSER,
        component_id="decision_method.v1",
        kind="decision_method",
        content={
            "features": ["strategy"],
            "estimator": "pooled_beta_binomial_v1",
            "selection": "noninferior_then_cheapest_v1",
            "fallback": "prior_v1",
            "utility_lambda": None,
        },
        source="proposer",
        rationale="test",
    )
    l4 = manifests.components.register(
        PROPOSER,
        component_id="decider.l4",
        kind="decider",
        content={
            "layer": "L4",
            "method": method,
            "table": None,
            "judge": None,
            "options": None,
            "policy": {"min_samples": 5, "margin": 0.1, "pooling_strength": 4},
        },
        source="proposer",
        rationale="test",
    )
    bad: list[dict[str, Any]] = [
        {"tools": notes},  # unknown slot
        {"env_bootstrap": notes},  # a memory_notes version in the env_bootstrap slot
        {"feedback_form": None},  # a required slot
        {"L6": l4},  # an L4 decider in the L6 slot
        {"prompt_bundle_ref": notes},  # not a prompt bundle
        {"retrieval": {"id": "nothing", "revision": 1, "digest": "sha256:" + "0" * 64}},
    ]
    for slots in bad:
        with pytest.raises(RuntimeFault) as fault:
            manifests.change(base, **slots)
        assert fault.value.code == "MANIFEST_SLOT", slots
    changed = manifests.change(base, env_bootstrap=None, memory_notes=notes, L4=l4)
    assert changed.context["env_bootstrap"] is None and changed.context["L4"] == l4
    assert base.context["memory_notes"] != notes  # the base is unchanged (frozen)


def test_diff_and_surface_class(manifests: ManifestService, store: Store) -> None:
    base = baseline(manifests)
    on = version(manifests, "env_bootstrap", enabled=True)
    short = version(manifests, "feedback_form", tail_chars=500)
    a_only = manifests.diff(base, manifests.change(base, env_bootstrap=on))
    assert [(c.slot, c.kind, c.surface_class) for c in a_only] == [
        ("env_bootstrap", "env_bootstrap", "A")
    ]
    assert a_only[0].before == base.context["env_bootstrap"] and a_only[0].after == on
    assert manifests.surface_class(a_only) == "A"
    both = manifests.diff(base, manifests.change(base, env_bootstrap=on, feedback_form=short))
    assert [c.slot for c in both] == ["env_bootstrap", "feedback_form"]
    assert manifests.surface_class(both) == "B"
    bundle = put_record(
        store,
        SCOPE,
        Contracts(),
        prompts.KIND,
        "implementer-x",
        prompts.bundle("implementer-x", ["Implement it in {app_id}."], "t"),
    )
    prompt = manifests.diff(base, manifests.change(base, prompt_bundle_ref=bundle))
    assert [(c.slot, c.kind, c.surface_class) for c in prompt] == [
        ("prompt_bundle_ref", "role_prompt", "A")
    ]
    assert manifests.diff(base, base) == [] and manifests.surface_class([]) == "A"


# ---------------------------------------------------------------- runtime readers (§3.1)


def _carriers(manifests: ManifestService, manifest: Manifest) -> dict[str, Any]:
    refs = manifests.write(PROPOSER, manifest, app_id="app")
    return {k: refs[k] for k in ("context_policy_ref", "budget_policy_ref", "router_policy_ref")}


def test_readers_return_the_component_contents(manifests: ManifestService, store: Store) -> None:
    base = baseline(manifests)
    on = version(manifests, "env_bootstrap", enabled=True, tree_depth=1)
    limits = version(manifests, "limits", max_wall_seconds=900, aux_max_tokens=1000)
    composition = _carriers(manifests, manifests.change(base, env_bootstrap=on, limits=limits))
    context = policies.context_policy(store, SCOPE, composition)
    assert context.env_bootstrap == {
        **policies.V1["env_bootstrap"],
        "enabled": True,
        "tree_depth": 1,
    }
    assert context.feedback_form == policies.V1["feedback_form"] and not context.is_v1
    assert context.refs["env_bootstrap"] == on and context.refs["L4"] is None
    budget = policies.budget_policy(store, SCOPE, composition, ceiling=CEILING)
    assert budget.limits["max_wall_seconds"] == 900 and budget.refs["limits"] == limits
    assert budget.attempt_policy == policies.V1["attempt_policy"] and budget.driver_options is None
    router = policies.router_policy(store, SCOPE, composition["router_policy_ref"])
    assert router.order == {"*": list(ROUTER_ORDER)} and router.roles == {}
    assert router.interpretation == policies.V1["interpretation"]
    assert router.deciders == {"L1": None, "L2": None, "L3": None}
    # the baseline carriers read as the v1 policies
    v1 = _carriers(manifests, base)
    assert policies.context_policy(store, SCOPE, v1).is_v1
    read = policies.budget_policy(store, SCOPE, v1, ceiling=CEILING)
    assert read == dataclasses.replace(policies.v1_budget(CEILING), refs=read.refs)


def test_a_legacy_policy_carrier_reads_as_v1(store: Store) -> None:
    legacy = put_record(
        store,
        SCOPE,
        Contracts(),
        "policy",
        "app-policy",
        {
            "policy_id": "app-policy",
            "scope": SCOPE.wire(),
            "production": False,
            "requested_ceiling": [],
            "classification": "internal",
        },
    )
    composition = {"context_policy_ref": legacy, "budget_policy_ref": legacy}
    context = policies.context_policy(store, SCOPE, composition)
    assert context.is_v1 and context == policies.v1_context()
    small = {**CEILING, "max_tokens": 1000}
    budget = policies.budget_policy(store, SCOPE, composition, ceiling=small)
    # a legacy goal runs within the deployment ceiling, as before carriers existed
    assert budget.limits == {
        "max_wall_seconds": 1800,
        "max_tokens": 1000,
        "max_attempts": 3,
        "aux_max_tokens": 0,
    }
    # a stand-in composition value without the field (the golden test's) reads as v1 too
    assert policies.context_policy(store, SCOPE, {}).is_v1


def test_limits_above_the_ceiling_hold(manifests: ManifestService, store: Store) -> None:
    composition = _carriers(manifests, baseline(manifests))
    with pytest.raises(Hold) as held:
        policies.budget_policy(
            store, SCOPE, composition, ceiling={**CEILING, "max_wall_seconds": 600}
        )
    assert held.value.code == "LIMITS_ABOVE_CEILING"
    assert held.value.details == {"max_wall_seconds": {"limit": 1800, "ceiling": 600}}


def test_a_carrier_of_another_kind_holds(manifests: ManifestService, store: Store) -> None:
    carriers = _carriers(manifests, baseline(manifests))
    bundle = prompt_ref(store)
    cases = [
        {"context_policy_ref": carriers["budget_policy_ref"]},
        {"context_policy_ref": bundle},
    ]
    for composition in cases:
        with pytest.raises(Hold) as held:
            policies.context_policy(store, SCOPE, composition)
        assert held.value.code == "CARRIER_KIND"
    with pytest.raises(Hold) as budget:
        policies.budget_policy(store, SCOPE, {"budget_policy_ref": bundle}, ceiling=CEILING)
    assert budget.value.code == "CARRIER_KIND"
    with pytest.raises(Hold) as router:
        policies.router_policy(store, SCOPE, carriers["context_policy_ref"])
    assert router.value.code == "CARRIER_KIND"
    # a hand-written carrier whose slot names a component of another kind
    notes = version(manifests, "memory_notes")
    _, ctx = resolve_ref(store, SCOPE, carriers["context_policy_ref"])
    forged = put_record(
        store,
        SCOPE,
        Contracts(),
        "context-policy",
        "ctx-forged",
        {**ctx, "components": {**ctx["components"], "env_bootstrap": notes}},
    )
    with pytest.raises(Hold) as slot:
        policies.context_policy(store, SCOPE, {"context_policy_ref": forged})
    assert slot.value.code == "CARRIER_KIND"


def test_a_legacy_router_reads_as_route_policy_v1(store: Store) -> None:
    legacy = put_record(
        store,
        SCOPE,
        Contracts(),
        "router-policy",
        "app-router",
        {
            "policy_id": "app-router",
            "scope": SCOPE.wire(),
            "kind": "task_class_baseline",
            "order": {"*": list(ROUTER_ORDER)},
            "source": "D-079",
        },
    )
    router = policies.router_policy(store, SCOPE, legacy)
    assert router.order == {"*": list(ROUTER_ORDER)} and router.roles == {}
    assert router.interpretation == policies.V1["interpretation"] and router.ref == legacy
    assert set(router.deciders.values()) == {None}


def test_root_and_node_budgets() -> None:
    v1 = policies.v1_budget(CEILING)
    root = policies.root_budget(CEILING, v1.limits)
    assert root == CEILING  # v1: the deployment Budget, as product.py:840 did
    node = policies.node_budget(root, v1.attempt_policy, v1.limits, 2)
    # today's rule (product.py:1033-1036): root tokens // (root attempts x nodes)
    assert node == {**root, "max_tokens": root["max_tokens"] // (root["max_attempts"] * 2)}
    limits = {
        "max_wall_seconds": 900,
        "max_tokens": 30_000_000,
        "max_attempts": 2,
        "aux_max_tokens": 3_000_000,
    }
    root = policies.root_budget(CEILING, limits)
    assert (root["max_wall_seconds"], root["max_tokens"], root["max_attempts"]) == (
        900,
        30_000_000,
        2,
    )
    assert root["max_parallel_works"] == 1 and root["currency"] == "USD"
    node = policies.node_budget(root, {"max_attempts": 1}, limits, 3)
    assert node["max_attempts"] == 1 and node["max_tokens"] == (30_000_000 - 3_000_000) // 3


# ---------------------------------------------------------------- installed compositions (rig)


def test_the_installed_composition_reads_as_the_baseline_manifest(
    deployment: Any, tmp_path: Path
) -> None:
    rig = build_rig(deployment, tmp_path)
    d, installed = rig.d, rig.service.apps["app"]
    service = ManifestService(d.store, d.scope, d.contracts, ComponentService(d.store, d.scope))
    manifest = service.of_composition(installed.compositions["codex-cli"])
    composition = d.store.get(d.scope, "harness-composition", installed.compositions["codex-cli"])
    assert manifest.prompt_bundle_ref == composition["prompt_bundle_ref"]
    assert all(manifest.context[s] for s in policies.CONTEXT_SLOTS)  # baseline versions
    assert manifest.budget["driver_options"] is None and manifest.router["L1"] is None
    assert composition["router_policy_ref"] == installed.router_ref
    assert installed.router_ref["id"] == "app-router"
    # a legacy composition (shared policy record, legacy router) reads as the same manifest
    legacy_router = rig.service._put(
        "router-policy",
        "legacy-router",
        {
            "policy_id": "legacy-router",
            "scope": d.scope.wire(),
            "kind": "task_class_baseline",
            "order": {"*": list(ROUTER_ORDER)},
            "source": "D-079",
        },
    )
    legacy = rig.service._put(
        "harness-composition",
        "app-codex-legacy",
        {
            **composition,
            "composition_id": "app-codex-legacy",
            "revision": 1,
            "context_policy_ref": installed.policy_ref,
            "budget_policy_ref": installed.policy_ref,
            "router_policy_ref": legacy_router,
        },
    )
    assert service.of_composition(legacy) == manifest


def test_materialize_registers_a_candidate_with_the_changed_carriers_only(
    deployment: Any, tmp_path: Path
) -> None:
    rig = build_rig(deployment, tmp_path)
    d, installed = rig.d, rig.service.apps["app"]
    proposer = Actor("meta-proposer", d.scope, frozenset({"harness.propose"}), "service")
    service = ManifestService(d.store, d.scope, d.contracts, ComponentService(d.store, d.scope))
    base_ref = installed.compositions["codex-cli"]
    base = d.store.get(d.scope, "harness-composition", base_ref)
    on = service.components.register(
        proposer,
        component_id="env_bootstrap.on",
        kind="env_bootstrap",
        content={**policies.V1["env_bootstrap"], "enabled": True},
        source="proposer",
        rationale="facts on",
    )
    manifest = service.change(service.of_composition(base_ref), env_bootstrap=on)
    ref = service.materialize(
        proposer, base_composition_ref=base_ref, manifest=manifest, suffix="s3m"
    )
    candidate = d.store.get(d.scope, "harness-composition", ref)
    assert candidate["composition_id"] == "app-codex__s3m"
    assert candidate["context_policy_ref"] != base["context_policy_ref"]
    # untouched groups keep the base's carriers; the verification policy is never touched
    for field in (
        "budget_policy_ref",
        "router_policy_ref",
        "verification_policy_ref",
        "model_profile_ref",
        "driver_profile_ref",
        "prompt_bundle_ref",
    ):
        assert candidate[field] == base[field], field
    assert service.of_composition(ref) == manifest
    # the pin rule accepts it as the codex composition's candidate
    assert releases.class_a_driver(d.store, d.scope, installed.compositions, ref) == "codex-cli"
    with pytest.raises(RuntimeFault) as denied:
        service.materialize(
            Actor("installer", d.scope, frozenset({"runtime.admin"}), "service"),
            base_composition_ref=base_ref,
            manifest=manifest,
            suffix="x",
        )
    assert denied.value.code == "FORBIDDEN"
    # without a suffix the id is the base's: the same manifest is the base, another is refused
    assert (
        service.materialize(
            proposer,
            base_composition_ref=base_ref,
            manifest=service.of_composition(base_ref),
            suffix=None,
        )
        == base_ref
    )
    with pytest.raises(Conflict) as overwrite:
        service.materialize(proposer, base_composition_ref=base_ref, manifest=manifest, suffix=None)
    assert overwrite.value.code == "IMMUTABLE_REVISION"
