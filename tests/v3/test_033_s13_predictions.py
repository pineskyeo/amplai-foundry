"""Work 033 S13: proposal predictions and their scoring (interfaces.md §2.12, §9.5, §9.6, IC-11).

A proposal carries a ``proposal-prediction`` record: improving and regressing development task ids
(at most 20 each, development split only) and buckets for validation and holdout (IC-11). After the
screening stage (development) the task level is scored (precision and recall of the predicted
improving and regressing tasks against the tasks that really changed); after focused and holdout the
bucket level is scored and stays operator-only because it summarizes validation and holdout results.

Real: rc06 product rig, ``LocalMeta``, frozen corpus v2, evaluator, ``StageRunner`` and
``TrialMetrics`` (the S11 ``World``). Stand-in: the trial executor answers a scripted outcome per
(arm, task). The tasks that changed are recomputed here from the stored trials, independent of the
scorer. No driver, docker or network is used.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_033_s11_stages import (
    BUDGET,
    CELL,
    World,
    build_world,
    fault,
    hold,
    task_ids,
)

from amplai_foundry.meta_harness import proposer
from amplai_foundry.meta_harness.proposer import (
    PREDICTION_KIND,
    SCORE_KIND,
    bucket_scores,
    parse_edit,
    score_predictions,
    task_scores,
)
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.runtime.execution import policies

DEV = task_ids("development")
VAL = task_ids("validation")
HOLD = task_ids("holdout")


@pytest.fixture
def w(tmp_path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


# -- helpers ---------------------------------------------------------------------------------------
def prediction(**over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "improve_task_ids": [],
        "regress_task_ids": [],
        "improve_buckets": [],
        "regress_buckets": [],
        "expected_delta": 0.1,
        "risk": "low",
    }
    value.update(over)
    return value


def scripted(improve: set[str], regress: set[str]) -> Any:
    """Baseline fails on ``improve`` tasks, candidate fails on ``regress`` tasks, else both pass."""

    def outcome(role: str, case_id: str, repeat: int) -> bool:
        if role == "baseline":
            return case_id not in improve
        if role == "candidate":
            return case_id not in regress
        return True

    return outcome


def screened(
    w: World, pred: dict[str, Any] | None, outcome: Any = None
) -> tuple[str, Any, dict[str, Any]]:
    pid = w.propose("a2", prediction=pred)
    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(pid)
    return pid, runner, w.step(steps, "screening")


def changed(w: World, report_ref: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """(evaluated, improved, regressed) task ids of a stage, from the stored trials: improved =
    the candidate passes and the baseline fails; regressed = the reverse."""
    by: dict[str, dict[str, bool]] = {}
    for trial in w.trials(report_ref):
        if trial["arm"] in ("baseline", "candidate"):
            by.setdefault(trial["task_id"], {})[trial["arm"]] = trial["success"] is True
    both = {t for t, arms in by.items() if len(arms) == 2}
    improved = {t for t in both if by[t]["candidate"] and not by[t]["baseline"]}
    regressed = {t for t in both if by[t]["baseline"] and not by[t]["candidate"]}
    return both, improved, regressed


def scores(w: World, pid: str, stage: str) -> dict[str, Any]:
    ref = max(
        (r for r, _ in w.objects(SCORE_KIND) if r["id"] == f"predscore-{pid}-{stage}"),
        key=lambda r: r["revision"],
    )
    value: dict[str, Any] = w.store.get(w.scope, SCORE_KIND, ref)
    return value


def evaluated_dev_ids(w: World) -> list[str]:
    """The development tasks the screening stage will evaluate (frozen by the plan)."""
    pid = w.propose("a1")
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    step = w.step(runner.advance(pid), "screening")
    return sorted(changed(w, step.report_ref)[0])


# ==================================================================================================
# the prediction record (§2.12)
# ==================================================================================================
def test_a_prediction_is_stored_as_pred_proposal_id(w: World) -> None:
    pred = prediction(
        improve_task_ids=[DEV[0]], regress_task_ids=[DEV[1]],
        improve_buckets=[{"domain": "bug"}], regress_buckets=[{"domain": "data"}],
        expected_delta=0.25, risk="medium",
    )  # fmt: skip
    pid = w.propose("a2", prediction=pred)
    ((ref, record),) = [(r, v) for r, v in w.objects(PREDICTION_KIND) if r["id"] == "pred-" + pid]
    assert ref["revision"] == 1 and record["schema"] == "amplai.proposal-prediction.v1"
    assert record["proposal_id"] == pid and record["scope"] == w.scope.wire()
    for key, value in pred.items():
        assert record[key] == value
    assert record["made_at"]
    artifact = w.store.get(w.scope, "harness-change-proposal", w.head(pid)["data"]["proposal_ref"])
    import json

    change = json.loads(w.artifacts.read(w.scope, artifact["change_artifact"]))
    assert change["prediction_ref"] == ref  # §2.12: the change artifact names the prediction


def test_a_proposal_without_a_prediction_has_no_prediction_record(w: World) -> None:
    pid = w.propose("a2", prediction=None)
    assert [r for r, _ in w.objects(PREDICTION_KIND) if r["id"] == "pred-" + pid] == []
    hold("META_STATE", score_predictions, w.store, w.scope, pid, "screening")


@pytest.mark.parametrize(
    "bad",
    [
        {"improve_task_ids": "bug-dev-00"},
        {"risk": "extreme"},
        {"expected_delta": "high"},
        {"surprise": 1},
    ],
)
def test_a_malformed_prediction_is_refused_with_proposal_prediction(
    w: World, bad: dict[str, Any]
) -> None:
    fault("PROPOSAL_PREDICTION", w.propose, "a2", prediction={**prediction(), **bad})


# ==================================================================================================
# the model-facing edit: prediction fields of §9.5
# ==================================================================================================
def good_edit(**over: Any) -> dict[str, Any]:
    content = {"enabled": True, "facts": ["repo_tree"], "tree_depth": 3,
               "tree_max_entries": 100, "max_chars": 2000}  # fmt: skip
    edit: dict[str, Any] = {
        "kind": "env_bootstrap",
        "component_id": "env_bootstrap.deeper",
        "content": content,
        "rationale": "the traces show repeated directory listing",
        "hypothesis": "a bootstrap saves turns",
        "predictions": {
            "improve_task_ids": [DEV[0], DEV[1]],
            "regress_task_ids": [DEV[2]],
            "improve_buckets": [{"domain": "bug"}],
            "regress_buckets": [],
            "expected_delta": 0.2,
        },
        "risk": "low",
    }
    edit.update(over)
    return edit


def test_parse_edit_keeps_development_task_ids_and_counts_the_others() -> None:
    raw = good_edit()
    raw["predictions"]["improve_task_ids"] = [DEV[0], VAL[0], HOLD[0], "no-such-task", DEV[0]]
    raw["predictions"]["regress_task_ids"] = [VAL[1]]
    edit, dropped = parse_edit(raw, development=set(DEV))
    assert edit is not None
    assert edit["predictions"]["improve_task_ids"] == [DEV[0]]  # deduplicated, development only
    assert edit["predictions"]["regress_task_ids"] == []
    assert dropped == 4  # validation, holdout, unknown, validation (the repeat is not counted)
    assert edit["predictions"]["expected_delta"] == 0.2 and edit["risk"] == "low"


def test_parse_edit_does_not_leak_dropped_ids_into_the_edit() -> None:
    raw = good_edit()
    raw["predictions"]["improve_task_ids"] = [VAL[0], HOLD[3]]
    edit, _ = parse_edit(raw, development=set(DEV))
    assert edit is not None
    flat = repr(edit)
    assert VAL[0] not in flat and HOLD[3] not in flat


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["predictions"].update(expected_delta=1.5),
        lambda e: e["predictions"].update(expected_delta=-1.01),
        lambda e: e["predictions"].update(expected_delta=True),
        lambda e: e["predictions"].update(expected_delta="0.1"),
        lambda e: e["predictions"].update(improve_task_ids=[f"t{i}" for i in range(21)]),
        lambda e: e["predictions"].update(regress_task_ids=[f"t{i}" for i in range(21)]),
        lambda e: e["predictions"].update(improve_task_ids="bug-dev-00"),
        lambda e: e["predictions"].update(improve_task_ids=[1, 2]),
        lambda e: e["predictions"].update(improve_buckets=[{"domain": ""}]),
        lambda e: e["predictions"].update(improve_buckets=[{"unknown_key": "x"}]),
        lambda e: e["predictions"].update(regress_buckets="bug"),
        lambda e: e["predictions"].pop("regress_buckets"),
        lambda e: e["predictions"].update(extra=1),
        lambda e: e.update(risk="extreme"),
        lambda e: e.update(rationale="r" * 2001),
        lambda e: e.update(hypothesis="h" * 1001),
        lambda e: e.update(predictions=None),
    ],
)
def test_parse_edit_refuses_an_edit_that_breaks_the_output_schema(mutate: Any) -> None:
    raw = good_edit()
    assert parse_edit(raw, development=set(DEV))[0] is not None
    mutate(raw)
    assert parse_edit(raw, development=set(DEV)) == (None, 0)


def test_twenty_task_ids_per_list_are_the_limit() -> None:
    raw = good_edit()
    raw["predictions"]["improve_task_ids"] = [f"x-{i}" for i in range(20)]
    edit, dropped = parse_edit(raw, development=set(DEV))
    assert edit is not None and dropped == 20  # all outside the development split, none kept


def test_buckets_are_normalized_and_deduplicated() -> None:
    raw = good_edit()
    raw["predictions"]["improve_buckets"] = [
        {"domain": "bug"}, {"domain": "bug"}, {"task_class": "bug_fix", "domain": "bug"}, {},
    ]  # fmt: skip
    edit, _ = parse_edit(raw, development=set(DEV))
    assert edit is not None
    assert edit["predictions"]["improve_buckets"] == [
        {"domain": "bug"}, {"domain": "bug", "task_class": "bug_fix"},
    ]  # fmt: skip


@pytest.mark.parametrize("kind", ["environment_image", "judge_model", "decider", "decision_method"])
def test_l9_kinds_are_not_proposer_kinds(kind: str) -> None:
    """§9.5: ``environment_image``, ``judge_model``, ``decider``, ``decision_method`` are not
    proposer kinds."""
    assert kind not in proposer.PROPOSER_KINDS
    raw = good_edit(kind=kind, component_id=f"{kind}.x", content={})
    assert parse_edit(raw, development=set(DEV)) == (None, 0)


def test_the_proposer_kinds_are_the_twelve_of_section_9_5() -> None:
    assert proposer.PROPOSER_KINDS == (
        "role_prompt", "interpretation", "env_bootstrap", "memory_notes", "retrieval",
        "feedback_form", "attempt_policy", "execution_strategy", "driver_options", "fast_checks",
        "limits", "route_policy",
    )  # fmt: skip
    assert set(proposer.PROPOSER_KINDS) - {"driver_options"} <= set(policies.V1)


def test_the_edit_schema_names_the_predictions_of_section_9_5() -> None:
    edit = proposer.PROPOSAL_SCHEMA["properties"]["edits"]["items"]
    assert edit["properties"]["kind"]["enum"] == list(proposer.PROPOSER_KINDS)
    assert set(edit["required"]) == {
        "kind", "component_id", "content", "rationale", "hypothesis", "predictions", "risk",
    }  # fmt: skip
    predictions = edit["properties"]["predictions"]
    assert set(predictions["required"]) == {
        "improve_task_ids", "regress_task_ids", "improve_buckets", "regress_buckets",
        "expected_delta",
    }  # fmt: skip
    assert edit["properties"]["risk"]["enum"] == ["low", "medium", "high"]


# ==================================================================================================
# task-level scoring after screening (§9.6): precision and recall, independent recomputation
# ==================================================================================================
def test_screening_scores_precision_and_recall_of_the_predicted_tasks(w: World) -> None:
    sample = evaluated_dev_ids(w)
    assert len(sample) == 12
    improve = set(sample[:4])
    regress = {sample[4]}
    pred = prediction(
        improve_task_ids=[sample[0], sample[1], sample[8]],  # two right, one that did not move
        regress_task_ids=[sample[4], sample[9]],  # one right, one wrong
    )
    pid, _runner, step = screened(w, pred, scripted(improve, regress))
    evaluated, improved, regressed = changed(w, step.report_ref)
    assert improved == improve and regressed == regress  # the script did what it says
    ref = score_predictions(w.store, w.scope, pid, "screening")
    assert ref["id"] == f"predscore-{pid}-screening" and ref["revision"] == 1
    value = w.store.get(w.scope, SCORE_KIND, ref)
    assert value["schema"] == "amplai.prediction-score.v1" and value["scope"] == w.scope.wire()
    assert (value["proposal_id"], value["stage"]) == (pid, "screening")
    assert value["bucket_level"] is None and value["scored_at"]
    task = value["task_level"]
    assert task["improve"] == {
        "precision": round(2 / 3, 4),
        "recall": 0.5,
        "n_pred": 3,
        "n_actual": 4,
    }
    assert task["regress"] == {"precision": 0.5, "recall": 1.0, "n_pred": 2, "n_actual": 1}
    assert evaluated >= improved | regressed


def test_screening_scores_only_tasks_the_stage_evaluated(w: World) -> None:
    sample = evaluated_dev_ids(w)
    outside = [t for t in DEV if t not in sample]
    assert outside, "14 informative development tasks, the stage takes 12"
    improve = {sample[0]}
    pred = prediction(improve_task_ids=[sample[0], outside[0]])
    pid, _r, _s = screened(w, pred, scripted(improve, set()))
    task = w.store.get(w.scope, SCORE_KIND, score_predictions(w.store, w.scope, pid, "screening"))[
        "task_level"
    ]
    # the task the stage did not run is not a miss: it is not counted at all
    assert task["improve"] == {"precision": 1.0, "recall": 1.0, "n_pred": 1, "n_actual": 1}


def test_screening_with_nothing_predicted_has_no_precision_but_still_recall(w: World) -> None:
    sample = evaluated_dev_ids(w)
    pid, _r, _s = screened(w, prediction(), scripted({sample[0]}, set()))
    task = w.store.get(w.scope, SCORE_KIND, score_predictions(w.store, w.scope, pid, "screening"))[
        "task_level"
    ]
    assert task["improve"] == {"precision": None, "recall": 0.0, "n_pred": 0, "n_actual": 1}
    assert task["regress"] == {"precision": None, "recall": None, "n_pred": 0, "n_actual": 0}


def test_scoring_is_idempotent_and_a_changed_score_is_a_new_revision(w: World) -> None:
    sample = evaluated_dev_ids(w)
    pid, _r, _s = screened(
        w, prediction(improve_task_ids=[sample[0]]), scripted({sample[0]}, set())
    )
    first = score_predictions(w.store, w.scope, pid, "screening")
    assert score_predictions(w.store, w.scope, pid, "screening") == first
    assert len([r for r, _ in w.objects(SCORE_KIND) if r["id"] == first["id"]]) == 1


def test_scoring_needs_a_prediction_and_an_evaluated_stage(w: World) -> None:
    pid = w.propose("a2", prediction=prediction(improve_task_ids=[DEV[0]]))
    # planned, nothing evaluated yet
    runner = w.runner()
    w.script(pid)
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    hold("META_STATE", score_predictions, w.store, w.scope, pid, "screening")
    hold("META_STATE", score_predictions, w.store, w.scope, pid, "focused")
    hold("META_STATE", score_predictions, w.store, w.scope, "harness-proposal-unknown", "screening")


@pytest.mark.parametrize("stage", ["ablation", "nightly", "", "SCREENING"])
def test_only_screening_focused_and_holdout_are_scored_stages(w: World, stage: str) -> None:
    pid, _r, _s = screened(w, prediction())
    fault("PREDICTION_STAGE", score_predictions, w.store, w.scope, pid, stage)


# ==================================================================================================
# bucket-level scoring after focused and holdout (IC-11), operator-only
# ==================================================================================================
def domain_of(task_id: str) -> str:
    return task_id.split("-")[0]


def test_focused_scores_buckets_by_the_sign_of_the_domain_delta(w: World) -> None:
    """IC-11: the proposer predicts buckets for validation and holdout, not task ids."""
    improve_domain, regress_domain = "bug", "data"
    improve = {t for t in VAL if domain_of(t) == improve_domain}
    regress = {t for t in VAL if domain_of(t) == regress_domain}
    pred = prediction(
        improve_buckets=[{"domain": improve_domain}, {"domain": "feature"}],
        regress_buckets=[{"domain": regress_domain}],
    )
    pid, runner, _s = screened(w, pred, scripted(improve, regress))
    focused = runner.approve_stage(pid, "focused")
    assert focused.report_ref is not None
    ref = score_predictions(w.store, w.scope, pid, "focused")
    assert ref["id"] == f"predscore-{pid}-focused"
    value = w.store.get(w.scope, SCORE_KIND, ref)
    assert value["stage"] == "focused" and value["task_level"] is None
    bucket = value["bucket_level"]
    deltas = {b["bucket"]["domain"]: b["delta"] for b in bucket["buckets"]}
    assert deltas[improve_domain] > 0 and deltas[regress_domain] < 0
    assert deltas["feature"] == 0  # predicted to improve, did not move
    assert bucket["improve"]["precision"] == 0.5 and bucket["improve"]["recall"] == 1.0
    assert bucket["regress"] == {"precision": 1.0, "recall": 1.0, "n_pred": 1, "n_actual": 1}
    assert bucket["unscored"] == 0
    assert all(isinstance(b["n_tasks"], int) and b["n_tasks"] > 0 for b in bucket["buckets"])


# ==================================================================================================
# every score due is written without a proposer run (§9.6 "stored and scored after evaluation")
# ==================================================================================================
def test_a_screened_proposal_gets_its_screening_score_without_a_proposer_run(w: World) -> None:
    sample = evaluated_dev_ids(w)
    pid, runner, _s = screened(
        w, prediction(improve_task_ids=[sample[0]]), scripted({sample[0]}, set())
    )
    refs = proposer.score_evaluated(w.store, w.scope, pid)
    assert set(refs) == {"screening"} and refs["screening"]["id"] == f"predscore-{pid}-screening"
    assert w.objects(proposer.RUN_KIND) == []  # no proposer run wrote it
    assert scores(w, pid, "screening")["task_level"]["improve"]["precision"] == 1.0
    # after focused both scores are due; an unchanged score is not rewritten
    runner.approve_stage(pid, "focused")
    again = proposer.score_evaluated(w.store, w.scope, pid)
    assert set(again) == {"screening", "focused"} and again["screening"] == refs["screening"]
    assert scores(w, pid, "focused")["bucket_level"] is not None


def test_score_evaluated_writes_nothing_without_a_prediction_or_a_report(w: World) -> None:
    pid, _r, _s = screened(w, None)
    assert proposer.score_evaluated(w.store, w.scope, pid) == {}
    planned_only = w.propose("a2", prediction=prediction())
    assert proposer.score_evaluated(w.store, w.scope, planned_only) == {}
    assert w.objects(SCORE_KIND) == []


def score_command(w: World, monkeypatch: Any, *args: str) -> Any:
    """``amplai meta proposer score`` on the World's store (the deployment open is the only
    stand-in)."""
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from amplai_foundry.runtime import cli
    from amplai_foundry.runtime.meta_commands import proposer as command

    @contextmanager
    def opened(config: Any) -> Any:
        yield SimpleNamespace(store=w.store, scope=w.scope)

    monkeypatch.setattr(command, "opened_deployment", opened)
    result = CliRunner().invoke(cli.app, ["meta", "proposer", "score", *args])
    return result.exit_code, json.loads(result.output)


def test_the_score_command_writes_the_due_scores(w: World, monkeypatch: Any) -> None:
    sample = evaluated_dev_ids(w)
    pid, _r, _s = screened(
        w, prediction(improve_task_ids=[sample[0]]), scripted({sample[0]}, set())
    )
    code, out = score_command(w, monkeypatch, pid)
    assert code == 0 and out["proposal_id"] == pid and set(out["scores"]) == {"screening"}
    assert out["scores"]["screening"]["score_ref"]["id"] == f"predscore-{pid}-screening"
    assert out["scores"]["screening"]["task_level"]["improve"]["recall"] == 1.0
    code, one = score_command(w, monkeypatch, pid, "--stage", "screening")
    assert (
        code == 0
        and one["scores"]["screening"]["score_ref"] == out["scores"]["screening"]["score_ref"]
    )
    code, held = score_command(w, monkeypatch, pid, "--stage", "focused")
    assert code == 3 and held["code"] == "META_STATE"  # focused has no report yet


def test_the_score_command_holds_for_a_proposal_without_a_prediction(
    w: World, monkeypatch: Any
) -> None:
    pid, _r, _s = screened(w, None)
    code, out = score_command(w, monkeypatch, pid)
    assert code == 3 and out["code"] == "META_STATE"


def test_a_task_class_bucket_is_not_scored_and_is_counted() -> None:
    units = {
        "a": {"domain": "bug", "baseline": False, "candidate": True},
        "b": {"domain": "bug", "baseline": False, "candidate": True},
    }
    value = bucket_scores(
        prediction(improve_buckets=[{"domain": "bug"}, {"task_class": "bug_fix"},
                                    {"domain": "bug", "task_class": "bug_fix"},
                                    {"domain": "unseen"}]),
        units,
    )  # fmt: skip
    assert value["unscored"] == 3  # task_class buckets and a domain without evaluated tasks
    assert value["improve"] == {"precision": 1.0, "recall": 1.0, "n_pred": 1, "n_actual": 1}


def test_bucket_delta_is_the_mean_candidate_minus_baseline_per_domain() -> None:
    units = {
        "a": {"domain": "bug", "baseline": False, "candidate": True},
        "b": {"domain": "bug", "baseline": True, "candidate": True},
        "c": {"domain": "bug", "baseline": True, "candidate": False},
        "d": {"domain": "bug", "baseline": False, "candidate": True},
        "e": {"domain": "data", "baseline": True, "candidate": False},
        "f": {"domain": "data", "baseline": None, "candidate": True},  # unpaired: not evaluated
    }
    value = bucket_scores(prediction(), units)
    by = {b["bucket"]["domain"]: b for b in value["buckets"]}
    assert by["bug"] == {"bucket": {"domain": "bug"}, "delta": 0.25, "n_tasks": 4}
    assert by["data"] == {"bucket": {"domain": "data"}, "delta": -1.0, "n_tasks": 1}


def test_task_scores_treat_an_unpaired_task_as_not_evaluated() -> None:
    units = {
        "a": {"domain": "bug", "baseline": False, "candidate": True},
        "b": {"domain": "bug", "baseline": None, "candidate": True},
        "c": {"domain": "bug", "baseline": True, "candidate": False},
    }
    value = task_scores(
        prediction(improve_task_ids=["a", "b"], regress_task_ids=["c", "zz"]), units
    )
    assert value["improve"] == {"precision": 1.0, "recall": 1.0, "n_pred": 1, "n_actual": 1}
    assert value["regress"] == {"precision": 1.0, "recall": 1.0, "n_pred": 1, "n_actual": 1}


# ==================================================================================================
# the proposer sees screening scores only (§9.4, §9.6)
# ==================================================================================================
def test_only_the_screening_score_is_a_proposer_input(w: World) -> None:
    from amplai_foundry.meta_harness.archive import EliteArchive
    from amplai_foundry.meta_harness.leak_gate import LeakGate
    from amplai_foundry.meta_harness.proposer import ProposerEnsemble
    from amplai_foundry.meta_harness.traces import TraceService

    improve = {t for t in VAL if domain_of(t) == "bug"}
    pid, runner, _s = screened(
        w, prediction(improve_buckets=[{"domain": "bug"}]), scripted(improve, set())
    )
    runner.approve_stage(pid, "focused")
    focused_ref = score_predictions(w.store, w.scope, pid, "focused")
    assert focused_ref["id"].endswith("-focused")
    ensemble = ProposerEnsemble(
        w.ops,
        breadth=object(),  # type: ignore[arg-type]
        depth=object(),  # type: ignore[arg-type]
        traces=TraceService(w.store, w.scope, w.artifacts),
        archive=EliteArchive(w.store, w.scope),
        leak_gate=LeakGate(w.operator, w.store, w.refs["leak_index_ref"]),
        components=w.ops.components,
    )
    inputs = ensemble.inputs(CELL)
    (entry,) = [p for p in inputs["proposals"] if p["proposal_id"] == pid]
    assert entry["screening_score"] is not None and entry["screening_score"]["task_level"]
    flat = repr(inputs)
    assert "bucket_level" not in flat and "buckets'" not in repr(entry["screening_score"])
    assert "focused" not in repr(entry["screening_score"])
    # the screening score was written on demand; the operator-only focused score exists apart
    ids = {r["id"] for r, _ in w.objects(SCORE_KIND)}
    assert f"predscore-{pid}-screening" in ids and f"predscore-{pid}-focused" in ids


# ==================================================================================================
# the stage runner scores and archives after each evaluated stage (§9.6, §9.9)
# ==================================================================================================
def archive_head(w: World) -> dict[str, Any]:
    from amplai_foundry.meta_harness.archive import EliteArchive

    head = EliteArchive(w.store, w.scope).head(CELL)
    assert head is not None
    return head


def lineage_of(w: World, pid: str) -> dict[str, Any]:
    (entry,) = [e for e in archive_head(w)["lineage"] if e["proposal_id"] == pid]
    return entry


def test_a_screened_proposal_gets_its_screening_score_from_the_stage_runner(w: World) -> None:
    sample = evaluated_dev_ids(w)
    pid, runner, step = screened(
        w, prediction(improve_task_ids=[sample[0]]), scripted({sample[0]}, set())
    )
    assert step.state == "passed"
    # written by StageRunner after the screening report: no proposer run, no score command
    assert w.objects(proposer.RUN_KIND) == []
    assert scores(w, pid, "screening")["task_level"]["improve"]["precision"] == 1.0
    assert [r["id"] for r, _ in w.objects(SCORE_KIND)] == [f"predscore-{pid}-screening"]
    # the archive took the candidate's development rows; no lineage verdict for screening
    entry = lineage_of(w, pid)
    assert entry["child"] == w.proposal(pid)["candidate_ref"] and entry["verdict"] is None
    # focused: the bucket score (operator-only) and the operator-only lineage verdict
    focused = runner.approve_stage(pid, "focused")
    assert scores(w, pid, "focused")["bucket_level"] is not None
    assert lineage_of(w, pid)["verdict"] == f"focused:{focused.decision_class}"
    assert w.stage_run(pid)["data"]["stages"]["focused"]["guard_findings"] == []


def test_the_scoring_hook_is_idempotent(w: World) -> None:
    pid, runner, _step = screened(w, prediction(improve_task_ids=[DEV[0]]))
    before = (w.objects(SCORE_KIND), archive_head(w)["lineage"], archive_head(w)["elites"])
    rows = [v for _r, v in w.objects("trial-metrics")]
    runner._after_report(pid, CELL, "screening", w.proposal(pid)["candidate_ref"], rows, None)
    assert w.objects(SCORE_KIND) == before[0]  # an unchanged score is not rewritten
    assert (archive_head(w)["lineage"], archive_head(w)["elites"]) == before[1:]
    assert w.stage_run(pid)["data"]["stages"]["screening"]["guard_findings"] == []


def test_a_proposal_without_a_prediction_is_archived_but_not_scored(w: World) -> None:
    pid, _runner, step = screened(w, None)
    assert step.state == "passed" and w.objects(SCORE_KIND) == []
    assert lineage_of(w, pid)["verdict"] is None
    assert w.stage_run(pid)["data"]["stages"]["screening"]["guard_findings"] == []


def test_ablation_variants_reach_the_archive_lineage_without_a_score(w: World) -> None:
    pid, runner, _step = screened(w, prediction(improve_task_ids=[DEV[0]]))
    runner.approve_stage(pid, "focused")
    runner.advance(pid)
    derived = w.stage_run(pid)["data"]["stages"]["ablation"]["ablation_proposals"]
    assert len(derived) == 2
    for d in derived:
        assert lineage_of(w, d)["verdict"] is None
        assert not any(r["id"].startswith(f"predscore-{d}-") for r, _ in w.objects(SCORE_KIND))


def test_a_scoring_failure_is_a_finding_and_never_changes_the_verdict(
    w: World, monkeypatch: Any
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("scorer down")

    monkeypatch.setattr(proposer, "score_predictions", broken)
    pid, runner, step = screened(w, prediction(improve_task_ids=[DEV[0]]))
    assert step.state == "passed" and w.state(pid) == "screened"
    findings = w.stage_run(pid)["data"]["stages"]["screening"]["guard_findings"]
    assert findings == ["PREDICTION_SCORE screening: RuntimeError"]
    assert w.objects(SCORE_KIND) == []
    assert lineage_of(w, pid)["verdict"] is None  # the archive still ran
    # the next stage still opens: a finding is not a hack guard
    assert w.step(runner.status(pid), "focused").waiting_for == "approve-stage focused"
    monkeypatch.undo()
    # a later call (amplai meta proposer score) still writes the due score
    assert set(proposer.score_evaluated(w.store, w.scope, pid)) == {"screening"}


def test_an_archive_failure_is_a_finding_and_never_changes_the_verdict(
    w: World, monkeypatch: Any
) -> None:
    from amplai_foundry.meta_harness.archive import EliteArchive

    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeFault("ELITE_ARCHIVE", "archive refused")

    monkeypatch.setattr(EliteArchive, "update", broken)
    pid, runner, step = screened(w, prediction(improve_task_ids=[DEV[0]]))
    assert step.state == "passed"
    assert w.stage_run(pid)["data"]["stages"]["screening"]["guard_findings"] == [
        "ELITE_ARCHIVE screening: ELITE_ARCHIVE"
    ]
    assert scores(w, pid, "screening")["task_level"] is not None  # scoring still ran
    focused = runner.approve_stage(pid, "focused")
    assert focused.state == "passed"
    assert w.stage_run(pid)["data"]["stages"]["focused"]["guard_findings"] == [
        "ELITE_ARCHIVE focused: ELITE_ARCHIVE"
    ]


def test_a_failed_screening_is_still_scored_and_archived(w: World) -> None:
    """A rejected candidate (screening class regression) has a report: it is scored."""
    pid, _runner, step = screened(
        w, prediction(regress_task_ids=[DEV[0]]), scripted(set(), set(DEV))
    )
    assert step.state == "failed" and w.state(pid) == "rejected"
    assert scores(w, pid, "screening")["task_level"]["regress"]["precision"] == 1.0
    assert lineage_of(w, pid)["proposal_id"] == pid
