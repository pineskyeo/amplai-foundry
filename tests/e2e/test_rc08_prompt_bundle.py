"""Work 030 S1 (D-089) — the IMPLEMENTER role text is a versioned prompt bundle.

The baseline bundle is the text the loop built before bundles, byte for byte; a candidate bundle
in another composition changes only the role lines; a plan without a bundle keeps the baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution import prompts
from rc06_rig import approved, rig_with_codex

OLD_ROLE = (
    "You are the IMPLEMENTER for AMPLAI. The current directory is a copy of the app repository at"
    " commit {commit} (no .git, network limited).\n"
    "Make the change below. Do not commit. When you are done, run the acceptance commands"
    " yourself; the result is judged by running them on a clean copy with exactly your file"
    " changes applied.\n"
)


def _prompt(rig: Any, loop: Any, goal: str) -> str:
    plan = rig.service.plan_record(goal)
    contract = rig.d.store.get(rig.d.scope, "goal-contract", plan["contract_ref"])
    text: str = loop.prompt(contract, plan, None)
    return text


def test_the_baseline_bundle_is_the_old_text_byte_for_byte(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    plan = rig.service.plan_record(goal)
    composition = rig.d.store.get(rig.d.scope, "harness-composition", plan["composition"]["ref"])
    bundle = rig.d.store.get(rig.d.scope, prompts.KIND, composition["prompt_bundle_ref"])
    assert bundle["bundle_id"] == prompts.BASELINE_ID
    text = _prompt(rig, loop, goal)
    assert text.startswith(OLD_ROLE.format(commit=plan["base_commit"]) + "\nObjective: ")


def test_a_candidate_bundle_changes_only_the_role_lines(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    plan = rig.service.plan_record(goal)
    d = rig.d
    baseline = d.store.get(d.scope, "harness-composition", plan["composition"]["ref"])
    candidate_bundle = rig.service._put(
        prompts.KIND,
        "implementer-candidate",
        prompts.bundle(
            "implementer-candidate",
            ["You implement one change in {app_id} at {base_commit}.", "Run every check."],
            "test candidate",
        ),
    )
    candidate = rig.service._put(
        "harness-composition",
        "app-codex-candidate",
        {**baseline, "composition_id": "app-codex-candidate",
         "prompt_bundle_ref": candidate_bundle},
    )  # fmt: skip
    before = _prompt(rig, loop, goal)
    plan["composition"] = {**plan["composition"], "ref": candidate}
    after = loop.prompt(d.store.get(d.scope, "goal-contract", plan["contract_ref"]), plan, None)
    head = f"You implement one change in app at {plan['base_commit']}.\nRun every check.\n"
    assert after.startswith(head)
    # everything after the role lines is identical
    assert after[len(head) :] == before[len(OLD_ROLE.format(commit=plan["base_commit"])) :]


def test_a_plan_without_a_bundle_keeps_the_baseline(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right")
    goal = approved(rig)
    plan = rig.service.plan_record(goal)
    plan.pop("composition")  # recorded before compositions (Work 018)
    contract = rig.d.store.get(rig.d.scope, "goal-contract", plan["contract_ref"])
    assert loop.prompt(contract, plan, None).startswith(OLD_ROLE.format(commit=plan["base_commit"]))


@pytest.mark.parametrize(
    "lines",
    [[], ["uses {secret}"], ["{contract}"], [""], ["x" * 4001], ["ok"] * 21],
)
def test_a_bundle_may_fill_only_its_two_placeholders(lines: list[str]) -> None:
    with pytest.raises(RuntimeFault) as bad:
        prompts.bundle("b", lines, "test")
    assert bad.value.code == "PROMPT_BUNDLE"
