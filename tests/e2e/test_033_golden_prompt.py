"""Work 033 S0, golden G1 (interfaces.md 4.2): the implementer prompt is frozen.

`ExecutionLoop.prompt` must equal the frozen oracle (`tests/golden033/prompt_oracle.py`, a copy of
`loop.py:522-616` and `prompts.py` at c9f896a) byte for byte over the matrix:

  (a) the 20 Work 030 tasks drafted by `TrialPlanner` for an app with verifiers `unit` and `lint`
  (b) feedback: none, one failing observation with a 5,000-char stdout tail, mixed pass/fail,
      design details with 25 `outside` items
  (c) a design-mode plan
  (d) a two-app plan with a 70,000-char upstream patch
  (e) a plan without `composition` (Work 018 fallback, `loop.py:608-610`)

Three literal snapshots (`tests/fixtures/033/golden_prompt_{a,b,d}.txt`) were written by the
oracle at S0, so an edited oracle fails as well as an edited loop.

The loop is built without a deployment: `prompt` reads only `service.apps`, `store` and `scope`
(`loop.py:522-616`). A later slice that gives `prompt` more inputs extends `make_loop` here; it
does not change the expected text (that is the point of the test).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from amplai_foundry.meta_harness import local_corpus
from amplai_foundry.meta_harness.local_executor import TrialPlanner
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution import prompts
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.product import VerifierCommand
from golden033.prompt_oracle import IMPLEMENTER_BASELINE, implementer_role, oracle_prompt

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "specs" / "030-meta-harness-live" / "corpus"
FIXTURES = ROOT / "tests" / "fixtures" / "033"
COMMIT = "9e574bcd0270ad8ef6e27aa6f6b757d7c032c06b"
UNIT = VerifierCommand("unit", ("python", "-m", "pytest", "-q"), "the unit tests pass")
LINT = VerifierCommand("lint", ("ruff", "check", "."), "the linter passes")
DESIGN = "the design document check (sections, sources, paths)"


class FakeStore:
    """Only `get(scope, kind, ref)` is used by `_implementer_role`."""

    def __init__(self, records: dict[tuple[str, str], dict[str, Any]]) -> None:
        self.records = records

    def get(self, scope: Any, kind: str, ref: Any) -> dict[str, Any]:
        try:
            return self.records[(kind, ref)]
        except KeyError:
            raise RuntimeFault("NOT_FOUND", "No such record") from None


BUNDLE = prompts.bundle(prompts.BASELINE_ID, prompts.IMPLEMENTER_BASELINE, "test")
RECORDS = {
    ("harness-composition", "comp-1"): {"prompt_bundle_ref": "bundle-1"},
    (prompts.KIND, "bundle-1"): BUNDLE,
}


def make_loop(*apps: tuple[str, tuple[VerifierCommand, ...]]) -> ExecutionLoop:
    loop = ExecutionLoop.__new__(ExecutionLoop)
    loop.service = SimpleNamespace(  # type: ignore[assignment]
        apps={
            name: SimpleNamespace(config=SimpleNamespace(app_id=name, verifiers=verifiers))
            for name, verifiers in apps
        }
    )
    loop.store = FakeStore(RECORDS)  # type: ignore[assignment]
    loop.scope = "scope"  # type: ignore[assignment]
    return loop


def check(
    loop: ExecutionLoop,
    contract: dict[str, Any],
    plan: dict[str, Any],
    feedback: list[dict[str, Any]] | None,
    *,
    app_name: str,
    node: dict[str, Any] | None = None,
    upstream: list[tuple[str, str]] | None = None,
) -> str:
    """The new loop's prompt, asserted equal to the oracle's; returns the text."""
    got = loop.prompt(contract, plan, feedback, node=node, upstream=upstream)
    app = loop.service.apps[app_name].config
    ref = (plan.get("composition") or {}).get("ref")
    bundle = RECORDS.get((prompts.KIND, "bundle-1")) if ref else None
    want = oracle_prompt(
        contract,
        plan,
        feedback,
        app=app,
        role_lines=implementer_role(plan, bundle),
        node=node,
        upstream=upstream,
    )
    assert got == want
    return got


# ---------------------------------------------------------------- feedback (b)


def _obs(acceptance_id: str, outcome: str, **details: Any) -> dict[str, Any]:
    return {
        "acceptance_id": acceptance_id,
        "outcome": outcome,
        "reason": f"{acceptance_id} {outcome} reason",
        "details": details,
    }


def _tail(n: int) -> str:
    """A deterministic n-char tail whose last 3,000 chars differ from its first ones."""
    text = "".join(f"line {i:05d} of the failing run\n" for i in range(n // 20 + 1))
    return text[:n]


FEEDBACKS: dict[str, list[dict[str, Any]] | None] = {
    "none": None,
    "empty": [],
    "failing_5000": [_obs("AC-1", "fail", stdout_tail=_tail(5000), stderr_tail="")],
    "mixed": [
        _obs("AC-1", "pass", stdout_tail="ok"),
        _obs("AC-2", "fail", stdout_tail="out", stderr_tail="err"),
        _obs("AC-3", "error"),
    ],
    "design_25_outside": [
        _obs(
            "AC-1",
            "fail",
            outside=[f"src/pkg/file_{i}.py" for i in range(25)],
            missing_sections=["Risks", "Sources"],
            unresolved_sources=[f"x/{i}.py:{i}" for i in range(25)],
        )
    ],
}


# ---------------------------------------------------------------- plans


def _work_plan(task: local_corpus.CorpusTask, *, composition: bool = True) -> tuple[Any, ...]:
    verifiers = {"unit": UNIT.description, "lint": LINT.description}
    draft = TrialPlanner(local_corpus.load(CORPUS)).draft(
        task.contract_text(), "amplai-demo-app", verifiers, Path("."), mode="work"
    )["draft"]
    contract = {
        "objective": draft["objective"],
        "non_goals": draft["non_goals"],
        "constraints": [{"statement": s} for s in draft["constraints"]],
        "acceptance": [
            {"id": f"AC-{i + 1}", "statement": a["statement"]}
            for i, a in enumerate(draft["acceptance"])
        ],
    }
    plan: dict[str, Any] = {
        "app": "amplai-demo-app",
        "base_commit": COMMIT,
        "draft": draft,
    }
    if composition:
        plan["composition"] = {"ref": "comp-1"}
    return contract, plan


def production_shape(contract: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Give `plan` the shape `LocalExecutionService.plan` records (product.py:572-651).

    Adds `work_items` (one item) and `acceptance_map` {AC-i: {app, verifier, statement}}, sets
    `mode` when missing, and returns the node `loop.py:394-397` passes to `prompt`. The one-item
    plan takes the `mapping and node is not None` branch without the "This part of the goal" line.
    """
    plan.setdefault("mode", "work")
    plan["work_items"] = [{"app": plan["app"]}]
    plan["acceptance_map"] = {
        a["id"]: {
            "app": plan["app"],
            "verifier": d["verifier"],
            "statement": a["statement"],
        }
        for a, d in zip(contract["acceptance"], plan["draft"]["acceptance"], strict=True)
    }
    return {
        "node_id": f"node-{plan['app']}",
        "objective": contract["objective"],
        "acceptance_ids": list(plan["acceptance_map"]),
    }


def _tasks() -> list[local_corpus.CorpusTask]:
    tasks = list(local_corpus.load(CORPUS).tasks)
    assert len(tasks) == 20
    return tasks


def _design_plan() -> tuple[Any, ...]:
    draft = {
        "objective": "Decide how the registry is loaded",
        "in_scope": ["src/registry/"],
        "acceptance": [
            {"statement": "the document names the options", "verifier": "design"},
            {"statement": "the document cites sources", "verifier": "design"},
        ],
    }
    contract = {
        "objective": draft["objective"],
        "non_goals": ["no implementation"],
        "constraints": [{"statement": "only the design document changes"}],
        "acceptance": [
            {"id": "AC-1", "statement": draft["acceptance"][0]["statement"]},
            {"id": "AC-2", "statement": draft["acceptance"][1]["statement"]},
        ],
    }
    plan = {
        "app": "amplai-demo-app",
        "base_commit": COMMIT,
        "mode": "design",
        "design_dir": "specs/design/registry-load/",
        "draft": draft,
        "composition": {"ref": "comp-1"},
    }
    return contract, plan


def _two_app_plan() -> tuple[Any, ...]:
    draft = {
        "objective": "Add a field in the API and show it in the UI",
        "in_scope": ["api/", "ui/"],
        "acceptance": [
            {"statement": "the API returns the field", "verifier": "unit"},
            {"statement": "the UI renders the field", "verifier": "lint"},
        ],
    }
    contract = {
        "objective": draft["objective"],
        "non_goals": ["no schema migration", "no new endpoint"],
        "constraints": [{"statement": "keep the wire format"}, {"statement": "no new dependency"}],
        "acceptance": [
            {"id": "AC-1", "statement": "the API returns the field"},
            {"id": "AC-2", "statement": "the UI renders the field"},
        ],
    }
    plan = {
        "app": "api",
        "bases": {"api": COMMIT, "ui": COMMIT},
        "base_commit": COMMIT,
        "draft": draft,
        "work_items": [{"app": "api"}, {"app": "ui", "after": ["api"]}],
        "acceptance_map": {
            "AC-1": {"statement": "the API returns the field", "verifier": "unit"},
            "AC-2": {"statement": "the UI renders the field", "verifier": "lint"},
        },
        "composition": {"ref": "comp-1"},
    }
    nodes = {
        "api": {"node_id": "node-api", "objective": "add the field", "acceptance_ids": ["AC-1"]},
        "ui": {"node_id": "node-ui", "objective": "render it", "acceptance_ids": ["AC-2"]},
    }
    return contract, plan, nodes


def _patch(n: int) -> str:
    return "".join(f"+added line {i:06d}\n" for i in range(n // 18 + 1))[:n]


# ---------------------------------------------------------------- snapshots


def snapshot(name: str) -> str:
    return (FIXTURES / f"golden_prompt_{name}.txt").read_text()


def case_a() -> str:
    """Snapshot (a): the first Work 030 task, no feedback."""
    contract, plan = _work_plan(_tasks()[0])
    node = production_shape(contract, plan)
    return check(make_loop(("amplai-demo-app", (UNIT, LINT))), contract, plan, None,
                 app_name="amplai-demo-app", node=node)  # fmt: skip


def case_b() -> str:
    """Snapshot (b): the first task after a failing attempt with a 5,000-char stdout tail."""
    contract, plan = _work_plan(_tasks()[0])
    node = production_shape(contract, plan)
    return check(
        make_loop(("amplai-demo-app", (UNIT, LINT))),
        contract,
        plan,
        FEEDBACKS["failing_5000"],
        app_name="amplai-demo-app",
        node=node,
    )


def case_d() -> str:
    """Snapshot (d): the second node of a two-app plan with a 70,000-char upstream patch."""
    contract, plan, nodes = _two_app_plan()
    return check(
        make_loop(("api", (UNIT,)), ("ui", (LINT,))),
        contract,
        plan,
        FEEDBACKS["mixed"],
        app_name="ui",
        node=nodes["ui"],
        upstream=[("api", _patch(70_000))],
    )


def test_snapshots_are_the_oracle_text_byte_for_byte() -> None:
    assert case_a() == snapshot("a")
    assert case_b() == snapshot("b")
    assert case_d() == snapshot("d")


def test_the_snapshots_show_the_truncation_and_the_baseline_role() -> None:
    b, d = snapshot("b"), snapshot("d")
    assert snapshot("a").startswith("You are the IMPLEMENTER for AMPLAI.")
    assert len(b.split("```\n")[1].split("\n```")[0]) == 3000  # FEEDBACK_TAIL
    assert len(d.split("```diff\n")[1].split("\n```")[0]) == 60_000  # UPSTREAM_PATCH


# ---------------------------------------------------------------- matrix


@pytest.mark.parametrize("feedback", list(FEEDBACKS))
@pytest.mark.parametrize("index", range(20))
def test_a_and_b_the_twenty_work030_tasks_across_the_feedback_forms(
    index: int, feedback: str
) -> None:
    contract, plan = _work_plan(_tasks()[index])
    node = production_shape(contract, plan)
    loop = make_loop(("amplai-demo-app", (UNIT, LINT)))
    text = check(loop, contract, plan, FEEDBACKS[feedback], app_name="amplai-demo-app", node=node)
    assert "Objective: " + contract["objective"] in text
    assert "This part of the goal" not in text  # one work item


@pytest.mark.parametrize("feedback", list(FEEDBACKS))
def test_a_legacy_plan_without_work_items_or_acceptance_map(feedback: str) -> None:
    """The pre-Work-018 fallback (loop.py:579-581): no node, no map, no work items."""
    contract, plan = _work_plan(_tasks()[0])
    assert "work_items" not in plan and "acceptance_map" not in plan
    loop = make_loop(("amplai-demo-app", (UNIT, LINT)))
    check(loop, contract, plan, FEEDBACKS[feedback], app_name="amplai-demo-app")


@pytest.mark.parametrize("feedback", list(FEEDBACKS))
def test_c_a_design_mode_plan(feedback: str) -> None:
    contract, plan = _design_plan()
    node = production_shape(contract, plan)
    loop = make_loop(("amplai-demo-app", (UNIT, LINT)))
    text = check(loop, contract, plan, FEEDBACKS[feedback], app_name="amplai-demo-app", node=node)
    assert text.startswith("You are the DESIGNER for AMPLAI.")
    assert "command: `the design document check (sections, sources, paths)`" in text


@pytest.mark.parametrize("feedback", ["none", "failing_5000", "mixed", "design_25_outside"])
@pytest.mark.parametrize("patch_size", [0, 100, 60_000, 70_000])
def test_d_a_two_app_plan_with_an_upstream_patch(feedback: str, patch_size: int) -> None:
    contract, plan, nodes = _two_app_plan()
    loop = make_loop(("api", (UNIT,)), ("ui", (LINT,)))
    check(loop, contract, plan, FEEDBACKS[feedback], app_name="api", node=nodes["api"])
    upstream = [("api", _patch(patch_size))] if patch_size else None
    text = check(
        loop,
        contract,
        plan,
        FEEDBACKS[feedback],
        app_name="ui",
        node=nodes["ui"],
        upstream=upstream,
    )
    assert "This part of the goal, in ui: render it" in text


@pytest.mark.parametrize("feedback", list(FEEDBACKS))
def test_e_a_plan_without_composition_keeps_the_baseline(feedback: str) -> None:
    contract, plan = _work_plan(_tasks()[0], composition=False)
    assert "composition" not in plan
    node = production_shape(contract, plan)
    loop = make_loop(("amplai-demo-app", (UNIT, LINT)))
    text = check(loop, contract, plan, FEEDBACKS[feedback], app_name="amplai-demo-app", node=node)
    head = "\n".join(IMPLEMENTER_BASELINE).format(app_id="amplai-demo-app", base_commit=COMMIT)
    assert text.startswith(head)


def test_a_bundle_that_is_not_in_the_store_falls_back_to_the_baseline() -> None:
    contract, plan = _work_plan(_tasks()[0])
    loop = make_loop(("amplai-demo-app", (UNIT, LINT)))
    loop.store = FakeStore({("harness-composition", "comp-1"): {"prompt_bundle_ref": "gone"}})  # type: ignore[assignment]
    got = loop.prompt(contract, plan, None)
    want = oracle_prompt(
        contract,
        plan,
        None,
        app=loop.service.apps["amplai-demo-app"].config,
        role_lines=implementer_role(plan, None),
    )
    assert got == want
