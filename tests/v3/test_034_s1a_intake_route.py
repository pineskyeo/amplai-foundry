"""Work 034 S1a — the intake route: front-agent token, identity map, filtered queries (H-5..H-8).

Real: `amplai ops local-init` output, LocalProductDeployment with an ``intake`` entry, the intake
routes, the operator routes, store backup. Stand-in: the planner returns a fixed draft (as in
test_rc06_local_product.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from amplai_foundry.control_plane.api_v3.intake import CancelBody
from amplai_foundry.control_plane.api_v3.server import (
    ApiCommands,
    ApiServices,
    BearerAuthenticator,
    create_app,
)
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import digest, now
from amplai_foundry.runtime.contracts.intake import STOP_EVENT, intake_actor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from amplai_foundry.runtime.recovery.service import RecoveryService
from rc06_rig import FixedPlanner, codex_inputs, make_repo

HERMES_TOKEN = "test-only-hermes-token-" + "h" * 40
SLACK_USER = {"provider": "slack", "user_id": "U0OPERATOR"}


def private(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, 0o600)
    return path


def identity_map(home: Path, entries: list[dict[str, str]]) -> Path:
    return private(
        home / "intake" / "identities.json",
        json.dumps({"schema_version": "intake-identity-1", "entries": entries}),
    )


def init_home(tmp_path: Path) -> Path:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
    result = CliRunner().invoke(
        cli.app,
        [
            "ops", "local-init", "--repo", str(repo), "--app", "app",
            "--codex-home", str(codex_home),
            "--container-profile", str(inputs.container_profile),
            "--qualification-report", str(inputs.qualification_report),
            "--egress-profile", str(inputs.egress_profile),
            "--egress-qualification", str(inputs.egress_qualification),
            "--verifier", "check=python3 -c 'import app' | app imports",
            "--home", str(home), "--operator", "pinesky",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    return home


def with_intake(home: Path) -> None:
    private(home / "intake" / "hermes.token", HERMES_TOKEN + "\n")
    identity_map(home, [{**SLACK_USER, "subject_id": "pinesky"}])
    config = home / "local.json"
    value = json.loads(config.read_text())
    value["intake"] = {
        "token_file": "intake/hermes.token",
        "identity_map_file": "intake/identities.json",
    }
    private(config, json.dumps(value))


@pytest.fixture
def product(tmp_path: Path) -> Any:
    home = init_home(tmp_path)
    with_intake(home)
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    operator = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as client:
        yield dep, client, operator, home
    settle(dep)
    dep.close()


def settle(dep: Any) -> None:
    """Let background planning finish before the store closes."""
    deadline = time.time() + 20
    while dep._planning and time.time() < deadline:
        time.sleep(0.05)


HERMES = {"Authorization": "Bearer " + HERMES_TOKEN}


def message(message_id: str, **extra: Any) -> dict[str, Any]:
    return {**SLACK_USER, "workspace_id": "T1", "channel_id": "C1", "message_id": message_id,
            **extra}  # fmt: skip


def submit(client: TestClient, message_id: str = "m-1", text: str = "make value return 2") -> Any:
    return client.post("/api/v3/intake/hermes", headers=HERMES, json=message(message_id, text=text))


def wait_status(client: TestClient, goal: str, not_in: set[str]) -> dict[str, Any]:
    deadline = time.time() + 20
    while time.time() < deadline:
        record: dict[str, Any] = client.get(
            f"/api/v3/intake/hermes/goals/{goal}", headers=HERMES, params=SLACK_USER
        ).json()
        if record.get("status") not in not_in:
            return record
        time.sleep(0.05)
    raise AssertionError("timed out")


# -- submission ----------------------------------------------------------------------------------


def test_a_message_becomes_a_goal_attributed_to_the_linked_operator(product: Any) -> None:
    dep, client, _operator, _home = product
    response = submit(client)
    assert response.status_code == 202, response.text
    goal = response.json()["goal_id"]
    assert set(response.json()) == {"goal_id", "status", "at"}
    # the contract is drafted next, as `amplai work` does
    assert wait_status(client, goal, {"planning"})["status"] == "awaiting_approval"
    head = dep.store.head(dep.scope, "goal", goal)
    intent = dep.store.get(dep.scope, "intent-envelope", head["data"]["intent_ref"])
    assert intent["source_channel"] == "hermes" and intent["external_message_id"] == "m-1"
    assert intent["actor"] == {
        "subject_id": "pinesky", "kind": "service", "authn_context_ref": "intake:hermes",
    }  # fmt: skip
    assert intent["target_hints"] == [] and intent["data_classification"] == "internal"


def test_a_resent_message_makes_one_goal(product: Any) -> None:
    dep, client, _operator, _home = product
    first, again = submit(client, "m-same"), submit(client, "m-same")
    assert first.json()["goal_id"] == again.json()["goal_id"]
    goals = dep.store.conn.execute("SELECT COUNT(*) FROM heads WHERE kind='goal'").fetchone()[0]
    assert goals == 1
    # the same message id with another text is a conflict, not a second goal
    assert submit(client, "m-same", "something else").status_code == 409


@pytest.mark.parametrize(
    "forged",
    [
        {"actor": {"subject_id": "admin", "kind": "human", "authn_context_ref": "x"}},
        {"subject_id": "admin"},
        {"role": "governor"},
        {"target_hints": ["other-repo"]},
        {"classification": "public"},
    ],
)
def test_request_fields_never_supply_identity_or_targets(
    product: Any, forged: dict[str, Any]
) -> None:
    _dep, client, _operator, _home = product
    body = message("m-forged", text="make value return 2", **forged)
    assert client.post("/api/v3/intake/hermes", headers=HERMES, json=body).status_code == 422


def test_an_unlinked_messenger_user_is_refused(product: Any) -> None:
    _dep, client, _operator, _home = product
    for user in ({"user_id": "U0STRANGER"}, {"provider": "telegram"}):
        body = {**message("m-x", text="make value return 2"), **user}
        response = client.post("/api/v3/intake/hermes", headers=HERMES, json=body)
        assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"


# -- credentials ---------------------------------------------------------------------------------


def test_the_two_tokens_open_only_their_own_routes(product: Any) -> None:
    _dep, client, operator, _home = product
    goal = submit(client).json()["goal_id"]
    wait_status(client, goal, {"planning"})
    for method, path in (
        ("post", f"/api/v3/local/goals/{goal}/approve"),
        ("post", f"/api/v3/local/goals/{goal}/cancel"),
        ("get", "/api/v3/local/goals"),
        ("get", "/api/v3/goals"),
        ("post", "/api/v3/authority/grants"),
    ):
        response = getattr(client, method)(path, headers={**HERMES, "Idempotency-Key": "k"})
        assert response.status_code == 401, (path, response.text)
    response = client.get(
        "/api/v3/intake/hermes/goals",
        headers={"Authorization": "Bearer " + operator},
        params=SLACK_USER,
    )
    assert response.status_code == 401


def test_a_removed_or_rotated_token_is_refused_at_once(product: Any) -> None:
    _dep, client, _operator, home = product
    assert submit(client, "m-a").status_code == 202
    private(home / "intake" / "hermes.token", "test-only-rotated-token-" + "r" * 40)
    assert submit(client, "m-b").status_code == 401
    (home / "intake" / "hermes.token").unlink()
    assert submit(client, "m-c").status_code == 401


def test_the_token_and_the_identity_map_are_not_in_a_store_backup(
    product: Any, tmp_path: Path
) -> None:
    dep, client, _operator, home = product
    submit(client)
    backup = tmp_path / "backup"
    actor = intake_actor("pinesky", dep.scope)
    RecoveryService(dep.store).backup(
        type(actor)("pinesky", dep.scope, frozenset({"runtime.backup"})), backup
    )
    stored = b"".join(p.read_bytes() for p in backup.rglob("*") if p.is_file())
    assert HERMES_TOKEN.encode() not in stored and b"U0OPERATOR" not in stored
    assert dep.store.root not in (home / "intake").resolve().parents


def test_a_shared_token_or_a_second_subject_is_held(product: Any) -> None:
    _dep, client, operator, home = product
    second = {"provider": "slack", "user_id": "U2", "subject_id": "someone"}
    identity_map(home, [{**SLACK_USER, "subject_id": "pinesky"}, second])
    response = submit(client, "m-two")
    assert response.status_code == 423 and response.json()["code"] == "INTAKE_IDENTITY_MAP"
    identity_map(home, [{**SLACK_USER, "subject_id": "pinesky"}])
    private(home / "intake" / "hermes.token", operator)
    # a caller without the configured token learns nothing about it: a plain 401
    stranger = submit(client, "m-shared")
    assert stranger.status_code == 401 and stranger.json()["code"] == "UNAUTHENTICATED"
    response = client.post(
        "/api/v3/intake/hermes",
        headers={"Authorization": "Bearer " + operator},
        json=message("m-shared", text="x"),
    )
    assert response.status_code == 423 and response.json()["code"] == "INTAKE_TOKEN_REUSED"


def test_configuration_faults_are_not_told_to_an_unauthenticated_caller(product: Any) -> None:
    _dep, client, _operator, home = product
    token = home / "intake" / "hermes.token"
    private(token, "short-token")
    assert submit(client, "m-short").status_code == 401  # not INTAKE_TOKEN
    shown = client.post(
        "/api/v3/intake/hermes",
        headers={"Authorization": "Bearer short-token"},
        json=message("m-short", text="x"),
    )
    assert shown.status_code == 423 and shown.json()["code"] == "INTAKE_TOKEN"
    private(token, HERMES_TOKEN)
    os.chmod(token, 0o644)  # not owner-only: the entry cannot be trusted
    response = submit(client, "m-open")
    assert response.status_code == 503
    assert response.json()["code"] == "INTAKE_UNAVAILABLE"
    assert "0600" not in response.text and "permission" not in response.text.lower()


def test_without_an_intake_entry_there_are_no_intake_routes(tmp_path: Path) -> None:
    dep = LocalProductDeployment(init_home(tmp_path) / "local.json", start_loop=False)
    try:
        with TestClient(dep.app) as client:
            assert submit(client).status_code == 404
    finally:
        dep.close()


# -- queries and controls ------------------------------------------------------------------------


def test_queries_show_only_the_linked_operator_goals_as_a_projection(product: Any) -> None:
    dep, client, _operator, _home = product
    mine = submit(client).json()["goal_id"]
    wait_status(client, mine, {"planning"})
    # a goal of another subject (e.g. a meta trial submitted by the service identity)
    theirs = dep.goals.submit(dep.actors.service, text="make value return 2", key="trial-1")
    dep.request_plan(theirs["goal_id"])
    listed = client.get("/api/v3/intake/hermes/goals", headers=HERMES, params=SLACK_USER).json()
    assert [g["goal_id"] for g in listed["items"]] == [mine]
    hidden = client.get(
        f"/api/v3/intake/hermes/goals/{theirs['goal_id']}", headers=HERMES, params=SLACK_USER
    )
    assert hidden.status_code == 404
    (item,) = listed["items"]
    # no planner text, PR title, log or steering text: ids, a closed status and a time
    assert set(item) == {"goal_id", "status", "at"} and item["status"] == "awaiting_approval"
    stranger = {**SLACK_USER, "user_id": "U0STRANGER"}
    assert (
        client.get("/api/v3/intake/hermes/goals", headers=HERMES, params=stranger).status_code
        == 403
    )


def test_steer_and_replan_reach_only_a_running_attempt(product: Any) -> None:
    dep, client, _operator, _home = product
    goal = submit(client).json()["goal_id"]
    wait_status(client, goal, {"planning"})
    for route, field in (("steer", "text"), ("replan", "reason")):
        response = client.post(
            f"/api/v3/intake/hermes/goals/{goal}/{route}",
            headers=HERMES,
            json=message("m-" + route, **{field: "value() must return 2"}),
        )
        assert response.status_code == 423, response.text
        assert response.json()["code"] in {"STEER_NOT_RUNNING", "REPLAN_NOT_RUNNING"}
    theirs = dep.goals.submit(dep.actors.service, text="x", key="trial-2")["goal_id"]
    response = client.post(
        f"/api/v3/intake/hermes/goals/{theirs}/steer",
        headers=HERMES,
        json=message("m-theirs", text="x"),
    )
    assert response.status_code == 404


def test_a_cancel_through_the_front_agent_states_why_and_is_recorded(product: Any) -> None:
    dep, client, _operator, _home = product
    goal = submit(client).json()["goal_id"]
    wait_status(client, goal, {"planning"})
    url = f"/api/v3/intake/hermes/goals/{goal}/cancel"
    assert client.post(url, headers=HERMES, json=message("m-c0")).status_code == 422  # no reason
    body = message("m-c1", reason="the operator said stop in the chat")
    response = client.post(url, headers=HERMES, json=body)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert client.post(url, headers=HERMES, json=body).json() == response.json()  # resent: once
    rows = dep.store.conn.execute(
        "SELECT data FROM events WHERE aggregate_id=? AND event_type=?", (goal, STOP_EVENT)
    ).fetchall()
    (record,) = [json.loads(r["data"]) for r in rows]
    assert record["reason"] == "the operator said stop in the chat"
    assert record["actor"]["authn_context_ref"] == "intake:hermes"


def test_operator_read_routes_refuse_an_intake_actor(deployment: Any) -> None:
    d = deployment
    token = "test-only-intake-token-" + "i" * 40
    hermes = intake_actor("demo-owner", d.scope)
    authenticate = BearerAuthenticator(
        {hashlib.sha256(token.encode()).hexdigest(): "hermes"}, lambda _b: hermes
    )
    with TestClient(create_app(ApiServices(d.runtime, d.goals, authenticate))) as c:
        c.headers["Authorization"] = "Bearer " + token
        for path in ("/api/v3/goals", "/api/v3/goals/goal-x", "/api/v3/events",
                     "/api/v3/metrics", "/api/v3/telemetry/events",
                     "/api/v3/objects/goal/x?revision=1&digest=sha256:" + "0" * 64):  # fmt: skip
            response = c.get(path)
            assert response.status_code == 403, (path, response.text)


# -- review fixes: a cancel while planning, receipts never stuck ---------------------------------


def test_a_front_agent_cancel_while_planning_stays_cancelled(product: Any) -> None:
    from test_034_s1a_cancel_durability import GatedPlanner

    dep, client, _operator, _home = product
    planner = GatedPlanner()
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = planner
    goal = submit(client, "m-plan").json()["goal_id"]
    assert planner.entered.wait(20)
    url = f"/api/v3/intake/hermes/goals/{goal}/cancel"
    body = message("m-stop", reason="not now, the operator said")
    first = client.post(url, headers=HERMES, json=body).json()
    assert first["status"] == "cancelled"
    planner.release.set()  # the planner finishes after the cancel
    settle(dep)
    assert client.post(url, headers=HERMES, json=body).json() == first  # a resend replays
    assert wait_status(client, goal, set())["status"] == "cancelled"
    with pytest.raises(RuntimeFault) as exc:
        dep.service.approve(dep.operator(), goal)
    assert exc.value.code == "GOAL_CANCELLED"


def test_a_non_domain_failure_is_recorded_not_left_running(deployment: Any) -> None:
    d = deployment
    commands = ApiCommands(d.store)
    calls: list[int] = []

    def broken() -> Any:
        calls.append(1)
        raise ValueError("disk path /secret said no")

    with pytest.raises(ValueError):
        commands.run(d.actor, "k-broken", "route", {}, broken)
    receipt = commands.receipt(d.actor, "k-broken")
    assert receipt is not None and receipt["state"] == "unknown"
    error = receipt["data"]["error"]
    assert error["code"] == "COMMAND_FAILED" and "/secret" not in error["message"]
    # its outcome is unknown: this process does not repeat it (test_api: inflight crash)
    with pytest.raises(Hold) as held:
        commands.run(d.actor, "k-broken", "route", {}, broken)
    assert held.value.code == "COMMAND_OUTCOME_UNKNOWN" and calls == [1]
    # after a restart: still held for a route that did not declare itself idempotent
    d.store.epoch += 1
    with pytest.raises(Hold):
        commands.run(d.actor, "k-broken", "route", {}, broken)
    assert calls == [1]
    # an idempotent operation (the intake relay) runs the resend
    rerun = commands.run(
        d.actor, "k-broken", "route", {}, lambda: {"ok": True}, rerun_interrupted=True
    )
    assert rerun == {"ok": True}
    receipt = commands.receipt(d.actor, "k-broken")
    assert receipt is not None and receipt["state"] == "completed"
    assert receipt["data"]["interrupted"][0]["state"] == "unknown"


def running_receipt(d: Any, key: str, owner_epoch: int) -> None:
    with d.store.tx() as db:
        d.store.cas(
            db, d.scope, "api-command", ApiCommands.identity(d.actor, key), 0, "running",
            {"fingerprint": digest({"route": "route", "payload": {}}), "route": "route",
             "actor": d.actor.subject_id, "started_at": now(), "owner_epoch": owner_epoch},
        )  # fmt: skip


def test_a_receipt_left_running_by_an_earlier_owner_runs_again(deployment: Any) -> None:
    d = deployment
    commands = ApiCommands(d.store)
    running_receipt(d, "k-crashed", d.store.epoch - 1)  # its process is gone
    with pytest.raises(Hold) as held:  # not declared idempotent: main's behaviour
        commands.run(d.actor, "k-crashed", "route", {}, lambda: {"ok": True})
    assert held.value.code == "COMMAND_OUTCOME_UNKNOWN"
    rerun = commands.run(
        d.actor, "k-crashed", "route", {}, lambda: {"ok": True}, rerun_interrupted=True
    )
    assert rerun == {"ok": True}
    receipt = commands.receipt(d.actor, "k-crashed")
    assert receipt is not None and receipt["state"] == "completed"
    (interrupted,) = receipt["data"]["interrupted"]
    assert interrupted["code"] == "COMMAND_INTERRUPTED"
    assert interrupted["owner_epoch"] == d.store.epoch - 1
    # one still running in this process is not repeated
    running_receipt(d, "k-in-flight", d.store.epoch)
    with pytest.raises(Hold) as held:
        commands.run(
            d.actor, "k-in-flight", "route", {}, lambda: {"ok": True}, rerun_interrupted=True
        )
    assert held.value.code == "COMMAND_OUTCOME_UNKNOWN"


def interrupt(dep: Any, actor: Any, key: str) -> None:
    """Make a finished receipt look left running by the process before a restart."""
    identity = ApiCommands.identity(actor, key)
    head = dep.store.head(dep.scope, "api-command", identity)
    with dep.store.tx() as db:
        dep.store.cas(
            db, dep.scope, "api-command", identity, head["row_version"], "running",
            {**head["data"], "owner_epoch": dep.store.epoch - 1},
        )  # fmt: skip


def test_after_a_restart_only_the_intake_relay_reruns_an_interrupted_command(
    product: Any,
) -> None:
    dep, client, operator, _home = product
    # an operator route (POST /api/v3/intents): resent after a restart, not run again
    headers = {"Authorization": "Bearer " + operator, "Idempotency-Key": "op-1"}
    body = {"text": "make value return 2", "target_hints": ["app"]}
    assert client.post("/api/v3/intents", headers=headers, json=body).status_code == 202
    interrupt(dep, dep.operator(), "op-1")
    goals = dep.store.conn.execute("SELECT COUNT(*) FROM heads WHERE kind='goal'").fetchone()[0]
    resent = client.post("/api/v3/intents", headers=headers, json=body)
    assert resent.status_code == 423 and resent.json()["code"] == "COMMAND_OUTCOME_UNKNOWN"
    assert dep.store.conn.execute(
        "SELECT COUNT(*) FROM heads WHERE kind='goal'"
    ).fetchone()[0] == goals  # fmt: skip
    # the intake relay (cancel): resent after a restart, run again and recorded
    goal = submit(client, "m-relay").json()["goal_id"]
    wait_status(client, goal, {"planning"})
    url = f"/api/v3/intake/hermes/goals/{goal}/cancel"
    stop = message("m-relay-stop", reason="stop")
    assert client.post(url, headers=HERMES, json=stop).json()["status"] == "cancelled"
    hermes = intake_actor("pinesky", dep.scope)
    interrupt(dep, hermes, CancelBody.model_validate(stop).key())
    again = client.post(url, headers=HERMES, json=stop)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    receipt = ApiCommands(dep.store).receipt(hermes, CancelBody.model_validate(stop).key())
    assert receipt is not None and receipt["state"] == "completed"
    assert receipt["data"]["interrupted"][0]["code"] == "COMMAND_INTERRUPTED"


def test_the_identity_map_is_read_on_every_request(product: Any) -> None:
    _dep, client, _operator, home = product
    listed = client.get("/api/v3/intake/hermes/goals", headers=HERMES, params=SLACK_USER)
    assert listed.status_code == 200
    # the operator unlinks this messenger user (another of the operator's accounts stays)
    identity_map(home, [{"provider": "slack", "user_id": "U0OTHER", "subject_id": "pinesky"}])
    refused = client.get("/api/v3/intake/hermes/goals", headers=HERMES, params=SLACK_USER)
    assert refused.status_code == 403 and refused.json()["code"] == "FORBIDDEN"
