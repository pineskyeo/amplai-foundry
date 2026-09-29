"""Work 030 S2 (D-089) — the active release pointer drives composition selection.

A baseline release of the installed compositions is booted; a promoted release carrying a class-A
candidate (``<composition_id>__<suffix>``, same driver/model/sandbox) is used by new plans, a
plan made earlier keeps its composition, and rollback returns to the baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from amplai_foundry.runtime.execution import prompts, releases
from rc06_rig import approved, rig_with_codex, submit

KEY = Ed25519PrivateKey.generate()


def boot(rig: Any) -> dict[str, Any]:
    d = rig.d
    return releases.bootstrap(
        d.store, d.scope, rig.service.runtime.contracts,
        [ref for app in rig.service.apps.values() for ref in app.compositions.values()],
        signer=KEY, key_id="local-authority",
    )  # fmt: skip


def candidate(rig: Any, suffix: str = "cand", **change: Any) -> dict[str, Any]:
    d, service = rig.d, rig.service
    base_ref = service.apps["app"].compositions["codex-cli"]
    base = d.store.get(d.scope, "harness-composition", base_ref)
    bundle = service._put(
        prompts.KIND, f"implementer-{suffix}",
        prompts.bundle(f"implementer-{suffix}", ["Implement it in {app_id}."], "test"),
    )  # fmt: skip
    value = {**base, "composition_id": base["composition_id"] + "__" + suffix,
             "prompt_bundle_ref": bundle, **change}  # fmt: skip
    ref: dict[str, Any] = service._put("harness-composition", value["composition_id"], value)
    return ref


def activate(rig: Any, components: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """What meta_harness promote does to the pointer (CAS), without its gates."""
    d = rig.d
    release = releases.build(
        d.store, d.scope, rig.service.runtime.contracts, name, components,
        signer=KEY, key_id="local-authority", basis="test",
    )  # fmt: skip
    with d.store.tx() as db:
        head = d.store.head(d.scope, "release-pointer", "active", db=db)
        d.store.cas(db, d.scope, "release-pointer", "active", head["row_version"], "active",
                    {"release_ref": release})  # fmt: skip
    return release


def selected(rig: Any) -> dict[str, Any]:
    ref: dict[str, Any] = rig.service.select_composition(rig.service.apps["app"])["ref"]
    return ref


def test_the_baseline_is_active_and_selection_is_unchanged(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    before = selected(rig)
    active = boot(rig)
    assert active["id"].startswith(releases.BASELINE_PREFIX)
    assert selected(rig) == before
    assert boot(rig) == active  # a restart with the same installation changes nothing


def test_a_promoted_candidate_serves_new_plans_and_rollback_returns(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    baseline_release = boot(rig)
    baseline = selected(rig)
    earlier = approved(rig)  # planned and approved on the baseline
    cand = candidate(rig)
    promoted = activate(rig, [cand], "local-candidate-1")
    assert selected(rig) == cand
    # the earlier goal keeps the composition fixed in its plan
    assert rig.service.plan_record(earlier)["composition"]["ref"] == baseline
    later = submit(rig, "make value return 2 later")
    assert rig.service.plan(later)["composition"]["ref"] == cand
    # a restart keeps a promoted release active
    assert boot(rig) == promoted
    with rig.d.store.tx() as db:  # rollback: the pointer back to the baseline
        head = rig.d.store.head(rig.d.scope, "release-pointer", "active", db=db)
        rig.d.store.cas(db, rig.d.scope, "release-pointer", "active", head["row_version"],
                        "active", {"release_ref": baseline_release})  # fmt: skip
    assert selected(rig) == baseline


def test_only_a_derived_class_a_candidate_can_replace_a_composition(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    boot(rig)
    baseline = selected(rig)
    d = rig.d
    base = d.store.get(d.scope, "harness-composition", baseline)
    other_model = candidate(rig, "model", model_profile_ref=base["driver_profile_ref"])
    unrelated = rig.service._put(
        "harness-composition", "someone-else",
        {**base, "composition_id": "someone-else", "prompt_bundle_ref": base["prompt_bundle_ref"]},
    )  # fmt: skip
    activate(rig, [other_model, unrelated], "local-candidate-bad")
    assert selected(rig) == baseline  # a model change or another composition is ignored
