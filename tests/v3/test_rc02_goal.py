"""RC-02 goal catalog cases T-007..T-014 (intent resolution, contract admission)."""

from __future__ import annotations

from copy import deepcopy

import pytest

from amplai_foundry.runtime.contracts.identity import new_id
from amplai_foundry.runtime.contracts.semantics import check_contract
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.goals.service import ContractCritic


def test_t007_ambiguous_target_hold(deployment):
    # given: two authorized apps share an alias; when: resolve intent;
    # expected: hold + one consolidated question; no write dispatch.
    d = deployment
    d.prepare(two_apps=True)
    bindings = d.store.list_objects(d.scope, "app-binding")
    _, alpha = next(pair for pair in bindings if pair[1]["app_id"] == "alpha")
    for app_id in ["gamma", "delta"]:
        binding = {
            "schema_version": "3.0.0",
            "app_id": app_id,
            "scope": d.scope.wire(),
            "repo_identity": "demo:" + app_id,
            "aliases": ["cortex"],
            "owner_subject_id": d.actor.subject_id,
            "allowed_roots": [str(d.root / "workspaces")],
            "environment_refs": alpha["environment_refs"],
            "invariant_refs": alpha["invariant_refs"],
            "verifier_profile_refs": alpha["verifier_profile_refs"],
            "data_classification": "internal",
            "requested_capabilities_ceiling": alpha["requested_capabilities_ceiling"],
            "registry_revision": 1,
        }
        d.goals.apps.register(d.actor, binding)
    submitted = d.goals.submit(
        d.actor, text="cortex 작업 처리", target_hints=["cortex"], key=new_id("submit")
    )
    goal_id = submitted["goal_id"]
    readiness = d.knowledge.readiness(d.scope, {})
    _ref, resolution = d.goals.resolve(d.actor, goal_id, readiness)
    assert resolution["status"] == "hold"
    assert len(resolution["question_refs"]) == 1
    questions = d.store.list_objects(d.scope, "question")
    assert len(questions) == 1
    assert questions[0][1]["kind"] == "business_intent"
    # negative: no contract was ever admitted for this goal, no dispatch is possible.
    contracts_for_goal = [
        pair for pair in d.store.list_objects(d.scope, "goal-contract") if pair[0]["id"] == goal_id
    ]
    assert contracts_for_goal == []
    assert d.runtime.claim(d.worker, goal_id=goal_id) is None


def test_t008_existing_fact_avoids_question(deployment):
    # given: build commands and invariants are in canonical repo facts (readiness "ready");
    # when: resolve rough request; expected: read sources, no question is raised.
    d = deployment
    prepared = d.prepare(two_apps=False)
    goal = d.store.head(d.scope, "goal", prepared["goal_id"])
    resolution = d.store.get(d.scope, "resolution", goal["data"]["resolution_ref"])
    assert resolution["status"] == "resolved"
    assert resolution["question_refs"] == []
    assert all(item["status"] == "ready" for item in resolution["readiness"])
    # negative: resolving a fully-grounded intent never mints a question object.
    assert d.store.list_objects(d.scope, "question") == []


def test_t009_unverifiable_goal_rejected(deployment, prepared):
    # given: objective says "make better" with no measurable/human rubric binding on a
    # mandatory criterion; when: freeze contract (check_contract, service.py:303);
    # expected: ACCEPTANCE_UNBOUND.
    d = deployment
    contract = deepcopy(d.store.get(d.scope, "goal-contract", prepared["contract_ref"]))
    contract["objective"] = "Make it better"
    assert contract["acceptance"][0]["mandatory"] is True
    contract["acceptance"][0]["success_rule"] = ""
    with pytest.raises(Hold) as exc:
        check_contract(contract)
    assert exc.value.code == "ACCEPTANCE_UNBOUND"


def test_t011_assumption_disclosed(deployment, prepared):
    # given: planner infers scope from non-authoritative text (origin=inferred, unverified);
    # when: create contract (check_contract, semantics.py:130); expected: the inferred
    # assumption blocks admission instead of silently authorizing execution.
    d = deployment
    contract = deepcopy(d.store.get(d.scope, "goal-contract", prepared["contract_ref"]))
    contract["assumptions"] = [
        {
            "id": "A-1",
            "statement": "Chat mention implies the repo owner approved a wider scope",
            "origin": "inferred",
            "source_refs": [],
            "status": "proposed",
            "blocks_execution": True,
        }
    ]
    with pytest.raises(Hold) as exc:
        check_contract(contract)
    assert exc.value.code == "UNVERIFIED_ASSUMPTION"


def test_t012_critic_budget_bounded(deployment, prepared):
    # given: same blocking ambiguity remains after configured rounds;
    # when: planner/critic repeat (ContractCritic.negotiate, service.py:516);
    # expected: HOLD with unresolved issue, no indefinite negotiation.
    d = deployment
    contract = deepcopy(d.store.get(d.scope, "goal-contract", prepared["contract_ref"]))
    critic = ContractCritic(d.contracts)

    def planner(candidate, _report):
        return candidate  # never repairs the flagged ambiguity

    def reviewer(_candidate):
        return [
            {
                "code": "TARGET_AMBIGUOUS",
                "severity": "blocking",
                "statement": "Target app remains ambiguous after clarification",
            }
        ]

    with pytest.raises(Hold) as exc:
        critic.negotiate(contract, planner, reviewer)
    assert exc.value.code == "CRITIC_LIMIT"
    assert exc.value.details["outcome"] == "hold"
    assert exc.value.details["findings"][0]["code"] == "TARGET_AMBIGUOUS"


def test_t013_criteria_id_collision(deployment, prepared):
    # given: two acceptance rows share the same ID; when: semantic validation
    # (check_contract, semantics.py:115); expected: reject duplicate criterion IDs.
    d = deployment
    contract = deepcopy(d.store.get(d.scope, "goal-contract", prepared["contract_ref"]))
    assert len(contract["acceptance"]) >= 2
    contract["acceptance"][1]["id"] = contract["acceptance"][0]["id"]
    with pytest.raises(RuntimeFault) as exc:
        check_contract(contract)
    assert exc.value.code == "ACCEPTANCE_DUPLICATE"


def test_t014_contract_revision_invalidates_grant(deployment, prepared):
    # given: grant bound to contract v1; when: a differing (v2) contract exists and a
    # request presents the v1 grant against it (authority.preflight, authority.py:130);
    # expected: rejected as a grant/contract binding mismatch, reauthorization required.
    # (invariant-registry.json:220 labels this class of failure GRANT_STALE; the concrete
    # code the implementation raises is GRANT_BINDING at authority.py:161.)
    d = deployment
    v1_contract = d.store.get(d.scope, "goal-contract", prepared["contract_ref"])
    v2_contract = {**deepcopy(v1_contract), "revision": 2, "objective": "Revised objective for v2"}
    with d.store.tx() as db:
        v2_ref = d.store.put(db, d.scope, "goal-contract", v1_contract["goal_id"], 2, v2_contract)
    with pytest.raises(Hold) as exc:
        d.authority.preflight(
            d.scope,
            prepared["grant_ref"],
            subject_id=d.actor.subject_id,
            contract_ref=v2_ref,
            graph_ref=prepared["graph_ref"],
            capabilities=v1_contract["requested_capabilities"],
        )
    assert exc.value.code == "GRANT_BINDING"
    # negative: the mismatched attempt never consumes the grant's use budget.
    used = d.store.conn.execute(
        "SELECT COUNT(*) FROM grant_uses WHERE tenant=? AND project=? AND grant_id=?",
        (*d.scope.keys(), prepared["grant_ref"]["id"]),
    ).fetchone()[0]
    assert used == 0
