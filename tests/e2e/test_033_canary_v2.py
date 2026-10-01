"""Work 033 (AC-11 path): a corpus v2 proposal from holdout to canary, promotion and rollback.

Contract: specs/033-harness-taxonomy/interfaces.md §1.2 (promotion of router candidates), §8.1
(stages, then "canary / promote / rollback (existing commands)"), §12.1, IC-01 (the holdout
experiment is the one bound to the evolution machine), IC-12/IC-31 (the stage plan's app and task
environment pins), IC-17 (the nightly identity never reaches canary or promotion), IC-24 (the
removal gate guards ``approve_canary``), IC-29 (the executor qualification restored from the
operator's record); spec.md AC-11.

Real: the rc06 local product with ``LocalMeta`` (the real ``MetaHarness`` evolution machine,
``EvaluationService``, releases), a frozen corpus v2, ``LocalMetaOps``, ``StageRunner`` and the
``amplai meta`` command line over a real ``amplai ops local-init`` deployment. Stand-in: the trial
executor (scripted outcomes with receipts of the receipt v2 shape,
``tests/v3/test_033_s11_stages.py``) and the calibration summary of the stage plan. No driver,
docker or network.

Covered: screening -> focused -> holdout (scripted passes) -> ``approve_canary`` with development
and validation tasks -> ``run_canary`` (each task once, on the candidate, mode ``canary``, from its
frozen case) -> ``promote`` (the cell's new plans use the candidate) -> ``rollback``; the refusals
(a holdout task, an unknown or duplicate task, a non-human identity, a failed holdout, a failing
canary task stops before promotion, a task edited after the freeze, a component proposal taken
through the Work 030 experiment without a stage plan); the command line dispatch
(a proposal with a stage plan opens the corpus v2 with the plan's cell and app; a Work 030
proposal keeps its corpus and options).
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
    TRIAL_TOKENS,
    World,
    build_world,
    hold,
    task_ids,
    write_corpus,
)
from test_rc12_meta_cli import mini_corpus

from amplai_foundry.meta_harness.local_canary import CANARY_MODE
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.execution import meta_ops, releases
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.execution.meta_local import nightly_actor
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from amplai_foundry.runtime.meta_cli import opened_gate
from rc06_rig import codex_inputs, make_repo

DEV, VAL = task_ids("development")[0], task_ids("validation")[0]
HOLDOUT = task_ids("holdout")[0]


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


def through_holdout(w: World, outcome: Any = None) -> str:
    """A class A corpus v2 proposal through screening, focused, ablation and holdout."""
    pid = w.propose("a2")
    w.script(pid, outcome)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert w.ops.approve_stage(pid, "focused")["state"] == "passed"
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    w.ops.approve_stage(pid, "holdout")
    assert w.state(pid) == "offline_evaluated"
    return pid


def effective(w: World) -> dict[str, Any]:
    app = w.rig.service.apps["app"]
    return dict(releases.effective(w.store, w.scope, app.compositions))


def active(w: World) -> dict[str, Any]:
    head = w.store.head(w.scope, releases.POINTER_KIND, releases.POINTER_ID)
    return dict(head["data"]["release_ref"])


def policies(w: World) -> int:
    return len(w.objects("canary-policy"))


# ==================================================================================================
# AC-11: holdout -> canary -> promote -> rollback
# ==================================================================================================
def test_a_v2_proposal_goes_from_holdout_through_canary_to_promotion_and_back(w: World) -> None:
    pid = through_holdout(w)
    proposal = w.proposal(pid)
    baseline_release = active(w)
    assert effective(w)[CELL] == proposal["baseline_ref"]

    approved = w.ops.approve_canary(pid, [DEV, VAL], max_trial_tokens=TRIAL_TOKENS)
    assert approved["tasks"] == [DEV, VAL] and w.state(pid) == "canary_approved"
    policy = w.store.get(w.scope, "canary-policy", approved["policy_ref"])
    binding = w.rig.service.apps["app"].binding_ref  # the stage plan's app (IC-31)
    assert policy["task_binding_refs"] == {DEV: binding, VAL: binding}
    assert policy["fallback_release_ref"] == baseline_release

    calls = len(w.executor.calls)
    w.store.epoch += 1  # run-canary is another command, i.e. another process
    ran = w.ops.run_canary(pid)
    assert ran["state"] == "promotion_pending", ran
    # each task once, on the candidate
    assert w.executor.calls[calls:] == [("candidate", DEV, 0), ("candidate", VAL, 0)]
    trials = w.head(pid)["data"]["canary_trials"]
    assert [(t["task_id"], t["success"]) for t in trials] == [(DEV, True), (VAL, True)]
    for trial in trials:  # a canary receipt over the trial receipt of the frozen case
        receipt = json.loads(w.artifacts.read(w.scope, trial["artifact_ref"], trusted=True))
        assert receipt["mode"] == CANARY_MODE
        assert receipt["composition_ref"] == proposal["candidate_ref"]
        inner = json.loads(w.artifacts.read(w.scope, receipt["trial_receipt_ref"], trusted=True))
        assert inner["mode"] == CANARY_MODE
        assert inner["split"] == ("development" if trial["task_id"] == DEV else "validation")
    # nothing changed before the operator promotes
    assert active(w) == baseline_release and effective(w)[CELL] == proposal["baseline_ref"]

    promoted = w.ops.promote(pid)
    assert w.state(pid) == "promoted"
    assert active(w) == promoted["candidate_release_ref"] != baseline_release
    assert promoted["baseline_release_ref"] == baseline_release
    assert effective(w)[CELL] == proposal["candidate_ref"]  # the cell's new plans use it
    selected = w.rig.service.select_composition(w.rig.service.apps["app"])
    assert selected["cell_id"] == CELL and selected["ref"] == proposal["candidate_ref"]

    rolled = w.ops.rollback(pid)
    assert rolled["state"] == "rolled_back" == w.state(pid)
    assert active(w) == baseline_release
    assert effective(w)[CELL] == proposal["baseline_ref"]
    selected = w.rig.service.select_composition(w.rig.service.apps["app"])
    assert selected["ref"] == proposal["baseline_ref"]


# ==================================================================================================
# refusals
# ==================================================================================================
def test_a_holdout_task_is_never_a_canary_task(w: World) -> None:
    pid = through_holdout(w)
    before = policies(w)
    error = hold("CANARY_TASKS", w.ops.approve_canary, pid, [HOLDOUT], max_trial_tokens=10)
    assert error.details["holdout"] == [HOLDOUT]
    error = hold("CANARY_TASKS", w.ops.approve_canary, pid, [DEV, HOLDOUT], max_trial_tokens=10)
    assert error.details["holdout"] == [HOLDOUT]
    # a task of no corpus, a task given twice, no task
    error = hold("CANARY_TASKS", w.ops.approve_canary, pid, ["t0"], max_trial_tokens=10)
    assert error.details["unknown"] == ["t0"]
    error = hold("CANARY_TASKS", w.ops.approve_canary, pid, [DEV, DEV], max_trial_tokens=10)
    assert error.details["duplicate"] == [DEV]
    hold("CANARY_TASKS", w.ops.approve_canary, pid, [], max_trial_tokens=10)
    assert policies(w) == before and w.state(pid) == "offline_evaluated"


def test_the_nightly_and_the_proposer_identity_reach_no_canary_promotion_or_rollback(
    w: World,
) -> None:
    pid = through_holdout(w)
    human = w.ops.operator
    others = (nightly_actor(w.scope), w.local.proposer, replace(human, kind="service"))

    def refused(gate: Any, *args: Any, **kwargs: Any) -> None:
        for actor in others:
            w.ops.operator = actor
            try:
                hold("APPROVAL_HUMAN", gate, *args, **kwargs)
            finally:
                w.ops.operator = human

    before = policies(w)
    refused(w.ops.approve_canary, pid, [DEV], max_trial_tokens=TRIAL_TOKENS)
    assert policies(w) == before and w.state(pid) == "offline_evaluated"
    w.ops.approve_canary(pid, [DEV], max_trial_tokens=TRIAL_TOKENS)
    calls = len(w.executor.calls)
    refused(w.ops.run_canary, pid)
    assert w.state(pid) == "canary_approved" and len(w.executor.calls) == calls
    assert w.ops.run_canary(pid)["state"] == "promotion_pending"
    release = active(w)
    refused(w.ops.promote, pid)
    assert w.state(pid) == "promotion_pending" and active(w) == release
    w.ops.promote(pid)
    promoted = active(w)
    refused(w.ops.rollback, pid)
    assert w.state(pid) == "promoted" and active(w) == promoted


def test_a_failing_canary_task_stops_the_canary_before_promotion(w: World) -> None:
    pid = through_holdout(w)
    w.ops.approve_canary(pid, [DEV, VAL, task_ids("validation")[1]], max_trial_tokens=TRIAL_TOKENS)
    release = active(w)
    w.executor.outcome = lambda role, case, repeat: not (role == "candidate" and case == VAL)
    calls = len(w.executor.calls)
    ran = w.ops.run_canary(pid)
    assert ran["state"] == "aborted"
    assert ran["outcomes"][-1]["reason"] == "canary_verifier_failure"
    # the canary stopped at the failing task: the third task never ran
    assert [c[1] for c in w.executor.calls[calls:]] == [DEV, VAL]
    incidents = w.head(pid)["data"]["canary_incidents"]
    assert [(i["task_id"], i["reason"]) for i in incidents] == [(VAL, "canary_verifier_failure")]
    hold("META_STATE", w.ops.promote, pid)
    assert active(w) == release and effective(w)[CELL] == w.proposal(pid)["baseline_ref"]


def test_a_canary_task_runs_once_and_a_finished_canary_is_not_rerun(w: World) -> None:
    pid = through_holdout(w)
    w.ops.approve_canary(pid, [DEV], max_trial_tokens=TRIAL_TOKENS)
    w.ops.run_canary(pid)
    calls = len(w.executor.calls)
    hold("META_STATE", w.ops.run_canary, pid)  # promotion_pending: no second run
    assert len(w.executor.calls) == calls


def test_a_failed_holdout_reaches_no_canary(w: World) -> None:
    pid = through_holdout(
        w, lambda role, case, repeat: not (role == "candidate" and "-hol-" in case)
    )
    assert w.head(pid)["data"]["verdict"] == "fail"
    before = policies(w)
    hold("EVAL_NOT_PASSING", w.ops.approve_canary, pid, [DEV], max_trial_tokens=10)
    assert policies(w) == before


def test_a_screened_proposal_before_its_holdout_reaches_no_canary(w: World) -> None:
    pid = w.propose("a2")
    w.script(pid)
    w.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert w.state(pid) == "screened"
    hold("META_STATE", w.ops.approve_canary, pid, [DEV], max_trial_tokens=10)


def test_the_holdout_stage_must_be_the_passed_stage_bound_to_the_machine(w: World) -> None:
    pid = through_holdout(w)
    run = w.stage_run(pid)
    stages = json.loads(json.dumps(run["data"]["stages"]))
    stages["holdout"]["state"] = "failed"  # a stage run that does not show the passed holdout
    with w.store.tx() as db:
        w.store.cas(db, w.scope, "stage-run", "stagerun-" + pid, run["row_version"], run["state"],
                    {**run["data"], "stages": stages})  # fmt: skip
    hold("EVAL_NOT_PASSING", w.ops.approve_canary, pid, [DEV], max_trial_tokens=10)


def test_a_canary_task_edited_after_the_freeze_holds_before_the_canary_starts(w: World) -> None:
    pid = through_holdout(w)
    w.ops.approve_canary(pid, [DEV], max_trial_tokens=TRIAL_TOKENS)
    task = next(t for t in w.ops.corpus.tasks if t.task_id == DEV)
    edited = replace(task, objective=task.objective + " Edited.")
    w.ops.corpus = replace(
        w.ops.corpus, tasks=tuple(edited if t.task_id == DEV else t for t in w.ops.corpus.tasks)
    )
    calls = len(w.executor.calls)
    hold("CORPUS_CHANGED", w.ops.run_canary, pid)
    assert w.state(pid) == "canary_approved" and len(w.executor.calls) == calls


def test_a_work_030_corpus_cannot_run_a_v2_proposals_canary(w: World) -> None:
    pid = through_holdout(w)
    plan = meta_ops.stage_plan(w.store, w.scope, pid)
    assert plan is not None and plan["cell_id"] == CELL and plan["app_id"] == "app"
    assert meta_ops.stage_plan(w.store, w.scope, "harness-proposal-none") is None
    w.ops.corpus = object()  # type: ignore[assignment]
    hold("META_STATE", w.ops._canary_cases, plan, [DEV])


# ==================================================================================================
# the command line: a proposal with a stage plan opens the corpus v2 (no driver is run)
# ==================================================================================================
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
    mini_corpus(tmp_path / "w030")
    content = tmp_path / "content.json"
    lines = [
        "You are the IMPLEMENTER for {app_id} at commit {base_commit}.",
        "Make the change below and run the acceptance commands yourself. Do not commit.",
        "Prefer small edits.",
    ]
    content.write_text(json.dumps({"implementer": lines}))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("\n".join(lines) + "\n")
    return {"config": str(where / "local.json"), "corpus": str(tmp_path / "corpus"),
            "w030": str(tmp_path / "w030"), "content": str(content),
            "prompt": str(prompt)}  # fmt: skip


def run_meta(*args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(cli.app, ["meta", *args])
    text = result.output.strip()
    return result.exit_code, json.loads(text[text.index("{") :]) if "{" in text else {}


def v2_proposal(home: dict[str, str]) -> str:
    """A component proposal by command with a stage plan record of cell CELL and app ``app``."""
    config = ["--config", home["config"]]
    code, added = run_meta("component", "add", "--kind", "role_prompt", "--name", "cv2",
                           "--file", home["content"], *config)  # fmt: skip
    assert code == 0, added
    code, out = run_meta(
        "propose-components", "--cell", CELL, "--set", "role_prompt=role_prompt.cv2@1",
        "--suffix", "cv2", "--hypothesis", "h", "--benefit", "b", "--risk", "r",
        "--observation", "o", *config, "--corpus", home["corpus"],
    )  # fmt: skip
    assert code == 0, out
    pid = str(out["proposal_id"])
    dep = LocalProductDeployment(Path(home["config"]), start_loop=False)
    try:
        with dep.store.tx() as db:
            dep.store.put(db, dep.scope, "stage-plan", "stageplan-" + pid, 1,
                          {"proposal_id": pid, "cell_id": CELL, "app_id": "app"})  # fmt: skip
    finally:
        dep.close()
    return pid


def test_a_proposal_with_a_stage_plan_opens_the_corpus_v2_with_its_cell_and_app(
    home: dict[str, str],
) -> None:
    from amplai_foundry.meta_harness.corpus_v2 import CorpusV2
    from amplai_foundry.meta_harness.local_corpus import Corpus

    pid = v2_proposal(home)
    with opened_gate(Path(home["config"]), pid, corpus=Path(home["corpus"])) as (ops, v2):
        assert v2 and isinstance(ops.corpus, CorpusV2)
        assert ops.driver == CELL and ops.app.config.app_id == "app"
    with opened_gate(Path(home["config"]), "harness-proposal-w030",
                     corpus=Path(home["w030"])) as (ops, v2):  # fmt: skip
        assert not v2 and isinstance(ops.corpus, Corpus) and ops.driver == "codex-cli"


def test_the_canary_commands_dispatch_a_v2_proposal_and_check_its_cell_and_app(
    home: dict[str, str],
) -> None:
    pid = v2_proposal(home)
    common = ("--config", home["config"], "--corpus", home["corpus"])
    # the gates need their states: a draft reaches none of them (the corpus v2 path ran)
    for gate in (
        ("approve-canary", pid, "--tasks", DEV, "--max-trial-tokens", "10"),
        ("run-canary", pid, "--per-trial-tokens", "10", "--basis", "x", "--evidence", "y"),
        ("promote", pid),
        ("rollback", pid),
    ):
        code, out = run_meta(*gate, *common)
        assert code == 3 and out["code"] == "META_STATE", (gate[0], out)
    # the stage plan names the cell and the app
    code, out = run_meta("approve-canary", pid, "--tasks", DEV, "--max-trial-tokens", "10",
                         "--driver", "claude-cli", *common)  # fmt: skip
    assert code == 3 and out["code"] == "CELL_UNKNOWN"
    code, out = run_meta("promote", pid, "--app", "other", *common)
    assert code == 3 and out["code"] == "TARGET_UNKNOWN"
    code, out = run_meta("promote", pid, "--driver", CELL, "--app", "app", *common)
    assert code == 3 and out["code"] == "META_STATE"


def test_run_canary_of_a_v2_proposal_restores_the_operators_executor_qualification(
    home: dict[str, str],
) -> None:
    pid = v2_proposal(home)
    common = ("--config", home["config"], "--corpus", home["corpus"])
    # no IC-29 record yet and no flags: nothing to restore
    code, out = run_meta("run-canary", pid, *common)
    assert code == 3 and out["code"] == "QUALIFIED_EXECUTOR_REQUIRED"
    code, out = run_meta("run-canary", pid, "--basis", "x", *common)  # the flags go together
    assert code == 2 and out["code"] == "LOCAL_INPUT"
    code, out = run_meta("run-canary", pid, "--per-trial-tokens", "10", "--basis", "scripted",
                         "--evidence", "tests", *common)  # fmt: skip
    assert code == 3 and out["code"] == "META_STATE"
    # the record the flags wrote is restored: no flags needed now
    code, out = run_meta("run-canary", pid, *common)
    assert code == 3 and out["code"] == "META_STATE"


def test_a_work_030_proposal_keeps_its_corpus_and_options(home: dict[str, str]) -> None:
    common = ("--config", home["config"], "--corpus", home["w030"])
    code, out = run_meta(
        "propose", "--suffix", "w030", "--prompt-file", home["prompt"], "--hypothesis", "h",
        "--benefit", "b", "--observation", "o", "--risk", "r", *common,
    )  # fmt: skip
    assert code == 0, out
    pid = str(out["proposal_id"])
    # a Work 030 canary still needs the executor qualification flags
    code, out = run_meta("run-canary", pid, *common)
    assert code == 2 and out["code"] == "LOCAL_INPUT"
    code, out = run_meta("run-canary", pid, "--per-trial-tokens", "10", "--basis", "x",
                         "--evidence", "y", *common)  # fmt: skip
    assert code == 3 and out["code"] == "META_STATE"
    code, out = run_meta("approve-canary", pid, "--tasks", "t00", "--max-trial-tokens", "10",
                         "--driver", "codex-cli", *common)  # fmt: skip
    assert code == 3 and out["code"] == "META_STATE"
    # --app names a corpus v2 proposal's app only
    code, out = run_meta("promote", pid, "--app", "app", *common)
    assert code == 2 and out["code"] == "LOCAL_INPUT"


# ==================================================================================================
# a component proposal without a stage plan reaches no canary on the Work 030 path (IC-01)
# ==================================================================================================
def work030_offline_evaluated(w: World, tmp_path: Path, pid: str) -> None:
    """``pid`` through the Work 030 gates (screen, approve-experiment, run-experiment) on a Work
    030 corpus, with no stage plan: what ``amplai meta`` does when the operator skips ``search``."""
    from amplai_foundry.meta_harness import local_corpus

    root = mini_corpus(tmp_path / "w030")
    # 16 tasks: the smallest corpus for which "no difference" passes margin 0.25 at 0.95
    # (tests/e2e/test_rc12_meta_rehearsal.py TASKS)
    manifest = json.loads((root / "manifest.json").read_text())
    for n in range(2, 16):
        task_id = f"t{n:02d}"
        (root / "tasks" / task_id).mkdir()
        for sub in ("hidden", "reference"):
            dest = root / "tasks" / task_id / sub
            dest.mkdir()
            for f in (root / "tasks" / "t00" / sub).iterdir():
                (dest / f.name).write_bytes(f.read_bytes())
        task = json.loads((root / "tasks" / "t00" / "task.json").read_text())
        task["task_id"] = task_id
        (root / "tasks" / task_id / "task.json").write_text(json.dumps(task))
        manifest["task_ids"].append(task_id)
    (root / "manifest.json").write_text(json.dumps(manifest))
    w.script(pid)
    w.ops.screen(pid)
    w.ops.corpus = local_corpus.load(root)
    w.ops.qualify_executor("scripted", ["tests/e2e/test_033_canary_v2.py"], TRIAL_TOKENS)
    w.ops.approve_experiment(pid, max_tokens=32 * TRIAL_TOKENS, max_wall_seconds=60)
    assert w.ops.run_experiment(pid)["verdict"] == "pass"
    assert w.state(pid) == "offline_evaluated"
    assert meta_ops.stage_plan(w.store, w.scope, pid) is None


@pytest.mark.parametrize("kind", ["a2", "b2"], ids=["two-components", "class-b"])
def test_a_component_proposal_without_a_holdout_stage_reaches_no_canary(
    w: World, tmp_path: Path, kind: str
) -> None:
    pid = w.propose(kind)
    if kind == "b2":
        w.ops.review(pid, outcome="pass", note="reviewed")
    work030_offline_evaluated(w, tmp_path, pid)
    before = policies(w)
    error = hold("EVAL_NOT_PASSING", w.ops.approve_canary, pid, ["t00"], max_trial_tokens=10)
    assert error.details == {"holdout": None, "stage_plan": None}
    assert policies(w) == before and w.state(pid) == "offline_evaluated"


def test_a_work_030_shaped_change_keeps_the_work_030_canary(w: World, tmp_path: Path) -> None:
    """One role_prompt change and no prediction, proposer run or origin: what ``propose`` writes."""
    pid = w.propose("a1")
    work030_offline_evaluated(w, tmp_path, pid)
    approved = w.ops.approve_canary(pid, ["t00"], max_trial_tokens=TRIAL_TOKENS)
    assert approved["tasks"] == ["t00"] and w.state(pid) == "canary_approved"
