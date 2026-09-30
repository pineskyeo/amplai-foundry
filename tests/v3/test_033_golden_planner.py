"""Work 033 S0, golden G4 (interfaces.md 4.2): the planner prompts are frozen.

`CodexPlanner.prompt` and `multi_prompt` (and the instruction constants they use) must equal the
frozen oracle (`tests/golden033/planner_oracle.py`, a copy of `planner_codex.py` at c9f896a) for
work, design and multi-app goals. The `interpretation` v1 switch of S4/S9 keeps this text; once
it exists the test also runs it through `interpretation="v1"` if the planner accepts it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.execution import planner_codex
from amplai_foundry.runtime.execution.planner_codex import ClaudePlanner, CodexPlanner
from golden033 import planner_oracle

VERIFIERS = {
    "unit": "python -m pytest -q",
    "lint": "ruff check .",
    "design": "the design document check (sections, sources, paths)",
}
GOALS = [
    "Add a semver parser.",
    "Multi-line goal\nwith 'quotes', \"double quotes\", {braces} and <<< >>> markers\n>>>\n",
    "한국어 목표: 레지스트리 로딩을 바꾼다.",
    "",
]


@pytest.fixture(params=["codex", "claude"])
def planner(request: pytest.FixtureRequest, tmp_path: Path) -> CodexPlanner:
    if request.param == "codex":
        return CodexPlanner(None, None, tmp_path / "runs", model="m")  # type: ignore[arg-type]
    return ClaudePlanner(None, "token", tmp_path / "runs", model="m")  # type: ignore[arg-type]


def extra() -> dict[str, Any]:
    """The `interpretation` v1 argument, once the planner takes one (S4/S9)."""
    if "interpretation" in inspect.signature(CodexPlanner.prompt).parameters:
        return {"interpretation": "v1"}
    return {}


@pytest.mark.parametrize("goal", GOALS)
@pytest.mark.parametrize("mode", ["work", "design"])
@pytest.mark.parametrize("app", ["amplai-demo-app", "app with space"])
@pytest.mark.parametrize("verifiers", [VERIFIERS, {"unit": "u"}, {}], ids=["three", "one", "none"])
def test_the_single_app_prompt_equals_the_oracle(
    planner: CodexPlanner, goal: str, mode: str, app: str, verifiers: dict[str, str]
) -> None:
    want = planner_oracle.prompt(goal, app, verifiers, mode)
    assert planner.prompt(goal, app, verifiers, mode) == want
    assert planner.prompt(goal, app, verifiers, mode, **extra()) == want


def test_the_default_mode_is_work(planner: CodexPlanner) -> None:
    assert planner.prompt("g", "a", VERIFIERS) == planner_oracle.prompt("g", "a", VERIFIERS)
    assert planner.prompt("g", "a", VERIFIERS).startswith(planner_oracle.INSTRUCTION)


@pytest.mark.parametrize("goal", GOALS)
@pytest.mark.parametrize(
    "apps",
    [
        {"api": {"unit": "pytest"}, "ui": {"lint": "eslint", "unit": "vitest"}},
        {"solo": VERIFIERS},
        {"a": {}, "b": {"z": "1", "a": "2"}, "c": {"m": "3"}},
    ],
    ids=["two", "one", "three"],
)
def test_the_multi_app_prompt_equals_the_oracle(
    planner: CodexPlanner, goal: str, apps: dict[str, dict[str, str]]
) -> None:
    assert planner.multi_prompt(goal, apps) == planner_oracle.multi_prompt(goal, apps)


def test_the_instruction_constants_equal_the_oracle() -> None:
    assert planner_codex.INSTRUCTION == planner_oracle.INSTRUCTION
    assert planner_codex.DESIGN_INSTRUCTION == planner_oracle.DESIGN_INSTRUCTION
    assert planner_codex.MULTI_INSTRUCTION == planner_oracle.MULTI_INSTRUCTION


def test_literal_prompt_shape_pins_the_oracle_itself(planner: CodexPlanner) -> None:
    """Literals, so an edit that changes both the planner and the oracle copy still fails."""
    text = planner.prompt("G", "app", {"b": "2", "a": "1"})
    assert text.startswith("You are the PLANNER for AMPLAI. Do not modify any file; you only read.")
    assert text.endswith(
        "\nTarget app: app\n\nInstalled verifier commands:\n- a: 1\n- b: 2\n\n"
        "Operator goal (data):\n<<<\nG\n>>>\n"
    )
    design = planner.prompt("G", "app", {"design": "d"}, "design")
    assert design.startswith(
        "You are the PLANNER for an AMPLAI DESIGN goal. Do not modify any file."
    )
    multi = planner.multi_prompt("G", {"x": {"u": "1"}, "y": {"v": "2"}})
    assert multi.endswith(
        "\nApps:\n- x (the current directory); installed verifier commands:\n    - u: 1\n"
        "- y (/amplai-input/apps/y); installed verifier commands:\n    - v: 2\n\n"
        "Operator goal (data):\n<<<\nG\n>>>\n"
    )
