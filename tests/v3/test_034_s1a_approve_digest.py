"""Work 034 S1a — approval binds the exact contract shown (spec H-13, design/10 §4, AC-H1 step).

`amplai approve` shows the frozen contract AMPLAI holds and its digest, and sends that revision as
``expected_contract_ref``; the server approves only that revision. A card another agent showed is
never the basis. Real: `amplai ops local-init`, LocalProductDeployment, the API, the CLI. Stand-in:
the planner returns a fixed draft.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from amplai_foundry.control_plane.api_v3.client import AmplaiClient
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import FixedPlanner, codex_inputs, make_repo


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


def settle(dep: Any) -> None:
    """Let background planning finish before the store closes."""
    deadline = time.time() + 20
    while dep._planning and time.time() < deadline:
        time.sleep(0.05)


@pytest.fixture
def product(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    home = init_home(tmp_path)
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    token = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as client:
        client.headers["Authorization"] = "Bearer " + token

        def test_client() -> AmplaiClient:
            return AmplaiClient("http://127.0.0.1/", token, transport=client._transport)

        monkeypatch.setattr(cli, "client", test_client)
        yield dep, client
    settle(dep)
    dep.close()


def drafted(client: TestClient) -> tuple[str, dict[str, Any]]:
    goal = client.post(
        "/api/v3/intents",
        headers={"Idempotency-Key": "g-" + str(time.time_ns())},
        json={"text": "make value return 2", "target_hints": ["app"]},
    ).json()["goal_id"]
    client.post(f"/api/v3/local/goals/{goal}/plan")
    deadline = time.time() + 20
    while time.time() < deadline:
        record: dict[str, Any] = client.get(f"/api/v3/local/goals/{goal}").json()
        if record["status"] != "planning":
            assert record["status"] == "awaiting_approval", record
            return goal, record
        time.sleep(0.05)
    raise AssertionError("timed out")


def other(ref: dict[str, Any]) -> dict[str, Any]:
    return {**ref, "digest": "sha256:" + "0" * 64}


# -- the server ----------------------------------------------------------------------------------


def test_the_server_approves_only_the_contract_shown(product: Any) -> None:
    dep, client = product
    goal, record = drafted(client)
    url = f"/api/v3/local/goals/{goal}/approve"
    assert client.post(url).status_code == 422  # no revision named: nothing is approved
    stale = client.post(url, json={"expected_contract_ref": other(record["contract_ref"])})
    assert stale.status_code == 409 and stale.json()["code"] == "STALE_CONTRACT"
    assert dep.service.plan_record(goal)["status"] == "awaiting_approval"
    approved = client.post(url, json={"expected_contract_ref": record["contract_ref"]})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"


def test_the_service_refuses_another_revision(product: Any) -> None:
    dep, client = product
    goal, record = drafted(client)
    with pytest.raises(RuntimeFault) as exc:
        dep.service.approve(
            dep.operator(), goal, expected_contract_ref={**record["contract_ref"], "revision": 2}
        )
    assert exc.value.code == "STALE_CONTRACT"


# -- the CLI -------------------------------------------------------------------------------------


def test_status_shows_the_digest_and_the_approve_command_that_binds_it(product: Any) -> None:
    _dep, client = product
    goal, record = drafted(client)
    shown = CliRunner().invoke(cli.app, ["status", goal])
    assert shown.exit_code == 0, shown.output
    digest = record["contract_ref"]["digest"]
    assert f"contract  : revision 1 {digest}" in shown.output
    assert f"next: amplai approve {goal} --expected-contract-ref {digest}" in shown.output


def test_approve_shows_the_contract_and_sends_its_revision(product: Any) -> None:
    dep, client = product
    goal, record = drafted(client)
    digest = record["contract_ref"]["digest"]
    result = CliRunner().invoke(cli.app, ["approve", goal, "--expected-contract-ref", digest])
    assert result.exit_code == 0, (result.output, repr(result.exception))
    for line in (
        f"contract {goal} revision 1",
        f"  digest    : {digest}",
        "  changes   : value() returns 2",
        "  not doing : no other behaviour",
        "  done when : AC-1 value() returns 2",
        f"goal {goal}: approved",
    ):
        assert line in result.output, line
    assert dep.service.plan_record(goal)["status"] == "approved"


def test_approve_without_the_option_still_binds_what_it_showed(product: Any) -> None:
    dep, client = product
    goal, _record = drafted(client)
    sent: list[Any] = []
    original = AmplaiClient.local_approve

    def spy(self: AmplaiClient, goal_id: str, expected: dict[str, Any]) -> Any:
        sent.append(expected)
        return original(self, goal_id, expected)

    AmplaiClient.local_approve = spy  # type: ignore[method-assign]
    try:
        result = CliRunner().invoke(cli.app, ["approve", goal])
    finally:
        AmplaiClient.local_approve = original  # type: ignore[method-assign]
    assert result.exit_code == 0, result.output
    assert sent == [dep.service.plan_record(goal)["contract_ref"]]


def test_approve_refuses_when_the_contract_is_not_the_one_read(product: Any) -> None:
    dep, client = product
    goal, _record = drafted(client)
    result = CliRunner().invoke(
        cli.app, ["approve", goal, "--expected-contract-ref", "sha256:" + "0" * 64]
    )
    assert result.exit_code == 2 and "STALE_CONTRACT" in result.output
    assert dep.service.plan_record(goal)["status"] == "awaiting_approval"


def test_approve_without_a_drafted_contract_is_held(product: Any) -> None:
    dep, client = product
    goal = client.post(
        "/api/v3/intents", headers={"Idempotency-Key": "g-none"}, json={"text": "x"}
    ).json()["goal_id"]
    dep.service._save_plan(goal, {"goal_id": goal, "status": "needs_answers"})
    result = CliRunner().invoke(cli.app, ["approve", goal])
    assert result.exit_code == 3 and "PLAN_NOT_READY" in result.output
