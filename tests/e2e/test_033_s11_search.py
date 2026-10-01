"""Work 033 S11 (AC-08 unit part): `amplai meta search` end to end through the operator's gates.

Real: the rc06 local product with `LocalMeta` (real `MetaHarness`, real `EvaluationService`, the
real `reference_validator` and leak-gate hook), a frozen corpus v2 with its task and leak index,
`LocalMetaOps`, `StageRunner`, the existing gates after the holdout stage, and the `amplai meta`
command line over a real `amplai ops local-init` deployment. Stand-in: the trial executor (scripted
outcomes, receipts of the receipt v2 shape; the world builder lives in
`tests/v3/test_033_s11_stages.py`) and the calibration summary of the stage plan.

Covered: a class B candidate from `propose_components` through review, `search` (stops at the
review, then at the focused gate), `approve-stage focused`, `search` (ablation of derived proposals,
stops at the holdout gate), `approve-stage holdout` (the one experiment bound to the evolution
machine) and `approve_canary` (the bound report passes the existing canary gate); calibration
(`calibrate`, `calibration_show`, `latest_summary`, the IC-20 hold after a saturated calibration);
the app of a corpus with a main and a regression set on two installed apps; `CORPUS_CHANGED` when
a task or the version loaded now differs from the frozen corpus (plan, focused gate, calibration);
the refusals of the meta operations; the command line (registration, options, exit codes 2 and 3,
the corpus, component, review, stage and calibration commands that need no real driver).
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v3"))

from test_033_s11_stages import (
    BUDGET,
    CELL,
    SHA,
    SPLIT_COUNTS,
    TRIAL_TOKENS,
    World,
    build_world,
    fault,
    hold,
    split_of,
    task_ids,
    write_corpus,
)

from amplai_foundry.meta_harness import corpus_v2
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps
from amplai_foundry.runtime.execution.product import AppConfig
from rc06_rig import codex_inputs, make_repo

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


def by_stage(steps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["stage"]: s for s in steps}


# ==================================================================================================
# AC-08: search runs the automatic stages and stops at every operator gate
# ==================================================================================================
def test_ac08_a_class_b_candidate_goes_through_every_gate_to_an_approved_canary(w: World) -> None:
    pid = w.propose("b2")
    w.script(pid, baseline=(2, 2), candidate=(4, 4))
    assert w.state(pid) == "draft"

    # 1. class B: search plans the stages and stops at the operator's code review
    first = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    steps = by_stage(first["steps"])
    assert first["proposal_id"] == pid and first["stopped"] is None
    assert first["plan_ref"]["id"] == "stageplan-" + pid
    assert (
        steps["screening"]["state"] == "pending" and steps["screening"]["waiting_for"] == "review"
    )
    assert w.executor.calls == [] and w.state(pid) == "draft"
    assert w.ops.stages(pid)["steps"][0]["waiting_for"] == "review"

    # 2. after the review the automatic screening runs (12 tasks, exploratory) and the search
    #    stops at the focused gate
    w.ops.review(pid, outcome="pass", note="read the attempt policy and the bootstrap facts")
    second = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert second["plan_ref"] == first["plan_ref"]  # the plan is made once per proposal
    steps = by_stage(second["steps"])
    assert steps["screening"]["state"] == "passed" and steps["screening"]["decision_class"]
    assert steps["focused"]["state"] == "waiting_approval"
    assert steps["focused"]["waiting_for"] == "approve-stage focused"
    assert steps["ablation"]["state"] == "pending" and steps["holdout"]["state"] == "pending"
    assert w.state(pid) == "screened"
    screening_trials = w.trials(steps["screening"]["report_ref"])
    assert len({t["task_id"] for t in screening_trials}) == 12  # the 12-task screening
    # searching again changes nothing and runs no trial again
    calls = len(w.executor.calls)
    assert by_stage(w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)["steps"]) == steps
    assert len(w.executor.calls) == calls

    # 3. the focused gate: a budget-matched reference arm (the candidate spent twice the tokens)
    focused = w.ops.approve_stage(pid, "focused")
    assert focused["proposal_id"] == pid and focused["stage"] == "focused"
    assert focused["state"] == "passed" and focused["decision_class"] == "non_inferior"
    assert w.sampling(focused["experiment_ref"])["arms"] == ["baseline", "candidate", "reference"]

    # 4. search runs the ablation of the derived proposals and stops at the holdout gate
    third = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    steps = by_stage(third["steps"])
    assert steps["ablation"]["state"] == "passed"
    assert steps["holdout"]["state"] == "waiting_approval"
    assert steps["holdout"]["waiting_for"] == "approve-stage holdout"
    derived = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    assert len(derived) == 2 and {w.state(d) for d in derived} == {"draft"}
    assert w.state(pid) == "screened"  # IC-01: nothing is bound to the machine yet

    # 5. the holdout gate: the one experiment bound to the evolution machine
    holdout = w.ops.approve_stage(pid, "holdout")
    assert holdout["state"] == "passed" and holdout["decision_class"] == "non_inferior"
    status = w.ops.status(pid)
    assert status["state"] == "offline_evaluated"
    assert w.head(pid)["data"]["experiment_ref"] == holdout["experiment_ref"]
    assert [s["state"] for s in w.ops.stages(pid)["steps"]] == ["passed"] * 4
    assert w.ops.stages(pid)["state"] == "offline_evaluated"

    # 6. the existing gates continue from there: the bound report passes the canary gate
    # a development task of the plan's app (a holdout task is never a canary task)
    task = task_ids("development")[0]
    canary = w.ops.approve_canary(pid, [task], max_trial_tokens=TRIAL_TOKENS)
    assert canary["tasks"] == [task] and w.ops.status(pid)["state"] == "canary_approved"


def test_ac08_a_class_a_candidate_needs_no_review_and_stops_at_the_focused_gate(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    result = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    steps = by_stage(result["steps"])
    assert steps["screening"]["state"] == "passed"
    assert steps["focused"]["waiting_for"] == "approve-stage focused"
    assert result["stopped"] is None
    # equal token use: no reference arm in focused
    focused = w.ops.approve_stage(pid, "focused")
    assert w.sampling(focused["experiment_ref"])["arms"] == ["baseline", "candidate"]


def test_ac08_search_stops_where_the_root_budget_cannot_cover_the_next_stage(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    result = w.ops.search(pid, cell_id=CELL, root_budget={**BUDGET, "max_tokens": 100})
    assert (
        result["stopped"]["code"] == "META_TOKEN_BUDGET"
        and result["stopped"]["stage"] == "screening"
    )
    assert by_stage(result["steps"])["screening"]["state"] == "pending"
    assert w.executor.calls == []


def test_ac08_search_stops_on_a_regression_and_the_rejected_candidate_reaches_no_gate(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid, lambda role, case, repeat: role != "candidate")
    result = w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    steps = by_stage(result["steps"])
    assert (
        steps["screening"]["state"] == "failed"
        and steps["screening"]["decision_class"] == "regression"
    )
    assert w.ops.status(pid)["state"] == "rejected"
    assert w.ops.status(pid)["rejection"]["reason"].startswith("screening: ")
    assert steps["focused"]["waiting_for"] is None
    hold("META_STATE", w.ops.approve_stage, pid, "focused")


def test_ac08_a_search_with_another_root_budget_or_cell_is_a_new_root_and_refused(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    hold(
        "META_BUDGET_CHANGE",
        w.ops.search,
        pid,
        cell_id=CELL,
        root_budget={**BUDGET, "max_tokens": 9},
    )


def test_approve_stage_and_stage_runner_need_a_plan(w: World) -> None:
    pid = w.propose("a2")
    hold("META_STATE", w.ops.approve_stage, pid, "focused")
    hold("META_STATE", w.ops.stage_runner, pid)  # no plan and no cell
    fault("STAGE_PLAN", w.ops.search, pid, cell_id=CELL, root_budget={**BUDGET, "max_attempts": 99})


def test_the_corpus_v2_proposal_runs_stages_not_the_work_030_experiment(w: World) -> None:
    pid = w.propose("a2")
    w.ops.screen(pid)
    error = hold("META_STATE", w.ops.approve_experiment, pid, max_tokens=10, max_wall_seconds=10)
    assert "stages" in str(error)


# -- a stage runner picks up what its plan pinned -------------------------------------------------
def test_stage_runner_keeps_the_plan_pins_when_a_newer_summary_exists(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    newer = w.summary({"validation": 0})  # a later calibration that would break the plan
    assert newer != w.summary_ref
    assert w.ops.latest_summary(w.refs["corpus_ref"], CELL, w.version_ref) == newer
    runner = w.ops.stage_runner(pid)
    assert runner.summary_ref == w.summary_ref and runner.version_ref == w.version_ref
    # a new proposal takes the newest summary, whose validation tasks are saturated: IC-20
    other = w.propose("a1")
    error = hold("NO_INFORMATIVE_TASKS", w.ops.search, other, cell_id=CELL, root_budget=BUDGET)
    assert error.details == {"stage": "focused", "available": 0, "need": 16}


def test_latest_summary_needs_the_cell_the_corpus_and_the_evaluator_version(w: World) -> None:
    assert w.ops.latest_summary(w.refs["corpus_ref"], CELL, w.version_ref) == w.summary_ref
    assert w.ops.latest_summary(w.refs["corpus_ref"], "claude-cli", w.version_ref) is None
    assert w.ops.latest_summary(w.refs["leak_index_ref"], CELL, w.version_ref) is None
    other = {**w.version_ref, "revision": 7}
    assert w.ops.latest_summary(w.refs["corpus_ref"], CELL, other) is None


def test_frozen_corpus_names_the_refs_of_the_loaded_corpus_version(w: World) -> None:
    assert w.ops.frozen_corpus() == w.refs
    assert w.ops.frozen_corpus(w.refs["corpus_ref"]) == w.refs
    w.ops.corpus = replace(w.ops.corpus, version="9.9.9")
    error = hold("META_STATE", w.ops.frozen_corpus)
    assert error.details == {"corpus_id": "s11-corpus", "version": "9.9.9"}
    hold("META_STATE", w.ops.search, w.propose("a2"), cell_id=CELL, root_budget=BUDGET)


# -- a corpus with both sets (the real manifest: main -> bench app, regression -> demo app) --------
DEMO_APP = "demo-app"


def with_regression_set(corpus: corpus_v2.CorpusV2) -> corpus_v2.CorpusV2:
    """The loaded corpus plus a regression task on a base of another app (§10.6), as in
    `specs/033-harness-taxonomy/corpus/manifest.json` (bench: amplai-bench-app, demo:
    amplai-demo-app)."""
    regression = replace(
        corpus.tasks[0], task_id="work030-r01", set="regression", base_id="demo",
        split="validation",
    )  # fmt: skip
    bases = {**corpus.bases, "demo": {"dir": "bases/bench", "base_commit": SHA,
                                      "app_id": DEMO_APP}}  # fmt: skip
    return replace(corpus, bases=bases, tasks=(*corpus.tasks, regression))


def test_with_both_sets_installed_the_operations_use_the_main_sets_app(w: World) -> None:
    service = w.rig.service
    service.install(AppConfig(DEMO_APP, w.rig.repo, service.apps["app"].config.verifiers))
    assert {"app", DEMO_APP} <= set(service.apps)  # both apps of the corpus are installed
    w.ops = LocalMetaOps(w.dep, with_regression_set(w.corpus), w.executor, driver=CELL)  # type: ignore[arg-type]
    assert w.ops.app is service.apps["app"]
    pid = w.propose("a2")  # propose_components needs the app
    w.script(pid)
    steps = by_stage(w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)["steps"])
    assert steps["screening"]["state"] == "passed"
    assert w.ops.approve_stage(pid, "focused")["state"] == "passed"
    # the regression task is never a stage case (§10.6: it freezes as its own corpus)
    assert "work030-r01" not in (w.ops.app_case_ids() or ())
    assert sampled_case_ids(w) and "work030-r01" not in sampled_case_ids(w)
    # a main set whose app is not installed still names no app
    lone = replace(w.corpus, bases={"bench": {**w.corpus.bases["bench"], "app_id": "nope"}})
    error = hold("TARGET_UNKNOWN", lambda: LocalMetaOps(w.dep, lone, w.executor, driver=CELL).app)  # type: ignore[arg-type]
    assert error.details == {"installed": [], "named": ["nope"]}


def sampled_case_ids(w: World) -> set[str]:
    """Every case id a stage experiment or a calibration plan sampled."""
    return {
        case_id
        for kind in ("sampling-plan", "calibration-plan")
        for _, value in w.objects(kind)
        for case_id in value.get("case_ids") or ()
    }


def two_main_apps(corpus: corpus_v2.CorpusV2) -> corpus_v2.CorpusV2:
    """A main set whose app-environment tasks name two apps (a second base of the demo app)."""
    task = replace(corpus.tasks[0], task_id="bug-dev-demo", base_id="demo")
    bases = {**corpus.bases, "demo": {"dir": "bases/bench", "base_commit": SHA,
                                      "app_id": DEMO_APP}}  # fmt: skip
    return replace(corpus, bases=bases, tasks=(*corpus.tasks, task))


def ops_for(w: World, corpus: corpus_v2.CorpusV2, app_id: str | None = None) -> LocalMetaOps:
    return LocalMetaOps(w.dep, corpus, w.executor, driver=CELL, app_id=app_id)  # type: ignore[arg-type]


def test_an_ambiguous_main_set_is_refused_explicitly_and_the_app_option_resolves_it(
    w: World,
) -> None:
    service = w.rig.service
    service.install(AppConfig(DEMO_APP, w.rig.repo, service.apps["app"].config.verifiers))
    both = two_main_apps(w.corpus)
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, both).app)
    assert "--app" in error.message
    assert error.details == {"installed": ["app", DEMO_APP], "named": ["app", DEMO_APP]}
    assert ops_for(w, both, "app").app is service.apps["app"]
    assert ops_for(w, both, DEMO_APP).app is service.apps[DEMO_APP]


def test_the_app_option_must_be_installed_and_named_by_the_main_set(w: World) -> None:
    service = w.rig.service
    service.install(AppConfig(DEMO_APP, w.rig.repo, service.apps["app"].config.verifiers))
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, w.corpus, "nope").app)
    assert error.details == ["nope"]
    # installed, but only the regression set names it: stages never run on it
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, with_regression_set(w.corpus), DEMO_APP).app)
    assert error.details == {"app": DEMO_APP, "named": ["app"]}
    # the single named app needs no option, and naming it changes nothing
    assert ops_for(w, w.corpus).app is ops_for(w, w.corpus, "app").app is service.apps["app"]


# -- --app: stages and calibration select the selected app's main-set tasks only -----------------
DEMO_TASKS = ("bug-dev-demo", "bug-val-demo", "bug-hol-demo")


def frozen_with_demo_tasks(w: World) -> corpus_v2.CorpusV2:
    """Version 2.0.0 of the corpus, frozen: the main set plus a development, a validation and a
    holdout task on a base of the (installed) demo app; the evaluator version and the
    calibration summary (the demo tasks informative too) are of the new frozen corpus."""
    service = w.rig.service
    service.install(AppConfig(DEMO_APP, w.rig.repo, service.apps["app"].config.verifiers))
    demo = tuple(
        replace(next(t for t in w.corpus.tasks if t.split == split), task_id=task_id,
                base_id="demo")
        for split, task_id in zip(("development", "validation", "holdout"), DEMO_TASKS,
                                  strict=True)
    )  # fmt: skip
    bases = {**w.corpus.bases, "demo": {"dir": "bases/bench", "base_commit": SHA,
                                        "app_id": DEMO_APP}}  # fmt: skip
    corpus = replace(w.corpus, version="2.0.0", bases=bases, tasks=(*w.corpus.tasks, *demo))
    w.refs = corpus_v2.freeze(w.operator, w.store, w.artifacts, corpus, holdout_use_limit=50)
    w.executor.corpus_version = "2.0.0"
    w.version_ref = w.evaluator()
    w.summary_ref = w.summary(extra=DEMO_TASKS[:2])
    frozen = w.store.get(w.scope, "eval-corpus", w.refs["corpus_ref"])
    assert set(DEMO_TASKS) <= {c["case_id"] for c in frozen["cases"]}
    return corpus


def test_with_app_every_stage_runs_only_the_selected_apps_main_set_tasks(w: World) -> None:
    corpus = frozen_with_demo_tasks(w)
    w.ops = ops_for(w, corpus, "app")
    assert w.ops.app_case_ids() == {t.task_id for t in w.corpus.tasks}
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert w.ops.approve_stage(pid, "focused")["state"] == "passed"
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert w.ops.approve_stage(pid, "holdout")["state"] == "passed"
    assert [s["state"] for s in w.ops.stages(pid)["steps"]] == ["passed"] * 4
    sampled = sampled_case_ids(w)
    # every split was sampled, and never a task of the demo app
    assert {split_of(c) for c in sampled} == {"development", "validation", "holdout"}
    assert not sampled & set(DEMO_TASKS)
    assert not {c[1] for c in w.executor.calls} & set(DEMO_TASKS)


def test_with_app_the_other_apps_tasks_count_for_no_stage_of_it(w: World) -> None:
    corpus = frozen_with_demo_tasks(w)
    w.ops = ops_for(w, corpus, DEMO_APP)
    assert w.ops.app_case_ids() == set(DEMO_TASKS)
    pid = w.propose("a2")
    # the demo app has one informative validation task: the bench app's 18 do not count
    error = hold("NO_INFORMATIVE_TASKS", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    assert error.details == {"stage": "focused", "available": 1, "need": 16}
    assert w.objects("stage-plan") == [] and w.executor.calls == []


# -- the loaded corpus must be the frozen one (the trial executor runs the loaded tasks) -----------
def edited(corpus: corpus_v2.CorpusV2, ids: list[str], **fields: Any) -> corpus_v2.CorpusV2:
    tasks = tuple(replace(t, **fields) if t.task_id in ids else t for t in corpus.tasks)
    return replace(corpus, tasks=tasks)


def test_a_task_edited_after_the_freeze_holds_the_plan_before_anything_is_planned(
    w: World,
) -> None:
    pid = w.propose("a2")
    holdout = task_ids("holdout")[0]
    w.ops.corpus = edited(w.ops.corpus, [holdout], hidden={"test_other.py": b"def test_x(): 0\n"})
    error = hold("CORPUS_CHANGED", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    assert error.details == {"corpus_ref": w.refs["corpus_ref"], "cases": [holdout]}
    assert w.objects("stage-plan") == [] and w.executor.calls == []


def test_a_contract_edited_after_screening_holds_the_focused_gate_before_its_freeze(
    w: World,
) -> None:
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    experiments, calls = len(w.objects("eval-experiment")), len(w.executor.calls)
    validation = task_ids("validation")
    w.ops.corpus = edited(w.ops.corpus, validation, objective="Make add return the product.")
    error = hold("CORPUS_CHANGED", w.ops.approve_stage, pid, "focused")
    changed = error.details["cases"]
    assert len(changed) == 16 and set(changed) <= set(validation)  # the 16 selected cases
    assert len(w.objects("eval-experiment")) == experiments and len(w.executor.calls) == calls
    assert by_stage(w.ops.stages(pid)["steps"])["focused"]["state"] == "waiting_approval"
    # restored, the gate runs
    w.ops.corpus = w.corpus
    assert w.ops.approve_stage(pid, "focused")["state"] == "passed"


def test_a_version_bumped_after_the_plan_holds_every_later_stage(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    w.ops.corpus = replace(w.ops.corpus, version="1.0.1")
    error = hold("CORPUS_CHANGED", w.ops.approve_stage, pid, "focused")
    assert error.details == {
        "corpus_id": "s11-corpus", "frozen_version": "1.0.0", "loaded_version": "1.0.1",
    }  # fmt: skip
    hold("CORPUS_CHANGED", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    hold("CORPUS_CHANGED", w.ops.check_corpus, w.refs["corpus_ref"])


def test_calibrate_holds_a_task_edited_after_the_freeze_before_anything_runs(w: World) -> None:
    development = task_ids("development")[3]
    plans = len(w.objects("calibration-plan"))  # the world's summary fixture wrote one
    w.ops.corpus = edited(w.ops.corpus, [development], acceptance=("add(2, 2) is 4",))
    error = hold("CORPUS_CHANGED", calibrate, w)
    assert error.details["cases"] == [development]
    assert w.executor.calls == [] and len(w.objects("calibration-plan")) == plans


def test_a_task_removed_from_the_loaded_corpus_is_a_change(w: World) -> None:
    gone = task_ids("development")[0]
    w.ops.corpus = replace(
        w.ops.corpus, tasks=tuple(t for t in w.ops.corpus.tasks if t.task_id != gone)
    )
    error = hold("CORPUS_CHANGED", w.ops.check_corpus, w.refs["corpus_ref"])
    assert error.details["cases"] == [gone]
    w.ops.check_corpus(w.refs["corpus_ref"], task_ids("validation"))  # other cases are unchanged


def test_search_without_a_calibration_summary_for_the_cell_holds(w: World) -> None:
    pid = w.propose("a2")
    error = hold(
        "NO_INFORMATIVE_TASKS", w.ops.search, pid, cell_id="claude-cli", root_budget=BUDGET
    )
    assert error.details == {"cell_id": "claude-cli"}


def test_search_without_an_evaluator_version_of_the_running_code_holds(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.evaluation import versions

    pid = w.propose("a2")
    other = {k: "sha256:" + "0" * 64 for k in versions.DIGEST_FIELDS}
    monkeypatch.setattr(versions, "code_digests", lambda **_: other)
    hold("EVALUATOR_UNQUALIFIED", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    assert w.objects("stage-plan") == []


# ==================================================================================================
# calibration (§8.2) through the meta operations
# ==================================================================================================
def calibrate(w: World, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "max_repeats": 1, "max_trials": 100, "parallel": 1, "max_tokens": 100_000,
        "max_wall_seconds": 3600,
    }  # fmt: skip
    args.update(over)
    return w.ops.calibrate([CELL], **args)


def test_calibrate_runs_every_development_and_validation_case_once_per_cell(w: World) -> None:
    result = calibrate(w)
    cases = SPLIT_COUNTS["development"] + SPLIT_COUNTS["validation"]
    assert result["state"] == "done" and result["trials"] == cases
    assert result["stop_reason"] is None
    assert not [c for c in w.executor.calls if "-hol-" in c[1]]  # holdout is never calibrated
    shown = w.ops.calibration_show(result["plan_ref"]["id"])
    assert shown["state"] == "done" and shown["trials"] == cases
    assert shown["summary_ref"] == result["summary_ref"] and shown["summary"]["cells"][CELL]
    classes = {t["class"] for t in shown["summary"]["cells"][CELL]["tasks"].values()}
    assert classes == {"saturated"}  # one run, all passing: nothing is informative yet
    # trial metrics are recorded for the calibration trials too
    sources = {r[1]["source"] for r in w.objects("trial-metrics")}
    assert sources == {"calibration"}


def test_a_saturated_calibration_leaves_nothing_for_search_ic20(w: World) -> None:
    result = calibrate(w)
    assert w.ops.latest_summary(w.refs["corpus_ref"], CELL, w.version_ref) == result["summary_ref"]
    pid = w.propose("a2")
    error = hold("NO_INFORMATIVE_TASKS", w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
    assert error.details["stage"] == "screening" and error.details["available"] == 0
    assert w.objects("stage-plan") == [] and w.state(pid) == "draft"


def test_calibrate_refuses_a_plan_above_max_trials_before_anything_runs(w: World) -> None:
    error = fault("CALIBRATION_PLAN", calibrate, w, max_repeats=5, max_trials=159)
    cases = SPLIT_COUNTS["development"] + SPLIT_COUNTS["validation"]
    assert error.details == {"worst_case_trials": cases * 5, "max_trials": 159}
    assert w.executor.calls == []
    fault("CALIBRATION_PLAN", calibrate, w, max_trials=0)
    fault("CALIBRATION_PLAN", calibrate, w, max_trials=True)


def test_calibrate_names_each_installed_cell_once(w: World) -> None:
    fault("CALIBRATION_PLAN", w.ops.calibrate, [], max_repeats=1, max_trials=9, parallel=1,
          max_tokens=10, max_wall_seconds=10)  # fmt: skip
    fault("CALIBRATION_PLAN", w.ops.calibrate, [CELL, CELL], max_repeats=1, max_trials=999,
          parallel=1, max_tokens=10, max_wall_seconds=10)  # fmt: skip
    error = hold("CELL_UNKNOWN", w.ops.calibrate, ["nope"], max_repeats=1, max_trials=999,
                 parallel=1, max_tokens=10, max_wall_seconds=10)  # fmt: skip
    assert error.details == ["nope"]


def test_calibrate_needs_a_qualified_executor(w: World) -> None:
    w.local.evaluation.executor_policy = None
    hold("QUALIFIED_EXECUTOR_REQUIRED", calibrate, w)


def test_calibration_show_of_an_unknown_plan_is_not_found(w: World) -> None:
    fault("NOT_FOUND", w.ops.calibration_show, "calplan-nope")


# ==================================================================================================
# the command line: registration, options, exit codes (2 = fault, 3 = hold)
# ==================================================================================================
NEW_COMMANDS = (
    "review", "search", "approve-stage", "stages", "calibrate", "calibration", "component",
    "corpus", "propose-components", "reconcile",
)  # fmt: skip
OLD_COMMANDS = (
    "propose", "screen", "approve-experiment", "run-experiment", "approve-canary", "run-canary",
    "promote", "rollback", "reject", "abort", "report", "status",
)  # fmt: skip


# plain, wide help text: no colour codes and no wrapped option names
PLAIN = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "250"}


def test_the_meta_command_group_lists_the_work_033_commands_and_keeps_the_old_gates() -> None:
    result = CliRunner().invoke(cli.app, ["meta", "--help"], env=PLAIN)
    assert result.exit_code == 0, result.output
    for name in (*NEW_COMMANDS, *OLD_COMMANDS):
        assert name in result.output, name
    # no command chains the gates
    assert "evolve" not in result.output and "auto" not in result.output


@pytest.mark.parametrize(
    "command,options",
    [
        (
            ["search"],
            (
                "--cell",
                "--max-tokens",
                "--max-wall-seconds",
                "--parallel",
                "--per-trial-tokens",
                "--basis",
                "--evidence",
                "--config",
                "--corpus",
                "--app",
            ),
        ),
        (
            ["approve-stage"],
            ("--stage", "--per-trial-tokens", "--basis", "--evidence", "--parallel", "--app"),
        ),
        (["review"], ("--outcome", "--note", "--config", "--app")),
        (["stages"], ("--config", "--corpus", "--app")),
        (
            ["reconcile"],
            ("--stage", "--allocation", "--tokens", "--cost", "--receipt", "--config"),
        ),
        (
            ["calibrate"],
            (
                "--cells",
                "--max-repeats",
                "--max-trials",
                "--max-tokens",
                "--max-wall-seconds",
                "--per-trial-tokens",
                "--parallel",
                "--app",
            ),
        ),
        (["calibration", "show"], ("--config", "--corpus", "--app")),
        (["component", "list"], ("--kind", "--config")),
        (["component", "show"], ("--config",)),
        (["component", "add"], ("--kind", "--name", "--file", "--rationale", "--config")),
        (["corpus", "check"], ("--repeats", "--domain", "--corpus")),
        (["corpus", "freeze"], ("--holdout-use-limit", "--set", "--config", "--corpus")),
        (["corpus", "import-work030"], ("--source", "--corpus")),
        (
            ["propose-components"],
            (
                "--cell",
                "--set",
                "--suffix",
                "--hypothesis",
                "--benefit",
                "--risk",
                "--observation",
                "--prediction-file",
                "--app",
            ),
        ),
    ],
)
def test_each_work_033_command_declares_its_options(
    command: list[str], options: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(cli.app, ["meta", *command, "--help"], env=PLAIN)
    assert result.exit_code == 0, result.output
    for option in options:
        assert option in result.output, (command, option)


def test_review_rejects_an_unknown_outcome_before_it_opens_the_deployment() -> None:
    result = CliRunner().invoke(
        cli.app,
        ["meta", "review", "p1", "--outcome", "maybe", "--note", "x", "--config", "/nonexistent"],
    )
    assert result.exit_code == 2
    assert json.loads(result.output[result.output.index("{") :])["code"] == "REVIEW_OUTCOME"


def test_corpus_check_refuses_unknown_domains_and_zero_repeats(tmp_path: Path) -> None:
    write_corpus(tmp_path / "corpus")
    for args, code in (
        (["--domain", "nonsense"], "TASK_META"),
        (["--repeats", "0"], "REPEATS"),
    ):
        result = CliRunner().invoke(
            cli.app, ["meta", "corpus", "check", *args, "--corpus", str(tmp_path / "corpus")]
        )
        assert result.exit_code == 2, result.output
        assert json.loads(result.output[result.output.index("{") :])["code"] == code


def test_corpus_import_work030_copies_the_regression_tasks_and_refuses_a_second_import(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    runner = CliRunner()
    first = runner.invoke(cli.app, ["meta", "corpus", "import-work030", "--corpus", str(root)])
    assert first.exit_code == 0, first.output
    imported = json.loads(first.output[first.output.index("{") :])["imported"]
    source = json.loads((REPO / "specs/030-meta-harness-live/corpus/manifest.json").read_text())
    assert imported == [f"work030-{t}" for t in source["task_ids"]]
    assert all((root / "tasks" / t / "task.json").is_file() for t in imported)
    second = runner.invoke(cli.app, ["meta", "corpus", "import-work030", "--corpus", str(root)])
    assert second.exit_code == 2
    assert json.loads(second.output[second.output.index("{") :])["code"] == "TASK_DUPLICATE"


# -- the commands against a real local-init deployment (no driver is run) --------------------------
@pytest.fixture
def home(tmp_path: Path) -> dict[str, str]:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    where = tmp_path / "amplai"
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
            "--home", str(where), "--operator", "pinesky",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    write_corpus(tmp_path / "corpus")
    content = tmp_path / "content.json"
    content.write_text(json.dumps({"implementer": [
        "You are the IMPLEMENTER for {app_id} at commit {base_commit}.",
        "Make the change below and run the acceptance commands yourself. Do not commit.",
        "Prefer small edits.",
    ]}))  # fmt: skip
    return {"config": str(where / "local.json"), "corpus": str(tmp_path / "corpus"),
            "content": str(content)}  # fmt: skip


def run_meta(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(cli.app, ["meta", *args])
    text = result.output.strip()
    return result.exit_code, json.loads(text[text.index("{") :]) if "{" in text else {}


def with_home(home: dict[str, str], *args: str, corpus: bool = True) -> tuple[int, dict[str, Any]]:
    extra = ["--config", home["config"]]
    return run_meta(*args, *extra, *(["--corpus", home["corpus"]] if corpus else []))


def propose_by_command(home: dict[str, str], suffix: str = "cli1") -> str:
    code, added = with_home(home, "component", "add", "--kind", "role_prompt", "--name", suffix,
                            "--file", home["content"], corpus=False)  # fmt: skip
    assert code == 0, added
    code, out = with_home(
        home, "propose-components", "--cell", CELL, "--set", f"role_prompt=role_prompt.{suffix}@1",
        "--suffix", suffix, "--hypothesis", "A short extra line helps",
        "--benefit", "Fewer big edits", "--risk", "A line may distract",
        "--observation", "edits are big",
    )  # fmt: skip
    assert code == 0, out
    return str(out["proposal_id"])


def test_components_are_added_listed_shown_and_proposed_by_command(home: dict[str, str]) -> None:
    pid = propose_by_command(home)
    code, listed = with_home(home, "component", "list", "--kind", "role_prompt", corpus=False)
    assert code == 0
    ids = {c["component_id"] for c in listed["components"]}
    assert "role_prompt.cli1" in ids and all(
        c["kind"] == "role_prompt" for c in listed["components"]
    )
    code, shown = with_home(home, "component", "show", "role_prompt.cli1", corpus=False)
    assert code == 0 and shown["versions"][0]["source"] == "operator"
    assert shown["versions"][0]["content"]["implementer"][-1] == "Prefer small edits."
    code, status = with_home(home, "stages", pid)
    assert code == 0 and status["state"] == "draft"
    assert [s["stage"] for s in status["steps"]] == ["screening", "focused", "ablation", "holdout"]
    assert {s["state"] for s in status["steps"]} == {"pending"}


def test_component_commands_refuse_bad_input_with_a_fault(
    home: dict[str, str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"implementer": "not a list"}))
    code, out = with_home(home, "component", "add", "--kind", "role_prompt", "--name", "bad",
                          "--file", str(bad), corpus=False)  # fmt: skip
    assert code == 2 and out["code"] == "COMPONENT_CONTENT"
    code, out = with_home(home, "component", "add", "--kind", "no_such_kind", "--name", "x",
                          "--file", home["content"], corpus=False)  # fmt: skip
    assert code == 2 and out["code"] == "COMPONENT_KIND"
    code, out = with_home(home, "component", "add", "--kind", "role_prompt", "--name", "a:b",
                          "--file", home["content"], corpus=False)  # fmt: skip
    assert code == 2 and out["code"] == "COMPONENT_ID"  # no ":" in an id (a change path)
    code, out = with_home(home, "component", "show", "role_prompt.nope", corpus=False)
    assert code == 2 and out["code"] == "NOT_FOUND"


def test_propose_components_refuses_a_malformed_or_unknown_set(home: dict[str, str]) -> None:
    propose_by_command(home)
    common = ("--cell", CELL, "--suffix", "x", "--hypothesis", "h", "--benefit", "b",
              "--risk", "r", "--observation", "o")  # fmt: skip
    code, out = with_home(home, "propose-components", "--set", "role_prompt", *common)
    assert code == 2 and out["code"] == "COMPONENT_SET"
    code, out = with_home(home, "propose-components", "--set", "role_prompt=role_prompt.cli1@1",
                          "--set", "role_prompt=role_prompt.cli1@1", *common)  # fmt: skip
    assert code == 2 and out["code"] == "COMPONENT_SET"
    code, out = with_home(
        home, "propose-components", "--set", "role_prompt=role_prompt.nope@1", *common
    )
    assert code == 2 and out["code"] == "NOT_FOUND"
    code, out = with_home(
        home, "propose-components", "--set", "bogus_slot=role_prompt.cli1@1", *common
    )
    assert code == 2 and out["code"] == "MANIFEST_SLOT"
    code, out = with_home(home, "propose-components", "--set", "role_prompt=role_prompt.cli1@1",
                          "--cell", "claude-cli", "--suffix", "x", "--hypothesis", "h",
                          "--benefit", "b", "--risk", "r", "--observation", "o")  # fmt: skip
    assert code == 3 and out["code"] == "CELL_UNKNOWN"


def test_review_fail_by_command_rejects_and_stages_shows_it(home: dict[str, str]) -> None:
    pid = propose_by_command(home)
    code, out = with_home(home, "review", pid, "--outcome", "fail", "--note", "the line distracts")
    assert code == 0 and out == {"outcome": "fail", "review_ref": None, "state": "rejected"}
    code, shown = with_home(home, "stages", pid)
    assert code == 0 and shown["state"] == "rejected"
    code, out = with_home(home, "review", pid, "--outcome", "pass", "--note", " ")
    assert code == 2 and out["code"] == "REVIEW_NOTE"


def test_search_needs_a_frozen_corpus_then_an_evaluator_version_and_a_plan(
    home: dict[str, str],
) -> None:
    pid = propose_by_command(home)
    search = ("search", pid, "--cell", CELL, "--max-tokens", "100000", "--max-wall-seconds", "600",
              "--per-trial-tokens", "10", "--basis", "scripted", "--evidence", "tests")  # fmt: skip
    code, out = with_home(home, *search)
    assert code == 3 and out["code"] == "META_STATE" and "not frozen" in out["message"]
    code, frozen = with_home(home, "corpus", "freeze", "--holdout-use-limit", "3")
    assert code == 0 and {"corpus_ref", "task_index_ref", "leak_index_ref"} <= set(frozen)
    # frozen, but no calibration and no evaluator version of the running code yet
    code, out = with_home(home, *search)
    assert code == 3 and out["code"] == "EVALUATOR_UNQUALIFIED"
    code, out = with_home(
        home, "approve-stage", pid, "--stage", "focused", "--per-trial-tokens", "10",
        "--basis", "scripted", "--evidence", "tests",
    )  # fmt: skip
    assert code == 3 and out["code"] == "META_STATE"  # no stage plan: search first
    code, shown = with_home(home, "stages", pid)
    assert code == 0 and shown["state"] == "draft"  # nothing moved


def test_a_command_refuses_more_than_four_parallel_trials(home: dict[str, str]) -> None:
    pid = propose_by_command(home)
    code, out = with_home(
        home, "search", pid, "--cell", CELL, "--max-tokens", "10", "--max-wall-seconds", "10",
        "--per-trial-tokens", "10", "--basis", "b", "--evidence", "e", "--parallel", "5",
    )  # fmt: skip
    assert code == 2 and out["code"] == "EVAL_PARALLEL"
    code, out = with_home(
        home, "approve-stage", pid, "--stage", "focused", "--per-trial-tokens", "10",
        "--basis", "b", "--evidence", "e", "--parallel", "0",
    )  # fmt: skip
    assert code == 2 and out["code"] == "EVAL_PARALLEL"


def test_calibrate_by_command_refuses_a_plan_above_max_trials_and_an_unknown_cell(
    home: dict[str, str],
) -> None:
    code, _ = with_home(home, "corpus", "freeze", "--holdout-use-limit", "3")
    assert code == 0
    base = ("--max-repeats", "1", "--max-tokens", "1000", "--max-wall-seconds", "100",
            "--per-trial-tokens", "10", "--basis", "scripted", "--evidence", "tests")  # fmt: skip
    code, out = with_home(home, "calibrate", "--cells", CELL, "--max-trials", "1", *base)
    assert code == 2 and out["code"] == "CALIBRATION_PLAN"
    cases = SPLIT_COUNTS["development"] + SPLIT_COUNTS["validation"]
    assert out["details"] == {"worst_case_trials": cases, "max_trials": 1}
    code, out = with_home(home, "calibrate", "--cells", "nope", "--max-trials", "500", *base)
    assert code == 3 and out["code"] == "CELL_UNKNOWN"
    code, out = with_home(home, "calibration", "show", "calplan-nope")
    assert code == 2 and out["code"] == "NOT_FOUND"


def test_the_app_option_is_checked_by_every_corpus_command(home: dict[str, str]) -> None:
    pid = propose_by_command(home)
    code, out = with_home(home, "stages", pid, "--app", "nope")
    assert code == 3 and out["code"] == "TARGET_UNKNOWN" and out["details"] == ["nope"]
    code, shown = with_home(home, "stages", pid, "--app", "app")
    assert code == 0 and shown["state"] == "draft"
    code, out = with_home(home, "review", pid, "--outcome", "fail", "--note", "n", "--app", "x")
    assert code == 3 and out["code"] == "TARGET_UNKNOWN"
    code, shown = with_home(home, "stages", pid)
    assert code == 0 and shown["state"] == "draft"  # the refused review did nothing


def test_reconcile_by_command_is_the_human_operators_and_names_one_target(
    home: dict[str, str], tmp_path: Path
) -> None:
    pid = propose_by_command(home)
    # the local-init operator is human and holds experiment.reconcile: the target is checked
    code, out = with_home(home, "reconcile", pid, corpus=False)
    assert code == 2 and out["code"] == "RECONCILE_TARGET"
    code, out = with_home(home, "reconcile", pid, "--stage", "screening", "--allocation", "a",
                          corpus=False)  # fmt: skip
    assert code == 2 and out["code"] == "RECONCILE_TARGET"
    code, out = with_home(home, "reconcile", pid, "--stage", "screening", corpus=False)
    assert code == 3 and out["code"] == "META_STATE"  # no stage run yet
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({"allocation_id": "a", "tokens": 1, "cost_microunits": 0,
                                   "process_stopped": True, "unknown_effects": 0}))  # fmt: skip
    code, out = with_home(home, "reconcile", pid, "--allocation", "a", "--tokens", "1",
                          "--cost", "0", "--receipt", str(receipt), corpus=False)  # fmt: skip
    assert code == 3 and out["code"] == "RECONCILIATION_STATE"
    code, shown = with_home(home, "stages", pid)
    assert code == 0 and shown["state"] == "draft"  # nothing moved
