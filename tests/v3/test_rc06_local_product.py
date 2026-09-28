"""Work 018 S9 — the local product: init, operator auth, plan/approve/cancel over HTTP and CLI.

Real: `amplai ops local-init` output, LocalProductDeployment composition (keys, operator
token, authority resolver, codex records from the measured report, git workspace), the API
and CLI. Stand-in: the planner returns a fixed draft (the Codex planner is covered in
test_rc06_planner.py and the real runs).
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
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import DRAFT, FixedPlanner, codex_inputs, make_repo


@pytest.fixture
def product(tmp_path: Path) -> Any:
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
    config = home / "local.json"
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert oct((home / "operator.token").stat().st_mode & 0o777) == "0o600"
    dep = LocalProductDeployment(config, start_loop=False)
    dep.service.planner = FixedPlanner()
    token = (home / "operator.token").read_text().strip()
    with TestClient(dep.app) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield dep, client, token
    dep.close()


def wait_status(client: TestClient, goal: str, not_in: set[str]) -> dict[str, Any]:
    deadline = time.time() + 20
    while time.time() < deadline:
        record: dict[str, Any] = client.get(f"/api/v3/local/goals/{goal}").json()
        if record.get("status") not in not_in:
            return record
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_operator_token_is_the_only_way_in(product: Any) -> None:
    dep, client, _ = product
    assert (
        client.get("/api/v3/local/goals", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert client.get("/api/v3/local/goals", headers={"Authorization": ""}).status_code == 401
    assert dep.operator().kind == "human"


def test_submit_plan_approve_and_cancel_over_http(product: Any) -> None:
    dep, client, _ = product
    submitted = client.post(
        "/api/v3/intents",
        headers={"Idempotency-Key": "g1"},
        json={"text": "make value return 2", "target_hints": ["app"]},
    ).json()
    goal = submitted["goal_id"]
    assert client.post(f"/api/v3/local/goals/{goal}/plan").status_code == 202
    record = wait_status(client, goal, {"planning"})
    assert record["status"] == "awaiting_approval"
    assert record["draft"]["acceptance"] == DRAFT["acceptance"]
    approved = client.post(f"/api/v3/local/goals/{goal}/approve").json()
    assert approved["status"] == "approved"
    decision = dep.store.get(
        dep.scope, "operator-approval", dep.service.plan_record(goal)["decision_ref"]
    )
    assert decision["approved_by"]["subject_id"] == "pinesky"
    assert decision["publish"] == {
        "mode": "draft_pr", "remote": "origin", "base_branch": "main",
        "apps": {"app": {"remote": "origin", "base_branch": "main"}},  # per app (D-081)
    }  # fmt: skip
    assert client.post(f"/api/v3/local/goals/{goal}/cancel").json()["status"] == "cancelling"
    listed = client.get("/api/v3/local/goals").json()
    assert [g["goal_id"] for g in listed] == [goal]


def test_a_failed_plan_is_reported_not_hidden(product: Any) -> None:
    dep, client, _ = product

    class Broken(FixedPlanner):
        def draft(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("planner unavailable")

    dep.service.planner = Broken()
    goal = client.post(
        "/api/v3/intents",
        headers={"Idempotency-Key": "g2"},
        json={"text": "x", "target_hints": ["app"]},
    ).json()["goal_id"]
    client.post(f"/api/v3/local/goals/{goal}/plan")
    record = wait_status(client, goal, {"planning"})
    assert record["status"] == "plan_failed" and "planner unavailable" in record["reason"]


def test_cli_work_waits_for_the_draft_and_shows_the_next_step(
    product: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client, token = product

    def test_client() -> AmplaiClient:
        return AmplaiClient("http://127.0.0.1/", token, transport=client._transport)

    monkeypatch.setattr(cli, "client", test_client)
    monkeypatch.setattr("amplai_foundry.runtime.cli.time.sleep", lambda _s: None)
    result = CliRunner().invoke(cli.app, ["work", "make value return 2", "--app", "app"])
    assert result.exit_code == 0, (result.output, repr(result.exception))
    assert "awaiting_approval" in result.output and "AC-1" in result.output
    assert "next: amplai approve goal-" in result.output
    goal = result.output.split("goal ")[1].split(":")[0]
    shown = CliRunner().invoke(cli.app, ["status", goal])
    assert shown.exit_code == 0 and "value() returns 2" in shown.output
