"""Tool/effect protocol: prepare/dispatch/unknown outcome, secret broker, containment.

Catalog group "effects" (T-044..T-051). Exercises amplai_foundry.runtime.effects.service.Effects
directly against a real ReferenceDeployment; no src/ mutation, no skip/xfail.
"""

from __future__ import annotations

import time

import pytest

from amplai_foundry.runtime.contracts.identity import canonical, new_id, now
from amplai_foundry.runtime.effects.service import Effects
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.envelope import execution_envelope
from amplai_foundry.tool_broker.service import Tool, ToolRegistry, ToolResult


def _setup(d, *, timeout=1, delay=0, reconcile=True):
    """Real prepared deployment + a registered sandbox_write demo tool wired to Effects."""
    d.prepare()
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "effects-session")
    envelope = execution_envelope(d.runtime, d.worker, x)
    calls = []
    external = {}
    tool_ref = d.put(
        "tool-definition", "demo-write", {"id": "demo-write", "effect_class": "sandbox_write"}
    )

    def invoke(args, key, limit):
        calls.append(key)
        if delay:
            time.sleep(delay)
        external[key] = args
        return ToolResult("applied", "operation-" + key, args)

    def reconcile_fn(key, op, limit):
        return ToolResult(
            "applied" if key in external else "not_applied", op, external.get(key, {})
        )

    tool = Tool(
        "demo-write",
        tool_ref,
        "workspace.write",
        "sandbox:demo-output",
        "sandbox_write",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        {"type": "object"},
        invoke,
        reconcile_fn if reconcile else None,
        timeout,
    )
    reg = ToolRegistry()
    reg.register(tool)
    effects = Effects(d.runtime, reg)
    return envelope, effects, tool_ref, tool, calls, external


def _request(d, envelope, tool_ref, tool, *, effect_key, value=1):
    """Build a real EffectRequest the way a broker would, for direct Effects.prepare calls."""
    args = {"value": value}
    artifact = d.runtime.artifacts.admit(
        d.scope, canonical(args), "application/json", trust="worker"
    )
    return {
        "schema_version": "3.0.0",
        "scope": d.scope.wire(),
        "effect_id": new_id("effect"),
        "run_id": envelope["run_id"],
        "contract_ref": envelope["contract_ref"],
        "graph_ref": envelope["graph_ref"],
        "lease_id": envelope["lease_id"],
        "fencing_token": envelope["fencing_token"],
        "tool_ref": tool_ref,
        "effect_class": tool.effect_class,
        "args_artifact": artifact,
        "effect_key": effect_key,
        "grant_ref": envelope["grant_ref"],
        "requested_at": now(),
    }


def test_t044_timeout_may_have_applied_requires_reconcile_before_resend(deployment):
    # given: remote receives write, response is lost (adapter exceeds tool timeout)
    d = deployment
    envelope, effects, tool_ref, tool, calls, external = _setup(d, timeout=0.02, delay=0.08)
    req = _request(d, envelope, tool_ref, tool, effect_key="t044-key")
    effects.prepare(d.worker, req)
    # when: dispatch races the timeout
    with pytest.raises(Hold, match="unknown") as excinfo:
        effects.dispatch(d.scope, req["effect_id"])
    assert excinfo.value.code == "EFFECT_UNKNOWN"
    # expected: receipt is UNKNOWN, not a guessed success/failure
    head = d.store.head(d.scope, "effect", req["effect_id"])
    assert head["state"] == "unknown"
    time.sleep(0.12)  # adapter's late write actually lands after the deadline
    assert len(external) == 1
    # expected: a blind resend is refused; reconcile is required first
    with pytest.raises(Hold) as resend:
        effects.dispatch(d.scope, req["effect_id"])
    assert resend.value.code == "EFFECT_UNKNOWN"
    assert len(calls) == 1
    receipt = effects.reconcile(d.actor, req["effect_id"])
    assert receipt["state"] == "reconciled" and receipt["resolved_outcome"] == "applied"
    assert len(calls) == 1  # reconcile never re-triggers the write adapter


def test_t045_same_effect_key_new_args_is_idempotency_conflict(deployment):
    # given: an effect key already bound to a prepared payload
    d = deployment
    envelope, effects, tool_ref, tool, calls, _external = _setup(d)
    first = _request(d, envelope, tool_ref, tool, effect_key="t045-key", value=1)
    receipt = effects.prepare(d.worker, first)
    # when: the same effect key is dispatched again with altered args
    second = _request(d, envelope, tool_ref, tool, effect_key="t045-key", value=2)
    with pytest.raises(Conflict) as excinfo:
        effects.prepare(d.worker, second)
    # expected: IDEMPOTENCY_CONFLICT (EFFECT_KEY_CONFLICT), original binding unchanged
    assert excinfo.value.code == "EFFECT_KEY_CONFLICT"
    still = d.store.head(d.scope, "effect", first["effect_id"])
    assert still["data"]["receipt"] == receipt and still["state"] == "prepared"
    with pytest.raises(RuntimeFault) as missing:
        d.store.head(d.scope, "effect", second["effect_id"])
    assert missing.value.code == "NOT_FOUND"  # no duplicate effect was created
    assert calls == []


def test_t046_revoke_before_dispatch_denies_send_and_stays_unsent(deployment):
    # given: grant revoked after preparation, before the broker call
    d = deployment
    import amplai_foundry.runtime.contracts.identity as identity_mod

    envelope, effects, tool_ref, tool, calls, _external = _setup(d)
    req = _request(d, envelope, tool_ref, tool, effect_key="t046-key")
    effects.prepare(d.worker, req)
    # revoke through the grant's authoritative decision, the same path other tests use
    grant = d.store.get(d.scope, "execution-grant", req["grant_ref"])
    d.decisions[identity_mod.digest(grant["decision_ref"])]["revoked"] = True
    # when: dispatch is attempted
    with pytest.raises(Hold):
        effects.dispatch(d.scope, req["effect_id"])
    # expected: deny new send; effect record is not applied because it was never sent
    assert not calls
    head = d.store.head(d.scope, "effect", req["effect_id"])
    assert head["state"] == "prepared"


def test_t047_revoke_after_dispatch_does_not_claim_rollback(deployment):
    # given: external write accepted (applied) before the grant is revoked
    d = deployment
    import amplai_foundry.runtime.contracts.identity as identity_mod

    envelope, effects, tool_ref, tool, calls, external = _setup(d)
    req = _request(d, envelope, tool_ref, tool, effect_key="t047-key")
    effects.prepare(d.worker, req)
    applied = effects.dispatch(d.scope, req["effect_id"])
    assert applied["state"] == "applied" and len(calls) == 1 and len(external) == 1
    grant = d.store.get(d.scope, "execution-grant", req["grant_ref"])
    # when: revocation is observed after the external effect already landed
    d.decisions[identity_mod.digest(grant["decision_ref"])]["revoked"] = True
    # expected: the applied receipt is not rewritten to any rolled-back state
    head = d.store.head(d.scope, "effect", req["effect_id"])
    assert head["state"] == "applied" and head["data"]["receipt"] == applied
    # expected: reconcile refuses to touch a terminal receipt; outcome is only what was observed
    with pytest.raises(Conflict) as excinfo:
        effects.reconcile(d.actor, req["effect_id"])
    assert excinfo.value.code == "EFFECT_RECONCILE_STATE"
    still = d.store.head(d.scope, "effect", req["effect_id"])
    assert still["data"]["receipt"] == applied  # receipt still reveals the real outcome


def test_t048_non_idempotent_endpoint_timeout_holds_for_human_reconciliation(deployment):
    # given: endpoint has no reconciliation adapter installed (no idempotency + lookup)
    d = deployment
    envelope, effects, tool_ref, tool, calls, external = _setup(
        d, timeout=0.02, delay=0.08, reconcile=False
    )
    req = _request(d, envelope, tool_ref, tool, effect_key="t048-key")
    effects.prepare(d.worker, req)
    # when: the write times out
    with pytest.raises(Hold) as first:
        effects.dispatch(d.scope, req["effect_id"])
    assert first.value.code == "EFFECT_UNKNOWN"
    time.sleep(0.12)
    assert len(external) == 1
    # expected: no blind retry is offered
    with pytest.raises(Hold) as second:
        effects.dispatch(d.scope, req["effect_id"])
    assert second.value.code == "EFFECT_UNKNOWN"
    assert len(calls) == 1
    # expected: reconciliation itself is a HOLD because no trusted read-only adapter exists
    with pytest.raises(Hold) as excinfo:
        effects.reconcile(d.actor, req["effect_id"])
    assert excinfo.value.code == "NO_RECONCILER"
    head = d.store.head(d.scope, "effect", req["effect_id"])
    assert head["state"] == "unknown"  # stays a human-reconciliation hold, not resolved


def test_t049_compensation_is_a_new_effect_original_not_erased(deployment):
    # given: an original write applied but wrong
    d = deployment
    envelope, effects, tool_ref, tool, calls, external = _setup(d)
    original_req = _request(d, envelope, tool_ref, tool, effect_key="t049-original", value=1)
    effects.prepare(d.worker, original_req)
    original_receipt = effects.dispatch(d.scope, original_req["effect_id"])
    assert original_receipt["state"] == "applied"
    # when: an undo is requested -> a second, distinct, newly-authorized effect referencing it
    compensation_req = _request(
        d, envelope, tool_ref, tool, effect_key="t049-compensate:" + original_req["effect_id"]
    )
    effects.prepare(d.worker, compensation_req)
    compensation_receipt = effects.dispatch(d.scope, compensation_req["effect_id"])
    # expected: new effect id, own receipt, linked to the original only by its own key/id
    assert compensation_req["effect_id"] != original_req["effect_id"]
    assert compensation_receipt["state"] == "applied"
    assert compensation_receipt["effect_id"] != original_receipt["effect_id"]
    # expected: original receipt is untouched, not overwritten or removed
    still_original = d.store.head(d.scope, "effect", original_req["effect_id"])
    assert still_original["data"]["receipt"] == original_receipt
    assert still_original["state"] == "applied"
    assert len(calls) == 2 and len(external) == 2


def test_t050_partial_batch_keeps_item_receipts_and_partial_state(deployment):
    # given: some external items applied, others unknown  # when: aggregate result
    # expected: item receipts + partial/unknown state; not all succeeded
    d = deployment
    envelope, effects, tool_ref, tool, _calls, _external = _setup(d, timeout=1)
    flaky_ref = d.put(
        "tool-definition", "demo-flaky", {"id": "demo-flaky", "effect_class": "sandbox_write"}
    )

    def explode(args, key, limit):
        raise RuntimeError("remote hung after commit")

    flaky = Tool(
        "demo-flaky",
        flaky_ref,
        tool.action,
        tool.resource,
        tool.effect_class,
        tool.input_schema,
        tool.output_schema,
        explode,
        None,
        1,
    )
    effects.tools.register(flaky)
    a = _request(d, envelope, tool_ref, tool, effect_key="t050-a", value=1)
    b = _request(d, envelope, flaky_ref, flaky, effect_key="t050-b", value=2)
    effects.prepare(d.worker, a)
    effects.prepare(d.worker, b)
    batch = effects.dispatch_batch(d.scope, [a["effect_id"], b["effect_id"]])
    assert batch["atomic"] is False and batch["all_succeeded"] is False
    assert batch["state"] == "partial"
    assert [i["state"] for i in batch["items"]] == ["applied", "unknown"]
    assert d.store.head(d.scope, "effect", a["effect_id"])["state"] == "applied"
    assert d.store.head(d.scope, "effect", b["effect_id"])["state"] == "unknown"


def test_t051_remote_callback_spoof_is_rejected_and_pending_request_unchanged(deployment):
    # given: callback with valid run ID but wrong project/call_id  # when: ingest callback
    # expected: reject, original pending request unchanged
    from amplai_foundry.runtime.storage.store import Scope

    d = deployment
    envelope, effects, tool_ref, tool, _calls, _external = _setup(d, timeout=0.02, delay=0.08)
    req = _request(d, envelope, tool_ref, tool, effect_key="t051-key")
    effects.prepare(d.worker, req)
    with pytest.raises(Hold):
        effects.dispatch(d.scope, req["effect_id"])  # times out -> unknown, pending reconcile
    before = d.store.head(d.scope, "effect", req["effect_id"])
    assert before["state"] == "unknown"
    payload = {"outcome": "applied", "note": "trust me"}
    # wrong call id, right run
    with pytest.raises(Hold) as wrong_call:
        effects.ingest_callback(
            d.scope,
            run_id=envelope["run_id"],
            effect_id=req["effect_id"],
            call_id="operation-forged",
            payload=payload,
        )
    assert wrong_call.value.code == "CALLBACK_REJECTED"
    # wrong project, right run id and effect id
    other = Scope(tenant_id=d.scope.tenant_id, project_id="other-project")
    with pytest.raises(Hold) as wrong_project:
        effects.ingest_callback(
            other,
            run_id=envelope["run_id"],
            effect_id=req["effect_id"],
            call_id="operation-t051-key",
            payload=payload,
        )
    assert wrong_project.value.code == "CALLBACK_REJECTED"
    after = d.store.head(d.scope, "effect", req["effect_id"])
    assert after == before  # pending request untouched, no callback object written
    assert d.store.list_objects(d.scope, "effect-callback") == []


def test_r403_batch_records_non_hold_faults_per_item_and_keeps_aggregating(deployment):
    d = deployment
    envelope, effects, tool_ref, tool, _calls, _external = _setup(d, timeout=1)
    a = _request(d, envelope, tool_ref, tool, effect_key="r403-a", value=1)
    effects.prepare(d.worker, a)
    batch = effects.dispatch_batch(d.scope, [a["effect_id"], "effect-does-not-exist"])
    assert [i["state"] for i in batch["items"]] == ["applied", "error"]
    assert batch["items"][1]["code"] == "NOT_FOUND"
    assert batch["state"] == "partial" and batch["all_succeeded"] is False
    assert d.store.head(d.scope, "effect", a["effect_id"])["state"] == "applied"
