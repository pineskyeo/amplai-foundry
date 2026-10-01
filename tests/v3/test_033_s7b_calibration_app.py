"""Work 033 S7b: calibration selects the cases of one app (interfaces.md §2.8, §8.2, IC-15, IC-12).

Contract: the S11 fix-wave note ("Open (S7b)": calibration pins every case of its splits, so
app/environment selection for calibration is decided with the TB2 environment siblings), §2.8 (the
calibration plan), §8.2 (calibration), §10.2 (bases name their app), §10.5 (TB2 tasks run on
`amplai-tb2`) and the S7b row of §13. The decision implemented and tested here (provisional IC-31,
"Provisional Operator Decisions IC-31, IC-32 And Clarifications After S7b"): a plan may name one
app, `app = {app_id, base_ids}`; its `case_ids` are then every case of its splits whose frozen
case payload names one of those bases, in corpus order (`SAMPLING_CHANGED` otherwise). A plan
without `app` keeps every case of its splits.

Two layers. `CalibrationService` over the S2 world (real store, approvals, evaluator version;
scripted executor): the plan shape, the recomputation of the pinned cases, what a run executes.
`LocalMetaOps.calibrate` over the S11 world (real product rig, frozen corpus v2 with a second app;
scripted executor): the plan it writes per app and the refusals of the app options.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_033_s2_calibration import plan_for, run_plan, service
from test_033_s2_service import Scripted, fault, hold, make_world
from test_033_s11_stages import CELL, SHA, build_world

from amplai_foundry.evaluation import calibration
from amplai_foundry.evaluation.corpus import CorpusService
from amplai_foundry.meta_harness import corpus_v2
from amplai_foundry.runtime.contracts.identity import canonical, new_id
from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps, set_case_ids
from amplai_foundry.runtime.execution.product import AppConfig

BENCH, TB2 = "app", "amplai-tb2"
BENCH_APP = {"app_id": BENCH, "base_ids": ["bench"]}
TB2_APP = {"app_id": TB2, "base_ids": ["tb2"]}
CELLS = ("cell-a", "cell-b")


# ==================================================================================================
# CalibrationService: the plan names one app
# ==================================================================================================
def build_corpus(w: Any, *, legacy: int = 0, ghost: int = 0) -> SimpleNamespace:
    """A frozen corpus whose case payloads name their base (§2.7 `base_id`): the two apps'
    cases interleaved within each split, in corpus order. `legacy` cases have a payload without
    a base; `ghost` cases name a base no plan names."""
    m, d = w.m, w.m.d
    corpus_id = new_id("corpus")
    layout = {"development": (2, 2), "validation": (3, 2), "holdout": (1, 1)}  # bench, tb2
    cases: list[dict[str, Any]] = []
    by_app: dict[str, list[str]] = {"bench": [], "tb2": [], "legacy": [], "ghost": []}

    def add(split: str, label: str, i: int, base: str | None) -> None:
        case_id = f"{split[:3]}-{label}-{i:02d}"
        payload: dict[str, Any] = {"case_id": case_id, "numbers": [i, i + 1], "salt": corpus_id}
        if base is not None:
            payload["base_id"] = base
        artifact = d.artifacts.admit(
            d.scope, canonical(payload), "application/json", trust="operator"
        )
        cases.append({"case_id": case_id, "split": split, "task_class": "alpha",
                      "artifact_ref": artifact})  # fmt: skip
        if split != "holdout":
            by_app[label].append(case_id)

    for split, (bench_n, tb2_n) in layout.items():
        for i in range(max(bench_n, tb2_n)):
            if i < bench_n:
                add(split, "bench", i, "bench")
            if i < tb2_n:
                add(split, "tb2", i, "tb2")
        if split == "development":
            for i in range(legacy):
                add(split, "legacy", i, None)
            for i in range(ghost):
                add(split, "ghost", i, "ghost")
    ref = CorpusService(d.store, d.artifacts).freeze(
        m.reviewer, corpus_id, cases, holdout_use_limit=10
    )
    comps = {"cell-a": w.composition("cell-a"), "cell-b": w.composition("cell-b")}
    return SimpleNamespace(
        corpus_ref=ref, cases=cases, comps=comps, roles=dict(comps),
        refs={cell: [r] for cell, r in comps.items()}, ev=w.evaluator(ref), by_app=by_app,
        ids=[c["case_id"] for c in cases if c["split"] != "holdout"],
    )  # fmt: skip


@pytest.fixture
def w(tmp_path: Path) -> Any:
    with make_world(tmp_path) as world:
        yield world


def always(role: str, case_id: str, repeat: int) -> bool:
    return True


def executor(w: Any, c: SimpleNamespace) -> Scripted:
    return Scripted(w, c.roles, always, tokens=(5, 5))


def pinned(c: SimpleNamespace, *labels: str) -> list[str]:
    """The cases of the named apps (and legacy/ghost labels) in corpus order, holdout excluded."""
    return [i for i in c.ids if any(i.split("-")[1] == label for label in labels)]


def test_an_app_plan_pins_exactly_that_apps_cases_in_corpus_order(w: Any) -> None:
    c = build_corpus(w)
    svc = service(w)
    for app, label in ((BENCH_APP, "bench"), (TB2_APP, "tb2")):
        ids = pinned(c, label)
        assert ids and ids != c.ids
        ref = svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=ids, app=app))
        stored = w.m.d.store.get(w.scope, "calibration-plan", ref)
        assert stored["case_ids"] == ids and stored["app"] == app
    # the corpus order is the interleaving of the apps, so an app's order is a subsequence of it
    assert pinned(c, "tb2") == [i for i in c.ids if "-tb2-" in i]
    assert c.ids.index("dev-tb2-00") < c.ids.index("dev-bench-01")


def test_a_plan_without_an_app_keeps_every_case_of_its_splits(w: Any) -> None:
    c = build_corpus(w)
    svc = service(w)
    ref = svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=c.ids))  # unchanged since S2
    assert "app" not in w.m.d.store.get(w.scope, "calibration-plan", ref)
    only_val = [i for i in c.ids if i.startswith("val")]
    ref = svc.freeze(w.m.reviewer, plan_for(w, c, splits=["validation"], case_ids=only_val))
    assert ref["id"].startswith("calplan-")
    # one app's cases alone are not every case of the splits
    hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer, plan_for(w, c, case_ids=pinned(c, "bench")))


def test_an_app_plan_runs_only_its_apps_cases_and_the_summary_covers_only_them(w: Any) -> None:
    c = build_corpus(w)
    ids = pinned(c, "tb2")
    ex = executor(w, c)
    r = run_plan(w, c, executor=ex, case_ids=ids, app=TB2_APP)
    assert r.head["state"] == "done"
    assert {case for _, case, _ in ex.calls} == set(ids)  # no bench case, no holdout case
    assert len(r.trials()) == len(CELLS) * len(ids)
    assert {t["task_id"] for t in r.trials()} == set(ids)
    for cell in CELLS:
        assert set(r.summary["cells"][cell]["tasks"]) == set(ids)
    assert r.plan["app"] == TB2_APP


def test_two_apps_calibrate_separately_on_one_corpus_each_on_its_own_compositions(w: Any) -> None:
    c = build_corpus(w)
    bench = run_plan(w, c, executor=executor(w, c), case_ids=pinned(c, "bench"), app=BENCH_APP)
    tb2 = run_plan(w, c, executor=executor(w, c), case_ids=pinned(c, "tb2"), app=TB2_APP)
    assert bench.plan_ref != tb2.plan_ref and bench.summary_ref != tb2.summary_ref
    bench_tasks = set(bench.summary["cells"]["cell-a"]["tasks"])
    tb2_tasks = set(tb2.summary["cells"]["cell-a"]["tasks"])
    assert bench_tasks == set(pinned(c, "bench")) and tb2_tasks == set(pinned(c, "tb2"))
    assert not bench_tasks & tb2_tasks
    # each summary's plan records the app it calibrated
    for result, app in ((bench, BENCH_APP), (tb2, TB2_APP)):
        plan = w.m.d.store.get(w.scope, "calibration-plan", result.summary["plan_ref"])
        assert plan["app"] == app


# -- SAMPLING_CHANGED holds per app ---------------------------------------------------------------
def refused_samples(c: SimpleNamespace) -> dict[str, dict[str, Any]]:
    bench, tb2 = pinned(c, "bench"), pinned(c, "tb2")
    return {
        "every case under an app": {"case_ids": c.ids, "app": TB2_APP},
        "another app's cases": {"case_ids": tb2, "app": BENCH_APP},
        "a subset of the app": {"case_ids": bench[:-1], "app": BENCH_APP},
        "a split of the app only": {
            "case_ids": [i for i in bench if i.startswith("dev")],
            "app": BENCH_APP,
        },
        "reversed": {"case_ids": bench[::-1], "app": BENCH_APP},
        "with a foreign case": {"case_ids": [*bench, tb2[0]], "app": BENCH_APP},
        "a base no case names": {"case_ids": bench, "app": {"app_id": BENCH, "base_ids": ["x"]}},
        "no case of the app": {"case_ids": bench, "app": {"app_id": "no", "base_ids": ["no"]}},
        "a holdout case": {"case_ids": [*bench, "hol-bench-00"], "app": BENCH_APP},
    }


def test_sampling_changed_holds_for_every_wrong_pin_of_an_app_plan(w: Any) -> None:
    c = build_corpus(w)
    svc = service(w)
    for over in refused_samples(c).values():
        hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer, plan_for(w, c, **over))
    assert w.m.d.store.list_objects(w.scope, "calibration-plan") == []  # nothing was stored


def test_an_app_naming_several_bases_pins_the_cases_of_all_of_them(w: Any) -> None:
    c = build_corpus(w)
    both = {"app_id": "everything", "base_ids": ["bench", "tb2"]}
    svc = service(w)
    ref = svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=c.ids, app=both))
    assert w.m.d.store.get(w.scope, "calibration-plan", ref)["app"] == both
    hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer,
         plan_for(w, c, case_ids=pinned(c, "bench"), app=both))  # fmt: skip


def test_a_case_whose_payload_names_no_base_belongs_to_no_app(w: Any) -> None:
    c = build_corpus(w, legacy=2)
    svc = service(w)
    legacy = pinned(c, "legacy")
    assert len(legacy) == 2
    for app, label in ((BENCH_APP, "bench"), (TB2_APP, "tb2")):
        ids = pinned(c, label)
        assert not set(ids) & set(legacy)
        svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=ids, app=app))
        hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer,
             plan_for(w, c, case_ids=c.ids, app=app))  # fmt: skip
    svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=c.ids))  # no app: every case, legacy too


def test_a_base_named_by_a_ghost_app_pins_only_its_own_cases(w: Any) -> None:
    c = build_corpus(w, ghost=2)
    ghost = pinned(c, "ghost")
    assert len(ghost) == 2
    svc = service(w)
    app = {"app_id": "ghost-app", "base_ids": ["ghost"]}
    svc.freeze(w.m.reviewer, plan_for(w, c, case_ids=ghost, app=app))
    wrong = plan_for(w, c, case_ids=pinned(c, "bench"), app=BENCH_APP | {"base_ids": ["ghost"]})
    hold("SAMPLING_CHANGED", svc.freeze, w.m.reviewer, wrong)


def test_the_holdout_is_never_read_by_an_app_calibration(w: Any) -> None:
    c = build_corpus(w)
    ids = pinned(c, "bench")
    assert not [i for i in ids if i.startswith("hol")]
    # a plan over the holdout split is refused whatever its app
    fault("CALIBRATION_PLAN", service(w).freeze, w.m.reviewer,
          plan_for(w, c, splits=["holdout"], case_ids=["hol-bench-00"], app=BENCH_APP))  # fmt: skip


def test_the_approval_binds_the_app_of_the_plan(w: Any) -> None:
    c = build_corpus(w)
    svc = service(w)
    approved = plan_for(w, c, case_ids=pinned(c, "tb2"), app=TB2_APP)
    other_app = {**approved, "app": BENCH_APP, "case_ids": pinned(c, "bench")}
    hold("DEMO_APPROVAL", svc.freeze, w.m.reviewer, other_app)  # the digest is of the TB2 plan
    assert svc.freeze(w.m.reviewer, approved)["id"].startswith("calplan-")


# -- the shape of `app` ---------------------------------------------------------------------------
def malformed_apps() -> dict[str, Any]:
    return {
        "none": None,
        "a string": "app",
        "a list": ["app"],
        "no base ids": {"app_id": BENCH},
        "no app id": {"base_ids": ["bench"]},
        "extra key": {**BENCH_APP, "environment": "app"},
        "empty app id": {"app_id": "", "base_ids": ["bench"]},
        "numeric app id": {"app_id": 3, "base_ids": ["bench"]},
        "empty bases": {"app_id": BENCH, "base_ids": []},
        "bases not a list": {"app_id": BENCH, "base_ids": "bench"},
        "a tuple of bases": {"app_id": BENCH, "base_ids": ("bench",)},
        "an empty base": {"app_id": BENCH, "base_ids": [""]},
        "a numeric base": {"app_id": BENCH, "base_ids": [1]},
        "duplicate bases": {"app_id": BENCH, "base_ids": ["bench", "bench"]},
    }


def test_a_malformed_app_is_a_calibration_plan_fault_before_anything_is_stored(w: Any) -> None:
    c = build_corpus(w)
    svc = service(w)
    for name, app in malformed_apps().items():
        error = fault("CALIBRATION_PLAN", svc.freeze, w.m.reviewer,
                      plan_for(w, c, case_ids=pinned(c, "bench"), app=app))  # fmt: skip
        assert error.outcome == "rejected", name
    assert w.m.d.store.list_objects(w.scope, "calibration-plan") == []


def test_the_plan_fields_stay_exactly_the_section_2_8_ones_plus_the_optional_app(w: Any) -> None:
    c = build_corpus(w)
    plan = plan_for(w, c, case_ids=c.ids)
    calibration.validate_calibration_plan(plan)  # no app: valid
    calibration.validate_calibration_plan({**plan, "app": TB2_APP})
    assert {"app"} == calibration.OPTIONAL_PLAN_FIELDS
    assert set(plan) | {"app"} > calibration.PLAN_FIELDS
    for extra in ({"environment": "tb2-alpha"}, {"app_id": TB2}, {"apps": [TB2_APP]}):
        fault("CALIBRATION_PLAN", calibration.validate_calibration_plan, {**plan, **extra})
    missing = {k: v for k, v in plan.items() if k != "case_ids"}
    fault("CALIBRATION_PLAN", calibration.validate_calibration_plan, missing)


# ==================================================================================================
# LocalMetaOps.calibrate: the plan it writes per app
# ==================================================================================================
APP_TASKS = ("tb2-dev-01", "tb2-dev-02", "tb2-val-01", "tb2-val-02", "tb2-hol-01")


@pytest.fixture
def stage_world(tmp_path: Path) -> Any:
    with build_world(tmp_path) as world:
        yield world


def with_tb2(w: Any, *, holdout_only: bool = False) -> corpus_v2.CorpusV2:
    """Version 2.0.0 of the S11 corpus, frozen, with `amplai-tb2` installed and a base `tb2`:
    two development and two validation tasks of it (or, `holdout_only`, none but a holdout one),
    beside the bench app's 14 development, 18 validation and 16 holdout tasks."""
    service = w.rig.service
    service.install(AppConfig(TB2, w.rig.repo, service.apps["app"].config.verifiers))
    pools = {s: [t for t in w.corpus.tasks if t.split == s] for s in
             ("development", "validation", "holdout")}  # fmt: skip
    picks = [("development", "tb2-dev-01"), ("development", "tb2-dev-02"),
             ("validation", "tb2-val-01"), ("validation", "tb2-val-02"),
             ("holdout", "tb2-hol-01")]  # fmt: skip
    if holdout_only:
        picks = picks[-1:]
    tasks = tuple(
        replace(pools[split][0], task_id=task_id, base_id="tb2") for split, task_id in picks
    )
    bases = {**w.corpus.bases, "tb2": {"dir": "bases/bench", "base_commit": SHA, "app_id": TB2}}
    corpus = replace(w.corpus, version="2.0.0", bases=bases, tasks=(*w.corpus.tasks, *tasks))
    w.refs = corpus_v2.freeze(w.operator, w.store, w.artifacts, corpus, holdout_use_limit=50)
    w.executor.corpus_version = "2.0.0"
    w.version_ref = w.evaluator()
    return corpus


def ops_for(w: Any, corpus: corpus_v2.CorpusV2, app_id: str | None = None) -> LocalMetaOps:
    return LocalMetaOps(w.dep, corpus, w.executor, driver=CELL, app_id=app_id)  # type: ignore[arg-type]


def calibrate(ops: LocalMetaOps, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "max_repeats": 1, "max_trials": 200, "parallel": 1, "max_tokens": 100_000,
        "max_wall_seconds": 3600,
    }  # fmt: skip
    args.update(over)
    return ops.calibrate([CELL], **args)


def plan_of(w: Any, result: dict[str, Any]) -> dict[str, Any]:
    plan: dict[str, Any] = w.store.get(w.scope, "calibration-plan", result["plan_ref"])
    return plan


def test_the_bench_app_calibration_records_its_app_and_every_one_of_its_cases(
    stage_world: Any,
) -> None:
    """Bench-app behaviour is unchanged (no TB2 app installed): every development and validation
    case, once per cell; the plan now also records the app and its base."""
    w = stage_world
    result = calibrate(w.ops)
    plan = plan_of(w, result)
    assert plan["app"] == {"app_id": "app", "base_ids": ["bench"]}
    assert len(plan["case_ids"]) == 14 + 18 and result["trials"] == 14 + 18
    assert not [c for c in w.executor.calls if "-hol-" in c[1]]
    assert plan["splits"] == ["development", "validation"]


def test_with_a_second_app_each_calibration_runs_only_its_apps_cases(stage_world: Any) -> None:
    w = stage_world
    corpus = with_tb2(w)
    bench = calibrate(ops_for(w, corpus, "app"))
    bench_calls = {c[1] for c in w.executor.calls}
    assert bench["trials"] == 14 + 18
    assert plan_of(w, bench)["app"] == {"app_id": "app", "base_ids": ["bench"]}
    assert not {c for c in bench_calls if c.startswith("tb2-")}
    w.executor.calls.clear()
    tb2 = calibrate(ops_for(w, corpus, TB2))
    plan = plan_of(w, tb2)
    assert plan["app"] == {"app_id": TB2, "base_ids": ["tb2"]}
    assert plan["case_ids"] == [t for t in APP_TASKS if "-hol-" not in t]
    assert tb2["trials"] == 4 and {c[1] for c in w.executor.calls} == set(plan["case_ids"])
    shown = ops_for(w, corpus, TB2).calibration_show(tb2["plan_ref"]["id"])
    assert set(shown["summary"]["cells"][CELL]["tasks"]) == set(plan["case_ids"])


def test_the_default_app_of_a_corpus_naming_two_apps_stays_ambiguous_without_app(
    stage_world: Any,
) -> None:
    w = stage_world
    corpus = with_tb2(w)
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, corpus).app)
    assert "--app" in error.message
    assert error.details == {"installed": [TB2, "app"], "named": [TB2, "app"]}
    assert ops_for(w, corpus, TB2).app is w.rig.service.apps[TB2]  # `--app amplai-tb2`


def test_an_app_no_main_set_task_names_is_refused_for_calibration(stage_world: Any) -> None:
    w = stage_world
    corpus = with_tb2(w)
    w.rig.service.install(
        AppConfig("unnamed", w.rig.repo, w.rig.service.apps["app"].config.verifiers)
    )
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, corpus, "unnamed").app)
    assert error.details == {"app": "unnamed", "named": ["amplai-tb2", "app"]}
    error = hold("TARGET_UNKNOWN", lambda: ops_for(w, corpus, "nope").app)
    assert error.details == ["nope"]
    plans = len(w.objects("calibration-plan"))
    hold("TARGET_UNKNOWN", calibrate, ops_for(w, corpus, "unnamed"))
    assert len(w.objects("calibration-plan")) == plans and w.executor.calls == []


def test_an_app_without_a_development_or_validation_case_is_refused_before_anything_runs(
    stage_world: Any,
) -> None:
    w = stage_world
    corpus = with_tb2(w, holdout_only=True)  # the app's one task is a holdout task
    plans = len(w.objects("calibration-plan"))
    error = hold("TARGET_UNKNOWN", calibrate, ops_for(w, corpus, TB2))
    assert error.details == {"app": TB2, "set": "main"}
    assert w.executor.calls == [] and len(w.objects("calibration-plan")) == plans
    # the bench app still calibrates every case of its own on the same corpus
    assert calibrate(ops_for(w, corpus, "app"))["trials"] == 14 + 18


def test_a_stage_plan_reads_the_calibration_of_its_own_app(stage_world: Any) -> None:
    """After the bench app and then `amplai-tb2` calibrate the same corpus, cell and evaluator
    version, a stage plan of the bench app must read the bench app's summary, not the newer one
    of the other app (`LocalMetaOps.latest_summary` has no app parameter)."""
    w = stage_world
    corpus = with_tb2(w)
    w.ops = ops_for(w, corpus, "app")
    bench = calibrate(w.ops)
    calibrate(ops_for(w, corpus, TB2))  # newer, on another app's cases
    pid = w.propose("a2")
    runner = w.ops.stage_runner(pid, cell_id=CELL)
    assert runner.summary_ref == bench["summary_ref"]


# -- the pure selection --------------------------------------------------------------------------
def test_set_case_ids_selects_one_sets_tasks_of_one_apps_bases(stage_world: Any) -> None:
    w = stage_world
    corpus = with_tb2(w)
    assert set_case_ids(corpus, TB2, "main") == set(APP_TASKS)
    bench = set_case_ids(corpus, "app", "main")
    assert len(bench) == 14 + 18 + 16 and not bench & set(APP_TASKS)
    assert set_case_ids(corpus, "nope", "main") == frozenset()
    assert set_case_ids(corpus, TB2, "regression") == frozenset()  # one set at a time (§10.6)
    assert ops_for(w, corpus, TB2).app_case_ids() == set(APP_TASKS)  # stages read the same rule
    assert ops_for(w, corpus, "app").app_case_ids() == bench
    assert ops_for(w, corpus, TB2).app_case_ids("app") == bench  # a stage plan names its app
