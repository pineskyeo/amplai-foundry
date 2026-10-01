"""DEV-02 actual local execution and deterministic control-plane boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest

from amplai_foundry.agent_drivers.ports import DriverRegistry, RecipePort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.envelope import execution_envelope
from amplai_foundry.runtime.execution.steering import SteeringService
from amplai_foundry.runtime.execution.strategies import StrategyRouter, TaskFacts
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.sandbox.workspace import WorkspaceManager
from amplai_foundry.verification.runtime.service import VerificationObservation


def rig(d, p, port=None):
    reg = DriverRegistry(d.store)
    port = port or RecipePort(SessionJournal(d.root / "native-journal"))
    reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    ws = WorkspaceManager(d.root / "isolated", d.artifacts)
    return WorkCoordinator(d.runtime, reg, ws), ws, port, reg


def execute(d, p, w, ws, x):
    recipe = p["recipes"][x["node"]["work_id"]]
    return w.execute(
        d.worker,
        x,
        prompt=json.dumps({"operations": recipe["operations"]}),
        base_snapshot=ws.empty_snapshot(d.scope),
        output_paths={k: v["path"] for k, v in recipe["outputs"].items()},
    )


def verify(d, x):
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    for ac in x["node"]["acceptance_ids"]:
        port = "result-" + x["node"]["target_ref"]["id"]
        d.verification.verify(d.verifier, x["run_id"], ac, work["data"]["outputs"][port])
    return d.verification.finish_work(d.verifier, x["run_id"])


def test_dev02_actual_parallel_coordinator_to_independent_global_verifier(deployment, prepared):
    d = deployment
    p = prepared
    w, ws, _port, _ = rig(d, p)
    claims = [d.runtime.claim(d.worker, goal_id=p["goal_id"]) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda x: execute(d, p, w, ws, x), claims))
    assert all(r["status"] == "verifying" for r in results)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] != "verified"
    assert len(list(ws.root.rglob("result.json"))) == 2
    for x in claims:
        verify(d, x)

    def integration(c, g, outputs):
        values = [
            json.loads(d.artifacts.read(d.scope, a)) for ps in outputs.values() for a in ps.values()
        ]
        ok = {v["app"] for v in values} == {"alpha", "beta"} and all(
            v["message"] == "AMPLAI V3" for v in values
        )
        return VerificationObservation(
            "pass" if ok else "fail", "Actual isolated outputs", {"apps": len(values)}
        )

    graph = d.store.get(d.scope, "workgraph", p["graph_ref"])
    d.verification.register_global(graph["global_verification_ref"], integration)
    d.verification.finish_goal(d.verifier, p["goal_id"], integration)
    assert d.store.head(d.scope, "goal", p["goal_id"])["state"] == "verified"
    assert d.runtime.budgets.totals(d.scope, p["goal_id"])["active_runs"] == 0


def test_dev02_dispatch_replay_does_not_spawn_or_regenerate(deployment):
    d = deployment
    p = d.prepare()
    w, ws, _port, _ = rig(d, p)
    x = d.runtime.claim(d.worker)
    a = execute(d, p, w, ws, x)
    path = ws.root / x["run_id"] / "result.json"
    before = path.stat().st_mtime_ns
    assert execute(d, p, w, ws, x) == a and path.stat().st_mtime_ns == before
    recipe = deepcopy(p["recipes"][x["node"]["work_id"]])
    recipe["operations"][0]["content"]["message"] = "changed"
    p["recipes"][x["node"]["work_id"]] = recipe
    with pytest.raises(Conflict):
        execute(d, p, w, ws, x)


def test_dev02_interrupted_dispatch_never_blindly_restarts(deployment):
    d = deployment
    p = d.prepare()

    class Failing(RecipePort):
        def start(self, prepared):
            self.journal.transition(prepared["dispatch_id"], {"prepared"}, "starting")
            raise Hold("TRANSPORT_LOST", "Creation outcome unknown")

    port = Failing(SessionJournal(d.root / "journal"))
    w, ws, _, _ = rig(d, p, port)
    x = d.runtime.claim(d.worker)
    with pytest.raises(Hold):
        execute(d, p, w, ws, x)
    with pytest.raises(Hold, match="Existing dispatch"):
        execute(d, p, w, ws, x)
    assert port.journal.read(x["dispatch_id"])["state"] == "starting"


def test_dev02_grant_revoked_during_native_run_stops_before_output(deployment):
    d = deployment
    p = d.prepare()

    class Revoker(RecipePort):
        def start(self, prepared):
            handle = super().start(prepared)
            d.decisions[digest(p["decision_ref"])]["revoked"] = True
            return handle

    w, ws, _, _ = rig(d, p, Revoker(SessionJournal(d.root / "journal")))
    x = d.runtime.claim(d.worker)
    with pytest.raises(Hold):
        execute(d, p, w, ws, x)
    h = d.store.head(d.scope, "worker-execution", x["dispatch_id"])
    assert h["state"] == "held" and h["data"]["process_stopped"] is True
    assert d.store.head(d.scope, "work", x["node"]["work_id"])["state"] != "verifying"


@pytest.mark.parametrize("field", ["contract_ref", "graph_ref", "node", "profile"])
def test_dev02_envelope_rejects_mutated_dispatch(deployment, field):
    d = deployment
    d.prepare()
    x = d.runtime.claim(d.worker)
    changed = deepcopy(x)
    if field == "node":
        changed[field]["objective"] = "different task"
    elif field == "profile":
        changed[field]["environment_ref"]["revision"] += 1
    else:
        changed[field]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeFault):
        execution_envelope(d.runtime, d.worker, changed)


def test_dev02_envelope_is_normative_and_effective_subset(deployment):
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    e = execution_envelope(d.runtime, d.worker, x)
    d.contracts.validate("execution-envelope", e)
    assert e["effective_capabilities"] == x["node"]["capabilities"]
    assert e["composition_ref"] == p["execution_profile"]["composition_ref"]
    assert e["scope"] == d.scope.wire() and e["fencing_token"] == x["lease"]["fencing_token"]


@pytest.mark.parametrize("event", ["pause", "cancel"])
def test_dev02_queued_stop_prevents_initial_start(deployment, event):
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    SteeringService(d.runtime).receive(
        d.actor, p["goal_id"], event, "stop", expected_contract_ref=p["contract_ref"], key=event
    )
    with pytest.raises(Hold):
        d.runtime.start(d.worker, x, "native_exact")
    with pytest.raises(Hold):
        execution_envelope(d.runtime, d.worker, x)


def paused(d, p):
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "native_exact")
    ss = SteeringService(d.runtime)
    q = ss.receive(
        d.actor, p["goal_id"], "pause", "pause", expected_contract_ref=p["contract_ref"], key="p"
    )
    a = d.artifacts.admit(d.scope, b'{"delta":[]}', "application/json")
    ss.quiesce(
        d.actor,
        q["steering_id"],
        lambda *_: {
            "process_stopped": True,
            "session_handle": "native_exact",
            "workspace_diff_artifact": a,
        },
    )
    return x, ss


def test_dev02_pause_releases_slot_and_resource_not_cost(deployment):
    d = deployment
    p = d.prepare()
    x, _ss = paused(d, p)
    row = d.store.conn.execute(
        "SELECT * FROM reservations WHERE run_id=?", (x["run_id"],)
    ).fetchone()
    assert row["status"] == "suspended" and row["tokens"] == 2000 and row["cost"] == 200000
    assert (
        d.store.conn.execute(
            "SELECT COUNT(*) FROM resources WHERE run_id=?", (x["run_id"],)
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(Hold):
        d.runtime.lease(d.worker, x["run_id"], x["lease"]["lease_id"], x["lease"]["fencing_token"])


def test_dev02_resume_reacquires_resources_before_io(deployment):
    d = deployment
    p = d.prepare()
    _x, ss = paused(d, p)
    r = ss.receive(
        d.actor, p["goal_id"], "resume", "go", expected_contract_ref=p["contract_ref"], key="r"
    )
    observations = []

    def native(run, cp):
        d.store.assert_outside_tx()
        observations.append(
            d.store.conn.execute(
                "SELECT status FROM reservations WHERE run_id=?", (run,)
            ).fetchone()[0]
        )
        assert (
            d.store.conn.execute(
                "SELECT COUNT(*) FROM resources WHERE run_id=?", (run,)
            ).fetchone()[0]
            == 1
        )
        return {"resumed": True, "session_handle": cp["driver_session_handle"]}

    result = ss.resume(d.actor, r["steering_id"], d.worker, native)
    assert result["status"] == "applied" and observations == ["active"]


def test_dev02_resume_resource_conflict_never_calls_native(deployment):
    d = deployment
    p = d.prepare()
    x, ss = paused(d, p)
    with d.store.tx() as db:
        claim = x["node"]["resource_claims"][0]
        db.execute(
            "INSERT INTO resources VALUES(?,?,?,?,?)",
            (*d.scope.keys(), claim["resource"], "another-run", "exclusive_write"),
        )
    r = ss.receive(
        d.actor, p["goal_id"], "resume", "go", expected_contract_ref=p["contract_ref"], key="r"
    )
    calls = []
    with pytest.raises(Hold):
        ss.resume(d.actor, r["steering_id"], d.worker, lambda *a: calls.append(a))
    assert not calls
    assert (
        d.store.conn.execute(
            "SELECT status FROM reservations WHERE run_id=?", (x["run_id"],)
        ).fetchone()[0]
        == "suspended"
    )


def test_dev02_unknown_resume_receipt_is_not_repeated(deployment):
    d = deployment
    p = d.prepare()
    x, ss = paused(d, p)
    r = ss.receive(
        d.actor, p["goal_id"], "resume", "go", expected_contract_ref=p["contract_ref"], key="r"
    )
    calls = []

    def native(*a):
        calls.append(a)
        return {"resumed": False, "session_handle": "native_exact"}

    with pytest.raises(Hold):
        ss.resume(d.actor, r["steering_id"], d.worker, native)
    with pytest.raises(Hold):
        ss.resume(d.actor, r["steering_id"], d.worker, native)
    assert len(calls) == 1
    assert (
        d.store.conn.execute(
            "SELECT status FROM reservations WHERE run_id=?", (x["run_id"],)
        ).fetchone()[0]
        == "active"
    )


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "cost_microunits"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1"])
def test_dev02_usage_not_silently_coerced(deployment, field, value):
    d = deployment
    d.prepare()
    x = d.runtime.claim(d.worker)
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_microunits": 0, "status": "measured"}
    usage[field] = value
    with pytest.raises(RuntimeFault), d.store.tx() as db:
        d.runtime.budgets.settle(db, d.scope, x["run_id"], usage)


def test_dev02_registry_is_scoped_admin_owned_exact_version(deployment):
    d = deployment
    p = d.prepare()
    reg = DriverRegistry(d.store)
    port = RecipePort(SessionJournal(d.root / "j"))
    with pytest.raises(RuntimeFault):
        reg.register(d.worker, p["execution_profile"]["driver_profile_ref"], port)
    port.version = "4.0.0"
    with pytest.raises(Hold):
        reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    port.version = "3.0.0"
    reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    with pytest.raises(Conflict):
        reg.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    with pytest.raises(Hold):
        reg.resolve(d.scope, p["execution_profile"]["driver_profile_ref"], "deliberative")


@pytest.mark.parametrize(
    "facts,expected",
    [
        (TaskFacts(deterministic_verifier=True, local_change=True, uncertainty="low"), "direct"),
        (TaskFacts(uncertainty="high"), "deliberative"),
        (TaskFacts(uncertainty="unknown"), "deliberative"),
        (TaskFacts(uncertainty="medium"), "bounded_loop"),
        (TaskFacts(unresolved_questions=True, read_only=True), "discovery"),
        (
            TaskFacts(
                target_count=2, deterministic_verifier=True, local_change=True, uncertainty="low"
            ),
            "bounded_loop",
        ),
    ],
)
def test_dev02_strategy_selection_keeps_mandatory_gates(facts, expected):
    c = {"risk": "low", "budget": {"max_wall_seconds": 10, "max_tokens": 100}}
    r = StrategyRouter().select(
        c, facts, available=frozenset({"direct", "bounded_loop", "deliberative", "discovery"})
    )
    assert r.strategy == expected and "independent_verification" in r.mandatory_gates


@pytest.mark.parametrize(
    "path",
    [
        ".ai-team/rules.md",
        ".github/workflows/ci.yaml",
        "policies/gate.json",
        "AGENTS.md",
        "contracts/a.json",
    ],
)
def test_dev02_protected_files_raise_risk_never_select_direct(path):
    c = {"risk": "low", "budget": {"max_wall_seconds": 10, "max_tokens": 100}}
    r = StrategyRouter().select(
        c,
        TaskFacts(
            changed_paths=(path,), local_change=True, deterministic_verifier=True, uncertainty="low"
        ),
        available=frozenset({"direct", "bounded_loop"}),
    )
    assert r.risk_floor == "high" and r.strategy == "bounded_loop"


def test_dev02_strategy_unknown_cannot_downgrade():
    c = {"risk": "low", "budget": {"max_wall_seconds": 10, "max_tokens": 100}}
    with pytest.raises(Hold):
        StrategyRouter().select(c, TaskFacts(), available=frozenset({"direct"}))
    with pytest.raises(Hold):
        StrategyRouter().select(
            c, TaskFacts(unresolved_questions=True), available=frozenset({"discovery"})
        )


def test_dev02_compile_adaptive_uses_observed_facts_not_draft_strategy(deployment):
    d = deployment
    p = d.prepare()
    c = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    g = d.store.get(d.scope, "workgraph", p["graph_ref"])
    facts = {
        n["node_id"]: TaskFacts(deterministic_verifier=True, local_change=True, uncertainty="low")
        for n in g["nodes"]
    }
    graph, _decisions = d.runtime.graphs.compile_adaptive(
        g,
        c,
        p["contract_ref"],
        facts_by_node=facts,
        available=frozenset({"direct", "bounded_loop"}),
    )
    assert graph["nodes"][0]["strategy"] == "direct" and g["nodes"][0]["strategy"] == "bounded_loop"
    facts[graph["nodes"][0]["node_id"]] = TaskFacts(
        changed_paths=("contracts/a.json",), uncertainty="low"
    )
    with pytest.raises(Hold):
        d.runtime.graphs.compile_adaptive(
            g, c, p["contract_ref"], facts_by_node=facts, available=frozenset({"bounded_loop"})
        )


def test_dev02_resume_wrong_worker_never_calls_provider(deployment):
    d = deployment
    p = d.prepare()
    _x, ss = paused(d, p)
    r = ss.receive(
        d.actor, p["goal_id"], "resume", "resume", expected_contract_ref=p["contract_ref"], key="r"
    )
    calls = []
    with pytest.raises(Hold):
        ss.resume(
            d.actor,
            r["steering_id"],
            replace(d.worker, subject_id="wrong-worker"),
            lambda *args: calls.append(args),
        )
    assert not calls


def test_dev02_direct_runtime_start_rechecks_live_authority(deployment):
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    d.decisions[digest(p["decision_ref"])]["revoked"] = True
    with pytest.raises(Hold):
        d.runtime.start(d.worker, x, "native-exact")
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "created"


@pytest.mark.parametrize("proof", [1, "yes", {}, None])
def test_dev02_output_stop_proof_must_be_boolean_true(deployment, proof):
    d = deployment
    d.prepare()
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "native-exact")
    with pytest.raises(Hold, match="termination"):
        d.runtime.output_ready(
            d.worker,
            x["run_id"],
            x["lease"]["lease_id"],
            x["lease"]["fencing_token"],
            {},
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_microunits": 0,
                "status": "measured",
            },
            process_stopped=proof,
        )


def test_dev02_actual_budget_overrun_blocks_verified_completion(deployment):
    d = deployment
    p = d.prepare()
    w, ws, _port, _ = rig(d, p)
    x = d.runtime.claim(d.worker)
    execute(d, p, w, ws, x)
    # Simulate a trusted meter's late measured overrun; evidence must not hide it.
    with d.store.tx() as db:
        d.runtime.budgets.settle(
            db,
            d.scope,
            x["run_id"],
            {
                "input_tokens": 100000,
                "output_tokens": 0,
                "cost_microunits": 0,
                "status": "measured",
            },
        )
    assert d.runtime.budgets.totals(d.scope, p["goal_id"])["overruns"] == 1
    with pytest.raises(Hold, match="overrun"):
        verify(d, x)
    assert d.store.head(d.scope, "work", x["node"]["work_id"])["state"] == "verifying"


def test_dev02_execution_reference_real_outputs_and_verification(tmp_path):
    from amplai_foundry.runtime.execution.reference import run_execution_reference

    root = tmp_path / "execution-reference"
    result = run_execution_reference(root)
    assert result["status"] == "verified" and len(result["outputs"]) == 2
    assert result["external_provider_qualified"] is False
    assert all((root / path).is_file() for path in result["outputs"])
    assert json.loads((root / "execution-report.json").read_text()) == result


@pytest.mark.parametrize("ceiling", ["root", "run"])
def test_dev02_elapsed_budgets_do_not_reset_at_resume_or_poll(deployment, ceiling):
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    with d.store.tx() as db:
        if ceiling == "root":
            h = d.store.head(d.scope, "goal", p["goal_id"], db=db)
            d.store.cas(
                db,
                d.scope,
                "goal",
                p["goal_id"],
                h["row_version"],
                h["state"],
                {**h["data"], "started_at_epoch": d.store.clock() - 100000},
            )
        else:
            db.execute(
                "UPDATE reservations SET started=? WHERE tenant=? AND project=? AND run_id=?",
                (d.store.clock() - 100000, *d.scope.keys(), x["run_id"]),
            )
    with pytest.raises(Hold, match="ceilings"):
        execution_envelope(d.runtime, d.worker, x)


@pytest.mark.parametrize("controller_first", [False, True])
def test_dev02_coordinator_pause_exact_resume_to_actual_verified_output(
    deployment, controller_first
):
    """Controlled native protocol fixture; not a live provider qualification.

    ``controller_first`` forces the order a loaded CI runner produced: the controller's
    stop_and_snapshot records "paused" before the execute thread records its pause request; the
    head must stay "paused" so resume_exact admits it."""
    import time

    from amplai_foundry.agent_drivers.ports import ZERO_USAGE
    from amplai_foundry.sandbox.local import DataSandbox

    d = deployment
    p = d.prepare()

    class ResumableFixture(RecipePort):
        def start(self, prepared):
            did = prepared["dispatch_id"]
            self.original = prepared
            self.journal.transition(
                did, {"prepared"}, "running", session_handle="fixture-exact", process_stopped=False
            )
            self.native_started = True
            return did

        def cancel(self, handle):
            self.journal.update(handle, state="paused", process_stopped=True)
            return {"process_stopped": True, "session_handle": "fixture-exact"}

        def resume(self, dispatch, prompt, workspace, checkpoint):
            assert checkpoint["session_handle"] == "fixture-exact"
            did = dispatch["dispatch_id"]
            self.resume_count = getattr(self, "resume_count", 0) + 1
            self.journal.create(did, {"resume_source": checkpoint, "workspace": str(workspace)})
            DataSandbox(workspace).execute(json.loads(prompt)["operations"])
            self.journal.update(
                did,
                state="completed",
                session_handle="fixture-exact",
                process_stopped=True,
                usage=ZERO_USAGE,
            )
            return did

    port = ResumableFixture(SessionJournal(d.root / "fixture-journal"))
    w, ws, _, _ = rig(d, p, port)
    x = d.runtime.claim(d.worker)
    ss = SteeringService(d.runtime)
    if controller_first:
        original_fail = w._fail

        def late_fail(worker, did, port, handles, options, exc):
            # The CI order: the execute thread saw the pause before the controller moved the
            # fence (EXECUTION_PAUSED, not STALE_RUN) and recorded it after stop_and_snapshot
            # had already set the head "paused"
            for _ in range(500):
                if d.store.head(d.scope, "worker-execution", did)["state"] == "paused":
                    break
                time.sleep(0.01)
            paused = Hold("EXECUTION_PAUSED", "Goal no longer admits a new driver operation")
            return original_fail(worker, did, port, handles, options, paused)

        w._fail = late_fail
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute, d, p, w, ws, x)
        # Pause only after the native session started: the run head can read "running" before
        # the fixture's start ran, and a pause that early leaves nothing to resume (loaded CI).
        for _ in range(500):
            if d.store.head(d.scope, "run", x["run_id"])["state"] == "running" and getattr(
                port, "native_started", False
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Fixture did not reach native running state")
        q = ss.receive(
            d.actor,
            p["goal_id"],
            "pause",
            "pause",
            expected_contract_ref=p["contract_ref"],
            key="pause-coordinator",
        )
        assert (
            ss.quiesce(
                d.actor,
                q["steering_id"],
                lambda run, kind: w.stop_and_snapshot(d.worker, run, kind),
            )["status"]
            == "applied"
        )
        with pytest.raises(Hold):
            future.result(timeout=5)
    q = ss.receive(
        d.actor,
        p["goal_id"],
        "resume",
        "resume",
        expected_contract_ref=p["contract_ref"],
        key="resume-coordinator",
    )
    result = ss.resume(
        d.actor, q["steering_id"], d.worker, lambda run, cp: w.resume_exact(d.worker, run, cp)
    )
    assert result["status"] == "applied" and port.resume_count == 1
    assert result["resumed"][0]["lease"]["fencing_token"] > x["lease"]["fencing_token"]
    assert w.continue_resumed(d.worker, x["run_id"])["status"] == "verifying"
    assert (ws.root / x["run_id"] / "result.json").is_file()
    assert verify(d, x)["outcome"] == "pass"
