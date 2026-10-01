"""Work 033 S3 (D-096, AC-02): each context and budget component switches on and off as declared.

Real: the local execution product and loop of the rc06 rig (store, runtime, verification, git
workspaces, operator approval), `ManifestService.materialize` for candidate compositions, and the
Codex-shaped CLI port. Stand-in (as in every rc06 test): the "container" runs a script on the host
that edits the workspace; `wrong-first` writes a wrong value on the base and the right one on a
repair copy that holds the previous attempt's change (`tests/rc06_rig.py` AGENT).

Covered: install writes the v1 carriers and the composition `revision` field (§14 Q12); the plan
records repo facts; env bootstrap and memory notes add their sections only when enabled; the
feedback form and the attempt policy (attempts, repair base, feedback on/off) change the loop; a
numeric observation detail renders as one item; a prompt fault after the claim ends the goal and
frees the app; limits set the root and node budgets; a component this loop does not honour yet
is held before any claim; router parts nobody honours (the composition's own route order, roles,
an L1 decider whose method cannot run) are refused when the goal is planned, before the planner
turn; an interpretation variant (ask_first) is honoured and runs; a router equal to the selection
router (or a legacy one) runs; limits above the deployment ceiling hold at planning.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness.components import ComponentService
from amplai_foundry.meta_harness.composition import CompositionService
from amplai_foundry.meta_harness.manifest import ManifestService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.contracts.semantics import resolve_ref
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import policies
from amplai_foundry.runtime.execution.codex import SeededCodexPort, install_codex_profile
from amplai_foundry.runtime.execution.context_assembly import FEEDBACK_HEADERS
from amplai_foundry.runtime.execution.product import ROUTER_ORDER, AppConfig, app_capabilities
from rc06_rig import approved, codex_inputs, rig_with_codex, submit

TOOLS = (("codex", "codex-cli 0.155.1"),)
ENV_HEADER = "Environment facts (collected by AMPLAI from the base commit):"
V1_HEADER, FRESH_HEADER = FEEDBACK_HEADERS["v1"], FEEDBACK_HEADERS["v1_fresh"]


class Components:
    """Candidate compositions of the rig's Codex composition, made by the meta-proposer."""

    def __init__(self, rig: Any) -> None:
        d = self.d = rig.d
        self.rig = rig
        self.proposer = Actor("meta-proposer", d.scope, frozenset({"harness.propose"}), "service")
        self.manifests = ManifestService(
            d.store, d.scope, d.contracts, ComponentService(d.store, d.scope)
        )
        self.base_ref = rig.service.apps["app"].compositions["codex-cli"]
        self.count = 0

    def version(self, kind: str, **fields: Any) -> Any:
        self.count += 1
        return self.manifests.components.register(
            self.proposer,
            component_id=f"{kind}.v{self.count}",
            kind=kind,
            content={**copy.deepcopy(policies.V1[kind]), **fields},
            source="proposer",
            rationale="S3 context test",
        )

    def candidate(self, **components: dict[str, Any]) -> Any:
        """``slot=fields``: a new version of that slot's kind with those fields changed."""
        slots = {slot: self.version(slot, **fields) for slot, fields in components.items()}
        return self.materialize(**slots)

    def materialize(self, **slots: Any) -> Any:
        """The base composition with the named slots set to these component refs."""
        manifest = self.manifests.change(self.manifests.of_composition(self.base_ref), **slots)
        return self.manifests.materialize(
            self.proposer,
            base_composition_ref=self.base_ref,
            manifest=manifest,
            suffix=f"s3c{self.count}",
        )

    def decider(self, layer: str) -> Any:
        self.count += 1
        register = self.manifests.components.register
        method = register(
            self.proposer,
            component_id=f"decision_method.v{self.count}",
            kind="decision_method",
            content={
                "features": ["task_class"],
                "estimator": "pooled_beta_binomial_v1",
                "selection": "noninferior_then_cheapest_v1",
                "fallback": "prior_v1",
                "utility_lambda": None,
            },
            source="proposer",
            rationale="S3 context test",
        )
        return register(
            self.proposer,
            component_id=f"decider.{layer.lower()}v{self.count}",
            kind="decider",
            content={
                "layer": layer,
                "method": method,
                "table": None,
                "judge": None,
                "options": None,
                "policy": {"min_samples": 5, "margin": 0.1, "pooling_strength": 4},
            },
            source="proposer",
            rationale="S3 context test",
        )


def setup(deployment: Any, tmp_path: Path, mode: str) -> tuple[Any, Any, Any, Components]:
    rig, loop, container = rig_with_codex(deployment, tmp_path, mode)
    installed = rig.service.apps["app"]
    # the facts env_bootstrap shows: tool versions and a quick check (AppConfig, §3.3)
    rig.service.install(
        AppConfig(
            "app",
            rig.repo,
            installed.config.verifiers,
            tool_versions=TOOLS,
            quick_verifiers=("check",),
        )
    )
    return rig, loop, container, Components(rig)


def pinned(rig: Any, composition: Any, text: str) -> str:
    goal = submit(rig, text)
    rig.service.plan(goal, composition=composition)
    rig.service.approve(rig.operator, goal)
    return goal


def plan_and_contract(rig: Any, composition: Any, text: str) -> tuple[dict[str, Any], Any]:
    goal = submit(rig, text)
    plan = rig.service.plan(goal, composition=composition)
    return plan, rig.d.store.get(rig.d.scope, "goal-contract", plan["contract_ref"])


def node_budget(rig: Any, plan: dict[str, Any]) -> dict[str, Any]:
    graph = rig.d.store.get(rig.d.scope, "workgraph", plan["graph_ref"])
    budget: dict[str, Any] = graph["nodes"][0]["budget"]
    return budget


FAILED = {
    "acceptance_id": "AC-1",
    "outcome": "fail",
    "reason": "check exited 1",
    "details": {
        "stdout_tail": "".join(f"line {i:04d}\n" for i in range(600)),
        "stderr_tail": "",
        "outside": [f"f{i}.py" for i in range(9)],
    },
}


# ---------------------------------------------------------------- install and plan


def test_install_writes_the_v1_carriers_and_the_revision_field(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
    d, installed = rig.d, rig.service.apps["app"]
    ref = installed.compositions["codex-cli"]
    value = d.store.get(d.scope, "harness-composition", ref)
    kinds = {
        f: resolve_ref(d.store, d.scope, value[f])[0]
        for f in (
            "context_policy_ref",
            "budget_policy_ref",
            "router_policy_ref",
            "verification_policy_ref",
        )
    }
    assert kinds == {
        "context_policy_ref": "context-policy",
        "budget_policy_ref": "budget-policy",
        "router_policy_ref": "router-policy",
        "verification_policy_ref": "policy",
    }
    assert value["verification_policy_ref"] == installed.policy_ref  # protected record unchanged
    assert value["router_policy_ref"] == installed.router_ref
    assert resolve_ref(d.store, d.scope, installed.router_ref)[1]["kind"] == "layered_v1"
    assert value["revision"] == ref["revision"] == 1
    # §14 Q12: a re-install that changes the composition writes the next store revision with
    # the same value in its "revision" field, and selection and a full run use it
    moved = tmp_path / "requalified"
    moved.mkdir()
    inputs = codex_inputs(moved)
    report = json.loads(inputs.qualification_report.read_text())
    report["reports"]["codex-cli"]["qualification_id"] = "qualification-codex-rig-2"
    inputs.qualification_report.write_text(json.dumps(report))
    refs = install_codex_profile(d.store, d.scope, inputs, app_capabilities("app"))
    loop.coordinator.registry.register(
        d.actor, refs["driver"], SeededCodexPort(container.driver, rig.home)
    )
    rig.service.codex, rig.service.drivers = refs, {"codex-cli": refs}
    again = rig.service.install(AppConfig("app", rig.repo, installed.config.verifiers))
    new_ref = again.compositions["codex-cli"]
    new_value = d.store.get(d.scope, "harness-composition", new_ref)
    assert new_ref["id"] == ref["id"] and new_value["revision"] == new_ref["revision"] == 2
    # carriers are content-addressed: the re-install reused them
    assert new_value["context_policy_ref"] == value["context_policy_ref"]
    assert rig.service.select_composition(again)["ref"] == new_ref
    record = loop.run_goal(approved(rig))
    assert record["status"] == "published" and record["composition"]["ref"] == new_ref
    # installing the same configuration again appends nothing
    assert (
        rig.service.install(AppConfig("app", rig.repo, installed.config.verifiers)).compositions[
            "codex-cli"
        ]
        == new_ref
    )


def test_the_plan_records_repo_facts_before_the_workspace_is_discarded(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _container, _c = setup(deployment, tmp_path, "right")
    plan = rig.service.plan(submit(rig))
    # the rig repo holds app.py; the workspace's own fresh .git is left out
    assert plan["repo_facts"] == {
        "app": {
            "tree": ["app.py"],
            "tree_truncated": False,
            "tool_versions": [["codex", "codex-cli 0.155.1"]],
        }
    }
    assert list(rig.workspaces.root.glob("plan-ws-*")) == []


# ---------------------------------------------------------------- context sections


def test_env_bootstrap_adds_its_section_only_when_enabled(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "right")
    base_plan, contract = plan_and_contract(rig, c.base_ref, "(base) make value return 2")
    off = loop.prompt(contract, base_plan, None)
    assert ENV_HEADER not in off
    on_ref = c.candidate(env_bootstrap={"enabled": True})
    plan, _ = plan_and_contract(rig, on_ref, "(env on) make value return 2")
    on = loop.prompt(contract, plan, None)
    command = "python3 -c import app, sys; sys.exit(0 if app.value() == 2 else 1)"
    section = [
        "",
        ENV_HEADER,
        f"Acceptance commands: check: `{command}`",
        f"Fast test command: `{command}`",
        "Tool versions: codex: codex-cli 0.155.1",
        "Repository tree (depth 2, directories end in /):",
        "  app.py",
    ]
    # the section follows the acceptance lines and is the only difference
    assert on == off + "\n" + "\n".join(section)
    # before the feedback when both are present
    with_feedback = loop.prompt(contract, plan, [FAILED])
    assert with_feedback.index(ENV_HEADER) < with_feedback.index(V1_HEADER)
    # declared facts only, capped at max_chars
    tools_ref = c.candidate(env_bootstrap={"enabled": True, "facts": ["tool_versions"]})
    tools_plan, _ = plan_and_contract(rig, tools_ref, "(tools) make value return 2")
    tools = loop.prompt(contract, tools_plan, None)
    assert tools == off + "\n\n" + ENV_HEADER + "\nTool versions: codex: codex-cli 0.155.1"
    short_ref = c.candidate(env_bootstrap={"enabled": True, "max_chars": 40})
    short_plan, _ = plan_and_contract(rig, short_ref, "(short) make value return 2")
    assert loop.prompt(contract, short_plan, None) == off + "\n\n" + ENV_HEADER[:40]
    # the running loop sends it to the agent
    record = loop.run_goal(pinned(rig, on_ref, "(run) make value return 2"))
    assert record["status"] == "published"
    assert container.prompts[-1].endswith("\n".join(section))


def test_memory_notes_add_the_notes_of_the_app_and_task_class(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, c = setup(deployment, tmp_path, "right")
    notes = [
        {"text": "Run the check before finishing", "app": "app", "task_class": None},
        {"text": "value() stays a pure function", "app": "app", "task_class": "logic_change"},
        {"text": "only for bug fixes", "app": "app", "task_class": "bug_fix"},
        {"text": "another app's note", "app": "consumer", "task_class": None},
    ]
    notes = [{**n, "evidence": [], "dated": "2026-09-30"} for n in notes]
    base_plan, contract = plan_and_contract(rig, c.base_ref, "(base) make value return 2")
    off = loop.prompt(contract, base_plan, None)
    on_ref = c.candidate(memory_notes={"enabled": True, "notes": notes})
    plan, _ = plan_and_contract(rig, on_ref, "(notes) make value return 2")
    assert plan["draft"]["task_class"] == "logic_change"  # the rig planner's class
    assert loop.prompt(contract, plan, None) == off + "\n\n" + "\n".join(
        [
            "Notes from earlier work in app:",
            "- Run the check before finishing",
            "- value() stays a pure function",
        ]
    )
    disabled = c.candidate(memory_notes={"enabled": False, "notes": notes})
    disabled_plan, _ = plan_and_contract(rig, disabled, "(notes off) make value return 2")
    assert loop.prompt(contract, disabled_plan, None) == off


# ---------------------------------------------------------------- feedback form and attempts


def test_the_feedback_form_renders_the_declared_tail_and_details(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, _container, c = setup(deployment, tmp_path, "right")
    base_plan, contract = plan_and_contract(rig, c.base_ref, "(base) make value return 2")
    v1 = loop.prompt(contract, base_plan, [FAILED])
    assert "```\n" + FAILED["details"]["stdout_tail"][-3000:] + "\n```" in v1
    ref = c.candidate(
        feedback_form={"tail_chars": 40, "detail_keys": ["outside"], "detail_limit": 2}
    )
    plan, _ = plan_and_contract(rig, ref, "(form) make value return 2")
    text = loop.prompt(contract, plan, [FAILED])
    assert "```\n" + FAILED["details"]["stdout_tail"][-40:] + "\n```" in text
    assert "  outside: f0.py, f1.py\n" in text
    no_tail = c.candidate(feedback_form={"tail_chars": 0, "detail_keys": []})
    no_tail_plan, _ = plan_and_contract(rig, no_tail, "(no tail) make value return 2")
    assert loop.prompt(contract, no_tail_plan, [FAILED]).endswith(
        V1_HEADER + "\n- AC-1 fail: check exited 1"
    )


def test_a_scalar_detail_value_renders_as_one_item(deployment: Any, tmp_path: Path) -> None:
    # the suite's failure details hold a number (patch_bytes, patch_commands.py:181-187)
    rig, loop, container, c = setup(deployment, tmp_path, "wrong-first")
    ref = c.candidate(feedback_form={"detail_keys": ["patch_bytes"]})
    record = loop.run_goal(pinned(rig, ref, "(scalar detail) make value return 2"))
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    assert re.search(
        r"\n- AC-1 fail: check exited 1\n  patch_bytes: [1-9][0-9]*$", container.prompts[1]
    )
    # a float is one item; a list and a string keep the oracle's slice (G1)
    form = c.candidate(
        feedback_form={"detail_keys": ["seconds", "outside", "argv"], "detail_limit": 2,
                       "tail_chars": 0}
    )  # fmt: skip
    plan, contract = plan_and_contract(rig, form, "(scalar float) make value return 2")
    details = {"seconds": 0.4, "outside": ["a.py", "b.py", "c.py"], "argv": "abc"}
    assert loop.prompt(contract, plan, [{**FAILED, "details": details}]).endswith(
        "- AC-1 fail: check exited 1\n  seconds: 0.4\n  outside: a.py, b.py\n  argv: a, b"
    )


def test_a_prompt_fault_after_the_claim_ends_the_goal_and_frees_the_app(
    deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "right")
    goal = pinned(rig, c.base_ref, "(render fault) make value return 2")

    def fault(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("render failed")

    monkeypatch.setattr(loop, "prompt", fault)
    record = loop.run_goal(goal)
    assert record["status"] == "held" and record["attempts"] == []
    assert record["reason"] == "prompt: RuntimeError: render failed"
    assert container.prompts == [] and rig.published == []
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "failed"
    monkeypatch.undo()
    # the goal's sandbox:app claim went with it: the next goal claims the app and runs
    assert loop.run_goal(approved(rig))["status"] == "published"


def test_feedback_off_repairs_without_the_feedback_section(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "wrong-first")
    ref = c.candidate(attempt_policy={"feedback": False})
    record = loop.run_goal(pinned(rig, ref, "(no feedback) make value return 2"))
    # previous_patch still: the second attempt starts from the first one's change and passes
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    first, second = container.prompts
    assert "did not pass" not in second and second == first


def test_max_attempts_sets_the_node_budget_and_stops_the_goal(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "wrong-first")
    base_plan = rig.service.plan(submit(rig, "(base) make value return 2"))
    assert node_budget(rig, base_plan)["max_attempts"] == 3  # v1 = today (product.py:1033)
    ref = c.candidate(attempt_policy={"max_attempts": 1})
    goal = pinned(rig, ref, "(one attempt) make value return 2")
    assert node_budget(rig, rig.service.plan_record(goal))["max_attempts"] == 1
    record = loop.run_goal(goal)
    assert [a["outcome"] for a in record["attempts"]] == ["fail"]
    assert record["status"] == "failed" and len(container.prompts) == 1


def test_fresh_base_restarts_every_attempt_from_the_base(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "wrong-first")
    ref = c.candidate(
        attempt_policy={"repair_base": "fresh_base"}, feedback_form={"header": "v1_fresh"}
    )
    record = loop.run_goal(pinned(rig, ref, "(fresh) make value return 2"))
    # every attempt sees the base again (value 1) and writes the wrong value: nothing repairs
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "fail", "fail"]
    assert record["status"] == "failed"
    assert FRESH_HEADER in container.prompts[1] and V1_HEADER not in container.prompts[1]
    assert "already contains" not in container.prompts[2]


# ---------------------------------------------------------------- limits


def test_limits_set_the_root_and_node_budgets(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _container, c = setup(deployment, tmp_path, "right")
    ref = c.candidate(
        limits={
            "max_wall_seconds": 900,
            "max_tokens": 30_000_000,
            "max_attempts": 2,
            "aux_max_tokens": 3_000_000,
        },
        attempt_policy={"max_attempts": 2},
    )
    plan, contract = plan_and_contract(rig, ref, "(limits) make value return 2")
    root = contract["budget"]
    assert (root["max_wall_seconds"], root["max_tokens"], root["max_attempts"]) == (
        900,
        30_000_000,
        2,
    )
    node = node_budget(rig, plan)
    assert node["max_attempts"] == 2 and node["max_tokens"] == (30_000_000 - 3_000_000) // 2
    above = c.candidate(limits={"max_wall_seconds": 3600})
    with pytest.raises(Hold) as held:
        rig.service.plan(submit(rig, "(above) make value return 2"), composition=above)
    assert held.value.code == "LIMITS_ABOVE_CEILING"


# ---------------------------------------------------------------- not honoured yet


def held_before_any_claim(rig: Any, loop: Any, container: Any, goal: str, part: str) -> None:
    with pytest.raises(RuntimeFault) as refused:  # the refused part, by name
        loop._policies(rig.service.plan_record(goal))
    assert refused.value.code == "COMPONENT_CONTENT" and refused.value.details == [part]
    record = loop.run_goal(goal)
    assert record["status"] == "held" and record["attempts"] == []
    assert "COMPONENT_CONTENT" in record["reason"] and container.prompts == []
    assert rig.d.store.head(rig.d.scope, "goal", goal)["state"] == "failed"
    assert rig.published == []


@pytest.mark.parametrize(
    ("components", "part"),
    [
        # S9 runs the strategies, S9b vote as well (test_vote_is_honoured_and_runs_its_candidates)
        (
            {"retrieval": {"enabled": True}},
            "retrieval (method path_keyword_v1 is not specified yet)",
        ),
        ({"feedback_form": {"mode": "summary"}}, "feedback_form.mode summary"),  # not specified
        # S10 runs fast checks (L7); the v1 max_followups 0 leaves no turn to report a failure in
        (
            {"fast_checks": {"enabled": True, "checks": ["check"]}},
            "fast_checks: max_followups 0 leaves no follow-up turn",
        ),
    ],
)
def test_a_component_the_loop_cannot_honour_is_held_before_any_claim(
    deployment: Any, tmp_path: Path, components: dict[str, dict[str, Any]], part: str
) -> None:
    rig, loop, container, c = setup(deployment, tmp_path, "right")
    goal = pinned(rig, c.candidate(**components), "(unsupported) make value return 2")
    held_before_any_claim(rig, loop, container, goal, part)


def test_vote_is_honoured_and_runs_its_candidates(deployment: Any, tmp_path: Path) -> None:
    """S9b (§14 Q16 settled, §5.1 M4): a vote composition is no longer held before the claim. Its
    one attempt runs k candidate turns from the same base (the run's own turn, then
    ``<dispatch_id>-c1`` in a scratch workspace), the app's quick verifier picks one, and the
    suite verifies the selected change once.

    The rc06 stand-in names one session per mode for every process; a real ``codex exec`` starts
    a new thread, so the candidate's process gets its own session id here (a candidate naming
    the run's bound session is held, SESSION_REBIND)."""
    rig, loop, container, c = setup(deployment, tmp_path, "right")
    command = container.command

    def own_session(argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        cmd: list[str] = command(argv, workspace, run_name, **kw)
        if re.search(r"-c[0-9]$", run_name):
            cmd[2] = cmd[2].replace('"thread_" + mode', repr("thread_" + run_name))
        return cmd

    container.command = own_session
    composition = c.candidate(
        execution_strategy={"enabled": ["vote"], "params": {"vote": {"k": 2}}}
    )
    goal = pinned(rig, composition, "(vote) make value return 2")
    plan = rig.service.plan_record(goal)
    assert plan["strategy"]["strategy"] == "vote" and plan["strategy"]["refused"] is None
    loop._policies(plan)  # honoured: nothing is refused
    record = loop.run_goal(goal)
    assert record["status"] in {"verified", "published"}
    (attempt,) = record["attempts"]
    assert attempt["outcome"] == "pass" and attempt["candidates"] == 2
    assert attempt["selected"] in {0, 1}
    assert [r["state"] for r in attempt["candidate_results"]] == ["collected", "released"]
    assert len(container.prompts) == 2  # two candidate turns, no follow-up
    metrics = rig.service.plan_record(goal)["strategy_metrics"]
    assert (metrics["candidates"], metrics["candidate_turns"], metrics["turns"]) == (2, 1, 2)


def refused_at_plan(rig: Any, container: Any, composition: Any, part: str) -> None:
    """Router parts nobody honours are refused when the goal is planned (S10, clarification
    after W0-W1): RuntimeFault COMPONENT_CONTENT before the planner turn, so no planner turn,
    no plan record, no claim and no agent run."""
    goal = submit(rig, "(refused at plan) make value return 2")
    with pytest.raises(RuntimeFault) as refused:
        rig.service.plan(goal, composition=composition)
    assert not isinstance(refused.value, Hold)
    assert refused.value.code == "COMPONENT_CONTENT" and refused.value.details == [part]
    assert rig.planner.calls == []  # refused before the planner turn
    assert list(rig.workspaces.root.glob("plan-ws-*")) == []
    with pytest.raises(RuntimeFault):
        rig.service.plan_record(goal)  # nothing was planned, so nothing can be approved
    assert container.prompts == [] and rig.published == []


@pytest.mark.parametrize(
    ("components", "part"),
    [
        # the router carrier: selection reads the app router; L3 decides the executor cell only
        (
            {"route_policy": {"order": {"*": ["claude-cli", "codex-cli", "opencode-server"]}}},
            "route_policy order of the composition (selection reads the app router)",
        ),
        (
            {"route_policy": {"roles": {"planner": ["codex-cli"]}}},
            "route_policy roles (L3 decides the executor cell only)",
        ),
    ],
)
def test_a_router_part_nobody_honours_is_refused_before_the_planner_turn(
    deployment: Any, tmp_path: Path, components: dict[str, dict[str, Any]], part: str
) -> None:
    rig, _loop, container, c = setup(deployment, tmp_path, "right")
    refused_at_plan(rig, container, c.candidate(**components), part)


def test_a_router_decider_that_cannot_run_is_refused_before_the_planner_turn(
    deployment: Any, tmp_path: Path
) -> None:
    # the fixture's method names task_class, which is not an L1 feature (§6.1): the L1 decider
    # cannot run, so the goal is refused when planned (deciders.check)
    rig, _loop, container, c = setup(deployment, tmp_path, "right")
    refused_at_plan(
        rig, container, c.materialize(L1=c.decider("L1")),
        "decider L1: features task_class are not L1 features",
    )  # fmt: skip


class InterpretingTurn:
    """The planner's read-only turn: replies with the rig's draft and keeps each prompt."""

    cell_id = "codex-cli"

    def __init__(self, draft: dict[str, Any]) -> None:
        self.draft, self.prompts = draft, []  # type: ignore[var-annotated]

    def run(self, *, prompt: str, schema: dict[str, Any], workspace: Path, **_: Any) -> Any:
        from amplai_foundry.runtime.execution.readonly_turn import TurnResult

        self.prompts.append(prompt)
        usage = {"input_tokens": 3, "output_tokens": 2}
        return TurnResult(copy.deepcopy(self.draft), usage, 0.0, "sha256:" + "0" * 64)


def test_an_interpretation_variant_is_honoured_and_the_goal_runs(
    deployment: Any, tmp_path: Path
) -> None:
    """Moved from the refusals above: the planner has text for ask_first (§2.2), so a goal of
    that composition is planned with it and runs; the v1 rule is replaced, nothing else."""
    from amplai_foundry.runtime.execution import planner_codex

    rig, loop, container, c = setup(deployment, tmp_path, "right")
    planner = planner_codex.CodexPlanner(None, None, tmp_path / "runs", model="m")  # type: ignore[arg-type]
    turn = InterpretingTurn(rig.planner.draft_value)
    planner.turn = turn  # type: ignore[assignment]
    rig.service.planner = planner
    ref = c.candidate(interpretation={"planner_instruction": "ask_first"})
    goal = pinned(rig, ref, "(ask first) make value return 2")
    plan = rig.service.plan_record(goal)
    assert plan["interpretation"] == {"planner_instruction": "ask_first", "contract_form": "v1"}
    (prompt,) = turn.prompts
    assert planner_codex.ASK_FIRST_WORK in prompt and planner_codex.AMBIGUITY_WORK not in prompt
    assert prompt == planner.prompt(
        "(ask first) make value return 2", "app",
        {v.id: v.description for v in rig.service.apps["app"].config.verifiers},
        interpretation="ask_first",
    )  # fmt: skip
    loop._policies(plan)  # the loop holds nothing for it
    record = loop.run_goal(goal)
    assert record["status"] == "published" and len(container.prompts) == 1


def test_a_router_the_loop_honours_runs(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _container, c = setup(deployment, tmp_path, "right")
    d = rig.d
    # another router record with the selection router's content (route_policy v1, no deciders)
    same = c.candidate(route_policy={})
    value = d.store.get(d.scope, "harness-composition", same)
    assert value["router_policy_ref"] != rig.service.apps["app"].router_ref
    assert loop.run_goal(pinned(rig, same, "(same router) make value return 2"))["status"] == (
        "published"
    )
    # a Work 030 candidate keeps the legacy task_class_baseline router: it reads as v1
    legacy_router = rig.service._put(
        "router-policy",
        "legacy-router",
        {
            "policy_id": "legacy-router",
            "scope": d.scope.wire(),
            "kind": "task_class_baseline",
            "order": {"*": list(ROUTER_ORDER)},
            "source": "D-079",
        },
    )
    base = d.store.get(d.scope, "harness-composition", c.base_ref)
    legacy = CompositionService(d.store, d.contracts).register(
        c.proposer,
        {
            **base,
            "composition_id": base["composition_id"] + "__legacy",
            "revision": 1,
            "router_policy_ref": legacy_router,
        },
    )
    record = loop.run_goal(pinned(rig, legacy, "(legacy router) make value return 2"))
    assert record["status"] == "published"
