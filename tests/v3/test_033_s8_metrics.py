"""Work 033 S8 - trial metrics (interfaces.md 2.10, 3.10; plan 8.1 metrics and 9.10 hack guards).

Pure parts (diff statistics, per-arm and per-strategy summaries, hack-guard findings, record schema)
run on plain rows. The trial, record and run helpers run on trials the real ``LocalTrialExecutor``
produced (scripted agent, no docker, no provider), wrapped in the trial heads
``EvaluationService`` and ``CalibrationService`` write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_033_s8_executor import QuestionPlanner, World, make_world

from amplai_foundry.meta_harness import trial_metrics
from amplai_foundry.meta_harness.trial_metrics import (
    CALIBRATION_ARM,
    TrialMetrics,
    diff_stats,
    goal_run_ids,
    open_effects,
    protected_paths,
    run_executions,
    validate_record,
)
from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.errors import Hold, RuntimeFault

# -- diff statistics and protected paths --
PATCH = b"""diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def value():
-    return 1
+    return 2
diff --git a/tests/test_old.py b/tests/test_old.py
index 1..2 100644
--- a/tests/test_old.py
+++ b/tests/test_old.py
@@ -1 +1,2 @@
 x = 1
+y = 2
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/tests/test_gone.py b/tests/test_gone.py
deleted file mode 100644
--- a/tests/test_gone.py
+++ /dev/null
@@ -1,3 +0,0 @@
-a
-b
-c
diff --git a/logo.png b/logo.png
new file mode 100644
Binary files /dev/null and b/logo.png differ
"""


def test_diff_stats_counts_files_lines_and_what_happened_to_tests() -> None:
    stats = diff_stats(PATCH)
    assert stats["files"] == 5
    assert stats["paths"] == [
        "app.py", "tests/test_old.py", "tests/test_new.py", "tests/test_gone.py", "logo.png",
    ]  # fmt: skip
    assert (stats["lines_added"], stats["lines_removed"]) == (1 + 1 + 2, 1 + 3)
    assert stats["tests_added"] == ["tests/test_new.py"]  # new tests are ordinary work
    assert stats["tests_extended"] == ["tests/test_old.py"]  # only lines appended: ordinary work
    assert stats["tests_changed"] == ["tests/test_gone.py"]  # deleted


def test_a_removed_or_changed_line_of_an_existing_test_is_a_test_change() -> None:
    patch = (
        b"diff --git a/tests/test_old.py b/tests/test_old.py\n--- a/tests/test_old.py\n"
        b"+++ b/tests/test_old.py\n@@ -1 +1 @@\n-assert x == 1\n+assert True\n"
    )
    stats = diff_stats(patch)
    assert stats["tests_changed"] == ["tests/test_old.py"] and stats["tests_extended"] == []


def test_diff_stats_of_an_empty_patch() -> None:
    assert diff_stats(b"") == {
        "files": 0, "paths": [], "lines_added": 0, "lines_removed": 0,
        "tests_added": [], "tests_extended": [], "tests_changed": [],
    }  # fmt: skip


def test_a_file_outside_a_tests_directory_is_never_a_test_edit() -> None:
    patch = b"diff --git a/test_app.py b/test_app.py\n--- a/test_app.py\n+++ b/test_app.py\n+x\n"
    assert diff_stats(patch)["tests_changed"] == []  # the rule is a `tests` path component


def test_protected_paths_are_the_paths_with_a_protected_component() -> None:
    paths = ["app.py", "eval/notes.txt", ".github/workflows/ci.yml", "src/verifiers/x.py",
             "docs/evaluation.md", "holdout/a", "contracts/b"]  # fmt: skip
    assert protected_paths(paths) == [
        ".github/workflows/ci.yml",
        "contracts/b",
        "eval/notes.txt",
        "holdout/a",
        "src/verifiers/x.py",
    ]
    assert protected_paths([]) == []


# -- per arm and per strategy --
def row(arm: str, success: bool | None, strategy: str | None, **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "arm": arm, "success": success, "strategy": strategy, "verified_hidden_fail": False,
        "input_tokens": 100, "output_tokens": 50, "seconds": 10.0, "diff": None,
        "api_cost": {"status": "no_usage"},
    }  # fmt: skip
    value.update(over)
    return value


def test_summarize_reports_each_arm_and_each_strategy_inside_it() -> None:
    cost = {"status": "estimated", "cost_microunits": 3_000_000, "upper_bound": False}
    diff = {
        "files": 2,
        "lines_added": 4,
        "lines_removed": 2,
        "tests_added": ["t"],
        "tests_changed": [],
    }
    rows = [
        row("baseline", True, "repair_loop", api_cost=cost, diff=diff),
        row("baseline", False, "repair_loop", verified_hidden_fail=True, api_cost=cost),
        row("baseline", None, None, api_cost=cost),  # before S8: no strategy recorded
        row("candidate", True, "best_of_n", seconds=4.0),
        row("candidate", True, "repair_loop", seconds=8.0),
    ]
    out = TrialMetrics.summarize(rows)
    assert list(out) == ["baseline", "candidate"]
    base = out["baseline"]
    assert (base["trials"], base["solved"], base["unknown"]) == (3, 1, 1)
    assert base["verified_hidden_fail"] == 1 and base["tests_added"] == 1
    assert base["tokens_per_solved"] == 450.0  # everything the arm spent over its one solve
    assert base["api_cost_usd_per_solved"] == 9.0 and base["api_cost_priced"] == "3/3"
    assert base["diff_files_mean"] == 2 and base["diff_lines_mean"] == 6
    assert set(base["strategies"]) == {"repair_loop", "unrecorded"}
    assert base["strategies"]["repair_loop"] == {
        "trials": 2, "solved": 1, "unknown": 0,
        "tokens_per_solved": 300.0, "seconds_per_solved": 20.0,
    }  # fmt: skip
    assert base["strategies"]["unrecorded"]["unknown"] == 1
    cand = out["candidate"]
    assert set(cand["strategies"]) == {"best_of_n", "repair_loop"}
    assert cand["strategies"]["best_of_n"]["seconds_per_solved"] == 4.0
    assert cand["api_cost_usd_per_solved"] is None and cand["api_cost_priced"] == "0/2"


def test_a_cost_is_reported_only_when_every_trial_was_priced() -> None:
    cost = {"status": "estimated", "cost_microunits": 1_000_000, "upper_bound": True}
    rows = [row("a", True, "s", api_cost=cost), row("a", True, "s")]
    summary = TrialMetrics.summarize(rows)["a"]
    assert summary["api_cost_usd_per_solved"] is None and summary["api_cost_priced"] == "1/2"
    assert summary["api_cost_upper_bound"] is True


def test_an_arm_with_no_solved_task_has_no_per_solved_figure() -> None:
    summary = TrialMetrics.summarize([row("a", False, "s")])["a"]
    assert summary["tokens_per_solved"] is None and summary["seconds_per_solved"] is None


# -- hack guards (9.10) ----------------------------------------------------------------------------
def grow(
    task_id: str, *, vhf: bool = False, ask: bool = False, lines: int = 10,
    broken: int | None = None, domain: str = "bug",
) -> dict[str, Any]:  # fmt: skip
    return {
        "task_id": task_id, "domain": domain,
        "guards": {"ask_back": ask, "edit_files": 1, "edit_lines": lines,
                   "broken_tool_calls": broken, "test_file_edits": 0, "verified_hidden_fail": vhf},
    }  # fmt: skip


def test_hack_guards_find_more_tasks_fitted_to_the_checks() -> None:
    baseline = [grow("t1"), grow("t2"), grow("t3", vhf=True)]
    candidate = [
        grow("t1", vhf=True),
        grow("t1", vhf=True),
        grow("t2", vhf=True),
        grow("t3", vhf=True),
    ]
    # tasks, not trials: t1 twice counts once, so +3 tasks - 1 = +2
    found = TrialMetrics.guards(baseline, candidate, {"verified_hidden_fail": 2})
    assert len(found) == 1 and found[0].startswith("verified_hidden_fail: +2 tasks")
    assert TrialMetrics.guards(baseline, candidate, {"verified_hidden_fail": 3}) == []


def test_ask_back_counts_on_non_ambiguity_tasks_only() -> None:
    baseline = [grow("t1"), grow("t2"), grow("a1", ask=True, domain="ambiguity")]
    candidate = [grow("t1", ask=True), grow("t2"), grow("a1", ask=True, domain="ambiguity")]
    found = TrialMetrics.guards(baseline, candidate, {"ask_back_rate": 0.4})
    assert len(found) == 1 and found[0].startswith("ask_back_rate: +0.500")
    # an ambiguity-only comparison has no eligible rows: no rate, no finding
    only = [grow("a1", ask=True, domain="ambiguity")]
    assert TrialMetrics.guards(only, only, {"ask_back_rate": 0.0}) == []


def test_broken_tool_calls_are_skipped_while_any_row_has_no_count() -> None:
    baseline = [grow("t1", broken=0), grow("t2", broken=0)]
    candidate = [grow("t1", broken=3), grow("t2", broken=None)]
    assert TrialMetrics.guards(baseline, candidate, {"broken_tool_calls_rate": 0.1}) == []
    complete = [grow("t1", broken=3), grow("t2", broken=1)]
    found = TrialMetrics.guards(baseline, complete, {"broken_tool_calls_rate": 0.5})
    assert len(found) == 1 and found[0].startswith("broken_tool_calls_rate: +1.000")


def test_edit_rate_compares_mean_edit_lines_and_skips_a_baseline_that_edits_nothing() -> None:
    baseline = [grow("t1", lines=10), grow("t2", lines=20)]
    candidate = [grow("t1", lines=40), grow("t2", lines=50)]
    found = TrialMetrics.guards(baseline, candidate, {"edit_rate_ratio": 2.0})
    assert len(found) == 1 and found[0].startswith("edit_rate_ratio: x3.00")
    assert TrialMetrics.guards(baseline, candidate, {"edit_rate_ratio": 4.0}) == []
    zero = [grow("t1", lines=0)]
    assert TrialMetrics.guards(zero, candidate, {"edit_rate_ratio": 1.0}) == []  # no ratio


def test_no_threshold_no_finding_and_test_file_edits_are_not_a_guard() -> None:
    bad = [{**grow("t1", vhf=True, ask=True, lines=999),
            "guards": {**grow("t1")["guards"], "test_file_edits": 9}}]  # fmt: skip
    assert TrialMetrics.guards([grow("t1")], bad, {}) == []
    # IC-18 (c): test_file_edits stays a per-trial metric, it is not a screening guard
    assert TrialMetrics.guards([grow("t1")], bad, {"test_file_edits": 0}) == []


# -- the stored record: schema ---------------------------------------------------------------------
REF = {"id": "x", "revision": 1, "digest": "sha256:" + "0" * 64}


def valid_record() -> dict[str, Any]:
    return {
        "schema": trial_metrics.SCHEMA, "scope": {"tenant": "t", "project": "p"},
        "trial_ref": REF, "experiment_ref": REF, "calibration_plan_ref": None,
        "source": "experiment", "split": "validation", "stage": "focused", "task_id": "t1",
        "domain": "bug", "arm": "candidate", "cell_id": "codex-cli", "strategy": "repair_loop",
        "success": True, "hidden_passed": True, "verified_hidden_fail": False,
        "tokens": {"input": 1, "output": 2, "cached_input": None, "reasoning": None},
        "api_cost": {"status": "no_usage"}, "wall_seconds": 1.5, "verification_seconds": None,
        "cells_used": ["codex-cli"], "agent_calls": 1, "turns": None, "attempts_used": 1,
        "escalations": 0, "reviewer_rounds": 0, "fix_requests": 0, "best_of_n_first_pass": None,
        "nodes": 1, "sub_agents": 0, "integration_conflicts": 0, "re_verifications": 0,
        "guards": {"ask_back": False, "edit_files": 1, "edit_lines": 2, "broken_tool_calls": None,
                   "test_file_edits": 0, "verified_hidden_fail": False},
        "phase": "stage", "fidelity": None, "computed_at": "2026-10-01T00:00:00Z",
    }  # fmt: skip


def test_a_valid_record_passes_and_the_calibration_shape_does_too() -> None:
    validate_record(valid_record())
    calibration = {**valid_record(), "source": "calibration", "stage": None, "experiment_ref": None,
                   "calibration_plan_ref": REF, "arm": CALIBRATION_ARM, "phase": "calibration",
                   "fidelity": {"rung": 1, "tasks": 8}}  # fmt: skip
    validate_record(calibration)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("split", "train"), ("split", None), ("stage", "nightly"), ("source", "canary"),
        ("phase", "later"), ("schema", "amplai.trial-metrics.v0"), ("task_id", ""), ("arm", ""),
        ("attempts_used", -1), ("nodes", 1.5), ("wall_seconds", -0.1), ("agent_calls", None),
        ("tokens", {"input": 1, "output": 2}), ("guards", {"ask_back": False}),
        ("fidelity", {"rung": 1}), ("success", "yes"), ("computed_at", ""),
    ],
)  # fmt: skip
def test_a_record_that_breaks_the_shape_is_refused(field: str, value: Any) -> None:
    with pytest.raises(RuntimeFault) as refused:
        validate_record({**valid_record(), field: value})
    assert refused.value.code == "TRIAL_METRICS" and refused.value.details


def test_a_record_with_an_extra_or_missing_field_is_refused() -> None:
    for broken in ({**valid_record(), "surplus": 1},):
        with pytest.raises(RuntimeFault):
            validate_record(broken)
    missing = valid_record()
    del missing["guards"]
    with pytest.raises(RuntimeFault):
        validate_record(missing)
    bad_guard = valid_record()
    bad_guard["guards"]["secret"] = 1
    with pytest.raises(RuntimeFault):
        validate_record(bad_guard)


# -- trials the real executor produced --
def trial_dict(world: World, obs: Any, task_id: str, **over: Any) -> dict[str, Any]:
    """The trial record ``EvaluationService`` stores for an observation (``evaluation/service.py``
    record step): the fields the metrics read."""
    value: dict[str, Any] = {
        "trial_id": "trial-" + task_id, "scope": world.scope.wire(),
        "experiment_ref": None, "composition_ref": world.baseline, "task_id": task_id,
        "task_class": "bug", "arm": "candidate", "repeat": 0, "mode": "sandbox_rerun",
        "success": obs.success, "safety_failures": obs.safety_failures,
        "unknown_effects": obs.unknown_effects, "artifact_refs": list(obs.artifact_refs),
        "input_tokens": obs.input_tokens, "output_tokens": obs.output_tokens,
        "usage_status": obs.usage_status, "elapsed_ms": 2500.0,
    }  # fmt: skip
    value.update(over)
    return value


def experiment(
    world: World, *, split: str = "validation", stage: str = "focused"
) -> dict[str, Any]:
    """An experiment record and its sampling plan, stored; returns the experiment ref."""
    store, scope = world.store, world.scope
    with store.tx() as db:
        sampling = store.put(
            db, scope, "sampling-plan", f"sp-{split}-{stage}", 1,
            {"scope": scope.wire(), "split": split, "stage": stage},
        )  # fmt: skip
        ref: dict[str, Any] = store.put(
            db, scope, "eval-experiment", f"exp-{split}-{stage}", 1,
            {"scope": scope.wire(), "experiment_id": f"exp-{split}-{stage}",
             "sampling_plan_ref": sampling},
        )  # fmt: skip
    return ref


def stored(world: World, trial: dict[str, Any], kind: str = "eval-trial") -> dict[str, Any]:
    with world.store.tx() as db:
        ref: dict[str, Any] = world.store.put(db, world.scope, kind, trial["trial_id"], 1, trial)
    return ref


@pytest.fixture
def world(deployment: Any, tmp_path: Path) -> World:
    return make_world(deployment, tmp_path)


def metrics_of(world: World) -> TrialMetrics:
    return TrialMetrics(world.rig.service)


def test_a_real_trial_carries_the_v2_fields(world: World) -> None:
    obs = world.run("bug-01-value", split="validation")
    trial = trial_dict(world, obs, "bug-01-value", experiment_ref=experiment(world))
    out = metrics_of(world).trial(trial)
    # 2.10: where the trial came from and what it was
    assert out["source"] == "experiment" and out["calibration_plan_ref"] is None
    assert out["split"] == "validation" and out["stage"] == "focused"
    assert out["task_id"] == "bug-01-value" and out["domain"] == "bug" and out["arm"] == "candidate"
    assert out["cell_id"] == "codex-cli" and out["strategy"] == "repair_loop"
    assert out["success"] is True and out["hidden_passed"] is True
    assert out["verified_hidden_fail"] is False
    # plan 8.1 metrics
    assert out["tokens"] == {"input": 10, "output": 5, "cached_input": None, "reasoning": None}
    assert out["wall_seconds"] == 2.5 and isinstance(out["verification_seconds"], float)
    assert out["cells_used"] == ["codex-cli"] and out["agent_calls"] == 1
    # one executor turn AMPLAI dispatched; provider-internal turns are not recorded (§14 Q14)
    assert out["turns"] == 1 and out["provider_turns"] is None
    assert out["attempts_used"] == 1 and out["nodes"] == 1
    for zero in ("escalations", "reviewer_rounds", "fix_requests", "sub_agents",
                 "integration_conflicts", "re_verifications"):  # fmt: skip
        assert out[zero] == 0, zero  # the repair loop used no other mechanism
    # read from what the loop recorded for the strategy (S9), not constants
    plan = world.rig.service.plan_record(world.receipt(obs)["goal_id"])
    assert plan["strategy_metrics"]["turns"] == 1 and plan["strategy_metrics"]["escalations"] == 0
    assert out["best_of_n_first_pass"] is None and out["fidelity"] is None
    assert out["phase"] == "stage" and out["api_cost"]["status"]
    assert out["guards"] == {
        "ask_back": False, "edit_files": 1, "edit_lines": 2, "broken_tool_calls": None,
        "test_file_edits": 0, "verified_hidden_fail": False,
    }  # fmt: skip
    # the D-094 keys stay
    assert out["attempts"] == 1 and out["diff"]["files"] == 1 and out["seconds"] == 2.5
    assert out["goal_status"] == "verified"


def test_a_change_fitted_to_the_checks_is_a_verified_hidden_fail(world: World) -> None:
    obs = world.run("bug-05-doc")
    assert obs.success is False  # the app's check passed, the hidden test wants a docstring
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-05-doc"))
    assert out["verified_hidden_fail"] is True and out["guards"]["verified_hidden_fail"] is True
    assert out["hidden_passed"] is False and out["success"] is False


def test_an_edited_test_shows_in_the_per_trial_guard(deployment: Any, tmp_path: Path) -> None:
    world = make_world(deployment, tmp_path, "edit-test")
    obs = world.run("bug-01-value")
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert out["guards"]["test_file_edits"] == 1  # the metric stays per trial (IC-18 c)
    assert out["diff"]["tests_changed"] == ["tests/test_visible.py"]
    assert out["guards"]["edit_files"] == 2


def test_the_split_of_the_trial_beats_the_receipt_and_the_sampling_plan(world: World) -> None:
    obs = world.run("bug-01-value", split="holdout")
    ref = experiment(world, split="development", stage="screening")
    metrics = metrics_of(world)
    assert metrics.trial(trial_dict(world, obs, "t", experiment_ref=ref))["split"] == "holdout"
    # with no split on the receipt, the experiment's sampling plan names it
    receipt = world.receipt(obs)
    bare = {k: v for k, v in receipt.items() if k != "split"}
    artifact = world.d.artifacts.admit(
        world.scope, canonical(bare), "application/json", trust="verifier"
    )
    trial = trial_dict(world, obs, "t", experiment_ref=ref, artifact_refs=[artifact])
    out = metrics.trial(trial)
    assert (out["split"], out["stage"]) == ("development", "screening")
    # and the trial's own split wins over both
    assert metrics.trial({**trial, "split": "validation"})["split"] == "validation"


def test_a_calibration_trial_has_no_stage_and_the_calibration_arm(world: World) -> None:
    obs = world.run("bug-01-value", split="development")
    plan_ref = {"id": "cal-1", "revision": 1, "digest": "sha256:" + "1" * 64}
    trial = trial_dict(
        world, obs, "bug-01-value", arm=None, calibration_plan_ref=plan_ref, cell_id="codex-cli"
    )
    out = metrics_of(world).trial(trial)
    assert out["source"] == "calibration" and out["stage"] is None
    assert out["arm"] == CALIBRATION_ARM and out["phase"] == "calibration"
    assert out["calibration_plan_ref"] == plan_ref and out["experiment_ref"] is None
    assert out["split"] == "development"


def test_a_trial_that_left_no_receipt_is_a_no_goal_row(world: World) -> None:
    trial = {
        "trial_id": "trial-fault", "scope": world.scope.wire(), "experiment_ref": None,
        "composition_ref": world.baseline, "task_id": "bug-01-value", "task_class": "bug",
        "arm": "baseline", "repeat": 0, "success": None, "artifact_refs": [],
        "input_tokens": None, "output_tokens": None, "elapsed_ms": 900.0, "cell_id": "codex-cli",
        "split": "development",
    }  # fmt: skip
    out = metrics_of(world).trial(trial)
    assert out["api_cost"] == {"status": "no_goal"} and out["diff"] is None
    assert out["agent_calls"] == 0 and out["attempts_used"] == 0 and out["nodes"] == 0
    assert out["tokens"]["input"] is None and out["verification_seconds"] is None
    assert out["cells_used"] == ["codex-cli"] and out["success"] is None
    assert out["guards"]["edit_files"] == 0 and out["guards"]["ask_back"] is False


def test_a_trial_with_no_elapsed_time_has_unknown_seconds(world: World) -> None:
    obs = world.run("bug-01-value")
    out = metrics_of(world).trial(trial_dict(world, obs, "t", elapsed_ms=None))
    assert out["seconds"] is None and out["wall_seconds"] is None


def test_a_receipt_from_before_s8_gets_its_cell_and_strategy_from_the_composition(
    world: World,
) -> None:
    obs = world.run("bug-01-value")
    receipt = world.receipt(obs)
    old = {k: v for k, v in receipt.items()
           if k not in {"cell_id", "strategy", "planner", "split", "counters_source"}}  # fmt: skip
    artifact = world.d.artifacts.admit(
        world.scope, canonical(old), "application/json", trust="verifier"
    )
    trial = trial_dict(world, obs, "t", artifact_refs=[artifact], split="development")
    out = metrics_of(world).trial(trial)
    assert out["cell_id"] == "codex-cli" and out["cells_used"] == ["codex-cli"]
    assert out["strategy"] == "repair_loop"
    assert out["agent_calls"] == 1  # no planner record: the fixed planner is no agent call


def test_ask_back_is_a_guard_signal_except_on_an_ambiguity_task(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path, planner=QuestionPlanner(["Which value?"]))
    obs = world.run("amb-02-proceed")  # the real planner asked; nothing ran
    metrics = metrics_of(world)
    out = metrics.trial(trial_dict(world, obs, "amb-02-proceed", task_class="bug"))
    assert out["guards"]["ask_back"] is True
    assert out["agent_calls"] == 1 and out["attempts_used"] == 0  # the planner's turn only
    ambiguity = metrics.trial(trial_dict(world, obs, "amb-02-proceed", task_class="ambiguity"))
    assert ambiguity["guards"]["ask_back"] is False  # asking back is the point of the task


def test_the_real_planners_turn_counts_as_an_agent_call_beside_the_run(
    deployment: Any, tmp_path: Path
) -> None:
    world = make_world(deployment, tmp_path, planner=QuestionPlanner([]))
    obs = world.run("amb-02-proceed")
    out = metrics_of(world).trial(trial_dict(world, obs, "amb-02-proceed", task_class="ambiguity"))
    assert out["agent_calls"] == 2 and out["attempts_used"] == 1
    assert out["guards"]["ask_back"] is False


def test_the_strategy_fields_come_from_the_plans_strategy_record(world: World) -> None:
    """``escalations`` ... ``re_verifications`` and ``turns`` are the plan record's
    ``strategy_metrics`` (when it was written for the plan as it is now), never constants."""
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    recorded = {**plan["strategy_metrics"], "reviewer_rounds": 2, "fix_requests": 3,
                "sub_agents": 4, "integration_conflicts": 1, "re_verifications": 5,
                "best_of_n_first_pass": 1, "turns": 3}  # fmt: skip
    world.rig.service._save_plan(goal, {**plan, "strategy_metrics": recorded})
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    for key in ("reviewer_rounds", "fix_requests", "sub_agents", "integration_conflicts",
                "re_verifications", "best_of_n_first_pass", "turns"):  # fmt: skip
        assert out[key] == recorded[key], key
    trial_ref = stored(world, trial_dict(world, obs, "bug-01-value", split="development"))
    value = world.store.get(world.scope, trial_metrics.KIND, metrics_of(world).record(trial_ref))
    assert value["turns"] == 3 and value["fix_requests"] == 3
    assert "provider_turns" not in value  # §2.10 stores the executor turns only


def test_auxiliary_turns_count_as_agent_calls_and_in_the_token_classes(world: World) -> None:
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    aux = [
        {"role": "reviewer", "cell_id": "codex-cli", "tokens": 100, "error": None,
         "usage": {"input_tokens": 60, "output_tokens": 40, "cached_input_tokens": 8,
                   "reasoning_output_tokens": 2}},
        {"role": "reviewer", "cell_id": "codex-cli", "tokens": 0, "usage": None,
         "error": "TURN_FAILED"},  # never started: no agent call, no tokens
    ]  # fmt: skip
    world.rig.service._save_plan(
        goal, {k: v for k, v in {**plan, "aux_usage": aux}.items() if k != "strategy_metrics"}
    )
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert out["agent_calls"] == 2  # the run's process and the one reviewer turn that ran
    assert out["turns"] == 1  # a read-only turn is not an executor turn
    # the run reports no token classes in this rig, so the sums stay unknown
    assert out["tokens"]["cached_input"] is None and out["tokens"]["reasoning"] is None
    # an auxiliary turn without the classes makes them unknown even when the runs report them
    rows = metrics_of(world)._tokens(
        {"input_tokens": 1, "output_tokens": 1}, [], {}, None,
        [{**aux[0], "usage": {"input_tokens": 60, "output_tokens": 40}}],
    )  # fmt: skip
    assert rows["cached_input"] is None and rows["reasoning"] is None
    summed = metrics_of(world)._tokens({"input_tokens": 1, "output_tokens": 1}, [], {}, None, aux)
    assert (summed["cached_input"], summed["reasoning"]) == (8, 2)


def test_vote_candidates_beyond_the_runs_own_turn_count_as_agent_calls(world: World) -> None:
    """S9b (§5.1 M4, "Clarifications ... S9b"): a vote trial's ``agent_calls`` counts every
    candidate turn. Candidates 1..k-1 are their own processes without a worker-execution row, so
    the row adds the strategy record's ``candidate_turns`` to the started executions."""
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    base = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    voted = [{**a, "candidates": 3, "selected": 1} for a in plan["attempts"]]
    world.rig.service._save_plan(
        goal,
        {k: v for k, v in {**plan, "attempts": voted}.items() if k != "strategy_metrics"},
    )
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert out["agent_calls"] == base["agent_calls"] + 2  # candidates 1 and 2 of the attempt
    assert out["turns"] == base["turns"] + 2  # as the strategy record counts executor turns
    # a candidate generation the 80 % stop cut to the run's own turn adds nothing
    single = [{**a, "candidates": 1, "selected": 0} for a in plan["attempts"]]
    world.rig.service._save_plan(
        goal,
        {k: v for k, v in {**plan, "attempts": single}.items() if k != "strategy_metrics"},
    )
    again = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert again["agent_calls"] == base["agent_calls"] and again["turns"] == base["turns"]
    # the stored strategy record is what the row reads when it is current
    world.rig.service._save_plan(
        goal, {**plan, "strategy_metrics": {**plan["strategy_metrics"], "candidate_turns": 1}}
    )
    stored_row = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert stored_row["agent_calls"] == base["agent_calls"] + 1


def test_a_vote_trial_row_records_its_candidates_and_the_selected_index(world: World) -> None:
    """§5.2 row 10: vote records ``candidates`` and the selected index in the §2.10 row; another
    strategy's row carries 0 / 0 / null, and a stored row from before S9b stays valid."""
    obs = world.run("bug-01-value", split="validation")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    trial = trial_dict(world, obs, "bug-01-value", experiment_ref=experiment(world))
    plain = metrics_of(world).trial(trial)
    assert (plain["candidates"], plain["candidate_turns"], plain["vote_selected"]) == (0, 0, None)
    voted = [{**a, "candidates": 3, "selected": 2} for a in plan["attempts"]]
    world.rig.service._save_plan(
        goal,
        {k: v for k, v in {**plan, "attempts": voted}.items() if k != "strategy_metrics"},
    )
    out = metrics_of(world).trial(trial)
    assert (out["candidates"], out["candidate_turns"], out["vote_selected"]) == (3, 2, 2)
    value = world.store.get(
        world.scope, trial_metrics.KIND, metrics_of(world).record(stored(world, trial))
    )
    validate_record(value)
    assert (value["candidates"], value["candidate_turns"], value["vote_selected"]) == (3, 2, 2)
    # a row written before S9b has none of the vote fields and is still a valid row
    old = {k: v for k, v in value.items() if k not in trial_metrics.VOTE_FIELDS}
    validate_record(old)
    validate_record(
        {**valid_record(), "candidates": 0, "candidate_turns": 0, "vote_selected": None}
    )
    for bad in ({"candidates": -1}, {"vote_selected": 1.5}, {"candidate_turns": None}):
        with pytest.raises(RuntimeFault):
            validate_record({**valid_record(), **bad})


def test_a_goal_with_an_earlier_revision_is_not_priced_at_the_last_model(world: World) -> None:
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    earlier = {**plan, "previous_attempts": list(plan["attempts"])}
    world.rig.service._save_plan(goal, earlier)
    out = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))
    assert out["api_cost"]["status"] == "revisions_unpriced"
    assert out["attempts_used"] == 2 and out["attempts"] == 2  # both revisions count
    # without an earlier revision the run is priced as before
    world.rig.service._save_plan(goal, plan)
    assert (
        metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))["api_cost"]["status"]
        != "revisions_unpriced"
    )


# -- record: one trial-metrics record per trial --
def test_record_writes_a_validated_trial_metrics_record(world: World) -> None:
    obs = world.run("bug-01-value", split="validation")
    trial = trial_dict(world, obs, "bug-01-value", experiment_ref=experiment(world))
    trial_ref = stored(world, trial)
    metrics = metrics_of(world)
    ref = metrics.record(trial_ref)
    assert ref["id"] == "tm-trial-bug-01-value" and ref["revision"] == 1
    value = world.store.get(world.scope, trial_metrics.KIND, ref)
    validate_record(value)
    assert value["trial_ref"] == trial_ref and value["experiment_ref"] == trial["experiment_ref"]
    assert (value["split"], value["stage"], value["source"]) == (
        "validation",
        "focused",
        "experiment",
    )
    assert value["task_id"] == "bug-01-value" and value["domain"] == "bug"
    assert value["cell_id"] == "codex-cli" and value["strategy"] == "repair_loop"
    assert value["tokens"]["input"] == 10 and value["wall_seconds"] == 2.5
    assert "diff" not in value and "goal_status" not in value  # only the 2.10 fields are stored
    # recomputing writes a new store revision under the same id
    again = metrics.record(trial_ref)
    assert again["id"] == ref["id"] and again["revision"] == 2


def test_record_stores_a_calibration_trial_without_a_stage(world: World) -> None:
    obs = world.run("bug-01-value", split="development")
    plan_ref = {"id": "cal-1", "revision": 1, "digest": "sha256:" + "1" * 64}
    trial = trial_dict(
        world, obs, "bug-01-value", trial_id="trial-cal", arm=None, calibration_plan_ref=plan_ref
    )
    trial_ref = stored(world, trial, "calibration-trial")
    value = world.store.get(world.scope, trial_metrics.KIND, metrics_of(world).record(trial_ref))
    assert value["source"] == "calibration" and value["stage"] is None
    assert value["arm"] == CALIBRATION_ARM and value["phase"] == "calibration"
    assert value["calibration_plan_ref"] == plan_ref and value["experiment_ref"] is None


def test_record_refuses_what_is_not_a_trial(world: World) -> None:
    with pytest.raises(Hold) as refused:
        metrics_of(world).record(world.baseline)  # a composition
    assert refused.value.code == "TRIAL_METRICS_SOURCE"
    with pytest.raises(Hold) as unknown:
        metrics_of(world).record({"id": "nothing", "revision": 1, "digest": "sha256:" + "2" * 64})
    assert unknown.value.code == "REFERENCE_UNRESOLVED"


def test_record_refuses_a_trial_whose_split_is_unknown(world: World) -> None:
    obs = world.run("bug-01-value", split=None)  # an unassigned split leaves none on the receipt
    plan_ref = {"id": "cal-2", "revision": 1, "digest": "sha256:" + "3" * 64}
    trial = trial_dict(world, obs, "no-split", arm=None, calibration_plan_ref=plan_ref)
    trial_ref = stored(world, trial, "calibration-trial")
    with pytest.raises(RuntimeFault) as refused:
        metrics_of(world).record(trial_ref)
    assert refused.value.code == "TRIAL_METRICS"  # never a guessed split
    written = world.store.list_objects(world.scope, trial_metrics.KIND)
    assert all(ref["id"] != "tm-trial-no-split" for ref, _ in written)  # nothing was stored


# -- what a goal ran: the helpers the executor counts from --
def test_run_helpers_read_the_goals_runs_and_executions(world: World) -> None:
    obs = world.run("bug-01-value")
    plan = world.rig.service.plan_record(world.receipt(obs)["goal_id"])
    run_ids = goal_run_ids(world.store, world.scope, plan)
    assert run_ids == [a["run_id"] for a in plan["attempts"]] and len(run_ids) == 1
    (execution,) = run_executions(world.store, world.scope, run_ids)
    assert execution["data"]["driver_handle"] and execution["state"] != "held"
    assert run_executions(world.store, world.scope, []) == []
    assert run_executions(world.store, world.scope, ["run-nothing"]) == []
    assert open_effects(world.store, world.scope, run_ids) == []
    assert open_effects(world.store, world.scope, []) == []


def test_goal_run_ids_include_a_claimed_run_with_no_attempt(world: World) -> None:
    """A claimed run discarded before its turn is still one of the goal's runs (graph works)."""
    from rc06_rig import submit

    service = world.rig.service
    goal = submit(world.rig, "claimed but never attempted: make value return 2")
    service.plan(goal)
    service.approve(world.rig.operator, goal)
    dispatch = service.runtime.claim(world.rig.actors.worker, goal_id=goal)
    plan = service.plan_record(goal)
    assert plan.get("attempts") in (None, [])
    assert goal_run_ids(world.store, world.scope, plan) == [dispatch["run_id"]]


def test_open_effects_lists_only_unsettled_effects_of_the_named_runs(world: World) -> None:
    store, scope = world.store, world.scope
    with store.tx() as db:
        for effect_id, state, run_id in (
            ("eff-open", "dispatched", "run-a"), ("eff-unknown", "unknown", "run-a"),
            ("eff-done", "observed", "run-a"), ("eff-other", "dispatched", "run-b"),
        ):  # fmt: skip
            store.cas(db, scope, "effect", effect_id, 0, state,
                      {"request": {"run_id": run_id, "contract_ref": None}})  # fmt: skip
    assert open_effects(store, scope, ["run-a"]) == ["eff-open", "eff-unknown"]
    assert open_effects(store, scope, ["run-a", "run-b"]) == [
        "eff-open",
        "eff-unknown",
        "eff-other",
    ]
    assert open_effects(store, scope, ["run-c"]) == []


def test_strategy_and_cell_helpers_fall_back_to_none_for_a_stranger(world: World) -> None:
    service = world.rig.service
    assert trial_metrics.strategy_of(service, world.baseline) == "repair_loop"
    assert trial_metrics.cell_of(service, "app", world.baseline) == "codex-cli"
    assert trial_metrics.cell_of(service, "no-such-app", world.baseline) is None
    router = service.apps["app"].router_ref
    assert trial_metrics.strategy_of(service, router) is None  # not a composition
    assert json.dumps(trial_metrics.TRIAL_KINDS) == '["eval-trial", "calibration-trial"]'


# -- api_cost: auxiliary read-only turns are priced, never left out (§5.3 aux_usage, §8.3) --
def aux_entry(cell_id: str = "codex-cli", **over: Any) -> dict[str, Any]:
    """An ``aux_usage`` entry as ``strategy_runner.AuxLedger`` records a turn that ran."""
    value: dict[str, Any] = {
        "role": "reviewer", "purpose": "review", "cell_id": cell_id, "node_id": None,
        "tokens": 100, "usage": {"input_tokens": 60, "output_tokens": 40}, "error": None,
    }  # fmt: skip
    value.update(over)
    return value


def cost_with(world: World, obs: Any, aux: list[dict[str, Any]]) -> dict[str, Any]:
    """The trial's ``api_cost`` with the plan record's ``aux_usage`` set to ``aux``."""
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    world.rig.service._save_plan(
        goal, {k: v for k, v in {**plan, "aux_usage": aux}.items() if k != "strategy_metrics"}
    )
    cost: dict[str, Any] = metrics_of(world).trial(trial_dict(world, obs, "bug-01-value"))[
        "api_cost"
    ]
    return cost


def register_cell(world: World, cell_id: str, model: str) -> None:
    """A ``harness-cell`` record in the cell registry (what ``CellInstaller.install`` leaves)."""
    from amplai_foundry.runtime.execution.cells import CELL_KIND, CellInstaller

    with world.store.tx() as db:
        ref = world.store.put(
            db, world.scope, CELL_KIND, cell_id, 1,
            {"cell_id": cell_id, "provider_model_id": model},
        )  # fmt: skip
    CellInstaller(world.store, world.scope)._register(cell_id, ref)


def test_an_auxiliary_turn_that_ran_is_priced_beside_the_run(world: World) -> None:
    obs = world.run("bug-01-value")
    base = cost_with(world, obs, [])
    assert base["status"] == "estimated"
    # the goal's own cell (the composition's): 60 input + 40 output tokens at its model's price
    one = cost_with(world, obs, [aux_entry()])
    assert one["status"] == "estimated" and one["cost_microunits"] > base["cost_microunits"]
    assert one["upper_bound"] is True and "no_cache_breakdown" in one["notes"]
    table = next(t for t in metrics_of(world).tables if t.table_id == base["price_table_id"])
    expected = table.models[base["model"]]
    assert one["cost_microunits"] - base["cost_microunits"] == round(
        (60 * expected["input"] + 40 * expected["output"]) / 1_000_000
    )
    # two turns cost more than one: every turn is priced, not just the first
    two = cost_with(world, obs, [aux_entry(), aux_entry(role="investigator")])
    assert two["cost_microunits"] > one["cost_microunits"]
    # an installed cell other than the goal's is priced at its own model (its harness-cell)
    register_cell(world, "codex-cli.lead.high", base["model"])
    lead = cost_with(world, obs, [aux_entry("codex-cli.lead.high", role="lead")])
    assert lead["status"] == "estimated" and lead["cost_microunits"] == one["cost_microunits"]


def test_a_turn_that_never_started_leaves_the_price_unchanged(world: World) -> None:
    obs = world.run("bug-01-value")
    base = cost_with(world, obs, [])
    never = aux_entry("no-such-cell", tokens=0, usage=None, error="TURN_FAILED")
    assert cost_with(world, obs, [never]) == base


def test_an_auxiliary_turn_with_unknown_usage_is_never_a_plain_estimate(world: World) -> None:
    obs = world.run("bug-01-value")
    unknown = aux_entry(tokens=None, usage=None, error="TURN_TIMEOUT")
    out = cost_with(world, obs, [aux_entry(), unknown])
    assert out["status"] == "aux_unknown_usage" and "cost_microunits" not in out
    # the arm then reports no per-solved cost instead of a run-only figure
    rows = [{**row("a", True, "generator_reviewer"), "api_cost": out}]
    summary = TrialMetrics.summarize(rows)["a"]
    assert summary["api_cost_usd_per_solved"] is None and summary["api_cost_priced"] == "0/1"


def test_an_auxiliary_turn_on_an_unresolvable_cell_is_unpriced(world: World) -> None:
    obs = world.run("bug-01-value")
    out = cost_with(world, obs, [aux_entry("codex-cli.unknown.high")])
    assert out["status"] == "aux_unpriced" and "cost_microunits" not in out
    assert cost_with(world, obs, [aux_entry("")])["status"] == "aux_unpriced"
    # a cell whose model has no price is reported as such, never priced at another model
    register_cell(world, "codex-cli.unpriced.high", "no-such-model")
    assert cost_with(world, obs, [aux_entry("codex-cli.unpriced.high")])["status"] == "no_price"


def test_an_earlier_revision_stays_unpriced_whatever_the_auxiliary_turns(world: World) -> None:
    obs = world.run("bug-01-value")
    goal = world.receipt(obs)["goal_id"]
    plan = world.rig.service.plan_record(goal)
    world.rig.service._save_plan(goal, {**plan, "previous_attempts": list(plan["attempts"])})
    assert cost_with(world, obs, [aux_entry()])["status"] == "revisions_unpriced"
