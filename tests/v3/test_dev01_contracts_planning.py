"""Approved structural fixtures and the real planner/critic pipeline.

Fixture planners test orchestration only; they are not claims about LLM quality.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from amplai_foundry.runtime.contracts.identity import new_id, reference, verify_signature
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.contracts.semantics import (
    check_context,
    check_contract,
    check_readiness,
    ordered_times,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.goals.planner import AdaptiveStrategy, PlanningService
from amplai_foundry.runtime.graphs.compiler import validate_graph

ROOT = Path(__file__).resolve().parents[2] / "design-reference"
VALID = sorted((ROOT / "fixtures/valid").glob("*.json"))
INVALID = sorted((ROOT / "fixtures/invalid").glob("*.json"))


@pytest.fixture(scope="module")
def schemas():
    return Contracts()


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_dev01_approved_valid_schema_fixtures(schemas, path):
    assert schemas.validate(path.stem, json.loads(path.read_text()))


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_dev01_approved_invalid_schema_fixtures(schemas, path):
    name = next(
        k
        for k in sorted(schemas.definitions, key=len, reverse=True)
        if path.stem.startswith(k + "-")
    )
    with pytest.raises(RuntimeFault):
        schemas.validate(name, json.loads(path.read_text()))


def test_dev01_unknown_schema_and_network_reference_fail_closed(schemas, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("Schema validation must not use the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    with pytest.raises(RuntimeFault):
        schemas.validate("missing-schema", {})
    c = Contracts()
    c.definitions["network-reference"] = {"$ref": "https://not-authorized.invalid/schema.json"}
    with pytest.raises(RuntimeFault):
        c.validate("network-reference", {})


@pytest.mark.parametrize(
    "name,code",
    [
        ("duplicate-acceptance", "ACCEPTANCE_DUPLICATE"),
        ("duplicate-readiness", "READINESS_INCOMPLETE"),
        ("incomplete-governing-set", "CONTEXT_INCOMPLETE"),
        ("expired-before-issued", "TIME_ORDER"),
        ("untrusted-signature", "UNTRUSTED_SIGNATURE"),
        ("self-cycle", "GRAPH_CYCLE"),
        ("nonexistent-criterion", "UNKNOWN_ACCEPTANCE"),
        ("contract-ref-stale", "GRAPH_CONTRACT_BINDING"),
    ],
)
def test_dev01_semantic_negative_fixtures(name, code):
    value = json.loads((ROOT / "fixtures/semantic-negative" / (name + ".json")).read_text())
    with pytest.raises(RuntimeFault) as error:
        if name == "duplicate-acceptance":
            check_contract(value)
        elif name == "duplicate-readiness":
            check_readiness(value["readiness"])
        elif name == "incomplete-governing-set":
            check_context(value, value["core_refs"])
        elif name == "expired-before-issued":
            ordered_times(value["not_before"], value["expires_at"])
        elif name == "untrusted-signature":
            verify_signature(value, {})
        else:
            contract = json.loads((ROOT / "fixtures/valid/goal-contract.json").read_text())
            rr = reference(contract, "goal_id")
            if name != "contract-ref-stale":
                value["contract_ref"] = rr
            validate_graph(value, contract, rr)
    assert error.value.code == code


@pytest.mark.parametrize(
    "values,strategy",
    [
        (
            dict(
                risk="low",
                uncertainty="low",
                files_changed=1,
                cross_app=False,
                verifier_available=True,
            ),
            "direct",
        ),
        (
            dict(
                risk="high",
                uncertainty="low",
                files_changed=1,
                cross_app=False,
                verifier_available=True,
            ),
            "bounded_loop",
        ),
        (
            dict(
                risk="medium",
                uncertainty="high",
                files_changed=4,
                cross_app=False,
                verifier_available=True,
            ),
            "deliberative",
        ),
        (
            dict(
                risk="low",
                uncertainty="low",
                files_changed=1,
                cross_app=True,
                verifier_available=True,
            ),
            "bounded_loop",
        ),
        (
            dict(
                risk="low",
                uncertainty="low",
                files_changed=1,
                cross_app=False,
                verifier_available=False,
            ),
            "discovery",
        ),
    ],
)
def test_dev01_adaptive_primitive(values, strategy):
    assert AdaptiveStrategy.choose(**values)["strategy"] == strategy


@pytest.mark.parametrize(
    "field,value",
    [
        ("risk", "unknown"),
        ("uncertainty", "maybe"),
        ("files_changed", -1),
        ("files_changed", True),
        ("cross_app", "false"),
    ],
)
def test_dev01_adaptive_invalid_inputs_do_not_select_direct(field, value):
    args = dict(
        risk="low", uncertainty="low", files_changed=1, cross_app=False, verifier_available=True
    )
    args[field] = value
    with pytest.raises(RuntimeFault):
        AdaptiveStrategy.choose(**args)


class FixturePlanner:
    def __init__(self, output):
        self.output = output
        self.calls = []

    def structured_plan(self, prompt, schema, **kwargs):
        self.calls.append(json.loads(prompt))
        return deepcopy(self.output)


class FixtureReviewer:
    def __init__(self, blocking_rounds=0):
        self.blocking_rounds = blocking_rounds
        self.calls = []

    def structured_plan(self, prompt, schema, **kwargs):
        self.calls.append(json.loads(prompt))
        findings = (
            []
            if len(self.calls) > self.blocking_rounds
            else [
                {
                    "code": "BUSINESS_UNKNOWN",
                    "severity": "blocking",
                    "statement": "Clarify the result boundary",
                }
            ]
        )
        return {"findings": findings}


@pytest.mark.parametrize("blocking_rounds", [0, 1, 2])
def test_dev01_raw_intent_to_critic_to_graph_never_self_authorizes(deployment, blocking_rounds):
    d = deployment
    prepared = d.prepare()
    contract = d.store.get(d.scope, "goal-contract", prepared["contract_ref"])
    graph = d.store.get(d.scope, "workgraph", prepared["graph_ref"])
    verification = d.store.get(d.scope, "verification-plan", contract["verification_plan_ref"])
    resolution = d.store.get(d.scope, "resolution", contract["resolution_ref"])
    submission = d.goals.submit(d.actor, text="alpha 결과 JSON 생성", key=new_id("planner"))
    draft = {
        k: deepcopy(contract[k])
        for k in [
            "objective",
            "non_goals",
            "constraints",
            "acceptance",
            "assumptions",
            "risk",
            "requested_capabilities",
        ]
    }
    draft.update(verification_bindings=verification["bindings"], nodes=graph["nodes"])
    planner, reviewer = FixturePlanner(draft), FixtureReviewer(blocking_rounds)
    service = PlanningService(
        d.goals,
        d.runtime,
        planner=planner,
        reviewer=reviewer,
        planner_profile_ref=prepared["execution_profile"]["model_profile_ref"],
        reviewer_profile_ref=prepared["execution_profile"]["model_profile_ref"],
        planning_policy={
            "capabilities": contract["requested_capabilities"],
            "budget": contract["budget"],
        },
    )

    def run():
        return service.plan(
            d.actor,
            submission["goal_id"],
            readiness=resolution["readiness"],
            context_bundle_ref=contract["context_bundle_ref"],
            policy_ref=contract["policy_ref"],
            global_verifier_ref=graph["global_verification_ref"],
        )

    if blocking_rounds == 2:
        with pytest.raises(Hold) as error:
            run()
        assert error.value.code == "CRITIC_LIMIT"
    else:
        result = run()
        assert result["status"] == "awaiting_execution_authority"
        assert result["automatic_approval"] is False
        assert result["contract_ref"]["id"] == submission["goal_id"]
        compiled = d.store.get(d.scope, "workgraph", result["graph_ref"])
        assert compiled["nodes"][0]["work_id"] != graph["nodes"][0]["work_id"]
    assert len(planner.calls) == len(reviewer.calls) == min(2, blocking_rounds + 1)
    assert (
        d.store.head(d.scope, "goal", submission["goal_id"])["data"]["active_contract_ref"] is None
    )
    assert d.store.list_objects(d.scope, "run-record") == []


def test_dev01_cli_reports_development_not_final():
    import platform

    from typer.testing import CliRunner

    from amplai_foundry import __version__
    from amplai_foundry.runtime.cli import app

    result = CliRunner().invoke(app, ["ops", "version"])
    assert result.exit_code == 0
    value = json.loads(result.stdout)
    assert value["package_version"] == __version__ == "3.0.0.dev3"
    assert value["stage"] == "DEV-03" and value["production_release"] is False
    assert value["platform"] == platform.system()
