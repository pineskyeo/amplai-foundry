"""Work 034 S1c — regression-review P3 follow-ups (#79-#83).

- a running goal cancelled through the front agent keeps the front agent's mark as its reason;
- tool contract version negatives (a tool ``since`` 1.1 or 2.0, a version "1");
- an open (0644) webhook URL file in a deployment shows as NOTIFY_UNAVAILABLE in the health line;
- a 302 from the webhook is not a send (retried, no receipt);
- ``latest_stop`` returns the newest stop request;
- an MCP tool error caps AMPLAI's message and never echoes argument values;
- P3-7: a malformed stop event is recorded as rejected and acknowledged, not retried forever;
- P3-8: receipts and the watermark are keyed by the notifier's module id, not its version.

Real: LocalProductDeployment with ``intake`` and ``notifier`` entries, the store and outbox, the
MCP caller. Stand-ins: the planner returns a fixed draft; the webhook POST is recorded.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_034_s1a_intake_route import HERMES, HERMES_TOKEN, message, submit, wait_status
from test_034_s1c_notifier import (
    Recorder,
    cancel_through_the_front_agent,
    outbox_due,
    product,  # noqa: F401 - the fixture
)

from amplai_foundry.integrations.intake_mcp import contract
from amplai_foundry.integrations.intake_mcp.server import IntakeCaller, Settings, ToolFailure
from amplai_foundry.runtime.contracts.intake import STOP_EVENT, latest_stop
from amplai_foundry.runtime.keys import private_bytes
from amplai_foundry.runtime.modules import ModuleSpec
from amplai_foundry.runtime.notifier import NotifierPump, SlackWebhookNotifier, StoreReceipts
from amplai_foundry.runtime.notifier.slack import spec as slack_spec

USER = {"provider": "slack", "workspace_id": "T1", "user_id": "U0OPERATOR"}


def drafted(client: TestClient, message_id: str = "m-1") -> str:
    goal: str = submit(client, message_id).json()["goal_id"]
    wait_status(client, goal, {"planning"})
    return goal


# -- the running cancel path ---------------------------------------------------------------------


def test_a_front_agent_cancel_of_an_approved_goal_records_its_mark_for_the_stop(
    product: Any,  # noqa: F811
) -> None:
    """The cancel of an approved goal remembers the front agent's mark, and the loop's stop
    helper (``_stop_goal``, called directly here: the attempt loop itself needs a container)
    records it as the reason."""
    dep, client, _post, home = product
    goal = drafted(client)
    operator = (home / "operator.token").read_text().strip()
    headers = {"Authorization": "Bearer " + operator}
    plan = client.get(f"/api/v3/local/goals/{goal}", headers=headers).json()
    approved = client.post(
        f"/api/v3/local/goals/{goal}/approve",
        headers=headers,
        json={"expected_contract_ref": plan["contract_ref"]},
    )
    assert approved.json()["status"] == "approved"
    stopped = client.post(
        f"/api/v3/intake/hermes/goals/{goal}/cancel",
        headers=HERMES,
        json=message("m-stop", reason="stop it"),
    )
    assert stopped.status_code == 200  # the projection still reads "approved": the loop stops it
    reason = dep.loop._cancelled_by(goal)
    assert reason == "cancelled by the front agent (intake:hermes)"
    # the running loop stops the goal with that reason (``ExecutionLoop._stop_goal``)
    dep.loop._stop_goal(goal, "cancelled", reason, [])
    record = client.get(f"/api/v3/local/goals/{goal}", headers=headers).json()
    assert record["status"] == "cancelled" and record["reason"] == reason


def test_latest_stop_is_the_newest_request(product: Any) -> None:  # noqa: F811
    dep, client, _post, _home = product
    goal = drafted(client)
    actor = {"subject_id": "pinesky", "kind": "service", "authn_context_ref": "intake:hermes"}
    with dep.store.tx() as db:
        dep.store.event(db, dep.scope, "goal", goal, STOP_EVENT,
                        {"kind": "pause", "actor": actor, "reason": "first"})  # fmt: skip
        dep.store.event(db, dep.scope, "goal", goal, STOP_EVENT,
                        {"kind": "cancel", "actor": actor, "reason": "second"})  # fmt: skip
    stop = latest_stop(dep.store, dep.scope, goal)
    assert stop is not None and (stop["kind"], stop["reason"]) == ("cancel", "second")


# -- the notifier ----------------------------------------------------------------------------------


def test_an_open_url_file_shows_as_unavailable(product: Any) -> None:  # noqa: F811
    dep, client, post, home = product
    os.chmod(home / "secrets" / "notifier.url", 0o644)
    cancel_through_the_front_agent(client)
    dep.notifier_pump.run_once(limit=1000)
    assert post.calls == []
    health = dep.notifier_pump.health()
    assert health["undelivered"] == 1 and health["last_error"]["code"] == "NOTIFY_UNAVAILABLE"


def test_a_redirect_is_not_a_send(product: Any) -> None:  # noqa: F811
    dep, client, post, _home = product
    cancel_through_the_front_agent(client)
    post.status = 302
    results = dep.notifier_pump.run_once(limit=1000)
    assert [r for r in results if not r["acknowledged"]]
    assert dep.notifier_pump.health()["last_error"]["http_status"] == 302
    assert dep.notifier_pump.health()["rejected"] == 0
    post.status = 200
    outbox_due(dep)
    dep.notifier_pump.run_once(limit=1000)
    assert len(post.calls) == 2  # the 302 left no receipt


def test_a_malformed_stop_event_is_rejected_not_retried(product: Any) -> None:  # noqa: F811
    """P3-7: a stop record without an actor mark is recorded as rejected and acknowledged."""
    dep, client, post, _home = product
    goal = drafted(client)
    with dep.store.tx() as db:
        dep.store.event(db, dep.scope, "goal", goal, STOP_EVENT, {"kind": "pause"})
    results = dep.notifier_pump.run_once(limit=1000)
    assert results
    assert all(r["acknowledged"] for r in results)
    outbox_due(dep)
    dep.notifier_pump.run_once(limit=1000)
    assert post.calls == []
    health = dep.notifier_pump.health()
    assert health["rejected"] == 1 and health["undelivered"] == 0
    assert health["last_error"]["code"] == "NOTIFY_INPUT"


def test_receipts_and_the_watermark_are_keyed_by_module_id(product: Any) -> None:  # noqa: F811
    """P3-8: a new notifier version keeps the receipts and the watermark of the module."""
    dep, client, _post, home = product
    cancel_through_the_front_agent(client)
    dep.notifier_pump.run_once(limit=1000)
    actors = {
        row[0]
        for row in dep.store.conn.execute(
            "SELECT DISTINCT actor FROM commands WHERE actor LIKE 'notifier:%'"
        )
    }
    assert actors == {"notifier:" + slack_spec().module_id}
    assert "@" not in next(iter(actors))
    (event_id,) = dep.store.conn.execute(
        "SELECT event_id FROM events WHERE event_type=?", (STOP_EVENT,)
    ).fetchone()
    receipt_rows = dep.store.conn.execute(
        "SELECT COUNT(*) FROM commands WHERE operation='notify' AND key=?", (event_id,)
    ).fetchone()[0]
    assert receipt_rows == 1
    # the same module at another version: same receipts, same watermark, no second POST
    newer = dataclasses.replace(slack_spec(), version="2")

    class NewerSlack(SlackWebhookNotifier):
        @property
        def spec(self) -> ModuleSpec:
            return newer

    post = Recorder()
    receipts = StoreReceipts(dep.store, dep.scope, newer.module_id)
    notifier = NewerSlack(
        home / "secrets" / "notifier.url", read=private_bytes, receipts=receipts, post=post
    )
    pump = NotifierPump(dep.store, dep.scope, notifier, lambda _goal: "cancelled", receipts)
    assert pump.watermark == dep.notifier_pump.watermark
    assert receipts.get(event_id) is not None
    with dep.store.tx() as db:  # as if the acknowledgment had been lost before the upgrade
        db.execute("UPDATE outbox SET status='pending', next_at=0")
    pump.run_once(limit=1000)
    assert post.calls == []


# -- the tool contract -----------------------------------------------------------------------------


@pytest.mark.parametrize("since", ["1.1", "2.0", "1", "0.9"])
def test_a_tool_since_outside_the_contract_version_is_refused(
    monkeypatch: pytest.MonkeyPatch, since: str
) -> None:
    document = contract.raw()
    document["tools"][0]["since"] = since
    monkeypatch.setattr(contract, "raw", lambda: document)
    with pytest.raises(ValueError):
        contract.load()


def test_a_contract_version_other_than_1_0_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    for version in ("1", "2.0", "1.1"):
        document = {**contract.raw(), "version": version}
        monkeypatch.setattr(contract, "raw", lambda d=document: d)
        with pytest.raises(ValueError):
            contract.load()


# -- the MCP server's errors -----------------------------------------------------------------------


def stub_caller(handler: Any) -> IntakeCaller:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://amplai.test")
    return IntakeCaller(Settings("http://amplai.test", "hermes", HERMES_TOKEN), http)


def test_an_amplai_error_message_is_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"code": "CONFLICT", "message": "m" * 5000})

    with pytest.raises(ToolFailure) as excinfo:
        stub_caller(handler).call("list_goals", USER)
    assert excinfo.value.code == "CONFLICT" and len(excinfo.value.message) == 500


def test_an_argument_error_names_fields_not_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # never reached
        raise AssertionError("bad arguments reached AMPLAI")

    secret = "SECRET-VALUE-IN-AN-ARGUMENT"
    with pytest.raises(ToolFailure) as excinfo:
        stub_caller(handler).call(
            "submit_work",
            {**USER, "channel_id": "C1", "message_id": secret + " with space", "text": secret},
        )
    failure = excinfo.value
    assert failure.code == "INVALID_ARGUMENTS" and "message_id" in failure.message
    assert secret not in json.dumps(failure.wire()) and secret not in repr(failure)
