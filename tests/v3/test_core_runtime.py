from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from amplai_foundry.runtime.contracts.gates import Observation, StateMachines
from amplai_foundry.runtime.contracts.identity import (
    ID,
    canonical,
    digest,
    digest_bytes,
    new_id,
    now,
)
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.steering import SteeringService
from amplai_foundry.runtime.recovery.service import OutboxPump, RecoveryService
from amplai_foundry.runtime.reference import ReferenceDeployment, run_reference
from amplai_foundry.runtime.storage.store import Scope, Store


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"b": 2, "a": 1}, b'{"a":1,"b":2}'),
        ({"n": -0.0}, b'{"n":0}'),
        ({"n": 1e-6}, b'{"n":0.000001}'),
        ({"n": 1e-7}, b'{"n":1e-7}'),
        ({"n": 1e21}, b'{"n":1e+21}'),
        ({"s": "한글"}, '{"s":"한글"}'.encode()),
    ],
)
def test_canonical_vectors(value, expected):
    assert canonical(value) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 2**53, {"x": "\ud800"}])
def test_noncanonical_rejected(value):
    with pytest.raises((RuntimeFault, ValueError, TypeError)):
        canonical(value)


def test_utf16_ordering():
    assert canonical({"\ufb33": 1, "\U0001f600": 2}) == '{"😀":2,"דּ":1}'.encode()


def test_approved_schemas_exact():
    contracts = Contracts()
    assert len(contracts.definitions) == 30
    base = Path(__file__).parents[2] / "src/amplai_foundry/runtime/contracts/data/schemas"
    for name, schema in contracts.definitions.items():
        assert schema == json.loads((base / (name + ".schema.json")).read_text())


def test_scope_immutable_and_cas(tmp_path):
    with Store(tmp_path / "state") as s:
        a = Scope("t", "a")
        b = Scope("t", "b")
        with s.tx() as db:
            ref = s.put(db, a, "record", "one", 1, {"v": 1})
            s.cas(db, a, "x", "one", 0, "draft", {"v": 1})
        with pytest.raises(RuntimeFault):
            s.get(b, "record", ref)
        with pytest.raises(Conflict), s.tx() as db:
            s.put(db, a, "record", "one", 1, {"v": 2})
        with pytest.raises(Conflict), s.tx() as db:
            s.cas(db, a, "x", "one", 0, "draft", {"v": 2})
        assert s.get(a, "record", ref) == {"v": 1}


def test_single_writer_and_owner_epoch(tmp_path):
    root = tmp_path / "s"
    with Store(root) as s:
        epoch = s.epoch
        with pytest.raises(Hold):
            Store(root)
    with Store(root) as s:
        assert s.epoch > epoch


def test_atomic_command_idempotency_and_scope(deployment):
    s = deployment.store
    scope = deployment.scope
    counter = []

    def op(db):
        counter.append(1)
        return {"answer": 1}

    assert s.command(scope, "actor", "key", {"x": 1}, op) == s.command(
        scope, "actor", "key", {"x": 1}, op
    )
    assert len(counter) == 1
    with pytest.raises(Conflict):
        s.command(scope, "actor", "key", {"x": 2}, op)
    assert s.command(Scope(scope.tenant_id, "other"), "actor", "key", {"x": 1}, op)["answer"] == 1
    assert len(counter) == 2


def test_transaction_rollback_and_no_external_io(deployment):
    s = deployment.store
    with pytest.raises(RuntimeFault), s.tx() as db:
        s.put(db, deployment.scope, "x", "rollback", 1, {"x": 1})
        s.assert_outside_tx()
    assert s.list_objects(deployment.scope, "x") == []


def test_artifact_actual_bytes_scope_and_secret_guard(deployment):
    d = deployment
    a = d.artifacts.admit(d.scope, b'{"x":1}', "application/json")
    assert d.artifacts.read(d.scope, a) == b'{"x":1}'
    with pytest.raises(RuntimeFault):
        d.artifacts.read(Scope("other", "project"), a)
    with pytest.raises(RuntimeFault):
        d.artifacts.read(d.scope, {**a, "size_bytes": 100})
    with pytest.raises(Hold):
        d.artifacts.admit(d.scope, b"api_key = " + b"a" * 40, "text/plain")
    with pytest.raises(Hold):
        d.artifacts.read(d.scope, a, trusted=True)
    d.artifacts._path(d.scope, a["digest"]).write_bytes(b"corrupt")
    with pytest.raises(Hold):
        d.artifacts.read(d.scope, a)


def test_full_cross_app_execution(tmp_path):
    result = run_reference(tmp_path / "actual", two_apps=True)
    assert result["status"] == "verified" and len(result["runs"]) == 2
    assert result["budget"]["unknown_runs"] == 0
    assert (tmp_path / "actual/reference-report.json").is_file()


def test_worker_output_not_success_and_independent_gate(deployment, prepared):
    d = deployment
    dispatch = d.runtime.claim(d.worker, goal_id=prepared["goal_id"])
    d.driver.run(d.runtime, d.worker, dispatch, prepared["recipes"][dispatch["lease"]["work_id"]])
    assert d.store.head(d.scope, "run", dispatch["run_id"])["state"] == "verifying"
    with pytest.raises(Hold):
        d.verification.finish_work(d.verifier, dispatch["run_id"])
    with pytest.raises(RuntimeFault):
        d.verification.finish_work(d.worker, dispatch["run_id"])
    assert d.store.head(d.scope, "goal", prepared["goal_id"])["state"] != "verified"


def test_revoked_live_decision_stops_admission(deployment, prepared):
    d = deployment
    d.decisions[digest(prepared["decision_ref"])]["generation"] += 1
    with pytest.raises(Hold):
        d.runtime.claim(d.worker)
    assert d.store.list_objects(d.scope, "run-record") == []


def test_unqualified_driver_cannot_claim(deployment, prepared):
    d = deployment
    old = prepared["execution_profile"]["driver_profile_ref"]
    profile = d.store.get(d.scope, "driver-capabilities", old)
    changed = {**profile, "driver_id": "disabled-driver", "maturity": "experimental"}
    ref = d.put("driver-capabilities", "disabled-driver", changed)
    with d.store.tx() as db:
        h = d.store.head(d.scope, "goal", prepared["goal_id"], db=db)
        data = copy.deepcopy(h["data"])
        data["profile"]["driver_profile_ref"] = ref
        d.store.cas(db, d.scope, "goal", prepared["goal_id"], h["row_version"], h["state"], data)
    with pytest.raises(Hold):
        d.runtime.claim(d.worker)


def test_old_fence_and_native_duplicate_start(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker)
    lease = x["lease"]
    d.runtime.start(d.worker, x, "exact:1")
    assert d.runtime.start(d.worker, x, "exact:1")["status"] == "acknowledged"
    with pytest.raises(Conflict):
        d.runtime.start(d.worker, x, "exact:2")
    with pytest.raises(Hold):
        d.runtime.lease(d.worker, x["run_id"], lease["lease_id"], lease["fencing_token"] - 1)


def test_native_start_does_not_allow_latest(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker)
    with pytest.raises(RuntimeFault):
        d.runtime.start(d.worker, x, "latest")


def test_parallel_claims_are_unique(deployment, prepared):
    d = deployment
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: d.runtime.claim(d.worker), range(2)))
    assert len({c["run_id"] for c in claims}) == 2
    assert len({c["lease"]["work_id"] for c in claims}) == 2
    assert d.runtime.claim(d.worker) is None


def test_pause_ack_not_applied_then_exact_resume(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "session-exact")
    ss = SteeringService(d.runtime)
    q = ss.receive(
        d.actor,
        prepared["goal_id"],
        "pause",
        "Pause safely",
        expected_contract_ref=prepared["contract_ref"],
        key="pause",
    )
    assert d.runtime.claim(d.worker) is None
    assert ss.native_ack(d.worker, q["steering_id"], "turn-1")["applied"] is False
    snapshot = d.artifacts.admit(d.scope, b'{"changes":[]}', "application/json")
    result = ss.quiesce(
        d.actor,
        q["steering_id"],
        lambda *_: {
            "process_stopped": True,
            "session_handle": "session-exact",
            "workspace_diff_artifact": snapshot,
        },
    )
    assert result["status"] == "applied"
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "paused"
    resume = ss.receive(
        d.actor,
        prepared["goal_id"],
        "resume",
        "Resume exact checkpoint",
        expected_contract_ref=prepared["contract_ref"],
        key="resume",
    )
    resumed = ss.resume(
        d.actor,
        resume["steering_id"],
        d.worker,
        lambda _, cp: {"resumed": True, "session_handle": cp["driver_session_handle"]},
    )
    assert resumed["status"] == "applied"
    with pytest.raises(Hold):
        d.runtime.lease(d.worker, x["run_id"], x["lease"]["lease_id"], x["lease"]["fencing_token"])
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "running"


def test_cancel_waits_for_confirmed_process(deployment, prepared):
    d = deployment
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "exact")
    ss = SteeringService(d.runtime)
    q = ss.receive(
        d.actor,
        prepared["goal_id"],
        "cancel",
        "Cancel",
        expected_contract_ref=prepared["contract_ref"],
        key="cancel",
    )
    assert (
        ss.quiesce(d.actor, q["steering_id"], lambda *_: {"process_stopped": False})["status"]
        == "queued"
    )
    assert d.store.head(d.scope, "goal", prepared["goal_id"])["state"] != "cancelled"
    assert (
        ss.quiesce(d.actor, q["steering_id"], lambda *_: {"process_stopped": True})["status"]
        == "applied"
    )
    assert d.store.head(d.scope, "goal", prepared["goal_id"])["state"] == "cancelled"


def test_expired_lease_requires_process_reconciliation(tmp_path):
    clock = [1_789_500_000.0]
    with ReferenceDeployment(tmp_path / "r", clock=lambda: clock[0]) as d:
        p = d.prepare()
        x = d.runtime.claim(d.worker)
        d.runtime.start(d.worker, x, "exact")
        clock[0] += 121
        with pytest.raises(Hold):
            d.runtime.lease(
                d.worker, x["run_id"], x["lease"]["lease_id"], x["lease"]["fencing_token"]
            )
        result = d.runtime.reap(d.scope, lambda *_: False)
        assert result[0]["process_stopped"] is False and result[0]["state"] != "lost"
        result = d.runtime.reap(d.scope, lambda *_: True)
        assert result[0]["state"] == "lost"
        assert d.runtime.budgets.totals(d.scope, p["goal_id"])["unknown_runs"] == 1


def test_backup_restore_enters_killed_state(deployment, prepared, tmp_path):
    d = deployment
    d.execute(prepared)
    actor = replace(d.actor, permissions=d.actor.permissions | {"runtime.backup"})
    backup = tmp_path / "backup"
    RecoveryService(d.store).backup(actor, backup)
    output = RecoveryService.restore(backup, tmp_path / "restore", operator_confirmed=True)
    with Store(tmp_path / "restore") as s:
        assert s.head(d.scope, "runtime-control", "kill")["data"]["enabled"]
        assert s.epoch > d.store.epoch
        assert len(s.list_objects(d.scope, "goal-contract")) == 1
    assert output["status"] == "restored_admission_disabled"


def test_outbox_retries_same_id(deployment):
    d = deployment
    with d.store.tx() as db:
        d.store.event(db, d.scope, "goal", "test", "test.event", {"x": 1})
    seen = []

    def deliver(event):
        seen.append(event["event_id"])
        return True

    first = OutboxPump(d.store).pump(deliver)
    assert first and first[0]["acknowledged"]
    assert OutboxPump(d.store).pump(deliver) == []


def test_now_is_microsecond_utc_iso8601_with_z_suffix():
    value = now()
    assert value.endswith("Z")
    assert "+00:00" not in value
    fractional = value[:-1].split(".")[1]
    assert len(fractional) == 6
    assert fractional.isdigit()


def test_new_id_matches_prefix_hex_and_regex_and_is_unique():
    first = new_id("goal")
    second = new_id("goal")
    prefix, _, hex_part = first.partition("-")
    assert prefix == "goal"
    assert len(hex_part) == 32
    assert all(c in "0123456789abcdef" for c in hex_part)
    assert ID.fullmatch(first)
    assert first != second


def test_digest_bytes_is_sha256_prefixed_hexdigest():
    import hashlib

    data = b"hello world"
    result = digest_bytes(data)
    assert result == "sha256:" + hashlib.sha256(data).hexdigest()
    digest_hex = result.removeprefix("sha256:")
    assert len(digest_hex) == 64
    assert all(c in "0123456789abcdef" for c in digest_hex)


def test_all_state_edges_require_declared_guards():
    c = Contracts()
    m = StateMachines(c)
    for machine, spec in c.states["machines"].items():
        for t in spec["transitions"]:
            for start in t["from"]:
                with pytest.raises(Hold):
                    m.transition(machine, start, t["command"], {})
                state, _ = m.transition(
                    machine,
                    start,
                    t["command"],
                    {g: Observation.check(True, "Test guard observation") for g in t["guards"]},
                )
                assert state == t["to"]
