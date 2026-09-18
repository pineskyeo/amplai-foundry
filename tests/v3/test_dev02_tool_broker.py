"""Native call/effect correlation, late results, live revocation and scoped secrets."""

import time
from copy import deepcopy

import pytest

from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.effects.service import Effects
from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.envelope import execution_envelope
from amplai_foundry.tool_broker.native import NativeToolBroker
from amplai_foundry.tool_broker.service import SecretBroker, Tool, ToolRegistry, ToolResult


def setup(d, *, timeout=1, delay=0):
    p = d.prepare()
    x = d.runtime.claim(d.worker)
    d.runtime.start(d.worker, x, "native-exact")
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
        return ToolResult("applied", "operation-one", args)

    def reconcile(key, op, limit):
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
        reconcile,
        timeout,
    )
    reg = ToolRegistry()
    reg.register(tool)
    effects = Effects(d.runtime, reg)
    broker = NativeToolBroker(effects, {"write": tool_ref})
    return p, x, envelope, broker, effects, calls, external


def invoke(d, env, broker, arguments='{"value":1}', **changes):
    args = {
        "session_handle": "native-exact",
        "native_turn_id": "turn-one",
        "native_call_id": "call-one",
        "name": "write",
        "arguments": arguments,
    }
    args.update(changes)
    return broker.invoke(d.worker, env, **args)


def test_dev02_native_call_replay_is_one_actual_effect(deployment):
    d = deployment
    _p, _x, e, broker, _effects, calls, external = setup(d)
    a = invoke(d, e, broker)
    b = invoke(d, e, broker)
    assert a == b and len(calls) == 1 and a["receipt"]["state"] == "applied"
    assert len(external) == 1 and a["goal_verified"] is False
    with pytest.raises(Conflict):
        invoke(d, e, broker, '{"value":2}')
    assert len(calls) == 1


def test_dev02_native_call_timeout_late_commit_requires_reconcile(deployment):
    d = deployment
    _p, _x, e, broker, effects, calls, external = setup(d, timeout=0.02, delay=0.08)
    with pytest.raises(Hold, match="unknown"):
        invoke(d, e, broker)
    effect = d.store.conn.execute("SELECT id FROM heads WHERE kind='effect'").fetchone()[0]
    assert d.store.head(d.scope, "effect", effect)["state"] == "unknown"
    time.sleep(0.12)
    assert len(external) == 1 and d.store.head(d.scope, "effect", effect)["state"] == "unknown"
    with pytest.raises(Hold):
        invoke(d, e, broker)
    assert len(calls) == 1
    receipt = effects.reconcile(d.actor, effect)
    assert receipt["state"] == "reconciled" and receipt["resolved_outcome"] == "applied"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"session_handle": "another-session"},
        {"native_turn_id": ""},
        {"native_call_id": ""},
        {"name": "shell"},
        {"arguments": '{"value":1,"value":2}'},
        {"arguments": '{"value":NaN}'},
        {"arguments": '{"unexpected":2}'},
    ],
)
def test_dev02_native_tool_rejects_ambiguous_or_unbound_call(deployment, changes):
    d = deployment
    _p, _x, e, broker, _effects, calls, _external = setup(d)
    with pytest.raises(RuntimeFault):
        invoke(d, e, broker, **changes)
    assert calls == []


def test_dev02_native_tool_live_grant_revocation(deployment):
    d = deployment
    p, _x, e, broker, _effects, calls, _external = setup(d)
    d.decisions[digest(p["decision_ref"])]["revoked"] = True
    with pytest.raises(Hold):
        invoke(d, e, broker)
    assert not calls


def test_dev02_native_tool_denied_envelope_cannot_borrow_grant(deployment):
    d = deployment
    _p, _x, e, broker, _effects, calls, _external = setup(d)
    e = deepcopy(e)
    e["effective_capabilities"] = []
    with pytest.raises(Hold):
        invoke(d, e, broker)
    assert not calls


def test_dev02_secret_handles_expire_and_are_scope_endpoint_bound():
    clock = [100.0]
    broker = SecretBroker(
        {"auth": "secret-value"},
        allowed_hosts={"auth": {"api.example.test"}},
        clock=lambda: clock[0],
    )
    handle = broker.issue_handle("auth", "api.example.test", ttl_seconds=5, scope="scope-one")
    assert "secret-value" not in repr(broker) and "secret-value" not in handle
    assert (
        broker.with_secret(handle, "https://api.example.test/v1", lambda s: s, scope="scope-one")
        == "secret-value"
    )
    for url in [
        "http://api.example.test",
        "https://evil.test",
        "https://api.example.test:444",
        "https://user:pw@api.example.test",
    ]:
        with pytest.raises(RuntimeFault):
            broker.with_secret(handle, url, lambda s: s, scope="scope-one")
    with pytest.raises(Hold):
        broker.with_secret(handle, "https://api.example.test", lambda s: s, scope="scope-two")
    clock[0] = 105
    with pytest.raises(Hold):
        broker.with_secret(handle, "https://api.example.test", lambda s: s, scope="scope-one")


def test_dev02_secret_revocation_is_immediate():
    b = SecretBroker({"auth": "secret"}, allowed_hosts={"auth": {"example.test"}})
    h = b.issue_handle("auth", "example.test")
    b.revoke(h)
    with pytest.raises(Hold):
        b.with_secret(h, "https://example.test", lambda s: s)
    with pytest.raises(Hold):
        b.issue_handle("auth", "example.test", ttl_seconds=0)


def test_dev02_direct_effect_prepare_cannot_borrow_root_capability(deployment):
    d = deployment
    _p, x, e, broker, _effects, calls, _external = setup(d)
    # A narrower work envelope must be enforced by Effects, not just the facade.
    work = d.store.head(d.scope, "work", x["node"]["work_id"])
    with d.store.tx() as db:
        d.store.cas(
            db,
            d.scope,
            "work",
            x["node"]["work_id"],
            work["row_version"],
            work["state"],
            {**work["data"], "node": {**work["data"]["node"], "capabilities": []}},
        )
    with pytest.raises(Hold):
        invoke(d, e, broker)
    assert calls == []
