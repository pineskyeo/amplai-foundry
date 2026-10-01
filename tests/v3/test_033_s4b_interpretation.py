"""Work 033: L1 interpretation variants and the planner turn's trace (interfaces.md §2.2 row
``interpretation``, §6.1 L1, §5.3 / IC-21, §9.1, §9.3).

Contract read: the ``interpretation`` component is ``{"planner_instruction": "v1" | "ask_first" |
"assume_and_state", "contract_form": "v1" | "steps"}`` with v1 = today's ``INSTRUCTION`` and
``plan_schema`` (golden G4); L1's ``replan_ask_first`` is one more planner turn with the ask_first
instruction, an auxiliary read-only turn counted against ``aux_max_tokens`` (IC-21); a capturing
trial's planner turn is stored as turn ``"planner"`` of a trace under the proposer-only ACL.

Real: ``CodexPlanner`` prompts and schemas, the rc06 product rig (store, runtime, workspaces,
``LocalExecutionService.plan``/``approve``, ``ExecutionLoop`` with the protected verifier), the
deciders, ``TraceService``. Stand-ins: the planner's read-only turn (``ScriptedTurn``: replies with
fixed drafts and records what it was asked) and the rc06 host-process agent. No driver, docker,
provider or network.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from test_033_s10_deciders import Env, rows

from amplai_foundry.meta_harness.traces import TRACE_KIND, TraceBuffer, TraceService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import planner_codex, policies
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.planner_codex import (
    AMBIGUITY_RULES,
    INTERPRETATION_RULES,
    CodexPlanner,
    instruction_text,
    plan_schema,
)
from amplai_foundry.runtime.execution.product import LocalExecutionService, TrialContext
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from e2e.test_033_s3_context import Components
from golden033 import planner_oracle
from rc06_rig import DRAFT, rig_with_codex, submit

VERIFIERS = {"check": "value() must return 2", "lint": "ruff check ."}
APPS = {"api": {"check": "pytest"}, "ui": {"lint": "eslint"}}
QUESTIONS = {**DRAFT, "questions": ["Which value should it return?"]}
STEPS = [{"step": "Change value() in app.py to return 2", "files": ["app.py"]}]
MARK = "PLANNER-TRACE-TEXT-4410"
EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE-PLANNER-REASONING"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": MARK}},
]
VARIANTS = ("ask_first", "assume_and_state")


def snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The default-deny sanitizer's buffer snapshot of ``events`` (``traces.TraceBuffer``)."""
    buffer = TraceBuffer("codex")
    for event in events:
        buffer.add(event)
    return buffer.snapshot()


class ScriptedTurn:
    """The planner's read-only turn: replies with ``outputs`` in order (the last one repeats; an
    exception is raised) and records each call; with ``capture_trace`` it returns the sanitized
    events of ``EVENTS`` (a ``TraceBuffer`` snapshot, the shape the real turns return)."""

    cell_id = "codex-cli"

    def __init__(self, *outputs: Any) -> None:
        self.outputs = list(outputs) or [DRAFT]
        self.calls: list[dict[str, Any]] = []

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None, **kw: Any
    ) -> TurnResult:
        self.calls.append({"prompt": prompt, "schema": schema, **kw})
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(out, Exception):
            raise out
        trace = snapshot(EVENTS) if kw.get("capture_trace") else None
        usage = {"input_tokens": 3, "output_tokens": 2}
        return TurnResult(copy.deepcopy(out), usage, 0.1, "sha256:" + "0" * 64, trace)

    @property
    def prompts(self) -> list[str]:
        return [c["prompt"] for c in self.calls]


def planner(tmp_path: Path, *outputs: Any) -> tuple[CodexPlanner, ScriptedTurn]:
    made = CodexPlanner(None, None, tmp_path / "runs", model="m")  # type: ignore[arg-type]
    turn = ScriptedTurn(*outputs)
    made.turn = turn  # type: ignore[assignment]
    return made, turn


def hold(code: str, fn: Any, *args: Any, **kwargs: Any) -> Hold:
    with pytest.raises(Hold) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def fault(code: str, fn: Any, *args: Any, **kwargs: Any) -> RuntimeFault:
    with pytest.raises(RuntimeFault) as caught:
        fn(*args, **kwargs)
    assert not isinstance(caught.value, Hold) and caught.value.code == code
    return caught.value


# ==================================================================================================
# prompts and schemas (§2.2): v1 unchanged, each variant replaces the ambiguity rule only
# ==================================================================================================
@pytest.mark.parametrize("mode", ["work", "design"])
def test_the_v1_prompts_are_byte_equal_to_the_oracle(tmp_path: Path, mode: str) -> None:
    made, _turn = planner(tmp_path)
    want = planner_oracle.prompt("G", "app", VERIFIERS, mode)
    assert made.prompt("G", "app", VERIFIERS, mode) == want
    assert made.prompt("G", "app", VERIFIERS, mode, interpretation="v1") == want
    assert made.multi_prompt("G", APPS, interpretation="v1") == planner_oracle.multi_prompt(
        "G", APPS
    )
    for base, kind in (
        (planner_codex.INSTRUCTION, "work"),
        (planner_codex.DESIGN_INSTRUCTION, "design"),
        (planner_codex.MULTI_INSTRUCTION, "multi"),
    ):
        assert instruction_text(base, kind, "v1") is base
        assert base.count(AMBIGUITY_RULES[kind]) == 1  # the rule a variant replaces is there


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_variant_replaces_only_the_ambiguity_rule_in_every_mode(
    tmp_path: Path, variant: str
) -> None:
    made, _turn = planner(tmp_path)
    for mode, kind in (("work", "work"), ("design", "design")):
        v1 = made.prompt("G", "app", VERIFIERS, mode)
        text = made.prompt("G", "app", VERIFIERS, mode, interpretation=variant)
        assert text == v1.replace(AMBIGUITY_RULES[kind], INTERPRETATION_RULES[variant][kind])
        assert AMBIGUITY_RULES[kind] not in text
    multi = made.multi_prompt("G", APPS, interpretation=variant)
    assert multi == made.multi_prompt("G", APPS).replace(
        AMBIGUITY_RULES["multi"], INTERPRETATION_RULES[variant]["multi"]
    )
    # a strategy's M7 rule still follows the rules
    steps = made.prompt("G", "app", VERIFIERS, variant="steps", interpretation=variant)
    assert planner_codex.STEPS_RULES in steps and INTERPRETATION_RULES[variant]["work"] in steps


def test_the_variant_texts_say_what_the_contract_says() -> None:
    ask = INTERPRETATION_RULES["ask_first"]["work"]
    assert "whenever the goal is ambiguous" in ask and '"questions"' in ask
    assert "instead of\n  assuming an answer" in ask
    assume = INTERPRETATION_RULES["assume_and_state"]["work"]
    assert "do not ask questions; questions is an empty list" in assume
    assert 'state each\n  assumption you made as one entry of "assumptions"' in assume
    assert "leave work_items empty" in INTERPRETATION_RULES["ask_first"]["multi"]


@pytest.mark.parametrize("bad", ["ask_maybe", "", "V1", None])
def test_an_unknown_planner_instruction_is_refused(tmp_path: Path, bad: Any) -> None:
    made, turn = planner(tmp_path)
    fault("PLANNER_VARIANT", made.prompt, "G", "app", VERIFIERS, interpretation=bad)
    fault("PLANNER_VARIANT", made.multi_prompt, "G", APPS, interpretation=bad)
    fault("PLANNER_VARIANT", instruction_text, planner_codex.INSTRUCTION, "work", bad)
    with pytest.raises(RuntimeFault) as caught:
        made.draft("G", "app", VERIFIERS, tmp_path, interpretation=bad)
    assert caught.value.code == "PLANNER_VARIANT" and turn.calls == []  # nothing ran
    # a component with an unknown value cannot even be registered (policies.validate_content)
    for content in (
        {"planner_instruction": bad, "contract_form": "v1"},
        {"planner_instruction": "v1", "contract_form": "outline"},
    ):
        fault("COMPONENT_CONTENT", policies.validate_content, "interpretation", content)


def test_a_router_with_an_unknown_interpretation_is_still_refused(
    deployment: Any, tmp_path: Path
) -> None:
    """``router_refusals`` lifts the known variants only (it is the loop's and the plan's
    refusal); a stored router whose interpretation names something else is refused."""
    service = Rigged(deployment, tmp_path).service
    ref = {"id": "r", "revision": 1, "digest": "sha256:" + "0" * 64}
    for interpretation, want in (
        ({"planner_instruction": "ask_first", "contract_form": "steps"}, []),
        ({"planner_instruction": "assume_and_state", "contract_form": "v1"}, []),
        (
            {"planner_instruction": "ask_maybe", "contract_form": "v1"},
            ["interpretation planner_instruction ask_maybe (no planner text)"],
        ),
        (
            {"planner_instruction": "v1", "contract_form": "outline"},
            ["interpretation contract_form outline"],
        ),
    ):
        router = policies.RouterPolicy(
            order={"*": ["codex-cli"]}, roles={}, interpretation=interpretation,
            deciders={"L1": None, "L2": None, "L3": None}, ref=ref,
        )  # fmt: skip
        assert service.router_refusals(router) == want


def test_draft_sends_the_variant_schema_and_returns_a_trace_only_when_captured(
    tmp_path: Path,
) -> None:
    made, turn = planner(tmp_path, {**DRAFT, "steps": STEPS})
    plain = made.draft("G", "app", VERIFIERS, tmp_path)
    assert "trace" not in plain and "capture_trace" not in turn.calls[0]  # today's call
    assert turn.calls[0]["schema"] == plan_schema(list(VERIFIERS))
    steps = made.draft(
        "G", "app", VERIFIERS, tmp_path, variant="steps", interpretation="assume_and_state",
        capture_trace=True,
    )  # fmt: skip
    call = turn.calls[1]
    assert call["schema"] == plan_schema(list(VERIFIERS), "steps")
    assert "steps" in call["schema"]["required"] and call["capture_trace"] is True
    assert INTERPRETATION_RULES["assume_and_state"]["work"] in call["prompt"]
    assert steps["trace"]["items"] == [
        {"type": "message", "role": "assistant", "text": MARK, "tool": None, "exit_code": None}
    ]
    assert "PRIVATE-PLANNER-REASONING" not in str(steps["trace"])


def test_a_planner_without_the_text_or_the_steps_variant_is_refused_before_its_turn() -> None:
    class Bare:
        pass

    bare = Bare()
    args = {"mode": "work", "multi": False, "variant": None, "variant_args": {},
            "planner_cell": "codex-cli"}  # fmt: skip
    interpret = LocalExecutionService._interpretation_args
    held = fault(
        "COMPONENT_CONTENT", interpret, bare,
        {"planner_instruction": "ask_first", "contract_form": "steps"}, **args,
    )  # fmt: skip
    assert held.details == [
        "interpretation planner_instruction ask_first (the planner of codex-cli has no text "
        "for it)",
        "interpretation contract_form steps (the planner of codex-cli drafts no steps)",
    ]
    made = CodexPlanner.__new__(CodexPlanner)  # the class attributes are what is read
    for mode, multi in (("design", False), ("work", True)):
        fault(
            "COMPONENT_CONTENT", interpret, made,
            {"planner_instruction": "v1", "contract_form": "steps"},
            **{**args, "mode": mode, "multi": multi},
        )  # fmt: skip
    # v1 passes nothing new; steps replaces a strategy's parts variant
    assert interpret(made, policies.V1["interpretation"], **args) == (None, {})
    variant, kw = interpret(
        made, {"planner_instruction": "ask_first", "contract_form": "steps"},
        **{**args, "variant": "parts", "variant_args": {"variant": "parts", "max_parts": 3}},
    )  # fmt: skip
    assert (variant, kw) == ("steps", {"variant": "steps", "interpretation": "ask_first"})


def test_contract_form_steps_is_refused_or_held_unless_plan_execute_runs() -> None:
    check = LocalExecutionService._check_contract_form
    steps = {"planner_instruction": "v1", "contract_form": "steps"}
    check(steps, {"strategy": "plan_execute"}, trial=False)  # shown to the executor
    check(policies.V1["interpretation"], {"strategy": "repair_loop"}, trial=False)  # v1
    trial = {"strategy": "repair_loop", "refused": None}
    check(steps, trial, trial=True)
    assert "plan_execute only" in trial["refused"]  # the loop holds it before any claim
    real = fault("COMPONENT_CONTENT", check, steps, {"strategy": "repair_loop"}, trial=False)
    assert real.details == [
        "interpretation contract_form steps: repair_loop shows no steps to the executor "
        "(plan_execute only)"
    ]


# ==================================================================================================
# the product (rc06 rig): plan, approve, run
# ==================================================================================================
class Rigged:
    def __init__(self, deployment: Any, tmp_path: Path, *outputs: Any) -> None:
        self.rig, self.loop, self.container = rig_with_codex(deployment, tmp_path, "right")
        self.planner, self.turn = planner(tmp_path, *outputs)
        self.rig.service.planner = self.planner
        self.c = Components(self.rig)
        self.d = self.rig.d

    @property
    def service(self) -> Any:
        return self.rig.service

    def plan(self, composition: Any, text: str, **kw: Any) -> str:
        goal = submit(self.rig, text)
        self.service.plan(goal, composition=composition, **kw)
        return goal

    def record(self, goal: str) -> dict[str, Any]:
        value: dict[str, Any] = self.service.plan_record(goal)
        return value

    def interpretation(self, **fields: Any) -> Any:
        return self.c.version("interpretation", **fields)

    def limits(self, aux: int) -> Any:
        return self.c.version("limits", aux_max_tokens=aux)

    def trials(self) -> ExecutionLoop:
        return ExecutionLoop(self.service, self.loop.coordinator, publisher=None)


def test_ask_first_plans_with_its_text_and_the_goal_runs(deployment: Any, tmp_path: Path) -> None:
    r = Rigged(deployment, tmp_path)
    ref = r.c.materialize(interpretation=r.interpretation(planner_instruction="ask_first"))
    goal = r.plan(ref, "(ask) make value return 2")
    plan = r.record(goal)
    assert plan["interpretation"] == {"planner_instruction": "ask_first", "contract_form": "v1"}
    (prompt,) = r.turn.prompts
    assert INTERPRETATION_RULES["ask_first"]["work"] in prompt
    assert AMBIGUITY_RULES["work"] not in prompt
    r.service.approve(r.rig.operator, goal)
    assert r.loop.run_goal(goal)["status"] == "published"


def test_assume_and_state_puts_the_stated_assumptions_in_the_contract(
    deployment: Any, tmp_path: Path
) -> None:
    stated = {**DRAFT, "assumptions": ["value() means the module-level function in app.py"]}
    r = Rigged(deployment, tmp_path, stated)
    ref = r.c.materialize(interpretation=r.interpretation(planner_instruction="assume_and_state"))
    plan = r.record(r.plan(ref, "(assume) make value return 2"))
    assert INTERPRETATION_RULES["assume_and_state"]["work"] in r.turn.prompts[0]
    contract = r.d.store.get(r.d.scope, "goal-contract", plan["contract_ref"])
    assert [a["statement"] for a in contract["assumptions"]] == stated["assumptions"]
    assert contract["assumptions"][0]["origin"] == "inferred"


def test_a_v1_interpretation_calls_the_planner_as_before(deployment: Any, tmp_path: Path) -> None:
    r = Rigged(deployment, tmp_path)
    goal = r.plan(r.c.base_ref, "(v1) make value return 2")
    assert "interpretation" not in r.record(goal)
    (call,) = r.turn.calls
    assert set(call) == {"prompt", "schema"}  # no interpretation or capture argument reached it
    assert call["schema"] == plan_schema(["check"])
    assert AMBIGUITY_RULES["work"] in call["prompt"]


def test_a_steps_contract_drafts_the_m7_steps_and_shows_them_to_the_executor(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, {**DRAFT, "steps": STEPS})
    ref = r.c.materialize(
        interpretation=r.interpretation(contract_form="steps"),
        execution_strategy=r.c.version("execution_strategy", enabled=["plan_execute"]),
    )
    goal = r.plan(ref, "(steps) make value return 2")
    plan = r.record(goal)
    (call,) = r.turn.calls  # the planner turn drafted the steps: no extra turn
    assert call["schema"] == plan_schema(["check"], "steps")
    assert planner_codex.STEPS_RULES in call["prompt"]
    assert plan["strategy"]["strategy"] == "plan_execute" and not plan["strategy"]["refused"]
    assert plan["plan_steps"] == STEPS and plan.get("aux_usage", []) == []
    r.service.approve(r.rig.operator, goal)
    record = r.loop.run_goal(goal)
    assert record["status"] == "published"
    assert (
        "Plan (follow these steps):\n1. Change value() in app.py to return 2 (files: app.py)"
        in (r.container.prompts[0])
    )


def test_a_steps_contract_without_plan_execute_enabled_is_refused_before_the_planner_turn(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path)
    ref = r.c.materialize(interpretation=r.interpretation(contract_form="steps"))
    goal = submit(r.rig, "(no plan_execute) make value return 2")
    refused = fault("COMPONENT_CONTENT", r.service.plan, goal, composition=ref)
    assert refused.details == [
        "interpretation contract_form steps (plan_execute is not an enabled strategy; steps "
        "reach the executor through it only)"
    ]
    assert r.turn.calls == [] and r.container.prompts == []


def test_a_steps_contract_whose_strategy_is_not_plan_execute_is_refused_or_held(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, {**DRAFT, "steps": STEPS})
    ref = r.c.materialize(
        interpretation=r.interpretation(contract_form="steps"),
        execution_strategy=r.c.version(
            "execution_strategy", enabled=["repair_loop", "plan_execute"]
        ),
    )
    # a real goal: refused after its planner turn, before a contract is frozen
    goal = submit(r.rig, "(real steps) make value return 2")
    refused = fault("COMPONENT_CONTENT", r.service.plan, goal, composition=ref)
    assert "repair_loop shows no steps" in refused.details[0]
    assert len(r.turn.calls) == 1  # the strategy is known only after the draft
    with pytest.raises(RuntimeFault):
        r.service.plan_record(goal)  # nothing was saved, so nothing can be approved
    # a trial: its strategy is refused, so the loop holds it before any claim
    trial = TrialContext(
        subject={"experiment_id": "exp-steps", "trial_id": "trial-steps"}, arm="candidate",
        cell_id="codex-cli", split="development", capture_trace=False, planner_mode="real",
        environment_id="app",
    )  # fmt: skip
    held = r.plan(ref, "(trial steps) make value return 2", trial=trial)
    plan = r.record(held)
    assert "plan_execute only" in plan["strategy"]["refused"]
    r.service.approve(r.rig.operator, held)
    record = r.trials().run_goal(held)
    assert record["status"] == "held" and "COMPONENT_CONTENT" in record["reason"]
    assert r.container.prompts == []


def test_a_plan_drafted_with_another_interpretation_than_its_composition_is_never_approved(
    deployment: Any, tmp_path: Path
) -> None:
    """Lifting the refusal must not let a plan drafted with the v1 text (a plan of an earlier
    build, or a changed record) run under a composition whose interpretation differs: approval
    holds ``COMPOSITION_CHANGED`` (plan again), so nothing is granted or run."""
    r = Rigged(deployment, tmp_path)
    goal = r.plan(r.c.base_ref, "(swapped) make value return 2")
    ask_first = r.c.materialize(interpretation=r.interpretation(planner_instruction="ask_first"))
    record = r.record(goal)
    record["composition"] = {**record["composition"], "ref": ask_first, "pinned": True}
    r.service._save_plan(goal, record, None)
    held = hold("COMPOSITION_CHANGED", r.service.approve, r.rig.operator, goal)
    assert held.details == {
        "planned": policies.V1["interpretation"],
        "now": {"planner_instruction": "ask_first", "contract_form": "v1"},
    }
    assert r.record(goal)["status"] == "awaiting_approval" and r.container.prompts == []
    # the plan of that composition, drafted with its text, is approved
    fine = r.plan(ask_first, "(drafted ask) make value return 2")
    assert r.service.approve(r.rig.operator, fine)["status"] == "approved"


def test_a_planner_without_the_variant_text_is_refused_before_its_turn(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path)
    r.service.planner = r.rig.planner  # the rig's fixed planner: v1 text only
    ref = r.c.materialize(interpretation=r.interpretation(planner_instruction="ask_first"))
    goal = submit(r.rig, "(fixed planner) make value return 2")
    refused = fault("COMPONENT_CONTENT", r.service.plan, goal, composition=ref)
    assert refused.details == [
        "interpretation planner_instruction ask_first (the planner of codex-cli has no text for it)"
    ]
    assert r.rig.planner.calls == [] and r.container.prompts == []


# ==================================================================================================
# L1 replan_ask_first (§6.1): one more planner turn, auxiliary (IC-21)
# ==================================================================================================
def l1_replan_decider(r: Rigged) -> Any:
    """An L1 decider whose table makes ``replan_ask_first`` credibly best for one question."""
    env = Env(r.d)
    method = env.method(["questions"], name="l1replan")
    data = [
        *rows("proceed", 20, 6, {"questions": 1}, tokens=100),
        *rows("ask_back", 20, 6, {"questions": 1}, tokens=100),
        *rows("replan_ask_first", 20, 19, {"questions": 1}, tokens=100),
    ]
    table = env.table(
        "L1", data, features=("questions",),
        options=("proceed", "ask_back", "replan_ask_first"), method=method,
    )  # fmt: skip
    return env.decider("L1", method, table)


def l1_of(r: Rigged, goal: str) -> dict[str, Any]:
    from amplai_foundry.meta_harness import deciders

    (found,) = [
        d
        for d in deciders.plan_decisions(r.d.store, r.d.scope, r.record(goal))
        if d["layer"] == "L1"
    ]
    return found


def test_replan_ask_first_runs_one_more_turn_with_ask_first_and_proceeds(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS, DRAFT)  # asks, then plans without a question
    ref = r.c.materialize(L1=l1_replan_decider(r), limits=r.limits(1000))
    goal = r.plan(ref, "(replan) make value return 2")
    plan = r.record(goal)
    l1 = l1_of(r, goal)
    assert l1["chosen"] == "replan_ask_first" and l1["used_prior"] is False
    first, second = r.turn.prompts  # exactly one more turn
    assert AMBIGUITY_RULES["work"] in first
    assert INTERPRETATION_RULES["ask_first"]["work"] in second
    assert plan["l1_replan"] == {
        "ran": True, "error": None, "first_questions": 1, "outcome": "proceed",
    }  # fmt: skip
    assert plan["draft"]["questions"] == [] and plan["status"] == "awaiting_approval"
    (entry,) = plan["aux_usage"]
    assert (entry["role"], entry["purpose"], entry["cell_id"]) == (
        "planner", "L1 replan_ask_first", "codex-cli",
    )  # fmt: skip
    assert entry["tokens"] == 5 and entry["usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert plan["aux_overrun"] is False  # 5 tokens of a 1000 cap


def test_replan_ask_first_that_still_asks_asks_back_without_a_third_turn(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS)  # every turn asks
    ref = r.c.materialize(L1=l1_replan_decider(r), limits=r.limits(1000))
    plan = r.record(r.plan(ref, "(asks twice) make value return 2"))
    assert len(r.turn.calls) == 2
    assert plan["status"] == "needs_answers" and plan["l1_replan"]["outcome"] == "ask_back"


def test_replan_ask_first_is_not_eligible_at_the_v1_aux_cap(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS)
    ref = r.c.materialize(L1=l1_replan_decider(r))  # limits v1: aux_max_tokens 0 (IC-21)
    goal = r.plan(ref, "(capped) make value return 2")
    options = {o["option"]: o for o in l1_of(r, goal)["options"]}
    assert options["replan_ask_first"]["eligible"] is False
    assert options["replan_ask_first"]["why"] == "AUX_BUDGET: limits.aux_max_tokens 0 (IC-21)"
    assert l1_of(r, goal)["chosen"] != "replan_ask_first" and len(r.turn.calls) == 1


def test_the_replan_turn_counts_against_the_cap_and_may_overrun_it(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS, DRAFT)
    ref = r.c.materialize(L1=l1_replan_decider(r), limits=r.limits(1))  # one token
    plan = r.record(r.plan(ref, "(overrun) make value return 2"))
    assert len(r.turn.calls) == 2  # it could start (0 < 1) ...
    assert plan["aux_overrun"] is True  # ... and spent 5 (§5.3: the last turn may pass the cap)


def test_replan_ask_first_is_not_eligible_when_the_draft_already_asked_first(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS)
    ref = r.c.materialize(
        L1=l1_replan_decider(r), limits=r.limits(1000),
        interpretation=r.interpretation(planner_instruction="ask_first"),
    )  # fmt: skip
    goal = r.plan(ref, "(already) make value return 2")
    options = {o["option"]: o for o in l1_of(r, goal)["options"]}
    assert options["replan_ask_first"]["why"] == "the draft already used the ask_first instruction"
    assert len(r.turn.calls) == 1


def test_a_failed_replan_turn_holds_a_trial_and_leaves_a_goal_on_its_first_draft(
    deployment: Any, tmp_path: Path
) -> None:
    failing = Hold("TURN_FAILED", "Read-only turn failed")
    r = Rigged(deployment, tmp_path, QUESTIONS, failing)
    ref = r.c.materialize(L1=l1_replan_decider(r), limits=r.limits(1000))
    plan = r.record(r.plan(ref, "(real fails) make value return 2"))
    assert plan["l1_replan"] == {
        "ran": True, "error": "PLANNER_FAILED", "first_questions": 1, "outcome": "ask_back",
    }  # fmt: skip
    assert plan["status"] == "needs_answers"  # the v1 rule on the first draft
    (entry,) = plan["aux_usage"]
    assert entry["tokens"] is None and entry["error"] == "PLANNER_FAILED"  # unknown usage
    # a trial is never graded on a draft it did not ask for
    r.turn.outputs = [QUESTIONS, failing]
    context = TrialContext(
        subject={"experiment_id": "exp-l1", "trial_id": "trial-l1"}, arm="candidate",
        cell_id="codex-cli", split="development", capture_trace=False, planner_mode="real",
        environment_id="app",
    )  # fmt: skip
    goal = submit(r.rig, "(trial fails) make value return 2")
    hold("PLANNER_FAILED", r.service.plan, goal, composition=ref, trial=context)
    with pytest.raises(RuntimeFault):
        r.service.plan_record(goal)  # no plan was saved for the trial
    assert len(r.turn.calls) == 4 and r.container.prompts == []


# ==================================================================================================
# the planner turn's trace (§9.1, §9.3)
# ==================================================================================================
def trial_head(r: Rigged, trial_id: str, task_id: str = "bug-dev-01") -> None:
    """The dispatching trial head ``TraceService`` reads the task from (§3.12)."""
    with r.d.store.tx() as db:
        r.d.store.cas(db, r.d.scope, "eval-trial", trial_id, 0, "dispatching", {"task_id": task_id})


def capturing(trial_id: str, *, split: str = "development", capture: bool = True) -> TrialContext:
    return TrialContext(
        subject={"experiment_id": "exp-pt", "trial_id": trial_id}, arm="candidate",
        cell_id="codex-cli", split=split, capture_trace=capture, planner_mode="real",
        environment_id="app", domain="bug",
    )  # fmt: skip


def traces_of(r: Rigged) -> TraceService:
    return TraceService(r.d.store, r.d.scope, r.d.artifacts)


def proposer(r: Rigged) -> Actor:
    return Actor("meta-proposer", r.d.scope, frozenset({"harness.propose"}), "service")


def test_a_capturing_trials_planner_turn_is_stored_as_turn_planner(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path)
    trial_head(r, "trial-pt-1")
    goal = r.plan(r.c.base_ref, "(traced plan) make value return 2", trial=capturing("trial-pt-1"))
    assert r.turn.calls[0]["capture_trace"] is True
    service = traces_of(r)
    ref = service.of_run(goal + ".planner")
    assert ref is not None and ref["id"] == f"trace-{goal}.planner"
    read = service.read(proposer(r), ref)  # the proposer-only ACL: a development trace
    record, body = read["record"], read["body"]
    assert record["goal_id"] == goal and record["split"] == "development"
    assert record["task_id"] == "bug-dev-01" and record["cell_id"] == "codex-cli"
    assert [t["turn"] for t in body["turns"]] == ["planner"]
    assert [i["text"] for i in body["turns"][0]["items"]] == [MARK]
    assert "PRIVATE-PLANNER-REASONING" not in str(body)
    artifact = r.d.store.get(r.d.scope, TRACE_KIND, ref)["artifact"]
    assert artifact["media_type"] == "application/json"
    # the plan record holds no trace text
    assert MARK not in str(r.record(goal))


def test_the_replan_turn_is_a_second_planner_turn_of_the_same_trace(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path, QUESTIONS, DRAFT)
    trial_head(r, "trial-pt-2")
    ref = r.c.materialize(L1=l1_replan_decider(r), limits=r.limits(1000))
    goal = r.plan(ref, "(traced replan) make value return 2", trial=capturing("trial-pt-2"))
    assert [c.get("capture_trace") for c in r.turn.calls] == [True, True]
    found = traces_of(r).of_run(goal + ".planner")
    assert found is not None
    body = traces_of(r).read(proposer(r), found)["body"]
    assert [t["turn"] for t in body["turns"]] == ["planner", "planner"]


def test_a_non_development_planner_trace_is_not_for_the_proposer(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path)
    trial_head(r, "trial-pt-3")
    goal = r.plan(
        r.c.base_ref, "(validation plan) make value return 2",
        trial=capturing("trial-pt-3", split="validation"),
    )  # fmt: skip
    ref = traces_of(r).of_run(goal + ".planner")
    assert ref is not None
    hold("TRACE_ACL", traces_of(r).read, proposer(r), ref)


def test_a_real_goal_and_an_uncaptured_trial_store_no_planner_trace(
    deployment: Any, tmp_path: Path
) -> None:
    r = Rigged(deployment, tmp_path)
    trial_head(r, "trial-pt-4")
    real = r.plan(r.c.base_ref, "(real plan) make value return 2")
    off = r.plan(
        r.c.base_ref, "(off plan) make value return 2", trial=capturing("trial-pt-4", capture=False)
    )
    assert all("capture_trace" not in c for c in r.turn.calls)  # nothing asked the turn to
    assert traces_of(r).of_run(real + ".planner") is None
    assert traces_of(r).of_run(off + ".planner") is None
    assert traces_of(r).records() == []


def test_a_secret_in_the_planner_turn_stores_nothing(deployment: Any, tmp_path: Path) -> None:
    r = Rigged(deployment, tmp_path)
    secret = "password: Zq9wXk3LmN8vBc2RtY6uHj4P"
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": secret}}]

    def leaky(*, prompt: str, schema: dict[str, Any], workspace: Path, **kw: Any) -> TurnResult:
        return TurnResult(
            copy.deepcopy(DRAFT), None, 0.1, "sha256:" + "0" * 64,
            snapshot(events) if kw.get("capture_trace") else None,
        )  # fmt: skip

    r.turn.run = leaky  # type: ignore[method-assign]
    trial_head(r, "trial-pt-5")
    goal = r.plan(r.c.base_ref, "(leaky plan) make value return 2", trial=capturing("trial-pt-5"))
    assert r.record(goal)["status"] == "awaiting_approval"  # capture never changes the plan
    assert traces_of(r).of_run(goal + ".planner") is None
    assert traces_of(r).counts()["drops"] == {"secret_pattern": 1}
