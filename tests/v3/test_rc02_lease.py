"""RC-02 lease/fencing catalog cases T-039..T-043.

design/09, test-catalog group "lease" (G-09). Each test names the catalog id it
evidences and exercises the real runtime/execution/service.py lease and fencing
code, not a re-implementation of it.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from amplai_foundry.runtime.contracts.gates import Observation
from amplai_foundry.runtime.contracts.identity import new_id, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.steering import SteeringService


def new_revision(d, p, *, constraint_text):
    """Contract v2 from the active v1 through normal admission (mirrors
    test_rc01_e2e_cross_app.new_revision)."""
    old = d.store.get(d.scope, "goal-contract", p["contract_ref"])
    candidate = deepcopy(old)
    candidate["revision"] = 2
    candidate["constraints"].append(
        {
            "id": "C-T042",
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
    grant = d.store.get(d.scope, "execution-grant", p["grant_ref"])
    decision = d.store.get(d.scope, "demo-decision", p["decision_ref"])
    decision2 = {
        **decision,
        "decision_id": new_id("demo-decision"),
        "contract_ref": contract_ref,
        "graph_ref": graph_ref,
    }
    decision2_ref = d.put("demo-decision", decision2["decision_id"], decision2)
    from amplai_foundry.runtime.contracts.identity import digest

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


# ---------------------------------------------------------------- T-039


def test_t039_expired_lease_rejects_heartbeat_with_no_renewed_authority(deployment):
    """given: worker holds old lease after recovery / when: heartbeat / expected:
    STALE_LEASE, no renewed authority (service.py:324-335 expires<=clock() check)."""
    d = deployment
    d.prepare()
    x = d.runtime.claim(d.worker)
    lease = x["lease"]
    row_before = d.store.conn.execute(
        "SELECT expires FROM leases WHERE tenant=? AND project=? AND run_id=?",
        (*d.scope.keys(), x["run_id"]),
    ).fetchone()
    # move the server clock past the lease's own recorded expiry
    d.store.clock = lambda: row_before[0] + 1
    with pytest.raises(Hold) as exc:
        d.runtime.heartbeat(d.worker, x["run_id"], lease["lease_id"], lease["fencing_token"], 1)
    assert exc.value.code == "STALE_LEASE"
    row_after = d.store.conn.execute(
        "SELECT expires FROM leases WHERE tenant=? AND project=? AND run_id=?",
        (*d.scope.keys(), x["run_id"]),
    ).fetchone()
    assert row_after[0] == row_before[0], "rejected heartbeat must not renew authority"


# ---------------------------------------------------------------- T-040


def test_t040_monotonic_fence_never_reused_or_decreased_after_restart(deployment):
    """given: CP owner epoch advances after crash / when: worker claims same Work
    again / expected: fencing token never reused or decreases (service.py:474-475
    fence = previous+1, keyed by work_id; store.py:200-207 owner_epoch bump)."""
    d = deployment
    p = d.prepare()
    x1 = d.runtime.claim(d.worker)
    fence1 = x1["lease"]["fencing_token"]
    epoch1 = x1["lease"]["owner_epoch"]
    work_id = x1["lease"]["work_id"]
    # simulate CP restart: a fresh non-readonly Store on the same file bumps
    # owner_epoch this way (store.py:203-208); reproduced here on the live store.
    d.store.epoch += 1
    # simulate recovery (reap(), service.py:944-952) releasing the crashed
    # worker's resource claims and returning the orphaned work to admissible.
    with d.store.tx() as db:
        db.execute(
            "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
            (*d.scope.keys(), x1["run_id"]),
        )
        work = d.store.head(d.scope, "work", work_id, db=db)
        d.store.cas(db, d.scope, "work", work_id, work["row_version"], "ready", work["data"])
    x2 = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    assert x2 is not None and x2["lease"]["work_id"] == work_id
    fence2 = x2["lease"]["fencing_token"]
    assert fence2 == fence1 + 1, "fencing token must strictly increment, never reuse/decrease"
    assert x2["lease"]["owner_epoch"] == epoch1 + 1
    assert x2["run_id"] != x1["run_id"]


# ---------------------------------------------------------------- T-041


def test_t041_malformed_grant_time_fails_closed_not_retry_ready(deployment):
    """given: not_before field malformed / when: schedule (grant issuance) /
    expected: validation fails closed, nothing committed as retry-ready
    (registry.py:42-59 SCHEMA_INVALID; common.schema.json utc format+pattern)."""
    d = deployment
    p = d.prepare()
    grant = dict(d.store.get(d.scope, "execution-grant", p["grant_ref"]))
    grant.pop("signature", None)
    grant["grant_id"] = new_id("grant")
    grant["not_before"] = "not-a-timestamp"
    with pytest.raises(RuntimeFault) as exc:
        d.authority.issue(d.actor, grant)
    assert exc.value.code == "SCHEMA_INVALID"
    # fail closed: nothing was persisted for retry, the goal's active grant is unchanged
    with pytest.raises(RuntimeFault):
        d.store.get(
            d.scope,
            "execution-grant",
            {"kind": "execution-grant", "id": grant["grant_id"], "revision": 1, "digest": "x"},
        )
    goal = d.store.head(d.scope, "goal", p["goal_id"])
    assert goal["data"]["grant_ref"] == p["grant_ref"]


# ---------------------------------------------------------------- T-042


def test_t042_pause_revision_mismatch_rejects_same_run_resume(deployment):
    """given: paused Run bound to contract v1, desired contract becomes v2 /
    when: resume same Run / expected: reject same-run ref mutation
    (steering.py:521-531 CHECKPOINT_STALE); new Run comes from a fresh claim
    under the new contract instead."""
    d = deployment
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "native_exact")
    ss = SteeringService(d.runtime)
    q = ss.receive(
        d.actor, p["goal_id"], "pause", "pause", expected_contract_ref=p["contract_ref"], key="p042"
    )
    checkpoint_artifact = d.artifacts.admit(d.scope, b'{"delta":[]}', "application/json")
    ss.quiesce(
        d.actor,
        q["steering_id"],
        lambda *_: {
            "process_stopped": True,
            "session_handle": "native_exact",
            "workspace_diff_artifact": checkpoint_artifact,
        },
    )
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "paused"
    # queue the resume while contract v1 is still active (matches expected ref)
    r = ss.receive(
        d.actor, p["goal_id"], "resume", "go", expected_contract_ref=p["contract_ref"], key="r042"
    )
    # block the goal for replanning (steering.py:401-410 does this for any
    # revision-changing kind) so a new revision can be admitted
    with d.store.tx() as db:
        goal = d.store.head(d.scope, "goal", p["goal_id"], db=db)
        state, _gates = d.runtime.machines.transition(
            "goal", goal["state"], "block", {"G-01": Observation.check(True, "T-042 replan")}
        )
        d.store.cas(
            db,
            d.scope,
            "goal",
            p["goal_id"],
            goal["row_version"],
            state,
            {**goal["data"], "replan_reason": "T-042 contract revision"},
        )
    contract_ref, graph_ref, graph = new_revision(d, p, constraint_text="v2 for T-042")
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
    assert goal["data"]["active_contract_ref"] == contract_ref

    def native_resume_must_not_be_called(*_args):
        raise AssertionError("checkpoint is stale; native resume must not be attempted")

    with pytest.raises(Hold) as exc:
        ss.resume(d.actor, r["steering_id"], d.worker, native_resume_must_not_be_called)
    assert exc.value.code == "CHECKPOINT_STALE"
    # the v1 run/ref were not mutated in place
    assert d.store.head(d.scope, "run", x["run_id"])["state"] == "paused"
    # a new Run is admitted from normal claim, under the new contract, not the old work
    fresh = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    assert fresh is not None and fresh["contract_ref"] == contract_ref
    assert fresh["graph_ref"] == graph_ref
    assert fresh["lease"]["work_id"] in {n["work_id"] for n in graph["nodes"]}
    assert fresh["lease"]["work_id"] != x["lease"]["work_id"]


# ---------------------------------------------------------------- T-043


def test_t043_heartbeat_expiry_is_server_clock_not_worker_claimed(deployment):
    """given: worker's own clock is skewed from the server / when: heartbeat /
    expected: server time decides the new expiry; the worker has no parameter to
    claim its own expiry (service.py:724-742 heartbeat has no expiry argument;
    expiry = self.store.clock() + 120, server-computed unconditionally)."""
    d = deployment
    d.prepare()
    x = d.runtime.claim(d.worker)
    lease = x["lease"]
    server_now = lease["expires_at_epoch"] - 50
    d.store.clock = lambda: server_now
    result = d.runtime.heartbeat(
        d.worker, x["run_id"], lease["lease_id"], lease["fencing_token"], 1
    )
    assert result["expires_at_epoch"] == server_now + 120
    row = d.store.conn.execute(
        "SELECT expires FROM leases WHERE tenant=? AND project=? AND run_id=?",
        (*d.scope.keys(), x["run_id"]),
    ).fetchone()
    assert row[0] == server_now + 120, "stored lease expiry follows only the server clock"
