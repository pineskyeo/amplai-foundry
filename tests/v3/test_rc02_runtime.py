"""RC-02 runtime catalog cases T-031..T-038 (scheduler strategy, budgets, fairness)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from amplai_foundry.agent_drivers.ports import DriverRegistry, RecipePort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.contracts.identity import digest, new_id, now
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.strategies import StrategyRouter, TaskFacts
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.sandbox.workspace import WorkspaceManager


def rig(d, p, port=None):
    reg = DriverRegistry(d.store)
    port = port or RecipePort(SessionJournal(d.root / "native-journal"))
    reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    ws = WorkspaceManager(d.root / "isolated", d.artifacts)
    return WorkCoordinator(d.runtime, reg, ws), ws, port, reg


def execute(d, p, w, ws, x, *, message=None):
    recipe = p["recipes"][x["node"]["work_id"]]
    if message is not None:
        recipe = json.loads(json.dumps(recipe))
        recipe["operations"][0]["content"]["message"] = message
    return w.execute(
        d.worker,
        x,
        prompt=json.dumps({"operations": recipe["operations"]}),
        base_snapshot=ws.empty_snapshot(d.scope),
        output_paths={k: v["path"] for k, v in recipe["outputs"].items()},
    )


def verify_once(d, x, *, expect):
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    port = x["node"]["produces"][0]["name"]
    for ac in x["node"]["acceptance_ids"]:
        d.verification.verify(d.verifier, x["run_id"], ac, work["data"]["outputs"][port])
    outcome = d.verification.finish_work(d.verifier, x["run_id"])
    assert outcome["outcome"] == expect
    return outcome


def second_root(d, p, *, resource, max_attempts=3, strategy="bounded_loop"):
    """A second, independently activated goal/graph reusing shared reference refs."""
    scope = d.scope

    def ref_of(kind, obj_id, revision=1):
        row = d.store.conn.execute(
            "SELECT digest FROM objects WHERE tenant=? AND project=? AND kind=? "
            "AND id=? AND revision=?",
            (*scope.keys(), kind, obj_id, revision),
        ).fetchone()
        return {"id": obj_id, "revision": revision, "digest": row[0]}

    env_ref = ref_of("environment", "demo-environment")
    policy_ref = ref_of("policy", "demo-policy")
    invariant_ref = ref_of("invariant-registry", "demo-invariants")
    global_ref = ref_of("global-verifier", "demo-global")
    verifier_ref = p["verifier_ref"]
    caps = [
        {
            "action": "workspace.write",
            "resource": "sandbox:demo-output",
            "effect_class": "sandbox_write",
        }
    ]
    submitted = d.goals.submit(d.actor, text="alpha 결과 생성", key=new_id("submit-" + resource))
    goal_id = submitted["goal_id"]
    intent_ref = submitted["intent_ref"]
    facts = d.knowledge.record_observation(
        scope, "second-root-" + resource, "Second root reference task"
    )
    readiness = d.knowledge.readiness(
        scope,
        {
            area: [facts]
            for area in [
                "terminology",
                "current_behavior",
                "boundary",
                "invariants",
                "ssot",
                "contradictions",
                "acceptance",
                "verifier",
            ]
        },
    )
    resolution_ref, resolution = d.goals.resolve(d.actor, goal_id, readiness)
    bundle_ref, _bundle = d.knowledge.bundle(
        scope,
        bundle_id=new_id("context-" + resource),
        core_refs=[policy_ref, invariant_ref],
        entries=[
            {
                "ref": facts,
                "kind": "repo_fact",
                "trust": "observed",
                "mandatory": True,
                "freshness": "current",
                "superseded_by": None,
                "excerpt": "Second root task.",
                "source_locator": "second-root-" + resource,
            }
        ],
        invariant_registry_ref=invariant_ref,
        token_budget=8192,
        assembled_at=now(),
    )
    target = resolution["target_refs"][0]
    app = target["id"]
    ac = "AC-" + resource
    port_name = "result-" + resource
    root_budget = {
        "max_wall_seconds": 3600,
        "max_attempts": max_attempts,
        "max_tokens": 12000,
        "max_cost_microunits": 2000000,
        "currency": "USD",
        "max_parallel_works": 4,
        "max_delegation_depth": 2,
    }
    acceptance = [
        {
            "id": ac,
            "statement": "App result contains the expected message and app identity",
            "facet": "functional",
            "mandatory": True,
            "verifier_ref": verifier_ref,
            "required_evidence_types": ["json-verification"],
            "success_rule": "message equals AMPLAI V3 and app is named",
            "human_acceptance_required": False,
        }
    ]
    node = {
        "node_id": "node-" + resource,
        "work_id": new_id("work-" + resource),
        "target_ref": target,
        "objective": "Create second root result JSON",
        "strategy": strategy,
        "depends_on": [],
        "join": "all_required",
        "consumes": [],
        "produces": [{"name": port_name, "media_type": "application/json", "required": True}],
        "acceptance_ids": [ac],
        "verification_profile_ref": verifier_ref,
        "capabilities": caps,
        "resource_claims": [{"resource": "sandbox:" + resource, "mode": "exclusive_write"}],
        "budget": {**root_budget, "max_tokens": 2000, "max_cost_microunits": 200000},
    }
    bindings = [
        {
            "acceptance_id": ac,
            "verifier_ref": verifier_ref,
            "subject_selector": port_name,
            "environment_ref": env_ref,
            "required_evidence_types": ["json-verification"],
            "decision_rule": "Pinned schema and exact message checks",
            "independent_review": True,
            "timeout_seconds": 30,
        }
    ]
    plan = {
        "schema_version": "3.0.0",
        "plan_id": new_id("plan-" + resource),
        "scope": scope.wire(),
        "contract_ref": None,
        "bindings": bindings,
        "protected_regression_refs": [],
        "policy_ref": policy_ref,
    }
    plan_ref = d.put("verification-plan", str(plan["plan_id"]), plan)
    t = now()
    contract = {
        "schema_version": "3.0.0",
        "goal_id": goal_id,
        "scope": scope.wire(),
        "revision": 1,
        "intent_ref": intent_ref,
        "resolution_ref": resolution_ref,
        "mode": "work",
        "objective": "Second root reference goal",
        "non_goals": [],
        "targets": resolution["target_refs"],
        "constraints": [
            {
                "id": "C-LOCAL",
                "statement": "Data-only sandbox; no production effects",
                "source_refs": [policy_ref],
                "protected": True,
            }
        ],
        "acceptance": acceptance,
        "assumptions": [],
        "open_question_refs": [],
        "risk": "low",
        "budget": root_budget,
        "requested_capabilities": caps,
        "verification_plan_ref": plan_ref,
        "context_bundle_ref": bundle_ref,
        "policy_ref": policy_ref,
        "created_at": t,
    }
    contract_ref = d.goals.freeze_contract(
        d.actor, contract, expected_version=d.store.head(scope, "goal", goal_id)["row_version"]
    )
    graph = {
        "schema_version": "3.0.0",
        "graph_id": new_id("graph-" + resource),
        "scope": scope.wire(),
        "revision": 1,
        "contract_ref": contract_ref,
        "previous_graph_ref": None,
        "replan_reason": None,
        "nodes": [node],
        "global_verification_ref": global_ref,
        "compiler_version": "3.0.0",
        "created_at": t,
    }
    graph_ref = d.runtime.save_graph(d.actor, graph, contract_ref)
    decision = {
        "decision_id": new_id("decision-" + resource),
        "scope": scope.wire(),
        "status": "approved",
        "revoked": False,
        "generation": 1,
        "issuer_subject_id": d.actor.subject_id,
        "subject_id": d.actor.subject_id,
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
        "capabilities": caps,
    }
    decision_ref = d.put("demo-decision", str(decision["decision_id"]), decision)
    d.decisions[digest(decision_ref)] = decision
    current = datetime.fromtimestamp(d.store.clock(), UTC)
    grant = {
        "schema_version": "3.0.0",
        "grant_id": new_id("grant-" + resource),
        "scope": scope.wire(),
        "issuer": d.actor.wire(),
        "subject_id": d.actor.subject_id,
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
        "policy_ref": policy_ref,
        "generation": 1,
        "capabilities": caps,
        "artifact_bounds": [],
        "max_uses": 32,
        "effect_key": None,
        "not_before": (current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (current + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "decision_ref": decision_ref,
    }
    grant_ref = d.authority.issue(d.actor, grant)
    execution_profile = {
        "composition_ref": p["execution_profile"]["composition_ref"],
        "driver_profile_ref": p["execution_profile"]["driver_profile_ref"],
        "model_profile_ref": p["execution_profile"]["model_profile_ref"],
        "environment_ref": env_ref,
    }
    d.runtime.activate(
        d.actor,
        contract_ref,
        graph_ref,
        grant_ref,
        execution_profile,
        expected_version=d.store.head(scope, "goal", goal_id)["row_version"],
    )
    recipe = {
        "operations": [
            {"op": "write_json", "path": "result.json", "content": {"message": "WRONG", "app": app}}
        ],
        "outputs": {port_name: {"path": "result.json", "media_type": "application/json"}},
    }
    return {
        "goal_id": goal_id,
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
        "recipes": {node["work_id"]: recipe},
        "execution_profile": execution_profile,
        "verifier_ref": verifier_ref,
    }


def test_t031_tiny_still_gated(deployment):
    # given: small typo work with direct strategy; when: strategy is selected/enforced;
    # expected: minimal contract still carries authority/budget/evidence/verification gates.
    router = StrategyRouter()
    facts = TaskFacts(
        local_change=True, deterministic_verifier=True, uncertainty="low", target_count=1
    )
    decision = router.select(
        {"risk": "low", "budget": {"max_wall_seconds": 60, "max_tokens": 10}},
        facts,
        available=frozenset({"direct", "bounded_loop", "deliberative", "discovery"}),
    )
    assert decision.strategy == "direct"
    assert set(decision.mandatory_gates) >= {
        "authority",
        "budget",
        "evidence",
        "independent_verification",
    }
    with pytest.raises(Hold) as exc:
        StrategyRouter.validate_node({"strategy": "direct", "capabilities": []}, {"risk": "medium"})
    assert exc.value.code == "DIRECT_RISK"

    d = deployment
    p = second_root(d, d.prepare(), resource="tiny-direct", strategy="direct", max_attempts=3)
    w, ws, _port, _reg = rig(d, p)
    x = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    execute(d, p, w, ws, x)
    verify_once(d, x, expect="fail")
    # A direct-strategy failure never becomes a silent repair loop, even with budget left.
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    assert work["state"] == "failed" and work["data"]["attempts"] == 1
    assert d.runtime.claim(d.worker, goal_id=p["goal_id"]) is None


def test_t032_exclusive_workspace_claim(deployment):
    # given: two writes target the same repo worktree resource; when: claim concurrently;
    # expected: only one exclusive writer, the other stays ready/blocked (no second claim).
    d = deployment
    p = d.prepare()
    graph = d.store.get(d.scope, "workgraph", p["graph_ref"])
    resource = graph["nodes"][0]["resource_claims"][0]["resource"]
    with d.store.tx() as db:
        db.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?)",
            (*d.scope.keys(), resource, "another-run", "exclusive_write"),
        )
    before = d.store.head(d.scope, "work", graph["nodes"][0]["work_id"])
    assert d.runtime.claim(d.worker, goal_id=p["goal_id"]) is None
    after = d.store.head(d.scope, "work", graph["nodes"][0]["work_id"])
    assert after["state"] == before["state"] == "pending"
    rows = d.store.conn.execute(
        "SELECT run_id FROM resources WHERE tenant=? AND project=? AND resource=?",
        (*d.scope.keys(), resource),
    ).fetchall()
    assert [r["run_id"] for r in rows] == ["another-run"]


def test_t033_root_budget_children(deployment):
    # given: parent plus native children collectively exceed the token cap;
    # when: admitting another child/continuation; expected: budget hold before extra work,
    # with only the admitted usage accounted (no phantom reservation for the rejected one).
    d = deployment
    scope = d.scope
    goal_id = "goal-t033"
    root = {
        "max_delegation_depth": 2,
        "max_attempts": 3,
        "currency": "USD",
        "max_wall_seconds": 3600,
        "max_parallel_works": 4,
        "max_tokens": 1000,
        "max_cost_microunits": None,
    }
    request = {
        "max_delegation_depth": 1,
        "max_attempts": 3,
        "currency": "USD",
        "max_tokens": 700,
        "max_cost_microunits": None,
        "max_wall_seconds": 60,
    }
    with d.store.tx() as db:
        d.runtime.budgets.reserve(db, scope, goal_id, "run-a", root, request)
    with pytest.raises(Hold) as exc, d.store.tx() as db:
        d.runtime.budgets.reserve(db, scope, goal_id, "run-b", root, request)
    assert exc.value.code == "TOKEN_BUDGET"
    totals = d.runtime.budgets.totals(scope, goal_id)
    assert totals["reserved_or_spent_tokens"] == 700
    assert totals["active_runs"] == 1


def test_t034_unknown_usage(deployment):
    # given: provider omits usage after a disconnect; when: closing the reservation;
    # expected: the unknown upper bound is held/reconciled, never treated as zero cost.
    d = deployment
    scope = d.scope
    goal_id = "goal-t034"
    root = {
        "max_delegation_depth": 2,
        "max_attempts": 3,
        "currency": "USD",
        "max_wall_seconds": 3600,
        "max_parallel_works": 4,
        "max_tokens": 1000,
        "max_cost_microunits": None,
    }
    request = {
        "max_delegation_depth": 1,
        "max_attempts": 3,
        "currency": "USD",
        "max_tokens": 500,
        "max_cost_microunits": None,
        "max_wall_seconds": 60,
    }
    with d.store.tx() as db:
        d.runtime.budgets.reserve(db, scope, goal_id, "run-unknown", root, request)
    with d.store.tx() as db:
        d.runtime.budgets.settle(db, scope, "run-unknown", {"status": "unknown"})
    row = d.store.conn.execute(
        "SELECT tokens,status FROM reservations WHERE tenant=? AND project=? AND run_id=?",
        (*scope.keys(), "run-unknown"),
    ).fetchone()
    assert row["status"] == "unknown" and row["tokens"] == 500
    totals = d.runtime.budgets.totals(scope, goal_id)
    assert totals["unknown_runs"] == 1 and totals["reserved_or_spent_tokens"] == 500


def test_budget_totals_without_reservations(deployment):
    d = deployment
    goal_id = "goal-without-reservations"
    # A goal with no reservation rows has nothing reserved, spent, unknown, overrun, or active.
    totals = d.runtime.budgets.totals(d.scope, goal_id)
    assert totals["reserved_or_spent_tokens"] == 0
    assert totals["reserved_or_spent_cost_microunits"] == 0
    assert totals["unknown_runs"] == 0
    assert totals["overruns"] == 0
    assert totals["active_runs"] == 0


def test_estimated_usage_settles_tokens_and_records_the_cost_estimate(deployment):
    # given: a driver-estimated cost (Claude CLI total_cost_usd) with provider-reported tokens
    d = deployment
    scope = d.scope
    goal_id = "goal-estimated"
    root = {
        "max_delegation_depth": 2, "max_attempts": 3, "currency": "USD",
        "max_wall_seconds": 3600, "max_parallel_works": 4, "max_tokens": 10_000,
        "max_cost_microunits": 10_000,
    }  # fmt: skip
    request = {
        "max_delegation_depth": 1, "max_attempts": 3, "currency": "USD", "max_tokens": 500,
        "max_cost_microunits": 1_000, "max_wall_seconds": 60,
    }  # fmt: skip

    def settle(run_id, usage):
        with d.store.tx() as db:
            d.runtime.budgets.reserve(db, scope, goal_id, run_id, root, request)
        with d.store.tx() as db:
            d.runtime.budgets.settle(db, scope, run_id, usage)
        return d.store.conn.execute(
            "SELECT tokens,cost,status FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), run_id),
        ).fetchone()

    def events(kind):
        return d.store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type=?", (kind,)
        ).fetchone()[0]

    estimate = {"status": "estimated", "input_tokens": 100, "output_tokens": 20}
    # expected: tokens settle; the cost stays the reservation (the estimate is kept in usage)
    row = settle("run-est", {**estimate, "cost_microunits": 900})
    assert (row["tokens"], row["cost"], row["status"]) == (120, 1_000, "estimated")
    # an estimate above the reservation is reported, not an overrun: work is not blocked
    row = settle("run-est-high", {**estimate, "cost_microunits": 5_000})
    assert row["status"] == "estimated" and events("budget.estimate_over") == 1
    assert events("budget.overrun") == 0
    # provider-reported tokens above the reservation are an actual overrun
    row = settle("run-est-tokens", {**estimate, "output_tokens": 900})
    assert row["status"] == "overrun" and events("budget.overrun") == 1
    # an estimate without token counts stays unknown, as before
    row = settle("run-est-blind", {"status": "estimated", "cost_microunits": 10})
    assert (row["tokens"], row["status"]) == (500, "unknown")
    totals = d.runtime.budgets.totals(scope, goal_id)
    assert totals["estimated_runs"] == 2 and totals["overruns"] == 1
    assert totals["unknown_runs"] == 1


def test_t035_attempt_budget(deployment):
    # given: verifier repeatedly fails under a bounded loop; when: attempt one (of one)
    # is exhausted; expected: the Run is terminal, no more auto repair, typed HOLD.
    d = deployment
    p = second_root(d, d.prepare(), resource="attempt-budget", max_attempts=1)
    w, ws, _port, _reg = rig(d, p)
    x = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    execute(d, p, w, ws, x)
    verify_once(d, x, expect="fail")
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    # Exhausted purely on attempts (only 1 signature, far below the 3-repeat escalation).
    assert work["state"] == "failed" and len(work["data"]["failure_signatures"]) == 1
    assert d.runtime.claim(d.worker, goal_id=p["goal_id"]) is None


def test_t036_repeated_failure_signature(deployment):
    # given: the same failure signature reaches the stop count; when: requesting the next
    # repair; expected: escalate (terminal) instead of a fruitless retry, with attempts left.
    d = deployment
    p = second_root(d, d.prepare(), resource="repeat-sig", max_attempts=5)
    w, ws, _port, _reg = rig(d, p)
    for _ in range(3):
        x = d.runtime.claim(d.worker, goal_id=p["goal_id"])
        execute(d, p, w, ws, x)
        verify_once(d, x, expect="fail")
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    assert work["state"] == "failed"
    assert work["data"]["attempts"] == 3 < 5
    assert len(set(work["data"]["failure_signatures"][-3:])) == 1
    assert d.runtime.claim(d.worker, goal_id=p["goal_id"]) is None


def test_t037_fairness_under_large_goal(deployment):
    # given: one root has many (two) ready nodes plus another root also ready;
    # when: the scheduler ticks (repeated claim calls); expected: last-dispatch fairness
    # interleaves the other root's work rather than draining the busy root first.
    d = deployment
    p = d.prepare(two_apps=True)
    other = second_root(d, p, resource="fair-other")

    def owner(dispatch):
        work = d.store.head(d.scope, "work", dispatch["node"]["work_id"])
        return work["data"]["goal_id"]

    owners = [owner(d.runtime.claim(d.worker)) for _ in range(3)]
    assert owners.count(p["goal_id"]) == 2 and owners.count(other["goal_id"]) == 1
    # The other root's ready node is interleaved, not pushed behind both busy-root nodes.
    assert owners[2] != owners[1] or owners[1] != owners[0]
    assert other["goal_id"] in owners[:2]


def test_t038_no_long_io_in_transaction(deployment):
    # given: the provider hangs for a long tool call; when: another store command
    # (heartbeat) runs meanwhile; expected: no DB transaction is held across the
    # provider call, so the heartbeat commit succeeds.
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    probed = {"outside_tx": False, "heartbeat": None}

    class Probing(RecipePort):
        def poll(self, handle):
            if not probed["heartbeat"]:
                d.store.assert_outside_tx()
                probed["outside_tx"] = True
                # The worker's own periodic heartbeat already fired at sequence 1 before
                # this first poll (last_heartbeat starts at 0); this simulates a second,
                # independent store command landing while the provider call is in flight.
                current = d.store.conn.execute(
                    "SELECT heartbeat_seq FROM leases WHERE tenant=? AND project=? AND run_id=?",
                    (*d.scope.keys(), x["run_id"]),
                ).fetchone()[0]
                probed["heartbeat"] = d.runtime.heartbeat(
                    d.worker,
                    x["run_id"],
                    x["lease"]["lease_id"],
                    x["lease"]["fencing_token"],
                    sequence=current + 1,
                )
                return {"state": "observing", "session_handle": None}
            return self.journal.read(handle)

    port = Probing(SessionJournal(d.root / "probe-journal"))
    w, ws, _port, _reg = rig(d, p, port)
    result = execute(d, p, w, ws, x)
    assert result["status"] == "verifying"
    assert probed["outside_tx"] is True
    assert probed["heartbeat"] is not None
