"""Work 033 S9: the ten execution strategies end to end (interfaces.md §5, §3.6, IC-04..06, IC-21).

Contract: specs/033-harness-taxonomy/interfaces.md §0 IC-04/05/06/21, §2 execution_strategy and
trial metrics (plan.md §8.1), §3.3 `escalate`/`terminal_nodes`, §3.6 `StrategyRunner` and
`IntegrationQueue`, §5.1-§5.4, §13 row S9, §14 Q4 and Q16.

Real: store, runtime, goals, worker coordinator, execution loop, git workspaces, the operator
approval, protected verification (`SuiteVerifier` + `NonEmptyChangeCheck`, `finish_work`), the
`StrategyRunner` and `IntegrationQueue`, `ManifestService.materialize` for candidate compositions,
the Codex-shaped CLI port with its credential seeding. Stand-ins (named, as in every rc06 test):
the "container" runs a script on the host that writes the files a per-call script asks for (the
host-process stand-in of `rc06_rig.ScriptContainer`), the read-only turns are `FakeTurn`s that
return scripted structured output and token usage, and the planner is the rig's fixed planner (or a
variant planner). No docker, provider, network or credential.

What each case proves, per strategy: the strategy runs claim -> attempt -> protected verification
to a published goal (or fails by the suite), the §5.2 records are in the plan record, and
`strategy_metrics` (what `plan.md` §8.1 asks of a trial) counts what the strategy did.

`vote` (M4) is held, not enabled: §14 Q16 (b) asks how `SessionStore`, `runtime.start`, steering
pause and effect reconciliation treat unbound candidate sessions and S9 builds M4 only after that
answer; the tests pin the hold (`COMPONENT_CONTENT`, before any claim) so an accidental enablement
fails here.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.meta_harness.components import ComponentService
from amplai_foundry.meta_harness.manifest import ManifestService
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import policies
from amplai_foundry.runtime.execution.integration_queue import IntegrationQueue
from amplai_foundry.runtime.execution.product import (
    AppConfig,
    TrialContext,
    VerifierCommand,
)
from amplai_foundry.runtime.execution.publish import terminal_nodes
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.execution.strategy_runner import (
    AUX_STRATEGIES,
    HELD,
    PRIOR,
    StrategyRunner,
)
from rc06_rig import (
    DRAFT,
    FixedPlanner,
    checkout,
    rig_with_codex,
    rig_with_two_drivers,
    submit,
)

# -- the stand-in suite and agent --------------------------------------------------------------
# The suite passes on the base and on any change that does not write the marker BAD into a .py
# file, so every part of a split and the integration node can be verified on its own change.
SUITE = (
    "python3",
    "-c",
    "import pathlib, sys; "
    "sys.exit(1 if any('BAD' in p.read_text() for p in pathlib.Path('.').rglob('*.py')) else 0)",
)
VERIFIER = "suite"  # a new id: the rig's own "check" keeps its runner (value() == 2) on re-install
GOOD = "def value():\n    return 2\n"
WRONG = "def value():\n    return 3  # BAD\n"
BASE_TEXT = "def value():\n    return 1\n"

AGENT = r"""
import json, pathlib, sys
ws, spec, home = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2]), pathlib.Path(sys.argv[3])
assert (home / ".codex" / "auth.json").is_file(), "credential was not leased"
for rel, text in spec["writes"].items():
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
(home / ".codex" / "auth.json").write_text('{"tokens": "refreshed"}')
sys.stdout.write(json.dumps({"type": "thread.started", "thread_id": spec["session"]}) + "\n")
sys.stdout.write(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": spec["usage"][0], "output_tokens": spec["usage"][1]}}) + "\n")
"""


@dataclass
class Call:
    """One executor process the scripted container is asked to start.

    ``files`` is the workspace as the process finds it (taken when the command is built, before
    the run workspace is discarded), so a test can tell a fresh base from a repair copy."""

    index: int
    prompt: str
    resume: bool
    session: str | None
    name: str  # the dispatch id
    workspace: Path
    home: Path
    files: dict[str, str] = field(default_factory=dict)

    @property
    def app_text(self) -> str:
        return self.files.get("app.py", "")

    def has(self, rel: str) -> bool:
        return rel in self.files


def read_tree(workspace: Path) -> dict[str, str]:
    return {
        str(p.relative_to(workspace)): p.read_text()
        for p in sorted(Path(workspace).rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(workspace).parts
    }


Script = Any  # Callable[[Call], dict[str, str]]: the files this turn writes


AGENT_CLAUDE = r"""
import json, os, pathlib, sys
ws, spec = pathlib.Path(sys.argv[1]), json.loads(sys.argv[2])
assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"), "token was not passed by env"
for rel, text in spec["writes"].items():
    target = ws / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
sid = spec["session"]
sys.stdout.write(json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n")
sys.stdout.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "session_id": sid, "result": "DONE",
                             "usage": {"input_tokens": spec["usage"][0],
                                       "output_tokens": spec["usage"][1]}}) + "\n")
"""


class Agents:
    """The scripted host-process agent: ``script(call)`` says which files a turn writes."""

    def __init__(self, container: Any, script: Script, dialect: str = "codex") -> None:
        self.container, self.script, self.dialect = container, script, dialect
        self.calls: list[Call] = []
        self.on_call: Any = None  # a hook run inside command(), while the claim is held
        self.order: list[tuple[str, str]] = []  # executor and read-only turns, in start order
        container.command = self.command

    def command(self, argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
        claude = self.dialect == "claude"
        resume = not claude and len(argv) > 5 and argv[4] == "resume"
        call = Call(
            len(self.calls), argv[argv.index("-p") + 1] if claude else argv[-1], resume,
            argv[5] if resume else None, run_name, Path(workspace),
            Path(kw["native_home"]), read_tree(workspace),
        )  # fmt: skip
        self.calls.append(call)
        self.order.append(("executor", "resume" if resume else "first"))
        if self.on_call is not None:
            self.on_call(call)
        writes = self.script(call)
        spec = {
            "writes": writes,
            "session": call.session or "thread_" + run_name,
            "usage": [10, 5],
        }
        import sys

        if claude:
            return [sys.executable, "-c", AGENT_CLAUDE, str(workspace), json.dumps(spec)]
        return [sys.executable, "-c", AGENT, str(workspace), json.dumps(spec), str(call.home)]

    @property
    def prompts(self) -> list[str]:
        return [c.prompt for c in self.calls]


def always(text: str) -> Script:
    return lambda call: {"app.py": text}


def right(call: Call) -> dict[str, str]:
    return {"app.py": GOOD}


def wrong_first(call: Call) -> dict[str, str]:
    """Wrong on the base (value 1), right on a repair copy that holds the previous change."""
    return {"app.py": GOOD if "BAD" in call.app_text else WRONG}


SUITE_DRAFT = {**DRAFT, "acceptance": [{"statement": "value() returns 2", "verifier": VERIFIER}]}


# -- fake read-only turns ---------------------------------------------------------------------
def kind_of(schema: dict[str, Any]) -> str:
    required = set(schema.get("required") or [])
    return {
        frozenset({"verdict", "requests"}): "review",
        frozenset({"findings"}): "investigate",
        frozenset({"parts", "integration_notes"}): "lead",
        frozenset({"parts"}): "split",
        frozenset({"steps"}): "steps",
    }[frozenset(required)]


@dataclass
class TurnCall:
    cell_id: str
    kind: str
    prompt: str
    workspace: Path
    existed: bool
    patch_in_prompt: str = ""


class FakeTurns:
    """The ``turns`` factory of a ``StrategyRunner``: one scripted ``ReadOnlyTurn`` per cell.

    ``outputs[kind]`` is a dict, a list of dicts (one per call, the last repeats) or a callable of
    the call; ``usage`` is the (input, output) token pair of a turn, ``None`` for unknown usage.
    """

    def __init__(self) -> None:
        self.outputs: dict[str, Any] = {}
        self.usage: Any = (50, 50)
        self.calls: list[TurnCall] = []
        self.fail: dict[str, Hold | RuntimeFault] = {}
        self.missing: set[str] = set()  # cells with no read-only turn
        self.barrier: threading.Barrier | None = None  # investigators wait for each other
        self.active = 0
        self.max_active = 0
        self.order: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, cell_id: str) -> Any:
        if cell_id in self.missing:
            raise Hold("TURN_FAILED", "No read-only turn is configured for this cell")
        return _Turn(self, cell_id)

    def of(self, kind: str) -> list[TurnCall]:
        return [c for c in self.calls if c.kind == kind]


class _Turn:
    def __init__(self, turns: FakeTurns, cell_id: str) -> None:
        self.turns, self.cell_id = turns, cell_id

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None
    ) -> TurnResult:
        turns, kind = self.turns, kind_of(schema)
        with turns._lock:
            turns.calls.append(
                TurnCall(self.cell_id, kind, prompt, Path(workspace), Path(workspace).is_dir())
            )
            turns.order.append(("turn", kind))
            turns.active += 1
            turns.max_active = max(turns.max_active, turns.active)
            index = len(turns.of(kind)) - 1
        try:
            if turns.barrier is not None and kind == "investigate":
                turns.barrier.wait(timeout=10)  # raises BrokenBarrierError when run one by one
            if kind in turns.fail:
                raise turns.fail[kind]
            spec = turns.outputs[kind]
            if callable(spec):
                output = spec(prompt, index)
            elif isinstance(spec, list):
                output = spec[min(index, len(spec) - 1)]
            else:
                output = spec
            usage = turns.usage(kind, index) if callable(turns.usage) else turns.usage
            counted = (
                None if usage is None else {"input_tokens": usage[0], "output_tokens": usage[1]}
            )
            return TurnResult(copy.deepcopy(output), counted, 0.0, "sha256:" + "0" * 64)
        finally:
            with turns._lock:
                turns.active -= 1


# -- compositions -----------------------------------------------------------------------------
class Components:
    """Candidate compositions of the rig's Codex composition (as the S3 context tests)."""

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
            component_id=f"{kind}.s9v{self.count}",
            kind=kind,
            content={**copy.deepcopy(policies.V1[kind]), **fields},
            source="proposer",
            rationale="S9 strategy test",
        )

    def candidate(self, **components: dict[str, Any]) -> Any:
        slots = {slot: self.version(slot, **fields) for slot, fields in components.items()}
        manifest = self.manifests.change(self.manifests.of_composition(self.base_ref), **slots)
        return self.manifests.materialize(
            self.proposer, base_composition_ref=self.base_ref, manifest=manifest,
            suffix=f"s9c{self.count}",
        )  # fmt: skip

    def strategy(self, name: str, params: dict[str, Any] | None = None, **more: Any) -> Any:
        """A composition whose execution strategy is ``name``; ``params`` override the defaults
        (every field of a strategy's params is required, ``policies._strategy_params``)."""
        full = {**DEFAULT_PARAMS[name], **(params or {})} if name in DEFAULT_PARAMS else None
        execution = {"enabled": [name], "params": {name: full} if full is not None else {}}
        return self.candidate(execution_strategy=execution, **more)


DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "workgraph_split": {"max_nodes": 2},
    "plan_execute": {"planner_role": "planner"},
    "best_of_n": {"n": 2},
    "generator_reviewer": {"reviewer_role": "reviewer", "max_rounds": 1},
    "cascade": {"cells": ["codex-cli", "claude-cli"], "max_escalations": 1},
    "orchestrator": {"max_parts": 3, "lead_role": "planner"},
    "parallel_readonly": {"steps": []},
    "vote": {"k": 2},
}
AUX = {"limits": {**policies.V1["limits"], "aux_max_tokens": 5_000_000}}  # a real goal's aux cap


@dataclass
class World:
    rig: Any
    loop: Any
    container: Any
    agents: Agents
    turns: FakeTurns
    comps: Components
    runner: StrategyRunner
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def service(self) -> Any:
        return self.rig.service

    def plan(self, composition: Any, text: str = "make value return 2", **kw: Any) -> str:
        self.extra["goals"] = n = self.extra.get("goals", 0) + 1
        goal = submit(self.rig, f"({n}) {text}")  # the submit key is the first 20 characters
        self.service.plan(goal, composition=composition, **kw)
        return goal

    def approved(self, composition: Any, text: str = "make value return 2", **kw: Any) -> str:
        goal = self.plan(composition, text, **kw)
        self.service.approve(self.rig.operator, goal)
        return goal

    def record(self, goal: str) -> dict[str, Any]:
        record: dict[str, Any] = self.service.plan_record(goal)
        return record

    def run(self, composition: Any, text: str = "make value return 2") -> dict[str, Any]:
        record: dict[str, Any] = self.loop.run_goal(self.approved(composition, text))
        return record

    def nodes(self, goal: str) -> list[dict[str, Any]]:
        graph = self.rig.d.store.get(self.rig.d.scope, "workgraph", self.record(goal)["graph_ref"])
        nodes: list[dict[str, Any]] = graph["nodes"]
        return nodes


def make_world(
    deployment: Any, tmp_path: Path, script: Script = right, *, planner: Any = None
) -> World:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
    if planner is not None:
        rig.service.planner = planner
    return finish_world(rig, loop, container, script)


def finish_world(rig: Any, loop: Any, container: Any, script: Script) -> World:
    # the suite of this file replaces the rig's (value() == 2): every node passes on its own change
    config = rig.service.apps["app"].config
    rig.service.install(
        AppConfig(
            "app", rig.repo,
            (VerifierCommand(VERIFIER, SUITE, "no BAD marker in a .py file", 60),),
            quick_verifiers=config.quick_verifiers,
        )
    )  # fmt: skip
    rig.planner.draft_value = copy.deepcopy(SUITE_DRAFT)
    turns = FakeTurns()
    runner = StrategyRunner(rig.service, turns, IntegrationQueue(rig.workspaces, rig.d.scope))
    rig.service.strategies = runner
    agents = Agents(container, script)
    agents.order = turns.order  # one log of every process, in the order it started
    return World(rig, loop, container, agents, turns, Components(rig), runner)


def metrics(record: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = record["strategy_metrics"]
    return value


def outcomes(record: dict[str, Any]) -> list[str]:
    return [a["outcome"] for a in record["attempts"]]


def change_files(world: World, record: dict[str, Any]) -> list[str]:
    """The files of the last verified change of a goal (its cumulative patch)."""
    d, mgr = world.rig.d, world.rig.workspaces
    last = next(a for a in reversed(record["attempts"]) if a.get("change"))
    raw = d.artifacts.read(d.scope, last["change"])
    patch = mgr.read_change(d.scope, raw)[1].decode()
    return sorted({line.split(" b/")[1] for line in patch.splitlines() if line.startswith("diff")})


# ===========================================================================================
# 1. single
# ===========================================================================================
def test_single_runs_one_attempt_and_verifies(deployment: Any, tmp_path: Path) -> None:
    w = make_world(deployment, tmp_path)
    goal = w.approved(w.comps.strategy("single"))
    node = w.nodes(goal)[0]
    assert node["budget"]["max_attempts"] == 1  # the node budget, not the attempt policy's 3
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and w.rig.published == [goal]
    assert outcomes(record) == ["pass"]
    assert record["strategy"]["strategy"] == "single" and record["strategy"]["used_prior"] is False
    m = metrics(record)
    assert (m["strategy"], m["attempts_used"], m["turns"], m["followups"]) == ("single", 1, 1, 0)
    assert (m["escalations"], m["reviewer_rounds"], m["nodes"], m["re_verifications"]) == (
        0, 0, 1, 0,
    )  # fmt: skip
    # the rig planner reports usage, so its read-only turn counts as an agent call
    assert m["agent_calls"] == 2 and m["aux_turns"] == 0 and m["cells_used"] == ["codex-cli"]


def test_single_fails_after_its_one_attempt_without_a_repair(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, always(WRONG))
    record = w.run(w.comps.strategy("single"))
    assert outcomes(record) == ["fail"] and record["status"] == "failed"
    assert len(w.agents.prompts) == 1 and w.rig.published == []
    assert metrics(record)["attempts_used"] == 1
    # the protected verification still decided: the goal ended failed in the runtime
    goal = record["goal_id"]
    assert w.rig.d.store.head(w.rig.d.scope, "goal", goal)["state"] == "failed"


# ===========================================================================================
# 2. repair_loop (v1; golden G1: unchanged)
# ===========================================================================================
def test_repair_loop_is_the_v1_loop_and_its_prompts_are_byte_identical(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, wrong_first)
    # the goal on the installed composition (no strategy component: the v1 prior) ...
    base_goal = w.approved(w.comps.base_ref, "make value return 2")
    base_record = w.loop.run_goal(base_goal)
    base_prompts = list(w.agents.prompts)
    # ... and the goal that declares repair_loop through a candidate composition
    w.agents.calls.clear()
    declared = w.run(w.comps.strategy("repair_loop"), "make value return 2")
    assert outcomes(base_record) == outcomes(declared) == ["fail", "pass"]
    assert declared["strategy"]["strategy"] == "repair_loop"
    assert base_record["strategy"]["strategy"] == PRIOR == "repair_loop"
    assert declared["strategy"]["used_prior"] is False and declared["strategy"]["refused"] is None
    # G1: no extra section, no hook, the same prompt text for the first and the repair attempt
    assert w.agents.prompts == base_prompts
    first, second = base_prompts
    assert "did not pass" not in first and "did not pass" in second
    for extra in ("Plan (follow", "Investigation notes", "Already applied", "Merged parts"):
        assert extra not in first + second
    # the base-composition prompt is what ``loop.prompt`` renders with no extra sections
    contract = w.rig.d.store.get(w.rig.d.scope, "goal-contract", declared["contract_ref"])
    assert first == w.loop.prompt(contract, declared, None, node=w.nodes(declared["goal_id"])[0])
    m = metrics(declared)
    assert (m["attempts_used"], m["turns"], m["followups"], m["aux_turns"]) == (2, 2, 0, 0)
    assert w.turns.calls == []  # no read-only turn: the v1 loop never starts an auxiliary one
    assert declared["aux_usage"] == [] and declared["aux_overrun"] is False


def test_repair_loop_stops_at_the_attempt_budget(deployment: Any, tmp_path: Path) -> None:
    w = make_world(deployment, tmp_path, always(WRONG))
    record = w.run(w.comps.strategy("repair_loop"))
    assert outcomes(record) == ["fail", "fail", "fail"] and record["status"] == "failed"
    assert metrics(record)["attempts_used"] == 3
    assert checkout(w.rig.repo)[1] == ""  # the operator's checkout never moved


# ===========================================================================================
# 3. workgraph_split (M7 + M5 chain)
# ===========================================================================================
def acceptance(statement: str = "the suite passes") -> list[dict[str, str]]:
    return [{"statement": statement, "verifier": VERIFIER}]


def part(objective: str, scope: list[str], statement: str = "the suite passes") -> dict[str, Any]:
    return {"objective": objective, "in_scope": scope, "acceptance": acceptance(statement)}


PARTS = [
    part("Part one: value() returns 2", ["app.py"]),
    part("Part two: add note.py", ["note.py"]),
]


def chain_script(call: Call) -> dict[str, str]:
    if "in app: Part one" in call.prompt:
        return {"app.py": GOOD}
    return {"note.py": "NOTE = 'part two'\n"}


class VariantPlanner(FixedPlanner):
    """A planner that drafts the M7 schema variants (``planner_codex.CodexPlanner.VARIANTS``)."""

    VARIANTS = ("steps", "parts")

    def __init__(self, steps: Any = None, parts: Any = None) -> None:
        super().__init__(SUITE_DRAFT)
        self.steps, self.parts = steps, parts
        self.asked: list[tuple[str | None, int]] = []

    def draft(  # type: ignore[override]
        self, goal: str, app: str, verifiers: dict[str, str], workspace: Path, *,
        mode: str = "work", variant: str | None = None, max_parts: int = 4,
    ) -> dict[str, Any]:  # fmt: skip
        out = super().draft(goal, app, verifiers, workspace, mode=mode)
        self.asked.append((variant, max_parts))
        if variant == "steps":
            out["draft"]["steps"] = copy.deepcopy(self.steps)
        elif variant == "parts":
            out["draft"]["parts"] = copy.deepcopy(self.parts)
        return out


def trial_context(**over: Any) -> TrialContext:
    fields: dict[str, Any] = {
        "subject": {"experiment_id": "exp-s9", "trial_id": "trial-s9"}, "arm": "candidate",
        "cell_id": "codex-cli", "split": "development", "capture_trace": False,
        "planner_mode": "fixed", "environment_id": "app",
    }  # fmt: skip
    return TrialContext(**{**fields, **over})


def test_workgraph_split_runs_a_chain_and_verifies_every_node_on_the_cumulative_change(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, chain_script)
    w.turns.outputs["split"] = {"parts": PARTS}
    goal = w.approved(w.comps.strategy("workgraph_split", {"max_nodes": 3}, **AUX))
    plan = w.record(goal)
    s1, s2 = w.nodes(goal)
    # §5.2: node ids .s<k>, the chain's second node after the first, goal acceptance on the last
    assert (s1["node_id"], s2["node_id"]) == ("node-app.s1", "node-app.s2")
    assert s1["depends_on"] == [] and s2["depends_on"] == ["node-app.s1"]
    assert (len(s1["acceptance_ids"]), len(s2["acceptance_ids"])) == (1, 2)
    assert plan["node_apps"] == {"node-app.s1": "app", "node-app.s2": "app"}
    assert plan["strategy"]["strategy"] == "workgraph_split" and plan["strategy"]["variant"] is None
    # a fixed planner cannot draft the variant: one planner-cell read-only turn made the parts
    (split,) = w.turns.of("split")
    assert split.cell_id == "codex-cli" and split.existed and not split.workspace.exists()
    assert [(e["purpose"], e["tokens"]) for e in plan["aux_usage"]] == [("split", 100)]
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and outcomes(record) == ["pass", "pass"]
    assert [a["node_id"] for a in record["attempts"]] == ["node-app.s1", "node-app.s2"]
    # the second node starts on the first node's verified change and says so
    assert w.agents.calls[1].app_text == GOOD
    assert (
        "Already applied in this directory (earlier parts of this goal, verified):"
        in (w.agents.prompts[1])
    )
    assert "- Part one: value() returns 2" in w.agents.prompts[1]
    assert "Already applied" not in w.agents.prompts[0]
    assert change_files(w, record) == ["app.py", "note.py"]  # the last node's change is cumulative
    last = terminal_nodes(
        w.rig.d.store.get(w.rig.d.scope, "workgraph", record["graph_ref"]), record["node_apps"]
    )
    assert list(last) == ["app"] and last["app"]["node_id"] == "node-app.s2"
    m = metrics(record)
    assert (m["nodes"], m["re_verifications"], m["attempts_used"]) == (2, 1, 2)
    assert (m["aux_turns"], m["aux_tokens"], m["agent_calls"]) == (1, 100, 4)  # 2 + split + planner


def test_workgraph_split_repairs_a_part_on_its_own_previous_patch(
    deployment: Any, tmp_path: Path
) -> None:
    def script(call: Call) -> dict[str, str]:
        if "in app: Part one" in call.prompt:
            return {"app.py": GOOD if "BAD" in call.app_text else WRONG}
        return {"note.py": "NOTE = 'part two'\n"}

    w = make_world(deployment, tmp_path, script)
    w.turns.outputs["split"] = {"parts": PARTS}
    record = w.run(w.comps.strategy("workgraph_split", {"max_nodes": 2}, **AUX))
    assert outcomes(record) == ["fail", "pass", "pass"] and record["status"] == "published"
    assert "did not pass" in w.agents.prompts[1] and "did not pass" not in w.agents.prompts[2]
    m = metrics(record)
    assert (m["nodes"], m["re_verifications"], m["attempts_used"]) == (2, 1, 3)


def test_workgraph_split_stops_when_a_part_fails_its_attempts(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, always(WRONG))
    w.turns.outputs["split"] = {"parts": PARTS}
    record = w.run(w.comps.strategy("workgraph_split", {"max_nodes": 2}, **AUX))
    assert record["status"] == "failed" and w.rig.published == []
    assert [a["node_id"] for a in record["attempts"]] == ["node-app.s1"] * 3
    assert len(w.agents.prompts) == 3  # the second part never ran


def test_workgraph_split_takes_the_variant_from_a_planner_that_drafts_it(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, chain_script)
    planner = VariantPlanner(parts=PARTS)
    # a variant planner needs no auxiliary turn, so the strategy is eligible with aux_max_tokens 0
    goal = w.approved(w.comps.strategy("workgraph_split", {"max_nodes": 3}), planner=planner)
    plan = w.record(goal)
    assert ("parts", 3) in planner.asked  # the variant and max_nodes reach the planner
    assert plan["strategy"]["strategy"] == "workgraph_split"
    assert plan["strategy"]["used_prior"] is False
    assert plan["strategy"]["variant"] == "parts" and plan["aux_usage"] == []
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and w.turns.calls == []
    assert metrics(record)["aux_turns"] == 0 and metrics(record)["agent_calls"] == 3


@pytest.mark.parametrize(
    ("parts", "why"),
    [
        ([PARTS[0]], "A split needs 2..2 parts"),  # one part is no split
        ([PARTS[0], PARTS[1], part("Part three", ["c.py"])], "A split needs 2..2 parts"),
        ([PARTS[0], {**PARTS[1], "acceptance": []}], "Every part needs an objective"),
        ([PARTS[0], {**PARTS[1], "objective": "  "}], "Every part needs an objective"),
    ],
)
def test_a_bad_split_is_turn_output_a_real_goal_runs_the_prior_and_a_trial_is_refused(
    deployment: Any, tmp_path: Path, parts: list[dict[str, Any]], why: str
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["split"] = {"parts": parts}
    strategy = w.comps.strategy("workgraph_split", {"max_nodes": 2}, **AUX)
    plan = w.record(w.plan(strategy))
    assert plan["strategy"]["strategy"] == PRIOR and plan["strategy"]["used_prior"] is True
    assert plan["strategy"]["eligibility"]["plan"].startswith("workgraph_split: TURN_OUTPUT: ")
    assert why in plan["strategy"]["eligibility"]["plan"]
    assert [i["objective"] for i in plan["work_items"]] == ["value() returns 2"]  # one plain item
    record = w.loop.run_goal(w.approved(strategy))
    assert record["status"] == "published" and len(w.agents.prompts) == 1  # the v1 loop ran
    trial = w.record(w.plan(strategy, trial=trial_context()))
    assert trial["strategy"]["refused"].startswith("workgraph_split: TURN_OUTPUT: ")


def test_a_failed_planner_turn_is_turn_failed_and_a_missing_cell_turn_too(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["split"] = {"parts": PARTS}
    strategy = w.comps.strategy("workgraph_split", {"max_nodes": 2}, **AUX)
    w.turns.fail["split"] = Hold("TURN_TIMEOUT", "Read-only turn exceeded its time budget")
    plan = w.record(w.plan(strategy))
    assert plan["strategy"]["strategy"] == PRIOR
    assert "TURN_TIMEOUT" in plan["strategy"]["eligibility"]["plan"]
    # the failed turn may have spent tokens: its usage is unknown and the goal starts no more
    assert [(e["error"], e["tokens"]) for e in plan["aux_usage"]] == [("TURN_TIMEOUT", None)]
    w.turns.fail.clear()
    w.turns.missing.add("codex-cli")
    plan = w.record(w.plan(strategy))
    assert "TURN_FAILED" in plan["strategy"]["eligibility"]["plan"]
    assert [(e["error"], e["tokens"]) for e in plan["aux_usage"]] == [("TURN_FAILED", 0)]
    trial = w.record(w.plan(strategy, trial=trial_context()))
    assert trial["strategy"]["refused"].startswith("workgraph_split: TURN_FAILED")


# ===========================================================================================
# 4. plan_execute (M7)
# ===========================================================================================
STEPS = [
    {"step": "Change value() to return 2", "files": ["app.py"]},
    {"step": "Run the suite", "files": []},
]


def test_plan_execute_renders_the_planner_steps_as_a_plan_section(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, wrong_first)
    w.turns.outputs["steps"] = {"steps": STEPS}
    record = w.run(w.comps.strategy("plan_execute", {"planner_role": "planner"}, **AUX))
    assert outcomes(record) == ["fail", "pass"] and record["status"] == "published"
    assert record["plan_steps"] == STEPS
    section = (
        "Plan (follow these steps):\n1. Change value() to return 2 (files: app.py)\n"
        "2. Run the suite"
    )
    # the section is on the first and on the repair attempt, before the feedback
    assert all(section in p for p in w.agents.prompts)
    assert w.agents.prompts[1].index(section) < w.agents.prompts[1].index("did not pass")
    (steps_turn,) = w.turns.of("steps")
    assert steps_turn.cell_id == "codex-cli" and "at most 12 steps" in steps_turn.prompt
    m = metrics(record)
    assert (m["attempts_used"], m["aux_turns"], m["agent_calls"]) == (
        2,
        1,
        4,
    )  # 2 + steps + planner
    assert m["cells_used"] == ["codex-cli"]


def test_plan_execute_takes_the_steps_from_a_variant_planner_without_a_turn(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    planner = VariantPlanner(steps=STEPS)
    strategy = w.comps.strategy("plan_execute", {"planner_role": "planner"})
    goal = w.approved(strategy, planner=planner)
    plan = w.record(goal)
    assert plan["strategy"]["strategy"] == "plan_execute" and plan["strategy"]["variant"] == "steps"
    assert plan["plan_steps"] == STEPS and plan["aux_usage"] == []
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and w.turns.calls == []
    assert "Plan (follow these steps):" in w.agents.prompts[0]
    assert metrics(record)["aux_turns"] == 0


@pytest.mark.parametrize("count", [13, 40])
def test_plan_execute_refuses_more_than_twelve_steps(
    deployment: Any, tmp_path: Path, count: int
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["steps"] = {"steps": [{"step": f"step {i}", "files": []} for i in range(count)]}
    strategy = w.comps.strategy("plan_execute", {"planner_role": "planner"}, **AUX)
    plan = w.record(w.plan(strategy))
    assert plan["strategy"]["strategy"] == PRIOR and plan["strategy"]["used_prior"] is True
    assert plan["strategy"]["eligibility"]["plan"].startswith("plan_execute: TURN_OUTPUT: ")
    assert "at most 12 steps" in plan["strategy"]["eligibility"]["plan"]
    assert "plan_steps" not in plan


# ===========================================================================================
# 5. best_of_n (M1: every attempt on the fresh base, no feedback)
# ===========================================================================================
def test_best_of_n_starts_every_attempt_on_the_fresh_base_and_the_first_pass_wins(
    deployment: Any, tmp_path: Path
) -> None:
    def script(call: Call) -> dict[str, str]:
        return {"app.py": WRONG if call.index == 0 else GOOD}

    w = make_world(deployment, tmp_path, script)
    goal = w.approved(w.comps.strategy("best_of_n", {"n": 3}))
    assert w.nodes(goal)[0]["budget"]["max_attempts"] == 3
    record = w.loop.run_goal(goal)
    assert outcomes(record) == ["fail", "pass"] and record["status"] == "published"
    # attempt 2 saw the base (value 1) and no feedback, unlike a repair attempt
    assert w.agents.calls[1].app_text == BASE_TEXT
    assert w.agents.prompts[0] == w.agents.prompts[1]
    m = metrics(record)
    assert (m["strategy"], m["best_of_n_first_pass"], m["attempts_used"]) == ("best_of_n", 2, 2)
    assert m["followups"] == 0 and m["turns"] == 2


def test_best_of_n_with_no_passing_attempt_fails_after_n_attempts(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, always(WRONG))
    goal = w.approved(w.comps.strategy("best_of_n", {"n": 2}))
    assert w.nodes(goal)[0]["budget"]["max_attempts"] == 2
    record = w.loop.run_goal(goal)
    assert outcomes(record) == ["fail", "fail"] and record["status"] == "failed"
    assert metrics(record)["best_of_n_first_pass"] is None
    assert metrics(record)["attempts_used"] == 2


@pytest.mark.parametrize("n", [1, 4, 5])
def test_best_of_n_and_vote_allow_two_or_three_only(
    deployment: Any, tmp_path: Path, n: int
) -> None:
    """IC-06: n <= 3 (``max_verifier_repair_attempts_including_initial``); 1 is no best-of-n."""
    w = make_world(deployment, tmp_path)
    for strategy, name in (("best_of_n", "n"), ("vote", "k")):
        with pytest.raises(RuntimeFault) as bad:
            w.comps.strategy(strategy, {name: n})
        assert bad.value.code == "COMPONENT_CONTENT"


def test_the_node_budget_of_best_of_n_never_exceeds_the_root_attempts(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    limits = {"limits": {**policies.V1["limits"], "max_attempts": 2}}
    strategy = w.comps.strategy("best_of_n", {"n": 3}, attempt_policy={"max_attempts": 2}, **limits)
    goal = w.plan(strategy)
    contract = w.rig.d.store.get(w.rig.d.scope, "goal-contract", w.record(goal)["contract_ref"])
    assert contract["budget"]["max_attempts"] == 2
    assert w.nodes(goal)[0]["budget"]["max_attempts"] == 2  # min(n, the root's attempts)


# ===========================================================================================
# 6. generator_reviewer (M3 follow-up turn + M2 reviewer turn)
# ===========================================================================================
CHANGES = {"verdict": "request_changes", "requests": ["add a docstring", "rename x"]}
APPROVE = {"verdict": "approve", "requests": []}


def reviewed(call: Call) -> dict[str, str]:
    """The generator writes the fix; the follow-up (the same session) adds a note file."""
    return {"NOTES.md": "reviewed\n"} if call.resume else {"app.py": GOOD}


def worker_head(w: World, dispatch_id: str) -> dict[str, Any]:
    head: dict[str, Any] = w.rig.d.store.head(w.rig.d.scope, "worker-execution", dispatch_id)
    return head


def test_generator_reviewer_resumes_the_same_session_with_the_reviewers_requests(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = [CHANGES, APPROVE]
    strategy = w.comps.strategy(
        "generator_reviewer", {"reviewer_role": "reviewer", "max_rounds": 2}, **AUX
    )
    record = w.run(strategy)
    assert record["status"] == "published" and outcomes(record) == ["pass"]
    first, follow = w.agents.calls
    # IC-04: the follow-up is a new dispatch <dispatch_id>-f1 of the bound session, resumed
    assert follow.resume is True and follow.name == first.name + "-f1"
    assert follow.session == "thread_" + first.name
    assert follow.prompt.startswith("A reviewer asked for these changes before verification")
    assert "- add a docstring\n- rename x\n" in follow.prompt
    assert follow.has("NOTES.md") is False and follow.app_text == GOOD  # the same workspace
    # the worker-execution head lists the follow-up; the attempt is one run, one verification
    head = worker_head(w, first.name)
    assert head["state"] == "verifying"
    (entry,) = head["data"]["followups"]
    assert entry["dispatch_id"] == first.name + "-f1" and entry["receipt_digest"].startswith(
        "sha256:"
    )
    assert w.container.driver.journal.read(first.name + "-f1")["state"] == "completed"
    # the reviewer read the change of the first turn, on a copy that was discarded afterwards
    reviews = w.turns.of("review")
    assert len(reviews) == 2 and all(c.cell_id == "codex-cli" and c.existed for c in reviews)
    assert "+    return 2" in reviews[0].prompt and not reviews[0].workspace.exists()
    # records: the attempt notes its follow-up and rounds; the plan record keeps each review
    attempt = record["attempts"][0]
    assert (attempt["followups"], attempt["reviewer_rounds"]) == (1, 2)
    assert [r["verdict"] for r in record["reviews"]] == ["request_changes", "approve"]
    assert record["reviews"][0]["requests"] == ["add a docstring", "rename x"]
    m = metrics(record)
    assert (m["reviewer_rounds"], m["fix_requests"], m["followups"]) == (2, 2, 1)
    assert (m["attempts_used"], m["turns"], m["aux_turns"]) == (1, 2, 2)
    assert m["agent_calls"] == 5  # 1 attempt + 1 follow-up + 2 reviewer turns + the planner turn
    # a resumed Codex session keeps the last receipt's usage until §14 Q4 settles cumulative
    run = w.rig.d.store.head(w.rig.d.scope, "run", record["attempts"][0]["run_id"])
    assert run["data"]["record"]["usage"]["input_tokens"] == 10


def test_generator_reviewer_approval_sends_no_follow_up(deployment: Any, tmp_path: Path) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = APPROVE
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    assert record["status"] == "published" and len(w.agents.calls) == 1
    m = metrics(record)
    assert (m["reviewer_rounds"], m["fix_requests"], m["followups"], m["turns"]) == (1, 0, 0, 1)
    assert worker_head(w, w.agents.calls[0].name)["data"].get("followups") in (None, [])


def test_a_change_request_without_requests_sends_no_follow_up(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = {"verdict": "request_changes", "requests": ["  ", ""]}
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 1}, **AUX))
    assert record["status"] == "published" and len(w.agents.calls) == 1
    assert record["reviews"][0]["requests"] == [] and metrics(record)["fix_requests"] == 0


def test_the_follow_up_count_is_the_max_rounds_and_never_above_two(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES  # the reviewer asks again every round
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    assert record["status"] == "published" and len(w.agents.calls) == 3  # first + f1 + f2
    assert [c.name for c in w.agents.calls[1:]] == [
        w.agents.calls[0].name + f"-f{k}" for k in (1, 2)
    ]
    assert all(c.session == "thread_" + w.agents.calls[0].name for c in w.agents.calls[1:])
    assert (metrics(record)["followups"], metrics(record)["turns"]) == (2, 3)
    with pytest.raises(RuntimeFault) as bad:  # max_rounds is 1..2
        w.comps.strategy("generator_reviewer", {"max_rounds": 3})
    assert bad.value.code == "COMPONENT_CONTENT"


def test_a_failed_reviewer_turn_is_recorded_and_the_attempt_goes_to_verification(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES
    w.turns.fail["review"] = Hold("TURN_FAILED", "provider refused")
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    assert record["status"] == "published" and len(w.agents.calls) == 1  # no follow-up
    assert [(r["verdict"], r["error"]) for r in record["reviews"]] == [(None, "TURN_FAILED")]
    m = metrics(record)
    assert (m["reviewer_rounds"], m["fix_requests"]) == (0, 0)  # no verdict, no round
    # the failed turn may have spent tokens: unknown usage, so the goal starts no more
    assert [(e["role"], e["tokens"], e["error"]) for e in record["aux_usage"]] == [
        ("reviewer", None, "TURN_FAILED")
    ]
    # no review reached the generator: degraded (a failure, not a hold at the cap)
    assert (m["aux_held"], m["aux_incomplete"], m["strategy_degraded"]) == (0, 1, True)


def test_a_reviewer_cell_without_a_read_only_turn_spends_nothing(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.missing.add("codex-cli")
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 1}, **AUX))
    assert record["status"] == "published" and len(w.agents.calls) == 1
    assert [(r["verdict"], r["error"]) for r in record["reviews"]] == [(None, "TURN_FAILED")]
    assert [e["tokens"] for e in record["aux_usage"]] == [0]
    assert metrics(record)["aux_turns"] == 0  # a turn that never started is not an agent call


def test_the_reviewers_change_still_has_to_pass_the_suite(deployment: Any, tmp_path: Path) -> None:
    def script(call: Call) -> dict[str, str]:  # the follow-up breaks what the generator fixed
        return {"app.py": WRONG} if call.resume else {"app.py": GOOD}

    w = make_world(deployment, tmp_path, script)
    w.turns.outputs["review"] = [CHANGES, APPROVE]
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    # the reviewer never decides: verification after the follow-up failed, the next attempt repairs
    assert outcomes(record)[0] == "fail" and record["status"] == "published"
    assert w.rig.published == [record["goal_id"]]


# ===========================================================================================
# 7. cascade (M1 + M6: a new contract revision pinned to the stronger cell, IC-05)
# ===========================================================================================
CASCADE = {"cells": ["codex-cli", "claude-cli"], "max_escalations": 1}


def make_cascade_world(
    deployment: Any, tmp_path: Path, *, claude: Script = right, script: Script | None = None
) -> World:
    """Two cells (the legacy codex-cli and claude-cli): ``script`` drives cell 1 (wrong unless a
    test says otherwise), ``claude`` drives cell 2 (``w.extra["claude"]`` is its ``Agents``)."""
    rig, loop, codex_box, claude_box, _planner = rig_with_two_drivers(deployment, tmp_path)
    w = finish_world(rig, loop, codex_box, script or always(WRONG))
    w.extra["claude"] = Agents(claude_box, claude, "claude")
    return w


def test_cascade_escalates_to_a_new_revision_on_the_next_cell_after_operator_approval(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    goal = w.approved(w.comps.strategy("cascade", CASCADE))
    first_contract = w.record(goal)["contract_ref"]
    record = w.loop.run_goal(goal)
    # three failed attempts on cell 1 end the revision; the next one waits for the operator
    assert len(w.agents.prompts) == 3 and w.extra["claude"].prompts == []
    assert record["status"] == "awaiting_approval" and w.rig.published == []
    assert record["escalation"]["from_cell"] == "codex-cli"
    assert record["escalation"]["to_cell"] == "claude-cli"
    assert record["escalations"] == [record["escalation"]]
    assert [a["outcome"] for a in record["previous_attempts"]] == ["fail"] * 3 and record[
        "attempts"
    ] == []
    # IC-05: a new contract revision (same draft and items), pinned to the stronger cell
    contract = w.rig.d.store.get(w.rig.d.scope, "goal-contract", record["contract_ref"])
    old = w.rig.d.store.get(w.rig.d.scope, "goal-contract", first_contract)
    assert (old["revision"], contract["revision"]) == (1, 2)
    assert contract["objective"] == old["objective"] and record["revision"] == 2
    assert (
        record["composition"]["cell_id"] == "claude-cli" and record["composition"]["pinned"] is True
    )
    assert record["strategy"]["roles"]["executor"] == "claude-cli"
    assert (
        record["strategy"]["strategy"] == "cascade"
        and record["draft"]["objective"] == old["objective"]
    )
    # nothing runs until a human approves the revision
    with pytest.raises(Hold) as unapproved:
        w.loop.run_goal(goal)
    assert unapproved.value.code == "PLAN_NOT_APPROVED"
    service_actor = Actor("svc", w.rig.d.scope, frozenset({"execution.approve"}), "service")
    with pytest.raises(RuntimeFault) as machine:
        w.service.approve(service_actor, goal)
    assert machine.value.code == "APPROVER_KIND"
    w.service.approve(w.rig.operator, goal)
    final = w.loop.run_goal(goal)
    assert final["status"] == "published" and w.rig.published == [goal]
    assert outcomes(final) == ["pass"] and len(w.extra["claude"].prompts) == 1
    assert len(w.agents.prompts) == 3  # cell 1 ran no more
    m = metrics(final)
    assert (m["strategy"], m["escalations"], m["attempts_used"], m["turns"]) == ("cascade", 1, 4, 4)
    assert m["cells_used"] == ["claude-cli", "codex-cli"]
    assert m["agent_calls"] == 5  # 4 attempts on both cells + the planner turn


def test_cascade_stops_after_max_escalations_when_the_stronger_cell_fails_too(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path, claude=always(WRONG))
    goal = w.approved(w.comps.strategy("cascade", CASCADE))
    assert w.loop.run_goal(goal)["status"] == "awaiting_approval"
    w.service.approve(w.rig.operator, goal)
    final = w.loop.run_goal(goal)
    assert final["status"] == "failed" and w.rig.published == []
    assert metrics(final)["escalations"] == 1 and metrics(final)["attempts_used"] == 6
    assert len(w.extra["claude"].prompts) == 3
    # a failed cascade does not escalate again: the limit is the cascade's max_escalations (1)
    with pytest.raises(Hold) as state:
        w.service.escalate(goal, to_cell="codex-cli", reason="again")
    assert state.value.code == "ESCALATION_STATE"
    w.service._save_plan(goal, {**w.record(goal), "status": "escalation_pending"})
    with pytest.raises(Hold) as limit:
        w.service.escalate(goal, to_cell="codex-cli", reason="again")
    assert limit.value.code == "ESCALATION_LIMIT"


def test_a_passing_first_cell_never_escalates(deployment: Any, tmp_path: Path) -> None:
    w = make_cascade_world(deployment, tmp_path, script=wrong_first)
    record = w.run(w.comps.strategy("cascade", CASCADE))
    assert record["status"] == "published" and outcomes(record) == ["fail", "pass"]
    m = metrics(record)
    assert (m["escalations"], m["cells_used"], m["attempts_used"]) == (0, ["codex-cli"], 2)
    assert w.extra["claude"].prompts == []


def test_escalate_holds_outside_a_cascade_failure_and_for_an_unknown_cell(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    goal = w.approved(w.comps.strategy("cascade", CASCADE))
    with pytest.raises(Hold) as running:  # approved, not failed by attempts
        w.service.escalate(goal, to_cell="claude-cli", reason="early")
    assert running.value.code == "ESCALATION_STATE" and running.value.details == "approved"
    other = w.plan(w.comps.strategy("single"))  # not a cascade, whatever its state
    w.service._save_plan(other, {**w.record(other), "status": "escalation_pending"})
    with pytest.raises(Hold) as plain:
        w.service.escalate(other, to_cell="claude-cli", reason="no cascade")
    assert plain.value.code == "ESCALATION_LIMIT" and plain.value.details["strategy"] == "single"
    pending = w.plan(w.comps.strategy("cascade", CASCADE))
    w.service._save_plan(pending, {**w.record(pending), "status": "escalation_pending"})
    with pytest.raises(Hold) as unknown:
        w.service.escalate(pending, to_cell="no-such-cell", reason="unknown")
    assert unknown.value.code == "CELL_UNKNOWN"


@pytest.mark.parametrize(
    ("params", "why"),
    [
        ({"cells": ["claude-cli", "codex-cli"]}, "cascade.cells[0] is not the goal's cell"),
        ({"cells": ["codex-cli", "ghost-cli"]}, "cascade cells not installed for the app: ghost"),
    ],
)
def test_a_cascade_that_does_not_start_on_the_goals_cell_or_names_no_cell_is_ineligible(
    deployment: Any, tmp_path: Path, params: dict[str, Any], why: str
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    plan = w.record(w.plan(w.comps.strategy("cascade", params)))
    assert plan["strategy"]["strategy"] == PRIOR and plan["strategy"]["used_prior"] is True
    assert why in plan["strategy"]["eligibility"]["cascade"]
    trial = w.record(w.plan(w.comps.strategy("cascade", params), trial=trial_context()))
    assert trial["strategy"]["refused"].startswith("cascade: ")


def test_a_cascade_needs_exactly_two_cells_and_one_escalation(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    for params in (
        {"cells": ["codex-cli"]},
        {"cells": ["codex-cli", "claude-cli", "opencode-server"]},
        {"max_escalations": 2},
    ):
        with pytest.raises(RuntimeFault) as bad:
            w.comps.strategy("cascade", params)
        assert bad.value.code == "COMPONENT_CONTENT"


def test_a_trial_follows_the_escalated_revision_under_the_experiment_operator(
    deployment: Any, tmp_path: Path
) -> None:
    """M6 inside one trial: the trial goal never publishes, on any cell."""
    w = make_cascade_world(deployment, tmp_path)
    goal = w.approved(w.comps.strategy("cascade", CASCADE), trial=trial_context())
    record = w.loop.run_goal(goal)
    assert (
        record["status"] == "awaiting_approval" and record["escalation"]["to_cell"] == "claude-cli"
    )
    assert record["trial"]["write_scope"] == "trial"  # the escalated revision keeps the trial scope
    node = w.nodes(goal)[0]
    assert node["resource_claims"] == [
        {"resource": f"sandbox:app:trial:{goal}", "mode": "exclusive_write"}
    ]
    w.service.approve(w.rig.operator, goal)  # the experiment's operator identity
    final = w.loop.run_goal(goal)
    assert final["status"] == "verified" and w.rig.published == []  # verified, never published
    assert metrics(final)["escalations"] == 1


def s8_helpers() -> Any:
    """The S8 executor test module (corpus writer, constants), loaded by path: it is a sibling
    suite in ``tests/v3``, which is not on this suite's import path."""
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[1] / "v3" / "test_033_s8_executor.py"
    name = "s9_loaded_s8_executor"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def trial_executor(w: World, tmp_path: Path) -> Any:
    from amplai_foundry.meta_harness.local_executor import LocalTrialExecutor
    from amplai_foundry.runtime.execution.loop import ExecutionLoop
    from rc06_rig import git

    s8 = s8_helpers()
    s8._write(w.rig.repo, {"tests/test_visible.py": s8.VISIBLE})
    git(w.rig.repo, "add", "-A")
    git(w.rig.repo, "commit", "-q", "-m", "visible test")
    base_commit = git(w.rig.repo, "rev-parse", "HEAD").strip()
    corpus = s8.write_corpus(tmp_path / "corpus", base_commit)
    loop = ExecutionLoop(
        w.service, w.loop.coordinator, publisher=None
    )  # a trial loop: no publisher
    return LocalTrialExecutor(
        w.service, loop, w.rig.d.goals, w.rig.operator, corpus, behaviour_verifier=VERIFIER
    )


def test_the_trial_executor_follows_one_escalated_revision_and_grades_the_final_change(
    deployment: Any, tmp_path: Path
) -> None:
    """M6 in ``LocalTrialExecutor``: cell 1 fails its three attempts, the executor approves the
    escalated revision under the experiment's operator identity and grades what cell 2 verified.
    The trial goal ends verified and is never published."""
    w = make_cascade_world(deployment, tmp_path)
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("cascade", CASCADE)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    assert observation.success is True and w.rig.published == []
    assert (observation.safety_failures, observation.unknown_effects) == (0, 0)
    d = w.rig.d
    receipt = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))
    proof = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[1], trusted=True))
    assert receipt["goal_status"] == "verified" and receipt["strategy"] == "cascade"
    assert receipt["hidden_passed"] is True
    # the receipt keeps the trial's cell (the arm's first cell); the proof lists every attempt of
    # both revisions, and the usage is the sum over the runs of both cells (4 runs of 10 + 5)
    assert receipt["cell_id"] == "codex-cli"
    assert [a["outcome"] for a in proof["attempts"]] == ["fail", "fail", "fail", "pass"]
    assert (observation.input_tokens, observation.output_tokens) == (40, 20)
    assert len(w.agents.prompts) == 3 and len(w.extra["claude"].prompts) == 1
    goal = receipt["goal_id"]
    plan = w.service.plan_record(goal)
    assert plan["status"] == "verified"
    assert plan["escalations"][0]["to_cell"] == "claude-cli"
    # the graded revision ran on the cell sibling of the arm composition: the receipt names it
    # as executed only together with the chain of every revision's cell and composition
    sibling = plan["composition"]["ref"]
    assert sibling != composition and plan["composition"]["cell_id"] == "claude-cli"
    assert receipt["executed_composition_ref"] == sibling
    assert receipt["composition_ref"] == composition  # the bound arm stays the arm
    assert receipt["escalation_chain"] == [
        {"revision": 1, "contract_ref": plan["escalations"][0]["previous_contract_ref"],
         "cell_id": "codex-cli", "composition_ref": composition},
        {"revision": 2, "contract_ref": plan["contract_ref"], "cell_id": "claude-cli",
         "composition_ref": sibling},
    ]  # fmt: skip


def test_a_trial_that_never_escalates_carries_no_chain_and_ran_its_arm(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path, script=right)
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("cascade", CASCADE)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    d = w.rig.d
    receipt = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))
    assert receipt["escalation_chain"] == []
    assert receipt["executed_composition_ref"] == receipt["composition_ref"] == composition


def test_a_trial_whose_first_cell_passes_never_escalates(deployment: Any, tmp_path: Path) -> None:
    w = make_cascade_world(deployment, tmp_path, script=right)
    executor = trial_executor(w, tmp_path)
    observation = executor(
        w.comps.strategy("cascade", CASCADE), {"case_id": "bug-01-value", "split": "development"},
        0, "sandbox_rerun",
    )  # fmt: skip
    assert observation.success is True and w.extra["claude"].prompts == []
    assert (observation.input_tokens, observation.output_tokens) == (10, 5)


def test_a_trial_that_fails_on_both_cells_is_graded_failed_after_one_escalation(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path, claude=always(WRONG))
    executor = trial_executor(w, tmp_path)
    observation = executor(
        w.comps.strategy("cascade", CASCADE), {"case_id": "bug-01-value", "split": "development"},
        0, "sandbox_rerun",
    )  # fmt: skip
    assert observation.success is False and w.rig.published == []
    assert len(w.agents.prompts) == 3 and len(w.extra["claude"].prompts) == 3  # one escalation only
    assert (observation.input_tokens, observation.output_tokens) == (60, 30)


def run_reviewed_trial(w: World, tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One generator_reviewer trial (one reviewer round, cap above 0) through the executor."""
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("generator_reviewer", {"max_rounds": 1}, **AUX)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    d = w.rig.d
    receipt = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))
    return observation, receipt


def test_the_trial_usage_adds_the_auxiliary_read_only_turns(
    deployment: Any, tmp_path: Path
) -> None:
    """§8.3: a trial's usage is runs + planner + auxiliary read-only turns (§5.3, ``aux_usage``):
    the EvaluationService settles the proposal root with it and IC-08 reads it as efficiency."""
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = APPROVE
    observation, receipt = run_reviewed_trial(w, tmp_path)
    assert observation.success is True and w.rig.published == []
    assert receipt["strategy"] == "generator_reviewer" and len(w.turns.of("review")) == 1
    # one executor run (10 + 5) and one reviewer turn (50 + 50)
    assert (observation.input_tokens, observation.output_tokens) == (60, 55)
    assert (receipt["input_tokens"], receipt["output_tokens"]) == (60, 55)
    assert receipt["usage_status"] == observation.usage_status == "measured"


def test_an_auxiliary_turn_with_unknown_usage_makes_the_trial_usage_unknown(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = APPROVE
    w.turns.usage = None  # the reviewer's provider reported no usage
    observation, receipt = run_reviewed_trial(w, tmp_path)
    assert observation.success is True and len(w.turns.of("review")) == 1
    assert observation.usage_status == "unknown" == receipt["usage_status"]
    assert (observation.input_tokens, observation.output_tokens) == (None, None)
    assert observation.cost_microunits is None


def test_an_auxiliary_turn_that_never_started_adds_nothing_to_the_trial_usage(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.missing.add("codex-cli")  # no read-only turn for the reviewer cell: nothing ran
    observation, receipt = run_reviewed_trial(w, tmp_path)
    assert observation.success is True and w.turns.calls == []
    assert (observation.input_tokens, observation.output_tokens) == (10, 5)  # the run alone
    assert receipt["usage_status"] == "measured"


def metrics_trial(w: World, composition: Any, observation: Any, **over: Any) -> dict[str, Any]:
    """The trial record ``EvaluationService`` stores for an observation (the fields the metrics
    read)."""
    value: dict[str, Any] = {
        "trial_id": "trial-s9-metrics", "scope": w.rig.d.scope.wire(), "experiment_ref": None,
        "composition_ref": composition, "task_id": "bug-01-value", "task_class": "bug",
        "arm": "candidate", "repeat": 0, "mode": "sandbox_rerun", "success": observation.success,
        "safety_failures": 0, "unknown_effects": 0,
        "artifact_refs": list(observation.artifact_refs),
        "input_tokens": observation.input_tokens, "output_tokens": observation.output_tokens,
        "usage_status": observation.usage_status, "elapsed_ms": 2500.0,
    }  # fmt: skip
    value.update(over)
    return value


def test_the_trial_metrics_of_an_escalated_trial_count_both_revisions(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.meta_harness.trial_metrics import TrialMetrics

    w = make_cascade_world(deployment, tmp_path)
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("cascade", CASCADE)
    case = {"case_id": "bug-01-value", "split": "development"}
    observation = executor(composition, case, 0, "sandbox_rerun")
    row = TrialMetrics(w.service).trial(metrics_trial(w, composition, observation))
    assert row["strategy"] == "cascade" and row["agent_calls"] == 4  # four processes started
    assert (row["escalations"], row["attempts_used"]) == (1, 4)
    assert row["cells_used"] == ["claude-cli", "codex-cli"]
    # executor turns AMPLAI dispatched over both revisions; provider turns are not recorded
    assert row["turns"] == 4 and row["provider_turns"] is None
    for zero in ("reviewer_rounds", "fix_requests", "sub_agents", "integration_conflicts",
                 "re_verifications"):  # fmt: skip
        assert row[zero] == 0, zero
    assert row["best_of_n_first_pass"] is None
    # the earlier revision ran on another cell: never priced at the last revision's model
    assert row["api_cost"]["status"] == "revisions_unpriced"
    plan = w.service.plan_record(json.loads(
        w.rig.d.artifacts.read(w.rig.d.scope, observation.artifact_refs[0], trusted=True)
    )["goal_id"])  # fmt: skip
    assert plan["strategy_metrics"]["escalations"] == 1  # what the row was read from


def test_the_trial_metrics_read_what_the_reviewer_strategy_did(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.meta_harness.trial_metrics import TrialMetrics

    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("generator_reviewer", {"max_rounds": 1}, **AUX)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    assert observation.success is True
    row = TrialMetrics(w.service).trial(metrics_trial(w, composition, observation))
    assert (row["strategy"], row["reviewer_rounds"], row["fix_requests"]) == (
        "generator_reviewer", 1, 2,
    )  # fmt: skip
    assert row["escalations"] == 0 and row["attempts_used"] == 1
    # one executor turn and its follow-up are the executor turns; the reviewer is an agent call
    assert row["turns"] == 2 and row["provider_turns"] is None
    assert row["agent_calls"] == 3  # the run's process, its follow-up, the reviewer turn


def test_a_stale_strategy_record_is_recomputed_from_the_plan(
    deployment: Any, tmp_path: Path
) -> None:
    """``strategy_metrics`` written before a later revision (or a revision closed without the
    loop finishing it) is not used: the row counts what the plan record holds now."""
    from amplai_foundry.meta_harness.trial_metrics import TrialMetrics

    w = make_cascade_world(deployment, tmp_path)
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("cascade", CASCADE)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    d = w.rig.d
    goal = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))[
        "goal_id"
    ]
    plan = w.service.plan_record(goal)
    stale = {**plan["strategy_metrics"], "escalations": 0, "attempts_used": 3, "turns": 3}
    w.service._save_plan(goal, {**plan, "strategy_metrics": stale})
    row = TrialMetrics(w.service).trial(metrics_trial(w, composition, observation))
    assert (row["escalations"], row["attempts_used"], row["turns"]) == (1, 4, 4)
    # a plan without the record at all (from before S9) is computed the same way
    w.service._save_plan(goal, {k: v for k, v in plan.items() if k != "strategy_metrics"})
    again = TrialMetrics(w.service).trial(metrics_trial(w, composition, observation))
    assert (again["escalations"], again["attempts_used"], again["turns"]) == (1, 4, 4)


# ===========================================================================================
# 10. vote (M4): held until §14 Q16 (b) is answered
# ===========================================================================================
def test_vote_is_held_before_any_claim_and_never_replaced_by_the_prior(
    deployment: Any, tmp_path: Path
) -> None:
    assert set(HELD) == {"vote"}
    w = make_world(deployment, tmp_path)
    for composition in (
        w.comps.strategy("vote", {"k": 2}),
        # the first enabled strategy is the declared one: held, not skipped for a later prior
        w.comps.candidate(
            execution_strategy={"enabled": ["vote", "repair_loop"], "params": {"vote": {"k": 3}}}
        ),
    ):
        goal = w.approved(composition)
        plan = w.record(goal)
        assert plan["strategy"]["declared"] == "vote" and plan["strategy"]["used_prior"] is False
        assert plan["strategy"]["refused"].startswith("vote: vote (M4 candidates held")
        with pytest.raises(RuntimeFault) as refused:
            w.loop._policies(plan)
        assert refused.value.code == "COMPONENT_CONTENT" and len(refused.value.details) == 1
        record = w.loop.run_goal(goal)
        assert record["status"] == "held" and record["attempts"] == []
        assert "COMPONENT_CONTENT" in record["reason"] and "vote" in record["reason"]
        assert w.rig.d.store.head(w.rig.d.scope, "goal", goal)["state"] == "failed"
    assert w.agents.prompts == [] and w.turns.calls == [] and w.rig.published == []
    # a trial of vote is refused the same way
    trial = w.approved(w.comps.strategy("vote", {"k": 2}), trial=trial_context())
    assert w.record(trial)["strategy"]["refused"].startswith("vote: ")
    assert w.loop.run_goal(trial)["status"] == "held" and w.agents.prompts == []


def test_a_later_enabled_vote_does_not_hold_the_declared_strategy(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    composition = w.comps.candidate(
        execution_strategy={"enabled": ["single", "vote"], "params": {"vote": {"k": 2}}}
    )
    record = w.run(composition)
    assert record["status"] == "published" and record["strategy"]["strategy"] == "single"


# ===========================================================================================
# IC-21 AUX_BUDGET: auxiliary turns stop at limits.aux_max_tokens
# ===========================================================================================
def capped(cap: int) -> dict[str, Any]:
    return {"limits": {**policies.V1["limits"], "aux_max_tokens": cap}}


def test_the_reviewer_stops_at_the_cap_and_the_last_turn_may_pass_it(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES
    w.turns.usage = (60, 60)  # 120 tokens per reviewer turn against a cap of 100
    strategy = w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **capped(100))
    record = w.run(strategy)
    assert record["status"] == "published"
    # round 1 ran (0 < 100) and passed the cap by its own usage; round 2 never started
    assert len(w.turns.of("review")) == 1 and len(w.agents.calls) == 2  # first + f1
    assert [(r["verdict"], r["error"]) for r in record["reviews"]] == [
        ("request_changes", None), (None, "AUX_BUDGET"),
    ]  # fmt: skip
    assert record["aux_overrun"] is True and metrics(record)["aux_overrun"] is True
    assert metrics(record)["aux_tokens"] == 120
    assert record["strategy"]["aux_cap"] == 100
    # the second round the strategy asked for never ran: the goal is marked degraded
    m = metrics(record)
    assert (m["strategy"], m["aux_held"], m["aux_incomplete"]) == ("generator_reviewer", 1, 1)
    assert m["strategy_degraded"] is True


def test_a_turn_with_unknown_usage_stops_every_later_auxiliary_turn(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES
    w.turns.usage = None  # the provider reported no usage
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    assert len(w.turns.of("review")) == 1
    assert [r["error"] for r in record["reviews"]] == [None, "AUX_BUDGET"]
    assert [e["tokens"] for e in record["aux_usage"]] == [None]  # the refused turn left no entry
    assert (metrics(record)["aux_held"], metrics(record)["strategy_degraded"]) == (1, True)


def test_a_strategy_whose_auxiliary_turns_all_completed_is_not_degraded(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = [CHANGES, APPROVE]
    record = w.run(w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **AUX))
    m = metrics(record)
    assert (m["aux_turns"], m["aux_held"], m["aux_incomplete"]) == (2, 0, 0)
    assert m["strategy_degraded"] is False


@pytest.mark.parametrize(
    ("strategy", "params"),
    [
        ("generator_reviewer", {"max_rounds": 1}),
        ("orchestrator", {"max_parts": 3}),
        ("parallel_readonly", {"steps": ["files_to_change"]}),
    ],
)
def test_with_a_cap_of_zero_an_auxiliary_strategy_is_ineligible_for_a_real_goal(
    deployment: Any, tmp_path: Path, strategy: str, params: dict[str, Any]
) -> None:
    assert strategy in AUX_STRATEGIES
    w = make_world(deployment, tmp_path)
    plan = w.record(w.plan(w.comps.strategy(strategy, params)))  # aux_max_tokens stays 0 (v1)
    assert plan["strategy"]["aux_cap"] == 0 and plan["strategy"]["used_prior"] is True
    assert plan["strategy"]["strategy"] == PRIOR and plan["strategy"]["declared"] == strategy
    assert "IC-21" in plan["strategy"]["eligibility"][strategy]
    record = w.loop.run_goal(w.approved(w.comps.strategy(strategy, params)))
    assert record["status"] == "published" and len(w.agents.prompts) == 1
    assert w.turns.calls == [] and metrics(record)["aux_turns"] == 0  # no auxiliary turn started


@pytest.mark.parametrize("strategy", ["workgraph_split", "plan_execute"])
def test_a_planner_that_cannot_draft_the_variant_needs_an_auxiliary_turn_so_cap_zero_is_ineligible(
    deployment: Any, tmp_path: Path, strategy: str
) -> None:
    w = make_world(deployment, tmp_path)
    params = {"max_nodes": 2} if strategy == "workgraph_split" else {"planner_role": "planner"}
    plan = w.record(w.plan(w.comps.strategy(strategy, params)))
    assert (
        plan["strategy"]["used_prior"] is True
        and "IC-21" in plan["strategy"]["eligibility"][strategy]
    )
    # a variant planner needs no extra turn, so the same cap 0 is fine
    ok = w.record(
        w.plan(w.comps.strategy(strategy, params), planner=VariantPlanner(parts=PARTS, steps=STEPS))
    )
    assert ok["strategy"]["used_prior"] is False and ok["strategy"]["strategy"] == strategy


@pytest.mark.parametrize(
    ("strategy", "params"),
    [
        ("generator_reviewer", {"max_rounds": 1}),  # the reviewer could never start
        ("orchestrator", {"max_parts": 3}),  # the lead turn could never start
        ("parallel_readonly", {"steps": ["files_to_change"]}),  # no investigator could start
        ("workgraph_split", {"max_nodes": 2}),  # a fixed planner needs one more planner turn
        ("plan_execute", {"planner_role": "planner"}),  # likewise, for the steps
    ],
)
def test_a_trial_with_a_cap_of_zero_refuses_an_auxiliary_strategy_before_any_claim(
    deployment: Any, tmp_path: Path, strategy: str, params: dict[str, Any]
) -> None:
    """IC-21 at cap 0: no auxiliary turn can start (§5.3, trials alike), so the trial cannot run
    its strategy. It is refused before any claim instead of running the plain repair loop under
    the strategy's name (a trial is never labelled with a strategy it did not run)."""
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs.update(
        review=CHANGES, lead=LEAD, investigate=FINDINGS, split={"parts": PARTS},
        steps={"steps": STEPS},
    )  # fmt: skip
    goal = w.approved(w.comps.strategy(strategy, params), trial=trial_context())  # cap 0 (v1)
    plan = w.record(goal)
    assert plan["strategy"]["aux_cap"] == 0 and plan["strategy"]["used_prior"] is False
    # recorded as what it is: an AUX_BUDGET ineligibility, not a strategy that ran
    assert plan["strategy"]["refused"] == (
        f"{strategy}: AUX_BUDGET: auxiliary turns with limits.aux_max_tokens 0 (IC-21)"
    )
    assert plan["strategy"]["eligibility"][strategy].startswith("AUX_BUDGET: ")
    assert plan["aux_usage"] == [] and w.turns.calls == []  # not even a plan-time turn started
    record = w.loop.run_goal(goal)
    assert record["status"] == "held" and "COMPONENT_CONTENT" in record["reason"]
    assert record["attempts"] == [] and w.agents.calls == [] and w.rig.published == []


def test_a_trial_executor_run_of_an_auxiliary_strategy_at_cap_zero_is_held_unknown(
    deployment: Any, tmp_path: Path
) -> None:
    """Through ``LocalTrialExecutor``: the trial is held before any claim, its observation is
    unknown (never a pass or a failure of the arm), its receipt says why, and nothing ran."""
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = APPROVE
    executor = trial_executor(w, tmp_path)
    composition = w.comps.strategy("generator_reviewer", {"max_rounds": 1})  # cap 0 (v1)
    observation = executor(composition, {"case_id": "bug-01-value", "split": "development"}, 0,
                           "sandbox_rerun")  # fmt: skip
    assert observation.success is None and w.rig.published == []
    assert w.agents.calls == [] and w.turns.calls == []
    d = w.rig.d
    receipt = json.loads(d.artifacts.read(d.scope, observation.artifact_refs[0], trusted=True))
    assert receipt["goal_status"] == "held" and "AUX_BUDGET" in receipt["goal_reason"]
    plan = w.service.plan_record(receipt["goal_id"])
    assert plan["strategy"]["refused"].startswith("generator_reviewer: AUX_BUDGET: ")
    assert plan["attempts"] == []


def test_a_cap_above_zero_makes_the_auxiliary_strategy_eligible_for_a_trial(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs.update(lead=LEAD, investigate=FINDINGS)  # the lead turn runs at plan time
    for strategy, params in (
        ("generator_reviewer", {"max_rounds": 1}),
        ("orchestrator", {"max_parts": 3}),
        ("parallel_readonly", {"steps": ["files_to_change"]}),
    ):
        plan = w.record(w.plan(w.comps.strategy(strategy, params, **capped(1)),
                               trial=trial_context()))  # fmt: skip
        assert plan["strategy"]["refused"] is None, strategy
        assert plan["strategy"]["eligibility"][strategy] is None
        assert plan["strategy"]["strategy"] == strategy and plan["strategy"]["aux_cap"] == 1


def test_a_trial_variant_planner_needs_no_auxiliary_turn_so_cap_zero_runs_the_strategy(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, chain_script)
    planner = VariantPlanner(parts=PARTS)
    goal = w.approved(
        w.comps.strategy("workgraph_split", {"max_nodes": 2}), planner=planner,
        trial=trial_context(),
    )  # fmt: skip
    plan = w.record(goal)
    assert plan["strategy"]["refused"] is None and plan["strategy"]["variant"] == "parts"
    record = w.loop.run_goal(goal)
    assert record["status"] == "verified" and w.rig.published == []  # a trial never publishes
    assert w.turns.calls == [] and metrics(record)["strategy"] == "workgraph_split"
    assert metrics(record)["nodes"] == 2 and metrics(record)["strategy_degraded"] is False


def test_a_trial_held_at_the_cap_after_an_earlier_turn_is_marked_degraded(
    deployment: Any, tmp_path: Path
) -> None:
    """A cap above 0 lets the first auxiliary turn start; a later one held at the cap leaves the
    trial running with less than its strategy, so its metrics say so (analysis filters it)."""
    w = make_world(deployment, tmp_path, reviewed)
    w.turns.outputs["review"] = CHANGES
    w.turns.usage = (60, 60)
    goal = w.approved(
        w.comps.strategy("generator_reviewer", {"max_rounds": 2}, **capped(100)),
        trial=trial_context(),
    )  # fmt: skip
    assert w.record(goal)["strategy"]["refused"] is None
    record = w.loop.run_goal(goal)
    assert record["status"] == "verified" and w.rig.published == []
    assert [(r["verdict"], r["error"]) for r in record["reviews"]] == [
        ("request_changes", None), (None, "AUX_BUDGET"),
    ]  # fmt: skip
    m = metrics(record)
    assert (m["strategy"], m["reviewer_rounds"], m["aux_held"]) == ("generator_reviewer", 1, 1)
    assert m["strategy_degraded"] is True


# ===========================================================================================
# 8. orchestrator (M7 lead turn + M5 parts from one base + the host merge + integration node)
# ===========================================================================================
PART_A = part("Part A: value() returns 2", ["app.py"])
PART_B = part("Part B: add the docs note", ["docs/"])
LEAD = {"parts": [PART_A, PART_B], "integration_notes": "A owns app.py and B owns docs/"}
NOTE_B = "notes\n"


def orchestrated(call: Call) -> dict[str, str]:
    if "in app: Part A" in call.prompt:
        return {"app.py": GOOD}
    if "in app: Part B" in call.prompt:
        return {"docs/NOTES.md": NOTE_B}
    if "in app: Integrate" in call.prompt:
        return {}  # the integration node: the merged tree already holds both parts
    return {"app.py": GOOD}  # a plain goal


ORCH = {"max_parts": 3}


def test_orchestrator_runs_parts_from_one_base_then_verifies_the_merged_change(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, orchestrated)
    w.turns.outputs["lead"] = LEAD
    goal = w.approved(w.comps.strategy("orchestrator", ORCH, **AUX))
    plan = w.record(goal)
    p1, p2, integration = w.nodes(goal)
    assert [n["node_id"] for n in (p1, p2, integration)] == [
        "node-app.p1", "node-app.p2", "node-app.int",
    ]  # fmt: skip
    assert p1["depends_on"] == [] and p2["depends_on"] == []  # the parts share one base
    assert integration["depends_on"] == ["node-app.p1", "node-app.p2"]
    assert len(integration["acceptance_ids"]) == 1  # the goal acceptance, on the merged change
    (lead,) = w.turns.of("lead")
    assert lead.cell_id == "codex-cli" and "Delegate the goal to 2..3 parts" in lead.prompt
    assert "Integration notes: A owns app.py and B owns docs/" in plan["work_items"][2]["objective"]
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and outcomes(record) == ["pass", "pass", "pass"]
    calls = w.agents.calls
    # part B starts on the same base as part A, not on A's change (parts are independent)
    assert calls[0].app_text == BASE_TEXT and calls[1].app_text == BASE_TEXT
    # the integration node starts on the host merge of both parts, and says what was merged
    assert calls[2].app_text == GOOD and calls[2].files["docs/NOTES.md"] == NOTE_B
    assert "Merged parts (applied in this directory, each verified on its own):" in calls[2].prompt
    assert "- Part A: value() returns 2\n- Part B: add the docs note" in calls[2].prompt
    assert "could not be merged" not in calls[2].prompt
    merged = record["integration"]["node-app.int"]
    assert merged["applied"] == ["node-app.p1", "node-app.p2"] and merged["conflicts"] == []
    assert change_files(w, record) == ["app.py", "docs/NOTES.md"]
    last = terminal_nodes(
        w.rig.d.store.get(w.rig.d.scope, "workgraph", record["graph_ref"]), record["node_apps"]
    )
    assert last["app"]["node_id"] == "node-app.int"  # the publisher's change is the merged one
    m = metrics(record)
    assert (m["nodes"], m["sub_agents"], m["integration_conflicts"], m["re_verifications"]) == (
        3, 2, 0, 1,
    )  # fmt: skip
    assert (m["attempts_used"], m["aux_turns"], m["agent_calls"]) == (3, 1, 5)  # + lead + planner


def test_orchestrator_hands_a_conflicting_part_to_the_integration_prompt(
    deployment: Any, tmp_path: Path
) -> None:
    def script(call: Call) -> dict[str, str]:
        if "in app: Part A" in call.prompt:
            return {"app.py": GOOD}
        if "in app: Part B" in call.prompt:  # both parts change the same line differently
            return {"app.py": "def value():\n    return 2  # part B\n"}
        return {"app.py": GOOD}  # the integrator resolves it by hand

    w = make_world(deployment, tmp_path, script)
    w.turns.outputs["lead"] = LEAD
    record = w.run(w.comps.strategy("orchestrator", ORCH, **AUX))
    assert record["status"] == "published" and outcomes(record) == ["pass"] * 3
    merged = record["integration"]["node-app.int"]
    assert merged["applied"] == ["node-app.p1"]
    assert [(c["node_id"], c["paths"]) for c in merged["conflicts"]] == [
        ("node-app.p2", ["app.py"])
    ]
    prompt = w.agents.calls[2].prompt
    assert "The part node-app.p2 could not be merged automatically" in prompt
    assert "conflicting paths: app.py" in prompt and "part B" in prompt  # its patch text
    assert w.agents.calls[2].app_text == GOOD  # the base is the clean parts' merge
    assert metrics(record)["integration_conflicts"] == 1


def test_the_integration_node_is_verified_again_and_repaired_like_any_node(
    deployment: Any, tmp_path: Path
) -> None:
    seen = {"int": 0}

    def script(call: Call) -> dict[str, str]:
        if "in app: Part A" in call.prompt or "in app: Part B" in call.prompt:
            return orchestrated(call)
        seen["int"] += 1
        return {"app.py": WRONG} if seen["int"] == 1 else {"app.py": GOOD}

    w = make_world(deployment, tmp_path, script)
    w.turns.outputs["lead"] = LEAD
    record = w.run(w.comps.strategy("orchestrator", ORCH, **AUX))
    assert [a["node_id"] for a in record["attempts"]][-2:] == ["node-app.int"] * 2
    assert outcomes(record) == ["pass", "pass", "fail", "pass"] and record["status"] == "published"
    assert "did not pass" in w.agents.calls[3].prompt  # the feedback form of the repair loop
    m = metrics(record)
    assert m["re_verifications"] == 2 and m["attempts_used"] == 4  # the suite ran twice on it


def test_a_failed_part_ends_the_goal_before_the_integration_node(
    deployment: Any, tmp_path: Path
) -> None:
    def script(call: Call) -> dict[str, str]:
        return {"app.py": WRONG} if "in app: Part A" in call.prompt else orchestrated(call)

    w = make_world(deployment, tmp_path, script)
    w.turns.outputs["lead"] = LEAD
    record = w.run(w.comps.strategy("orchestrator", ORCH, **AUX))
    assert record["status"] == "failed" and w.rig.published == []
    assert [a["node_id"] for a in record["attempts"]] == ["node-app.p1"] * 3
    assert record.get("integration") is None  # nothing was merged
    assert metrics(record)["integration_conflicts"] == 0


@pytest.mark.parametrize(
    ("parts", "why"),
    [
        ([PART_A, part("B", ["app.py"])], "disjoint in_scope"),
        ([part("A", ["docs"]), part("B", ["docs/guide"])], "disjoint in_scope"),
        ([part("A", []), part("B", ["docs/"])], "disjoint in_scope"),  # empty = the whole repo
        ([PART_A], "A split needs 2..3 parts"),
        ([PART_A, PART_B, part("C", ["c/"]), part("D", ["d/"])], "A split needs 2..3 parts"),
    ],
)
def test_a_lead_that_breaks_the_part_rules_is_turn_output(
    deployment: Any, tmp_path: Path, parts: list[dict[str, Any]], why: str
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["lead"] = {"parts": parts, "integration_notes": ""}
    strategy = w.comps.strategy("orchestrator", ORCH, **AUX)
    plan = w.record(w.plan(strategy))
    assert plan["strategy"]["strategy"] == PRIOR and plan["strategy"]["used_prior"] is True
    assert plan["strategy"]["eligibility"]["plan"].startswith("orchestrator: TURN_OUTPUT: ")
    assert why in plan["strategy"]["eligibility"]["plan"]
    trial = w.record(w.plan(strategy, trial=trial_context()))
    assert trial["strategy"]["refused"].startswith("orchestrator: TURN_OUTPUT")


def test_nothing_runs_in_parallel_on_one_repo_while_the_parts_run(
    deployment: Any, tmp_path: Path
) -> None:
    """Write concurrency 1 per repo (design 07 §4): while one node runs, no other node of the
    goal and no other goal can claim ``sandbox:app``; the integration node runs after the parts."""
    w = make_world(deployment, tmp_path, orchestrated)
    w.turns.outputs["lead"] = LEAD
    goal = w.approved(w.comps.strategy("orchestrator", ORCH, **AUX))
    other = w.approved(w.comps.strategy("single"), "another goal on the same repository")
    worker, probes = w.rig.actors.worker, []

    def probe(call: Call) -> None:
        for target in (goal, other):  # claimed from inside the running turn, the claim is held
            try:
                got = w.service.runtime.claim(worker, goal_id=target)
            except Hold as exc:
                got = exc.code
            probes.append((call.index, target == goal, got))

    w.agents.on_call = probe
    record = w.loop.run_goal(goal)
    assert record["status"] == "published" and len(probes) == 6
    assert all(got is None for _, _, got in probes), probes
    w.agents.on_call = None
    again = w.loop.run_goal(other)  # the repo is free once the orchestrated goal finished
    assert again["status"] == "published"
    claims = [n["resource_claims"] for n in w.nodes(goal)]
    assert claims == [[{"resource": "sandbox:app", "mode": "exclusive_write"}]] * 3


# ===========================================================================================
# 9. parallel_readonly (M2: up to four investigators at once before attempt 1)
# ===========================================================================================
FINDINGS = {"findings": [{"path": "app.py", "line": 2, "note": "returns a constant"}]}
ALL_QUESTIONS = ["files_to_change", "tests_to_run", "conventions"]


def test_parallel_readonly_investigates_in_parallel_before_the_first_claim(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, wrong_first)
    w.turns.outputs["investigate"] = FINDINGS
    w.turns.barrier = threading.Barrier(3)  # only passes when the three turns run at once
    strategy = w.comps.strategy("parallel_readonly", {"steps": ALL_QUESTIONS}, **AUX)
    record = w.run(strategy)
    assert record["status"] == "published" and outcomes(record) == ["fail", "pass"]
    assert w.turns.max_active == 3 <= 4
    # the investigators finished before any executor process started (no lease waited on them)
    assert w.turns.order[:4] == [("turn", "investigate")] * 3 + [("executor", "first")]
    asked = [c.prompt.split("Question: ")[1].split("\n")[0] for c in w.turns.of("investigate")]
    assert sorted(asked) == sorted(
        [
            "Which conventions of this repository must the change follow (naming, structure, "
            "error handling, tests)?",
            "Which existing tests and commands exercise the behaviour this goal changes?",
            "Which files must change to reach the objective, and where exactly?",
        ]
    )
    assert all(c.cell_id == "codex-cli" and c.existed for c in w.turns.of("investigate"))
    notes = record["investigations"]["node-app"]
    assert {n["question"] for n in notes} == set(ALL_QUESTIONS) and len(notes) == 3
    section = (
        "Investigation notes (read-only investigators; verify before relying on them):\n"
        "- app.py:2: returns a constant"
    )
    assert all(section in p for p in w.agents.prompts)  # on the repair attempt too
    m = metrics(record)
    assert (m["aux_turns"], m["aux_tokens"], m["attempts_used"]) == (3, 300, 2)
    assert m["agent_calls"] == 6  # 2 attempts + 3 investigators + the planner turn


def test_the_investigator_questions_follow_the_declared_steps(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["investigate"] = FINDINGS
    record = w.run(w.comps.strategy("parallel_readonly", {"steps": ["tests_to_run"]}, **AUX))
    assert record["status"] == "published" and len(w.turns.of("investigate")) == 1
    assert {n["question"] for n in record["investigations"]["node-app"]} == {"tests_to_run"}
    # no steps = the three questions
    every = w.run(w.comps.strategy("parallel_readonly", {"steps": []}, **AUX))
    assert len(every["investigations"]["node-app"]) == 3


def test_investigator_output_is_bounded_and_empty_paths_are_dropped(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    many = [{"path": f"f{i}.py", "line": None, "note": "n" * 500} for i in range(25)]
    w.turns.outputs["investigate"] = {"findings": [{"path": " ", "line": 1, "note": "x"}, *many]}
    record = w.run(w.comps.strategy("parallel_readonly", {"steps": ["files_to_change"]}, **AUX))
    notes = record["investigations"]["node-app"]
    # the first 20 findings are read (the blank-path one is among them and dropped)
    assert len(notes) == 19 and all(len(n["note"]) == 300 for n in notes)
    assert all(n["path"].strip() for n in notes)


def test_a_failed_investigator_is_recorded_and_the_goal_runs_without_its_notes(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["investigate"] = FINDINGS
    w.turns.fail["investigate"] = Hold("TURN_TIMEOUT", "Read-only turn exceeded its time budget")
    record = w.run(w.comps.strategy("parallel_readonly", {"steps": ["files_to_change"]}, **AUX))
    assert record["status"] == "published"
    assert [(s["question"], s["code"]) for s in record["aux_stops"]] == [
        ("files_to_change", "TURN_TIMEOUT")
    ]
    assert record["investigations"] == {} and "Investigation notes" not in w.agents.prompts[0]
    assert [e["tokens"] for e in record["aux_usage"]] == [None]  # unknown usage
    # three investigators at once: the first failure leaves unknown usage, so a turn that has not
    # started yet holds AUX_BUDGET instead (which of them is a race; the set of codes is not)
    again = w.run(w.comps.strategy("parallel_readonly", {"steps": ALL_QUESTIONS}, **AUX))
    codes = [s["code"] for s in again["aux_stops"]]
    assert len(codes) == 3 and set(codes) <= {"TURN_TIMEOUT", "AUX_BUDGET"}
    assert "TURN_TIMEOUT" in codes and again["status"] == "published"
    # both goals ran without what their investigators were asked: degraded
    assert (metrics(record)["aux_incomplete"], metrics(record)["strategy_degraded"]) == (1, True)
    m = metrics(again)
    assert m["aux_incomplete"] == 3 and m["aux_held"] == codes.count("AUX_BUDGET")
    assert m["strategy_degraded"] is True


def test_investigators_of_a_trial_with_a_cap_run_and_the_trial_is_not_degraded(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    w.turns.outputs["investigate"] = FINDINGS
    goal = w.approved(w.comps.strategy("parallel_readonly", {"steps": ALL_QUESTIONS}, **AUX),
                      trial=trial_context())  # fmt: skip
    record = w.loop.run_goal(goal)
    assert record["status"] == "verified" and w.rig.published == []
    assert record["aux_stops"] == [] and len(w.turns.of("investigate")) == 3
    assert "Investigation notes" in w.agents.prompts[0]
    m = metrics(record)
    assert (m["strategy"], m["aux_turns"], m["aux_tokens"]) == ("parallel_readonly", 3, 300)
    assert m["strategy_degraded"] is False


@pytest.mark.parametrize("steps", [["no_such_question"], ["tests_to_run", "tests_to_run"]])
def test_investigator_steps_must_be_distinct_known_questions(
    deployment: Any, tmp_path: Path, steps: list[str]
) -> None:
    w = make_world(deployment, tmp_path)
    plan = w.record(w.plan(w.comps.strategy("parallel_readonly", {"steps": steps}, **AUX)))
    assert plan["strategy"]["used_prior"] is True and plan["strategy"]["strategy"] == PRIOR
    assert (
        "parallel_readonly.steps are distinct ids of"
        in (plan["strategy"]["eligibility"]["parallel_readonly"])
    )
    with pytest.raises(RuntimeFault) as too_many:
        w.comps.strategy("parallel_readonly", {"steps": [*ALL_QUESTIONS, "a", "b"]})
    assert too_many.value.code == "COMPONENT_CONTENT"  # at most 4 steps


# ===========================================================================================
# eligibility, refusal and node attempts (the pure parts of the runner)
# ===========================================================================================
def choose(w: World, ref: Any, **kw: Any) -> dict[str, Any]:
    installed = w.service.apps["app"]
    chosen = w.service.select_composition(installed, pin=ref)
    budget = w.service._budget_policy(chosen["ref"])
    kw.setdefault("mode", "work")
    kw.setdefault("apps", ["app"])
    kw.setdefault("trial", False)
    record: dict[str, Any] = w.runner.choose(
        installed=installed, composition=chosen, budget=budget, **kw
    )
    return record


@pytest.mark.parametrize(
    ("strategy", "kw", "why"),
    [
        ("orchestrator", {"mode": "design"}, "a design goal runs single or repair_loop"),
        ("best_of_n", {"mode": "design"}, "a design goal runs single or repair_loop"),
        ("workgraph_split", {"apps": ["app", "consumer"]}, "a one-app strategy on a goal across"),
        ("plan_execute", {"apps": ["app", "consumer"]}, "a one-app strategy on a goal across"),
        ("orchestrator", {"apps": ["app", "consumer"]}, "a one-app strategy on a goal across"),
    ],
)
def test_choose_refuses_a_strategy_the_goal_cannot_run(
    deployment: Any, tmp_path: Path, strategy: str, kw: dict[str, Any], why: str
) -> None:
    w = make_world(deployment, tmp_path)
    ref = w.comps.strategy(strategy, **AUX)
    real = choose(w, ref, **kw)
    assert real["strategy"] == PRIOR and real["used_prior"] is True
    assert why in real["eligibility"][strategy] and real["refused"] is None
    trial = choose(w, ref, trial=True, **kw)  # a trial runs its strategy or nothing
    assert trial["strategy"] == strategy and trial["refused"].startswith(f"{strategy}: ")


@pytest.mark.parametrize("strategy", ["single", "repair_loop"])
def test_a_design_goal_may_run_single_or_repair_loop(
    deployment: Any, tmp_path: Path, strategy: str
) -> None:
    w = make_world(deployment, tmp_path)
    record = choose(w, w.comps.strategy(strategy), mode="design")
    assert record["strategy"] == strategy and record["eligibility"][strategy] is None


def test_choose_records_the_declared_strategy_params_roles_and_cascade(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_cascade_world(deployment, tmp_path)
    record = choose(w, w.comps.strategy("cascade", CASCADE))
    assert record["declared"] == record["strategy"] == "cascade" and record["params"] == CASCADE
    assert record["cascade"] == ["codex-cli", "claude-cli"] and record["decision_ref"] is None
    assert record["roles"]["executor"] == "codex-cli" and record["enabled"] == ["cascade"]
    prior = choose(w, w.comps.base_ref)
    assert (
        prior["strategy"] == PRIOR and prior["params"] == {} and prior["cascade"] == ["codex-cli"]
    )


def test_node_attempts_are_fixed_only_by_single_and_best_of_n(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.runtime.execution.strategy_runner import StrategyChoice

    root = {"max_attempts": 3}

    def attempts(strategy: str, **params: Any) -> int | None:
        return StrategyRunner.node_attempts(StrategyChoice(strategy, params, {}, (), None), root)

    assert attempts("single") == 1 and attempts("best_of_n", n=2) == 2
    assert attempts("best_of_n", n=3) == 3
    assert (
        StrategyRunner.node_attempts(
            StrategyChoice("best_of_n", {"n": 3}, {}, (), None), {"max_attempts": 2}
        )
        == 2
    )
    for other in ("repair_loop", "workgraph_split", "plan_execute", "cascade", "orchestrator",
                  "generator_reviewer", "parallel_readonly", "vote"):  # fmt: skip
        assert attempts(other) is None


def test_a_plan_recorded_before_s9_runs_the_v1_loop_and_holds_a_non_v1_strategy(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path)
    old = {"goal_id": "g", "composition": {"cell_id": "codex-cli"}}  # no "strategy" record
    installed = w.service.apps["app"]
    v1 = w.service._budget_policy(w.comps.base_ref)
    other = w.service._budget_policy(
        w.service.select_composition(installed, pin=w.comps.strategy("single"))["ref"]
    )
    assert w.runner.refusal(old, v1) is None
    assert w.runner.refusal(old, other) == "execution_strategy (planned before S9)"
    refused = {**old, "strategy": {"strategy": "single", "refused": "single: a reason"}}
    assert w.runner.refusal(refused, other) == "single: a reason"
    assert w.runner.refusal({**old, "strategy": {"strategy": "vote"}}, other) == HELD["vote"]
    assert w.runner.refusal({**old, "strategy": {"strategy": "mystery"}}, other) == (
        "unknown strategy"
    )
    assert w.runner.refusal({**old, "strategy": {"strategy": "single"}}, other) is None


def test_a_replan_of_a_splitting_strategy_runs_the_v1_prior(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.runtime.execution.product import LocalExecutionService

    plan = {"strategy": {"strategy": "orchestrator", "params": {"max_parts": 3}, "eligibility": {}}}
    kept = LocalExecutionService._replanned_strategy(
        {"strategy": {"strategy": "single", "params": {}}}, {"cell_id": "codex-cli"}
    )
    assert kept == {"strategy": "single", "params": {}}
    moved = LocalExecutionService._replanned_strategy(plan, {"cell_id": "codex-cli"})
    assert moved is not None and moved["strategy"] == PRIOR and moved["used_prior"] is True
    assert "replan" in moved["eligibility"] and moved["params"] == {}
    assert LocalExecutionService._replanned_strategy({}, {"cell_id": "codex-cli"}) is None


# ===========================================================================================
# the declared strategy only, and what the publisher sees
# ===========================================================================================
def test_a_real_goal_runs_exactly_the_first_enabled_strategy_and_never_explores(
    deployment: Any, tmp_path: Path
) -> None:
    w = make_world(deployment, tmp_path, wrong_first)
    composition = w.comps.candidate(
        execution_strategy={
            "enabled": ["best_of_n", "single", "repair_loop"],
            "params": {"best_of_n": {"n": 3}},
        }
    )
    record = w.run(composition)
    plan = record["strategy"]
    assert plan["strategy"] == plan["declared"] == "best_of_n" and plan["decision_ref"] is None
    assert plan["enabled"] == ["best_of_n", "single", "repair_loop"] and plan["used_prior"] is False
    assert metrics(record)["strategy"] == "best_of_n"
    assert w.turns.calls == []  # nothing was tried on the side


def node(node_id: str, after: list[str] | None = None) -> dict[str, Any]:
    return {"node_id": node_id, "depends_on": after or []}


def test_terminal_nodes_are_the_last_node_of_each_app_in_dependency_order() -> None:
    chain = {"nodes": [node("node-app.s1"), node("node-app.s2", ["node-app.s1"])]}
    names = {"node-app.s1": "app", "node-app.s2": "app"}
    assert {a: n["node_id"] for a, n in terminal_nodes(chain, names).items()} == {
        "app": "node-app.s2"
    }
    # the order of the listing does not matter: the node nothing else follows is the last one
    backwards = {"nodes": list(reversed(chain["nodes"]))}
    assert terminal_nodes(backwards, names)["app"]["node_id"] == "node-app.s2"
    parts = {
        "nodes": [
            node("node-app.p1"), node("node-app.p2"),
            node("node-app.int", ["node-app.p1", "node-app.p2"]),
        ]
    }  # fmt: skip
    names = {n["node_id"]: "app" for n in parts["nodes"]}
    assert terminal_nodes(parts, names)["app"]["node_id"] == "node-app.int"
    # legacy graphs (node-<app>, no node_apps) and several apps: one terminal node per app
    legacy = {"nodes": [node("node-app"), node("node-consumer", ["node-app"])]}
    found = terminal_nodes(legacy)
    assert list(found) == ["app", "consumer"]
    assert [n["node_id"] for n in found.values()] == ["node-app", "node-consumer"]
    mixed = {
        "nodes": [
            node("node-app.s1"), node("node-app.s2", ["node-app.s1"]),
            node("node-consumer", ["node-app.s2"]),
        ]
    }  # fmt: skip
    mixed_names = {"node-app.s1": "app", "node-app.s2": "app", "node-consumer": "consumer"}
    found = terminal_nodes(mixed, mixed_names)
    assert {a: n["node_id"] for a, n in found.items()} == {
        "app": "node-app.s2",
        "consumer": "node-consumer",
    }
