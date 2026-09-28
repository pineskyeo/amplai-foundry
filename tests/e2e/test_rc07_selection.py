"""Work 019 S2 (E) — the system selects the composition; Codex first, Claude as fallback (D-079).

Stand-ins as in the rc06 rig: fixed planners and host-process "containers" that speak each CLI's
stream dialect. Real Claude/Codex runs are recorded under specs/019-v3-completion/runs/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.cli import render_plan
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import install_driver_profile
from amplai_foundry.runtime.execution.product import AppConfig, app_capabilities
from rc06_rig import codex_inputs, rig_with_two_drivers, submit


def test_codex_is_selected_first_and_the_choice_is_shown(deployment: Any, tmp_path: Path) -> None:
    rig, loop, codex_box, claude_box, claude_planner = rig_with_two_drivers(deployment, tmp_path)
    goal = submit(rig)
    plan = rig.service.plan(goal)
    chosen = plan["composition"]
    assert chosen["driver_id"] == "codex-cli" and chosen["rank"] == 1
    assert [c["eligible"] for c in chosen["candidates"]] == [True, True]
    assert plan["planned_with"]["driver_id"] == "codex-cli"
    assert rig.planner.calls and not claude_planner.calls
    assert "codex-cli (gpt-5.6-sol)" in render_plan(plan)
    rig.service.approve(rig.operator, goal)
    assert loop.run_goal(goal)["status"] == "published"
    assert codex_box.prompts and not claude_box.prompts


def test_claude_takes_over_when_codex_is_not_eligible(deployment: Any, tmp_path: Path) -> None:
    rig, loop, codex_box, claude_box, claude_planner = rig_with_two_drivers(
        deployment, tmp_path, codex_enabled=False
    )
    goal = submit(rig)
    plan = rig.service.plan(goal)
    chosen = plan["composition"]
    assert chosen["driver_id"] == "claude-cli" and chosen["rank"] == 2
    excluded = next(c for c in chosen["candidates"] if c["driver_id"] == "codex-cli")
    assert excluded == {
        "driver_id": "codex-cli", "model": "gpt-5.6-sol", "eligible": False,
        "reasons": ["model disabled"],
    }  # fmt: skip
    assert claude_planner.calls and not rig.planner.calls
    rig.service.approve(rig.operator, goal)
    record = loop.run_goal(goal)
    assert record["status"] == "published" and [a["outcome"] for a in record["attempts"]] == [
        "pass"
    ]
    assert claude_box.prompts and not codex_box.prompts
    # the activated profile, fixed for every dispatch of this goal, is the Claude composition
    goal_head = rig.d.store.head(rig.d.scope, "goal", goal)
    assert goal_head["data"]["profile"]["driver_profile_ref"]["id"] == "claude-cli"


def test_approval_refuses_a_choice_that_is_no_longer_eligible(
    deployment: Any, tmp_path: Path
) -> None:
    rig, _loop, _codex, _claude, _planner = rig_with_two_drivers(deployment, tmp_path)
    goal = submit(rig)
    assert rig.service.plan(goal)["composition"]["driver_id"] == "codex-cli"
    # the operator disables Codex after planning: the approved plan must be re-planned
    d = rig.d
    rig.service.drivers["codex-cli"] = install_driver_profile(
        d.store, d.scope, codex_inputs(tmp_path, enabled=False), app_capabilities("app")
    )
    rig.service.install(AppConfig("app", rig.repo, rig.service.apps["app"].config.verifiers))
    with pytest.raises(Hold) as held:
        rig.service.approve(rig.operator, goal)
    assert held.value.code == "COMPOSITION_CHANGED"
    assert rig.service.plan_record(goal)["status"] == "awaiting_approval"


def test_nothing_eligible_is_a_hold_not_a_guess(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _codex, _claude, _planner = rig_with_two_drivers(
        deployment, tmp_path, codex_enabled=False
    )
    d = rig.d
    from rc06_rig import claude_inputs

    rig.service.drivers["claude-cli"] = install_driver_profile(
        d.store, d.scope, claude_inputs(tmp_path, enabled=False), app_capabilities("app")
    )
    rig.service.install(AppConfig("app", rig.repo, rig.service.apps["app"].config.verifiers))
    with pytest.raises(Hold) as held:
        rig.service.plan(submit(rig))
    assert held.value.code == "NO_COMPOSITION"


def test_a_plan_made_before_selection_still_approves_on_codex(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, codex_box, _claude, _planner = rig_with_two_drivers(deployment, tmp_path)
    goal = submit(rig)
    plan = rig.service.plan(goal)
    legacy = {k: v for k, v in plan.items() if k != "composition"}  # Work 018 record shape
    rig.service._save_plan(goal, legacy)
    rig.service.approve(rig.operator, goal)
    assert loop.run_goal(goal)["status"] == "published" and codex_box.prompts
