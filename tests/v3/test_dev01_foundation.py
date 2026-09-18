"""DEV-01 boundary tests against the approved V3 core, not provider qualification."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from amplai_foundry.runtime.contracts.identity import new_id, reference
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.goals.service import ContractCritic
from amplai_foundry.runtime.graphs.compiler import GraphCompiler
from amplai_foundry.verification.runtime.service import JsonVerifier


def fresh_candidate(d, prepared):
    """A new draft Goal reuses only authorized immutable facts, not prior authority."""
    old = d.store.get(d.scope, "goal-contract", prepared["contract_ref"])
    resolution = d.store.get(d.scope, "resolution", old["resolution_ref"])
    submission = d.goals.submit(d.actor, text="alpha 결과 JSON 생성", key=new_id("test"))
    rr, _ = d.goals.resolve(d.actor, submission["goal_id"], resolution["readiness"])
    candidate = deepcopy(old)
    candidate.update(
        goal_id=submission["goal_id"],
        intent_ref=submission["intent_ref"],
        resolution_ref=rr,
        revision=1,
    )
    return candidate


def freeze(d, candidate):
    head = d.store.head(d.scope, "goal", candidate["goal_id"])
    return d.goals.freeze_contract(d.actor, candidate, expected_version=head["row_version"])


def changed_plan(d, candidate, edit):
    plan = deepcopy(d.store.get(d.scope, "verification-plan", candidate["verification_plan_ref"]))
    plan["plan_id"] = new_id("plan")
    edit(plan)
    candidate["verification_plan_ref"] = d.put("verification-plan", plan["plan_id"], plan)


def test_dev01_valid_candidate(deployment):
    p = deployment.prepare()
    c = fresh_candidate(deployment, p)
    assert freeze(deployment, c) == reference(c, "goal_id")


def test_dev01_contract_scope_is_authenticated(deployment):
    p = deployment.prepare()
    c = fresh_candidate(deployment, p)
    c["scope"]["project_id"] = "another-project"
    with pytest.raises(RuntimeFault):
        freeze(deployment, c)


def test_dev01_stale_intent_cannot_be_frozen(deployment):
    d = deployment
    p = d.prepare()
    c = fresh_candidate(d, p)
    with d.store.tx() as db:
        old = d.store.get(d.scope, "intent-envelope", c["intent_ref"])
        refined = {**old, "text": "Changed requirement; previous contract is stale"}
        rr = d.store.put(db, d.scope, "intent-envelope", old["intent_id"], 2, refined)
        h = d.store.head(d.scope, "goal", c["goal_id"], db=db)
        d.store.cas(
            db,
            d.scope,
            "goal",
            c["goal_id"],
            h["row_version"],
            "draft",
            {**h["data"], "intent_ref": rr, "resolution_ref": None},
        )
    with pytest.raises(Hold):
        freeze(d, c)


def test_dev01_initial_contract_revision_must_be_one(deployment):
    d = deployment
    c = fresh_candidate(d, d.prepare())
    c["revision"] = 8
    with pytest.raises(RuntimeFault):
        freeze(d, c)


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "evidence", "independence"])
def test_dev01_verification_bindings_are_not_declarations(deployment, mutation):
    d = deployment
    c = fresh_candidate(d, d.prepare())

    def edit(plan):
        if mutation == "duplicate":
            plan["bindings"].append(deepcopy(plan["bindings"][0]))
        elif mutation == "unknown":
            b = deepcopy(plan["bindings"][0])
            b["acceptance_id"] = "AC-unknown"
            plan["bindings"].append(b)
        elif mutation == "evidence":
            plan["bindings"][0]["required_evidence_types"] = ["unrelated-log"]
        else:
            plan["bindings"][0]["independent_review"] = False

    changed_plan(d, c, edit)
    with pytest.raises(Hold):
        freeze(d, c)


@pytest.mark.parametrize("change", ["model_safety", "unprotected", "wrong_environment"])
def test_dev01_verifier_profiles_are_enforced(deployment, change):
    d = deployment
    c = fresh_candidate(d, d.prepare())
    profile = deepcopy(d.store.get(d.scope, "verifier-profile", c["acceptance"][0]["verifier_ref"]))
    profile["profile_id"] = new_id("verifier")
    if change == "model_safety":
        profile["kind"] = "model_assisted"
        c["acceptance"][0]["facet"] = "safety"
    elif change == "unprotected":
        profile["protected"] = False
    else:
        env = d.put("environment", "wrong-environment", {"id": "wrong-environment"})
        profile["environment_ref"] = env
    rr = d.put("verifier-profile", profile["profile_id"], profile)
    c["acceptance"][0]["verifier_ref"] = rr
    changed_plan(d, c, lambda plan: plan["bindings"][0].update(verifier_ref=rr))
    # Installed app verifier registry must also pin the new profile; omission itself is unsafe.
    with pytest.raises(Hold):
        freeze(d, c)


def test_dev01_missing_governing_policy_is_rejected(deployment):
    d = deployment
    c = fresh_candidate(d, d.prepare())
    bundle = deepcopy(d.store.get(d.scope, "context-bundle", c["context_bundle_ref"]))
    bundle["bundle_id"] = new_id("bundle")
    bundle["core_refs"] = [r for r in bundle["core_refs"] if r != c["policy_ref"]]
    c["context_bundle_ref"] = d.put("context-bundle", bundle["bundle_id"], bundle)
    with pytest.raises(Hold):
        freeze(d, c)


def test_dev01_nonexistent_readiness_sources_hold(deployment):
    d = deployment
    p = d.prepare()
    c = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    readiness = deepcopy(d.store.get(d.scope, "resolution", c["resolution_ref"])["readiness"])
    readiness[0]["source_refs"] = [
        {"id": "invented", "revision": 1, "digest": "sha256:" + "0" * 64}
    ]
    submission = d.goals.submit(d.actor, text="alpha", key=new_id("test"))
    _, resolution = d.goals.resolve(d.actor, submission["goal_id"], readiness)
    assert resolution["status"] == "hold"
    assert d.store.head(d.scope, "goal", submission["goal_id"])["state"] == "awaiting_decision"


@pytest.mark.parametrize("text", [" ", "\n\t"])
def test_dev01_blank_intent_rejected(deployment, text):
    with pytest.raises(RuntimeFault):
        deployment.goals.submit(deployment.actor, text=text, key=new_id("blank"))


def test_dev01_korean_particles_do_not_lose_cross_app_targets(deployment):
    d = deployment
    d.prepare(two_apps=True)
    found = d.goals.apps.resolve(d.scope, "alpha와 beta의 결과를 검증해줘", [])
    assert {b["app_id"] for _, b in found} == {"alpha", "beta"}


def test_dev01_shared_id_alias_is_ambiguous(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    contract = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    for rr in contract["targets"]:
        app = deepcopy(d.store.get(d.scope, "app-binding", rr))
        app["aliases"] = ["first"] if app["app_id"] == "alpha" else ["alpha"]
        app["registry_revision"] = 2
        d.goals.apps.register(d.actor, app)
    with pytest.raises(Hold):
        d.goals.apps.resolve(d.scope, "alpha 변경", [])


def test_dev01_questions_are_deduplicated(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    c = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    readiness = d.store.get(d.scope, "resolution", c["resolution_ref"])["readiness"]
    sub = d.goals.submit(d.actor, text="어느 앱인지 아직 불명확", key=new_id("ambiguous"))
    _, a = d.goals.resolve(d.actor, sub["goal_id"], readiness)
    _, b = d.goals.resolve(d.actor, sub["goal_id"], readiness)
    assert a["question_refs"] == b["question_refs"] and a["status"] == "hold"


@pytest.mark.parametrize("field", ["objective", "success_rule"])
def test_dev01_critic_rejects_blank_semantics(deployment, field):
    d = deployment
    c = fresh_candidate(d, d.prepare())
    if field == "objective":
        c[field] = "   "
    else:
        c["acceptance"][0][field] = "   "
    assert ContractCritic(d.contracts).review(c)["outcome"] == "hold"


def test_dev01_critic_never_mutates_previous_candidate(deployment):
    d = deployment
    c = fresh_candidate(d, d.prepare())
    before = deepcopy(c)
    critic = ContractCritic(d.contracts)
    count = []

    def reviewer(candidate):
        count.append(1)
        return [{"severity": "blocking", "code": "ASK", "statement": "Unresolved value"}]

    def planner(candidate, report):
        candidate["objective"] = "Mutated by an untrusted planner"
        return candidate

    with pytest.raises(Hold):
        critic.negotiate(c, planner, reviewer)
    assert c == before and len(count) == 2


def graph_input(d, prepared):
    c = d.store.get(d.scope, "goal-contract", prepared["contract_ref"])
    g = deepcopy(d.store.get(d.scope, "workgraph", prepared["graph_ref"]))
    return c, g


def test_dev01_graph_contract_digest_is_verified(deployment):
    d = deployment
    p = d.prepare()
    c, g = graph_input(d, p)
    c["objective"] = "A different contract with the same identity fields"
    with pytest.raises(Hold):
        GraphCompiler(d.contracts).compile(g, c, p["contract_ref"])


@pytest.mark.parametrize(
    "mutation", ["duplicate_resource", "missing_exclusive", "profile", "output_selector"]
)
def test_dev01_graph_semantic_bindings(deployment, mutation):
    d = deployment
    p = d.prepare()
    c, g = graph_input(d, p)
    g["graph_id"] = new_id("testgraph")
    n = g["nodes"][0]
    if mutation == "duplicate_resource":
        n["resource_claims"].append(deepcopy(n["resource_claims"][0]))
    elif mutation == "missing_exclusive":
        n["resource_claims"] = []
    elif mutation == "profile":
        n["verification_profile_ref"] = g["global_verification_ref"]
    else:
        n["produces"][0]["name"] = "no-longer-the-bound-subject"
    with pytest.raises(RuntimeFault):
        d.runtime.save_graph(d.actor, g, p["contract_ref"])


def test_dev01_graph_compilation_stable_and_immutable(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    c, g = graph_input(d, p)
    g["nodes"].reverse()
    before = deepcopy(g)
    a = GraphCompiler(d.contracts).compile(g, c, p["contract_ref"])
    b = GraphCompiler(d.contracts).compile(before, c, p["contract_ref"])
    assert a == b and g == before
    assert [n["node_id"] for n in a["nodes"]] == sorted(n["node_id"] for n in a["nodes"])


@pytest.mark.parametrize(
    "mutation", ["cycle", "duplicate_work", "unknown_acceptance", "budget", "capability"]
)
def test_dev01_existing_graph_negative_invariants(deployment, mutation):
    d = deployment
    p = d.prepare(two_apps=True)
    c, g = graph_input(d, p)
    a, b = g["nodes"]
    if mutation == "cycle":
        a["depends_on"] = [b["node_id"]]
        b["depends_on"] = [a["node_id"]]
    elif mutation == "duplicate_work":
        b["work_id"] = a["work_id"]
    elif mutation == "unknown_acceptance":
        a["acceptance_ids"] = ["invented"]
    elif mutation == "budget":
        a["budget"]["max_tokens"] = c["budget"]["max_tokens"] + 1
    else:
        a["capabilities"][0]["action"] = "production.control"
    with pytest.raises(RuntimeFault):
        GraphCompiler(d.contracts).compile(g, c, p["contract_ref"])


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1e999}'])
def test_dev01_json_verifier_rejects_ambiguous_or_nonfinite_json(raw):
    assert JsonVerifier({"type": "object"})(raw).outcome == "fail"


def ready_output(d, p):
    dispatch = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    d.driver.run(d.runtime, d.worker, dispatch, p["recipes"][dispatch["lease"]["work_id"]])
    w = d.store.head(d.scope, "work", dispatch["lease"]["work_id"])
    for ac in dispatch["node"]["acceptance_ids"]:
        d.verification.verify(
            d.verifier, dispatch["run_id"], ac, next(iter(w["data"]["outputs"].values()))
        )
    return dispatch


def test_dev01_producer_cannot_finalize_own_work(deployment):
    d = deployment
    p = d.prepare()
    dispatch = ready_output(d, p)
    adversary = replace(d.worker, permissions=frozenset({"worker.execute", "verifier.run"}))
    with pytest.raises(RuntimeFault):
        d.verification.finish_work(adversary, dispatch["run_id"])


def test_dev01_epoch_change_invalidates_old_verification(deployment):
    d = deployment
    p = d.prepare()
    dispatch = ready_output(d, p)
    with d.store.tx() as db:
        h = d.store.head(d.scope, "goal", p["goal_id"], db=db)
        d.store.cas(
            db,
            d.scope,
            "goal",
            p["goal_id"],
            h["row_version"],
            h["state"],
            {**h["data"], "execution_epoch": h["data"]["execution_epoch"] + 1},
        )
    with pytest.raises(Hold):
        d.verification.finish_work(d.verifier, dispatch["run_id"])


def test_dev01_worker_with_verifier_permission_cannot_finish_goal(deployment):
    d = deployment
    p = d.prepare()
    dispatch = ready_output(d, p)
    d.verification.finish_work(d.verifier, dispatch["run_id"])
    from amplai_foundry.verification.runtime.service import VerificationObservation

    graph = d.store.get(d.scope, "workgraph", p["graph_ref"])

    def integration(*_):
        return VerificationObservation("pass", "JSON integration", {})

    d.verification.register_global(graph["global_verification_ref"], integration)
    adversary = replace(d.worker, permissions=frozenset({"worker.execute", "verifier.run"}))
    with pytest.raises(RuntimeFault):
        d.verification.finish_goal(adversary, p["goal_id"])
