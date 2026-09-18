"""V3-013 — Readiness and context completeness validator (design/12 §2-§3, T-015/016/017)."""

from __future__ import annotations

import pytest

from amplai_foundry.runtime.contracts.identity import new_id, now
from amplai_foundry.runtime.errors import Hold


def facts(d, n=1):
    return [d.knowledge.record_observation(d.scope, f"fact-{i}", f"text {i}") for i in range(n)]


def invariant_ref(d, p):
    contract = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    bundle = d.store.get(d.scope, "context-bundle", contract["context_bundle_ref"])
    return bundle["invariant_registry_ref"]


AREAS = [
    "terminology",
    "current_behavior",
    "boundary",
    "invariants",
    "ssot",
    "contradictions",
    "acceptance",
    "verifier",
]


def entries_for(refs, **overrides):
    out = []
    for area in AREAS:
        out.append(
            {
                "area": area,
                "status": "ready",
                "source_refs": refs,
                "reason": "Current source-backed evidence",
            }
        )
    for area, patch in overrides.items():
        next(e for e in out if e["area"] == area).update(patch)
    return out


def test_t015_eight_areas_must_be_unique_not_just_eight_rows(deployment):
    from amplai_foundry.knowledge_runtime.readiness import ReadinessValidator

    d = deployment
    refs = facts(d)
    rows = entries_for(refs)
    rows[1] = dict(rows[0])  # duplicate 'terminology', 'current_behavior' missing, still 8 rows
    assert len(rows) == 8
    with pytest.raises(Hold) as exc:
        ReadinessValidator(d.store, d.contracts).evaluate(
            d.scope, rows, invariant_registry_ref=invariant_ref(d, d.prepare())
        )
    assert exc.value.code == "READINESS_COMPLETENESS"
    assert exc.value.details["duplicates"] == ["terminology"]
    assert exc.value.details["missing"] == ["current_behavior"]


def test_t016_conflicting_active_decisions_hold_and_disclose_both(deployment):
    from amplai_foundry.knowledge_runtime.readiness import ReadinessValidator

    d = deployment
    p = d.prepare()
    refs = facts(d)
    with d.store.tx() as db:
        a = d.store.put(
            db,
            d.scope,
            "knowledge-canonical",
            "DEC-A",
            1,
            {
                "memory_id": "DEC-A",
                "kind": "decision",
                "status": "active",
                "statement": "writes allowed",
                "contradicts": ["DEC-B"],
                "superseded_by": None,
            },
        )
        b = d.store.put(
            db,
            d.scope,
            "knowledge-canonical",
            "DEC-B",
            1,
            {
                "memory_id": "DEC-B",
                "kind": "decision",
                "status": "active",
                "statement": "writes forbidden",
                "contradicts": [],
                "superseded_by": None,
            },
        )
    with pytest.raises(Hold) as exc:
        ReadinessValidator(d.store, d.contracts).evaluate(
            d.scope,
            entries_for(refs),
            invariant_registry_ref=invariant_ref(d, p),
            active_decision_refs=[a, b],
        )
    assert exc.value.code == "CONTEXT_NOT_READY"
    pair = exc.value.details["conflicts"][0]
    assert {pair["left"]["id"], pair["right"]["id"]} == {a["id"], b["id"]}
    assert "winner" not in pair


def test_ready_verdict_carries_invariant_completeness_marker(deployment):
    from amplai_foundry.knowledge_runtime.readiness import ReadinessValidator

    d = deployment
    p = d.prepare()
    result = ReadinessValidator(d.store, d.contracts).evaluate(
        d.scope, entries_for(facts(d)), invariant_registry_ref=invariant_ref(d, p)
    )
    assert result["verdict"] == "ready"
    assert result["invariant_discovery_complete"] is True
    assert result["areas"] == AREAS or sorted(result["areas"]) == sorted(AREAS)


def test_stale_invariant_registry_is_not_current(deployment):
    from amplai_foundry.knowledge_runtime.readiness import ReadinessValidator

    d = deployment
    p = d.prepare()
    old = invariant_ref(d, p)
    registry = d.store.get(d.scope, "invariant-registry", old)
    newer = d.put(
        "invariant-registry",
        old["id"],
        {**registry, "note": "revised"},
        revision=old["revision"] + 1,
    )
    assert newer["revision"] == old["revision"] + 1
    with pytest.raises(Hold) as exc:
        ReadinessValidator(d.store, d.contracts).evaluate(
            d.scope, entries_for(facts(d)), invariant_registry_ref=old
        )
    assert exc.value.code == "INVARIANTS_STALE"


def test_not_applicable_needs_rule_and_verifier_rule(deployment):
    from amplai_foundry.knowledge_runtime.readiness import ReadinessValidator

    d = deployment
    p = d.prepare()
    v = ReadinessValidator(d.store, d.contracts)
    rows = entries_for(
        facts(d),
        contradictions={
            "status": "not_applicable",
            "reason": "no-conflict-possible: single source",
            "source_refs": [],
        },
    )
    with pytest.raises(Hold) as exc:
        v.evaluate(
            d.scope,
            rows,
            invariant_registry_ref=invariant_ref(d, p),
            na_rules={"no-conflict-possible"},
        )
    assert exc.value.code == "READINESS_NA_RULE"
    rows = entries_for(
        facts(d),
        contradictions={
            "status": "not_applicable",
            "reason": "no-conflict-possible: single source",
            "source_refs": [],
            "verifier_rule": "assert len(sources)==1",
        },
    )
    assert (
        v.evaluate(
            d.scope,
            rows,
            invariant_registry_ref=invariant_ref(d, p),
            na_rules={"no-conflict-possible"},
        )["verdict"]
        == "ready"
    )


def test_t017_core_never_dropped_under_budget_pressure(deployment):
    d = deployment
    p = d.prepare()
    refs = facts(d)
    entry = {
        "ref": refs[0],
        "kind": "repo_fact",
        "trust": "observed",
        "mandatory": True,
        "freshness": "current",
        "superseded_by": None,
        "excerpt": "x" * 4000,
        "source_locator": "fact-0",
    }
    with pytest.raises(Hold) as exc:
        d.knowledge.bundle(
            d.scope,
            bundle_id=new_id("context"),
            core_refs=[invariant_ref(d, p)],
            entries=[entry],
            invariant_registry_ref=invariant_ref(d, p),
            token_budget=512,
            assembled_at=now(),
        )
    assert exc.value.code == "CORE_CONTEXT_BUDGET"


def test_t017_progressive_bundle_keeps_map_for_dropped_optional_excerpts(deployment):
    d = deployment
    p = d.prepare()
    refs = facts(d, 3)
    core = {
        "ref": refs[0],
        "kind": "repo_fact",
        "trust": "observed",
        "mandatory": True,
        "freshness": "current",
        "superseded_by": None,
        "excerpt": "core",
        "source_locator": "fact-0",
    }
    big = [
        {
            "ref": refs[i],
            "kind": "repo_fact",
            "trust": "observed",
            "mandatory": False,
            "freshness": "current",
            "superseded_by": None,
            "excerpt": "y" * 3000,
            "source_locator": f"fact-{i}",
        }
        for i in (1, 2)
    ]
    _ref, bundle = d.knowledge.progressive_bundle(
        d.scope,
        bundle_id=new_id("context"),
        core_refs=[invariant_ref(d, p)],
        entries=[core, *big],
        invariant_registry_ref=invariant_ref(d, p),
        token_budget=2500,
        assembled_at=now(),
    )
    kept = [e for e in bundle["entries"] if e["excerpt"] is not None]
    mapped = [e for e in bundle["entries"] if e["excerpt"] is None]
    assert [e["source_locator"] for e in kept] == ["fact-0"]
    assert {e["source_locator"] for e in mapped} == {"fact-1", "fact-2"}, (
        "dropped excerpts stay addressable in the map"
    )
    assert all(e["mandatory"] is False for e in mapped)
    assert bundle["governing_set_complete"] is True
