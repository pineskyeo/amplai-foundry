"""V3-057 — Cross-app end-to-end goal/steer/replan demo.

design/27 Scenario C and E, test-catalog T-006/T-027/T-029/T-063/T-064/T-107.
Rough intent → two sandbox apps in parallel through the real coordinator → node
verification → global integration; then a mid-run constraint steer that yields
contract v2 and fences the v1 worker out of new effects. Local deterministic apps
only: this is not a live provider or production qualification.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from amplai_foundry.agent_drivers.ports import DriverRegistry, RecipePort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.contracts.identity import new_id, now
from amplai_foundry.runtime.errors import Conflict, Hold
from amplai_foundry.runtime.execution.steering import SteeringService
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.sandbox.workspace import WorkspaceManager
from amplai_foundry.verification.runtime.service import VerificationObservation


def rig(d, p, port=None):
    registry = DriverRegistry(d.store)
    port = port or RecipePort(SessionJournal(d.root / "native-journal"))
    registry.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    workspaces = WorkspaceManager(d.root / "isolated", d.artifacts)
    return WorkCoordinator(d.runtime, registry, workspaces), workspaces, port


def execute(d, p, coordinator, workspaces, dispatch):
    recipe = p["recipes"][dispatch["node"]["work_id"]]
    return coordinator.execute(
        d.worker,
        dispatch,
        prompt=json.dumps({"operations": recipe["operations"]}),
        base_snapshot=workspaces.empty_snapshot(d.scope),
        output_paths={k: v["path"] for k, v in recipe["outputs"].items()},
    )


def verify_node(d, dispatch):
    work = d.store.head(d.scope, "work", dispatch["node"]["work_id"])
    for ac in dispatch["node"]["acceptance_ids"]:
        port = "result-" + dispatch["node"]["target_ref"]["id"]
        d.verification.verify(d.verifier, dispatch["run_id"], ac, work["data"]["outputs"][port])
    return d.verification.finish_work(d.verifier, dispatch["run_id"])


def integration(d, expected_apps):
    def check(_contract, _graph, outputs):
        values = [
            json.loads(d.artifacts.read(d.scope, a))
            for ports in outputs.values()
            for a in ports.values()
        ]
        ok = {v["app"] for v in values} == expected_apps and all(
            v["message"] == "AMPLAI V3" for v in values
        )
        return VerificationObservation(
            "pass" if ok else "fail",
            "Independent cross-app integration",
            {"apps": sorted(v["app"] for v in values)},
        )

    return check


# ---------------------------------------------------------------- T-006 / T-107


def test_t107_rough_intent_to_two_sandbox_apps_to_integration_evidence(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    intent = d.store.get(
        d.scope,
        "intent-envelope",
        d.store.get(d.scope, "goal-contract", p["contract_ref"])["intent_ref"],
    )
    assert intent["target_hints"] == [], "T-006: no hint; targets come from registry evidence"
    contract = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    assert {t["id"] for t in contract["targets"]} == {"alpha", "beta"}
    coordinator, workspaces, _port = rig(d, p)
    claims = [d.runtime.claim(d.worker, goal_id=p["goal_id"]) for _ in range(2)]
    assert {c["lease"]["work_id"] for c in claims} == {
        n["work_id"] for n in d.store.get(d.scope, "workgraph", p["graph_ref"])["nodes"]
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda x: execute(d, p, coordinator, workspaces, x), claims))
    assert all(r["status"] == "verifying" for r in results)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] != "verified", (
        "node success is not goal success"
    )
    for x in claims:
        assert verify_node(d, x)["outcome"] == "pass"
    graph = d.store.get(d.scope, "workgraph", p["graph_ref"])
    check = integration(d, {"alpha", "beta"})
    d.verification.register_global(graph["global_verification_ref"], check)
    final = d.verification.finish_goal(d.verifier, p["goal_id"], check)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] == "verified"
    evidence = d.store.list_objects(d.scope, "evidence")
    verdicts = d.store.list_objects(d.scope, "verdict")
    assert len(evidence) >= 2 and len(verdicts) >= 2, "evidence chain exists per node"
    assert final and d.runtime.budgets.totals(d.scope, p["goal_id"])["active_runs"] == 0
    outputs = sorted(workspaces.root.rglob("result.json"))
    assert len(outputs) == 2 and {json.loads(o.read_bytes())["app"] for o in outputs} == {
        "alpha",
        "beta",
    }


# ---------------------------------------------------------------- T-063


def test_t063_node_passes_but_global_integration_fails(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    coordinator, workspaces, _port = rig(d, p)
    claims = [d.runtime.claim(d.worker, goal_id=p["goal_id"]) for _ in range(2)]
    for x in claims:
        execute(d, p, coordinator, workspaces, x)
        assert verify_node(d, x)["outcome"] == "pass"
    graph = d.store.get(d.scope, "workgraph", p["graph_ref"])
    mismatch = integration(d, {"alpha", "gamma"})  # interface expects a third app
    d.verification.register_global(graph["global_verification_ref"], mismatch)
    with pytest.raises(Hold):
        d.verification.finish_goal(d.verifier, p["goal_id"], mismatch)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] != "verified"


# ---------------------------------------------------------------- Scenario E / T-027 / T-029


class SlowFixture(RecipePort):
    """A native run that stays running until cancelled, so a steer lands mid-run."""

    def start(self, prepared):
        did = prepared["dispatch_id"]
        self.journal.transition(
            did, {"prepared"}, "running", session_handle="fx-exact", process_stopped=False
        )
        return did

    def cancel(self, handle):
        self.journal.update(handle, state="paused", process_stopped=True)
        return {"process_stopped": True, "session_handle": "fx-exact"}


def new_revision(d, p, *, constraint_text):
    """Contract v2 from the active v1 with one added (non-protected) constraint."""
    old = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    candidate = deepcopy(old)
    candidate["revision"] = 2
    candidate["constraints"].append(
        {
            "id": "C-ADD",
            "statement": constraint_text,
            "source_refs": [old["policy_ref"]],
            "protected": False,
        }
    )
    candidate["created_at"] = now()
    head = d.store.head(d.scope, "goal", p["goal_id"])
    contract_ref = d.goals.freeze_contract(d.actor, candidate, expected_version=head["row_version"])
    graph = deepcopy(d.store.get(d.scope, "workgraph", p["graph_ref"]))
    graph["graph_id"] = new_id("graph")
    graph["revision"] = 2
    graph["contract_ref"] = contract_ref
    graph["previous_graph_ref"] = p["graph_ref"]
    graph["replan_reason"] = "constraint_add"
    for node in graph["nodes"]:
        node["work_id"] = new_id("work-" + node["target_ref"]["id"])
    graph["created_at"] = now()
    graph_ref = d.runtime.save_graph(d.actor, graph, contract_ref)
    return contract_ref, graph_ref, graph


def issue_v2_grant(d, p, contract_ref, graph_ref):
    from amplai_foundry.runtime.contracts.identity import digest

    grant = d.store.get(d.scope, "execution-grant", p["grant_ref"])
    decision = d.store.get(d.scope, "demo-decision", p["decision_ref"])
    decision2 = {
        **decision,
        "decision_id": new_id("demo-decision"),
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
    }
    decision2_ref = d.put("demo-decision", decision2["decision_id"], decision2)
    d.decisions[digest(decision2_ref)] = decision2
    grant2 = {
        **grant,
        "grant_id": new_id("grant"),
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
        "decision_ref": decision2_ref,
    }
    grant2.pop("signature", None)
    return d.authority.issue(d.actor, grant2)


def test_scenario_e_mid_run_steer_yields_v2_and_fences_v1_worker(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    port = SlowFixture(SessionJournal(d.root / "fx-journal"))
    coordinator, workspaces, _ = rig(d, p, port)
    x = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    old_fence = x["lease"]["fencing_token"]
    ss = SteeringService(d.runtime)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, d, p, coordinator, workspaces, x)
        for _ in range(150):
            if d.store.head(d.scope, "run", x["run_id"])["state"] == "running":
                break
            time.sleep(0.02)
        else:
            pytest.fail("fixture never reached running")
        # steering ledger: received → validated → queued (design/17 §3)
        q = ss.receive(
            d.actor,
            p["goal_id"],
            "constraint_add",
            "기존 API 는 유지하고 신규 API 는 추가만 한다",
            expected_contract_ref=p["contract_ref"],
            key="steer-keep-api",
        )
        assert q["status"] == "queued"
        assert d.runtime.claim(d.worker, goal_id=p["goal_id"]) is None, "admission paused"
        # provider delivery is not application
        assert ss.native_ack(d.worker, q["steering_id"], "turn-9")["applied"] is False
        outcome = ss.quiesce(
            d.actor,
            q["steering_id"],
            lambda run, kind: coordinator.stop_and_snapshot(d.worker, run, kind),
        )
        # a revision-changing steer stops v1 but stays queued until v2 is admitted
        assert outcome["status"] == "queued"
        assert outcome["outcomes"][0]["status"] == "cancelled"
        with pytest.raises(Hold):
            future.result(timeout=5)
    # already dispatched effects were inspected: none pending
    assert ss._unknown(d.scope, x["run_id"]) == []
    goal = d.store.head(d.scope, "goal", p["goal_id"])
    assert goal["state"] == "blocked" and goal["data"]["replan_reason"].startswith("기존 API")
    assert d.store.head(d.scope, "work", x["lease"]["work_id"])["state"] == "cancelled"
    # v1 lease can no longer start effects (old fence rejected)
    with pytest.raises(Hold):
        d.runtime.lease(d.worker, x["run_id"], x["lease"]["lease_id"], old_fence - 1)
    with pytest.raises(Hold) as exc:
        ss.apply_revision(d.actor, q["steering_id"])
    assert exc.value.code == "REVISION_NOT_EFFECTIVE", "delivery does not activate a revision"
    # contract v2 + graph v2 through normal admission
    contract_ref, graph_ref, graph = new_revision(
        d, p, constraint_text="Keep the existing API; only add new endpoints"
    )
    assert contract_ref["revision"] == 2
    grant2_ref = issue_v2_grant(d, p, contract_ref, graph_ref)
    head = d.store.head(d.scope, "goal", p["goal_id"])
    d.runtime.activate(
        d.actor,
        contract_ref,
        graph_ref,
        grant2_ref,
        p["execution_profile"],
        expected_version=head["row_version"],
    )
    goal = d.store.head(d.scope, "goal", p["goal_id"])
    assert goal["state"] == "ready"
    assert goal["data"]["active_contract_ref"] == contract_ref
    assert goal["data"]["execution_epoch"] == 2
    assert ss.apply_revision(d.actor, q["steering_id"])["status"] == "applied"
    steer = d.store.head(d.scope, "steering", q["steering_id"])
    assert steer["data"]["event"]["effective_contract_ref"] == contract_ref
    # T-027: the v1 dispatch is dead under v2; only v2 work is claimable, with a higher fence
    with pytest.raises((Hold, Conflict)):
        d.runtime.start(d.worker, x, "fx-exact")
    fresh = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    assert fresh is not None and fresh["contract_ref"] == contract_ref
    assert fresh["graph_ref"] == graph_ref
    assert fresh["lease"]["work_id"] in {n["work_id"] for n in graph["nodes"]}
    assert fresh["lease"]["work_id"] != x["lease"]["work_id"], "v2 work, not the v1 work"
    # fencing is per work; the goal execution epoch (1 → 2) fences v1 dispatches out of v2
    assert d.store.head(d.scope, "goal", p["goal_id"])["data"]["execution_epoch"] == 2
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "cancelled"


def test_t029_concurrent_replans_one_wins(deployment):
    d = deployment
    p = d.prepare(two_apps=True)
    d.execute(p)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] == "verified"
    contract_ref, graph_ref, _graph = new_revision(d, p, constraint_text="first")
    grant2_ref = issue_v2_grant(d, p, contract_ref, graph_ref)
    version = d.store.head(d.scope, "goal", p["goal_id"])["row_version"]

    def activate():
        return d.runtime.activate(
            d.actor,
            contract_ref,
            graph_ref,
            grant2_ref,
            p["execution_profile"],
            expected_version=version,
        )

    outcomes = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for f in [pool.submit(activate), pool.submit(activate)]:
            try:
                outcomes.append(("ok", f.result()))
            except (Hold, Conflict) as exc:
                outcomes.append(("err", exc.code))
    assert sorted(k for k, _ in outcomes) == ["err", "ok"], outcomes
    goal = d.store.head(d.scope, "goal", p["goal_id"])
    assert goal["data"]["active_contract_ref"] == contract_ref
    assert goal["data"]["execution_epoch"] == 2, "exactly one activation applied"
