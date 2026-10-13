"""Work 034 S1c — intake adapters are configuration, not code (spec F-2, F-3, F-6; AC-F1, AC-F2).

The core names no front agent or messenger. ``intake.adapters[]`` gives each adapter its token
file, identity map, permission ceiling (at most H-2) and rate limits; its routes are
``/api/v3/intake/<adapter_id>``, its actors are marked ``intake:<adapter_id>`` and its dedup keys
are ``<adapter_id>:<digest>``. The wire ``source_channel`` stays ``hermes`` for every adapter
(frozen 3.0.0 enum, F-3). A pre-S1c single ``intake`` entry still works as adapter ``hermes``.

Real: `amplai ops local-init`, LocalProductDeployment, the intake and operator routes. Stand-in:
the planner returns a fixed draft (as in test_034_s1a_intake_route.py). Execution after approval
needs a container sandbox, which the default run does not start: the flow here ends at an approved
contract and the feed that reports it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_034_s1a_intake_route import GENEROUS, init_home, private, settle

from amplai_foundry.runtime.contracts.intake import INTAKE_PERMISSIONS
from amplai_foundry.runtime.local_deployment import IntakeEntry, LocalProductDeployment
from rc06_rig import FixedPlanner

STUB_TOKEN = "test-only-stub-front-token-" + "s" * 40
OTHER_TOKEN = "test-only-other-front-token-" + "o" * 40
LIMITS = {op: GENEROUS for op in ("submit", "steer", "replan", "cancel")}
USER = {"provider": "chatx", "workspace_id": "W1", "user_id": "u-operator"}


def adapter(
    home: Path,
    adapter_id: str,
    token: str,
    *,
    user: dict[str, str] = USER,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    private(home / "intake" / f"{adapter_id}.token", token + "\n")
    private(
        home / "intake" / f"{adapter_id}.identities.json",
        json.dumps(
            {
                "schema_version": "intake-identity-2",
                "entries": [{**user, "subject_id": "pinesky"}],
            }
        ),
    )
    return {
        "adapter_id": adapter_id,
        "token_file": f"intake/{adapter_id}.token",
        "identity_map_file": f"intake/{adapter_id}.identities.json",
        "permissions": sorted(permissions or INTAKE_PERMISSIONS),
        "rate_limits": LIMITS,
    }


def configure(home: Path, intake: dict[str, Any]) -> None:
    config = home / "local.json"
    value = json.loads(config.read_text())
    value["intake"] = intake
    private(config, json.dumps(value))


def start(home: Path) -> LocalProductDeployment:
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    return dep


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def message(message_id: str, user: dict[str, str] = USER, **extra: Any) -> dict[str, Any]:
    return {**user, "channel_id": "C1", "message_id": message_id, **extra}


def wait_status(client: TestClient, route: str, token: str, goal: str) -> dict[str, Any]:
    deadline = time.time() + 20
    while time.time() < deadline:
        record: dict[str, Any] = client.get(
            f"{route}/goals/{goal}", headers=bearer(token), params=USER
        ).json()
        if record.get("status") != "planning":
            return record
        time.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def two(tmp_path: Path) -> Any:
    """Two adapters side by side: ``stubfront`` and ``other``."""
    home = init_home(tmp_path)
    configure(
        home,
        {
            "adapters": [
                adapter(home, "stubfront", STUB_TOKEN),
                adapter(home, "other", OTHER_TOKEN, permissions=["goal.submit"]),
            ]
        },
    )
    dep = start(home)
    operator = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as client:
        yield dep, client, operator, home
    settle(dep)
    dep.close()


# -- AC-F1: another adapter id by configuration only -------------------------------------------


def test_a_stub_adapter_passes_the_intake_flow_by_configuration_only(two: Any) -> None:
    dep, client, operator, _home = two
    route = "/api/v3/intake/stubfront"
    response = client.post(route, headers=bearer(STUB_TOKEN), json=message("m-1", text="x = 2"))
    assert response.status_code == 202, response.text
    goal = response.json()["goal_id"]
    record = wait_status(client, route, STUB_TOKEN, goal)
    assert record["status"] == "awaiting_approval"
    # the actor names the adapter; the wire channel stays the front-agent channel (F-3)
    head = dep.store.head(dep.scope, "goal", goal)
    intent = dep.store.get(dep.scope, "intent-envelope", head["data"]["intent_ref"])
    assert intent["source_channel"] == "hermes"
    assert intent["actor"] == {
        "subject_id": "pinesky", "kind": "service", "authn_context_ref": "intake:stubfront",
    }  # fmt: skip
    key = dep.store.conn.execute(
        "SELECT key FROM commands WHERE operation='command' AND actor='pinesky'"
    ).fetchone()["key"]
    assert key.startswith("stubfront:")
    # the operator approves the contract AMPLAI shows (H-13); the adapter cannot
    plan = client.get(f"/api/v3/local/goals/{goal}", headers=bearer(operator)).json()
    approved = client.post(
        f"/api/v3/local/goals/{goal}/approve",
        headers=bearer(operator),
        json={"expected_contract_ref": plan["contract_ref"]},
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    feed = client.get(f"{route}/events", headers=bearer(STUB_TOKEN), params=USER).json()
    events = client.get(
        f"{route}/events",
        headers=bearer(STUB_TOKEN),
        params={**USER, "cursor": feed["cursor"].rsplit(":", 1)[0] + ":0"},
    ).json()
    assert goal in {item["goal_id"] for item in events["items"]}
    assert feed["snapshot_ref"] == f"{route}/snapshot"
    snapshot = client.get(feed["snapshot_ref"], headers=bearer(STUB_TOKEN), params=USER).json()
    assert {"goal_id": goal, "status": "approved"}.items() <= snapshot["items"][0].items()
    # the stub's cancel is recorded with its mark, like the first adapter's
    cancelled = client.post(
        f"{route}/goals/{goal}/cancel",
        headers=bearer(STUB_TOKEN),
        json=message("m-stop", reason="the operator asked to stop"),
    )
    assert cancelled.status_code == 200, cancelled.text
    stops = [
        e
        for e in dep.store.events(dep.scope, limit=1000)
        if e["event_type"] == "intake.stop_requested"
    ]
    assert stops[-1]["data"]["actor"]["authn_context_ref"] == "intake:stubfront"


def test_no_route_exists_for_an_unconfigured_adapter(two: Any) -> None:
    _dep, client, _operator, _home = two
    for token in (STUB_TOKEN, OTHER_TOKEN):
        response = client.post(
            "/api/v3/intake/hermes", headers=bearer(token), json=message("m-h", text="x")
        )
        assert response.status_code == 404


# -- AC-F2: two adapters are separate -----------------------------------------------------------


def test_one_adapter_token_is_401_on_the_other_adapter_routes(two: Any) -> None:
    _dep, client, _operator, _home = two
    for token, route in ((STUB_TOKEN, "other"), (OTHER_TOKEN, "stubfront")):
        base = "/api/v3/intake/" + route
        calls = (
            ("post", base, {"json": message("m-x", text="x")}),
            ("get", base + "/goals", {"params": USER}),
            ("get", base + "/goals/g-1", {"params": USER}),
            ("get", base + "/events", {"params": USER}),
            ("get", base + "/snapshot", {"params": USER}),
            ("post", base + "/goals/g-1/steer", {"json": message("m-s", text="x")}),
            ("post", base + "/goals/g-1/replan", {"json": message("m-r", reason="x")}),
            ("post", base + "/goals/g-1/cancel", {"json": message("m-c", reason="x")}),
        )
        for method, path, kwargs in calls:
            response = getattr(client, method)(path, headers=bearer(token), **kwargs)
            assert response.status_code == 401, (token[:12], path, response.text)


def test_each_adapter_has_its_own_permission_ceiling(two: Any) -> None:
    _dep, client, _operator, _home = two
    route = "/api/v3/intake/other"
    submitted = client.post(route, headers=bearer(OTHER_TOKEN), json=message("m-1", text="x"))
    assert submitted.status_code == 202, submitted.text
    goal = submitted.json()["goal_id"]
    # `other` holds goal.submit only: no reads, no controls
    for method, path, kwargs in (
        ("get", route + "/goals", {"params": USER}),
        ("get", route + "/events", {"params": USER}),
        ("get", route + "/snapshot", {"params": USER}),
        ("post", f"{route}/goals/{goal}/cancel", {"json": message("m-c", reason="stop")}),
        ("post", f"{route}/goals/{goal}/steer", {"json": message("m-s", text="x")}),
    ):
        response = getattr(client, method)(path, headers=bearer(OTHER_TOKEN), **kwargs)
        assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN", path
    # the full adapter reads the same operator's goal: attribution is the operator's
    seen = client.get(
        f"/api/v3/intake/stubfront/goals/{goal}", headers=bearer(STUB_TOKEN), params=USER
    )
    assert seen.status_code == 200


def test_each_adapter_reads_only_its_own_identity_map(two: Any) -> None:
    _dep, client, _operator, home = two
    elsewhere = {"provider": "chatx", "workspace_id": "W1", "user_id": "u-elsewhere"}
    adapter(home, "other", OTHER_TOKEN, user=elsewhere, permissions=["goal.submit"])
    # the user linked only for `other` is unknown to `stubfront`, and the other way round
    response = client.post(
        "/api/v3/intake/stubfront",
        headers=bearer(STUB_TOKEN),
        json=message("m-1", elsewhere, text="x"),
    )
    assert response.status_code == 403
    response = client.post(
        "/api/v3/intake/other", headers=bearer(OTHER_TOKEN), json=message("m-2", text="x")
    )
    assert response.status_code == 403
    response = client.post(
        "/api/v3/intake/other",
        headers=bearer(OTHER_TOKEN),
        json=message("m-3", elsewhere, text="x"),
    )
    assert response.status_code == 202


def test_a_v2_identity_entry_matches_only_its_workspace(two: Any) -> None:
    _dep, client, _operator, _home = two
    route = "/api/v3/intake/stubfront"
    stranger = {**USER, "workspace_id": "W-OTHER"}
    response = client.post(
        route, headers=bearer(STUB_TOKEN), json=message("m-1", stranger, text="x")
    )
    assert response.status_code == 403
    listed = client.get(
        route + "/goals",
        headers=bearer(STUB_TOKEN),
        params={k: v for k, v in USER.items() if k != "workspace_id"},
    )
    assert listed.status_code == 403  # a workspace-bound entry needs the workspace


def test_the_same_message_through_two_adapters_is_two_commands(two: Any) -> None:
    dep, client, _operator, home = two
    adapter(home, "other", OTHER_TOKEN, permissions=["goal.submit"])
    first = client.post(
        "/api/v3/intake/stubfront", headers=bearer(STUB_TOKEN), json=message("m-1", text="x")
    )
    again = client.post(
        "/api/v3/intake/stubfront", headers=bearer(STUB_TOKEN), json=message("m-1", text="x")
    )
    other = client.post(
        "/api/v3/intake/other", headers=bearer(OTHER_TOKEN), json=message("m-1", text="x")
    )
    assert first.json()["goal_id"] == again.json()["goal_id"] != other.json()["goal_id"]
    keys = {
        r["key"].split(":")[0]
        for r in dep.store.conn.execute("SELECT key FROM commands WHERE operation='command'")
    }
    assert keys == {"stubfront", "other"}


def test_a_token_shared_by_two_adapters_is_held(two: Any) -> None:
    _dep, client, _operator, home = two
    private(home / "intake" / "other.token", STUB_TOKEN)
    response = client.post(
        "/api/v3/intake/stubfront", headers=bearer(STUB_TOKEN), json=message("m-1", text="x")
    )
    assert response.status_code == 423 and response.json()["code"] == "INTAKE_TOKEN_REUSED"


# -- configuration -------------------------------------------------------------------------------


def entry(adapter_id: str = "a", **extra: Any) -> dict[str, Any]:
    return {
        "adapter_id": adapter_id,
        "token_file": f"intake/{adapter_id}.token",
        "identity_map_file": f"intake/{adapter_id}.json",
        "permissions": ["goal.submit"],
        "rate_limits": LIMITS,
        **extra,
    }


def test_a_pre_s1c_single_entry_is_the_hermes_adapter_with_the_h2_set() -> None:
    legacy = {
        "token_file": "intake/hermes.token",
        "identity_map_file": "intake/identities.json",
        "rate_limits": LIMITS,
    }
    (only,) = IntakeEntry.model_validate(legacy).adapters
    assert only.adapter_id == "hermes" and set(only.permissions) == INTAKE_PERMISSIONS
    assert only.token_file == "intake/hermes.token"


@pytest.mark.parametrize(
    "bad",
    [
        {"adapters": []},
        {"adapters": [entry(permissions=["execution.approve"])]},
        {"adapters": [entry(permissions=["grant.issue"])]},
        {"adapters": [entry(permissions=[])]},
        {"adapters": [entry(permissions=["goal.submit", "goal.submit"])]},
        {"adapters": [entry("Bad/Id")]},
        {"adapters": [entry("a"), entry("a", token_file="intake/x.token")]},
        {"adapters": [entry("a"), entry("b", token_file="intake/a.token")]},
        {"adapters": [entry("a"), entry("b", identity_map_file="intake/a.json")]},
        {"adapters": [{k: v for k, v in entry().items() if k != "permissions"}]},
    ],
)
def test_adapter_configuration_is_refused_beyond_h2_or_shared(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntakeEntry.model_validate(bad)


def test_a_v1_identity_map_and_a_v2_shape_mismatch(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    configure(home, {"adapters": [adapter(home, "stubfront", STUB_TOKEN)]})
    mixed = {
        "schema_version": "intake-identity-2",
        "entries": [{"provider": "chatx", "user_id": "u-operator", "subject_id": "pinesky"}],
    }
    private(home / "intake" / "stubfront.identities.json", json.dumps(mixed))
    dep = start(home)
    try:
        with TestClient(dep.app) as client:
            response = client.post(
                "/api/v3/intake/stubfront",
                headers=bearer(STUB_TOKEN),
                json=message("m-1", text="x"),
            )
            assert response.status_code == 423
            assert response.json()["code"] == "INTAKE_IDENTITY_MAP"
    finally:
        settle(dep)
        dep.close()
