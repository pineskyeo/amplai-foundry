"""Work 033 S13: proposer ensemble, dreaming job, removal sweep (interfaces.md §9.4-§9.10, §3.12,
IC-14, AC-07 unit part, ``plan.md`` §11).

The proposer reads development data only (the input builder filters ``trial-metrics`` on the
record's ``split`` field, reads traces as the proposer identity and takes the archive's
``proposer_view``). Its read-only turns run on an empty scratch directory. A cheap cell drafts
several component edits, near-duplicates of seen edits are dropped, every draft passes the leak
gate,
a strong cell refines the top ones, and the survivors become draft proposals: the proposer never
approves, screens or promotes anything.

Real: store, CAS, rc06 product rig, ``LocalMeta``, frozen corpus v2 with its leak index, evaluator,
``StageRunner``, ``TraceService``, ``EliteArchive``, ``ProposerEnsemble``, ``dream`` and
``removal_sweep``. Stand-in: the two read-only turns (scripted answers that record what they saw in
their workspace) and the trial executor of the S11 ``World``. No driver, docker or network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_033_s11_stages import (
    BUDGET,
    CELL,
    GOOD,
    KEY,
    World,
    build_world,
    fault,
    hold,
    task_ids,
)
from test_033_s13_traces_acl import admit

from amplai_foundry.meta_harness import proposer
from amplai_foundry.meta_harness.archive import EliteArchive
from amplai_foundry.meta_harness.leak_gate import LeakGate
from amplai_foundry.meta_harness.proposer import (
    OBSERVATION_KIND,
    PREDICTION_KIND,
    RUN_KIND,
    ProposerEnsemble,
    apply_deltas,
    dream,
    removal_sweep,
    removal_targets,
)
from amplai_foundry.meta_harness.traces import TraceService
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import canonical, new_id, now
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import policies, prompts, releases
from amplai_foundry.runtime.execution.readonly_turn import TurnResult

DEV, VAL, HOLD = task_ids("development"), task_ids("validation"), task_ids("holdout")
REPO = Path(__file__).resolve().parents[2]
# identifiers of the reference solutions of validation and holdout tasks (the leak index tokens)
LEAK = "NovelAccumulator().zqx_carry_fold(a, b)"
NIGHT_TEXT = "Check the visible tests before editing."
HIDDEN = (*VAL, *HOLD, "zqx_carry_fold", "NovelAccumulator")  # what no proposer input may hold


@pytest.fixture
def w(tmp_path):  # type: ignore[no-untyped-def]
    with build_world(tmp_path) as world:
        yield world


# ==================================================================================================
# scripted turns and builders
# ==================================================================================================
class Turn:
    """A read-only turn that records everything its workspace held while it ran."""

    def __init__(self, cell_id: str, script: Any) -> None:
        self.cell_id, self.script = cell_id, script
        self.calls: list[dict[str, Any]] = []

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None
    ) -> TurnResult:
        files = sorted(p for p in Path(workspace).rglob("*") if p.is_file())
        seen = {
            "prompt": prompt,
            "schema": schema,
            "workspace": Path(workspace),
            "mounts": mounts,
            "files": [str(p.relative_to(workspace)) for p in files],
            "dirs": sorted(
                str(p.relative_to(workspace)) for p in Path(workspace).rglob("*") if p.is_dir()
            ),
            "text": "\n".join(p.read_text() for p in files),
            "inputs": json.loads((Path(workspace) / "inputs.json").read_text()),
            "traces": {
                p.stem: json.loads(p.read_text())
                for p in (Path(workspace) / "traces").glob("*.json")
            },
        }
        self.calls.append(seen)
        out = self.script(seen, len(self.calls))
        if isinstance(out, Exception):
            raise out
        return TurnResult(
            out, {"input_tokens": 100, "output_tokens": 20}, 0.1, "sha256:" + "0" * 64
        )


def draft_in(prompt: str) -> dict[str, Any]:
    """The draft a depth prompt carries."""
    start = prompt.index("Draft:\n") + len("Draft:\n")
    return dict(json.loads(prompt[start : prompt.index("\nReturn exactly")]))


def role_content(*lines: str) -> dict[str, Any]:
    return {"implementer": [*GOOD, *lines]}


def edit(
    kind: str,
    name: str,
    content: dict[str, Any],
    *,
    delta: float = 0.1,
    improve: list[str] | None = None,
    regress: list[str] | None = None,
    improve_buckets: list[dict[str, str]] | None = None,
    risk: str = "low",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "component_id": f"{kind}.{name}",
        "content": content,
        "rationale": f"rationale of {name}",
        "hypothesis": f"hypothesis of {name}",
        "predictions": {
            "improve_task_ids": improve if improve is not None else [DEV[0]],
            "regress_task_ids": regress or [],
            "improve_buckets": improve_buckets or [],
            "regress_buckets": [],
            "expected_delta": delta,
        },
        "risk": risk,
    }


def bootstrap(depth: int = 3, entries: int = 100, chars: int = 2000, facts: Any = None) -> Any:
    return {
        "enabled": True,
        "facts": facts if facts is not None else ["repo_tree"],
        "tree_depth": depth,
        "tree_max_entries": entries,
        "max_chars": chars,
    }


E_ROLE = edit(
    "role_prompt", "s13a",
    role_content("Read the failing test, then change only the code it names.",
                 "Rerun the acceptance commands before you stop."),
    delta=0.30, improve=[DEV[0], DEV[1], VAL[0]], risk="medium",
    improve_buckets=[{"domain": "bug"}],
)  # fmt: skip
E_BOOT = edit("env_bootstrap", "s13b", bootstrap(), delta=0.20, improve=[DEV[2]])
E_NEAR_V1 = edit(  # the v1 implementer text plus one line: Jaccard with v1 is above 0.9
    "role_prompt", "s13near", {"implementer": [*prompts.IMPLEMENTER_BASELINE, "Good luck."]},
    delta=0.50,
)  # fmt: skip
E_SAME_AS_ROLE = edit(  # the same words as E_ROLE: case and spacing differ only
    "role_prompt", "s13copy",
    role_content("READ the failing  test, then change only the code it names.",
                 "Rerun the   acceptance commands before you stop."),
    delta=0.45,
)  # fmt: skip
E_LEAK = edit("role_prompt", "s13leak", role_content("Start from " + LEAK + "."), delta=0.60)
E_INVALID = edit(
    "attempt_policy", "s13bad", {"max_attempts": 99, "repair_base": "previous_patch",
                                 "feedback": True}, delta=0.70,
)  # fmt: skip
E_DECIDER = edit("decider", "s13dec", {"layer": "L2"}, delta=0.80)  # not a proposer kind


def refine_ok(seen: dict[str, Any], n: int) -> dict[str, Any]:
    """A strong-cell answer: the draft with a rationale marker and a slightly changed content."""
    draft = draft_in(seen["prompt"])
    draft["rationale"] = "refined: " + draft["rationale"]
    if draft["kind"] == "env_bootstrap":
        draft["content"] = bootstrap(
            depth=2, entries=150, chars=3000, facts=["repo_tree", "tool_versions"]
        )
    else:
        draft["content"] = role_content(
            *draft["content"]["implementer"][2:], "Say what you changed."
        )
    return {"edits": [draft]}


def ensemble(
    w: World, tmp_path: Path, breadth: Turn, depth: Turn, *, gate: LeakGate | None = None
) -> ProposerEnsemble:
    return ProposerEnsemble(
        w.ops, breadth=breadth, depth=depth,  # type: ignore[arg-type]
        traces=TraceService(w.store, w.scope, w.artifacts),
        archive=EliteArchive(w.store, w.scope),
        leak_gate=gate or LeakGate(w.operator, w.store, w.refs["leak_index_ref"]),
        components=w.ops.components, scratch_root=tmp_path / "scratch",
    )  # fmt: skip


def turns(breadth_edits: list[dict[str, Any]], depth: Any = refine_ok) -> tuple[Turn, Turn]:
    return (
        Turn("cheap-cell", lambda seen, n: {"edits": breadth_edits}),
        Turn("strong-cell", depth),
    )


def run_record(w: World, ref: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = w.store.get(w.scope, RUN_KIND, ref)
    return value


# -- development data fixtures ---------------------------------------------------------------
def with_outcome(w: World, run_id: str, success: bool | None, task: str = "bug-dev-00") -> None:
    """The trial record (and its receipt naming the goal) of a trace's run: the trace's outcome."""
    if success is None:
        return
    receipt = w.artifacts.admit(
        w.scope, canonical({"goal_id": "goal-" + run_id}), "application/json", trust="verifier"
    )
    w.put(
        "eval-trial", new_id("trial"),
        {"task_id": task, "artifact_refs": [receipt], "success": success, "arm": "candidate"},
    )  # fmt: skip


def seed_traces(w: World) -> dict[str, Any]:
    """Development, validation and holdout traces of the cell; texts name their split."""
    out: dict[str, Any] = {}
    for split in ("development", "validation", "holdout"):
        run = f"run-{split[:3]}"
        out[split] = admit(w, run, split=split, texts=(f"{split.upper()}-TRACE-TEXT",))
    return out


def screened_proposal(w: World, outcome: Any = None, *, focused: bool = False) -> str:
    pid = w.propose("a2")
    w.script(pid, outcome)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    if focused:
        runner.approve_stage(pid, "focused")
    return pid


def promote_directly(w: World, composition: dict[str, Any]) -> None:
    """Make a composition the cell's champion through a release (what a promotion writes)."""
    release = releases.build(
        w.store, w.scope, w.d.contracts, "local-test-" + composition["id"], [composition],
        signer=KEY, key_id="local-authority", basis="S13 proposer test",
    )  # fmt: skip
    with w.store.tx() as db:
        head = w.store.head(w.scope, releases.POINTER_KIND, releases.POINTER_ID, db=db)
        w.store.cas(
            db, w.scope, releases.POINTER_KIND, releases.POINTER_ID, head["row_version"],
            "active", {"release_ref": release},
        )  # fmt: skip


def pointer(w: World) -> dict[str, Any]:
    return dict(w.store.head(w.scope, releases.POINTER_KIND, releases.POINTER_ID))


def proposals_of(w: World, ids: list[str]) -> list[dict[str, Any]]:
    return [w.proposal(p) for p in ids]


# ==================================================================================================
# inputs: development data only (§9.4, AC-07 unit part)
# ==================================================================================================
def test_the_input_builder_keeps_only_development_metric_rows(w: World, tmp_path: Path) -> None:
    pid = screened_proposal(w)  # 24 development screening rows
    template = dict(w.objects("trial-metrics")[0][1])
    expected = len([1 for _r, v in w.objects("trial-metrics") if v["split"] == "development"])
    assert expected == 24
    # calibration rows of the validation split (and a development one), as calibration writes them
    for task in VAL[:6]:
        w.put(
            "trial-metrics", new_id("tm"),
            {**template, "split": "validation", "source": "calibration", "phase": "calibration",
             "task_id": task, "domain": task.split("-")[0], "stage": None, "arm": None},
        )  # fmt: skip
    w.put(
        "trial-metrics", new_id("tm"),
        {**template, "split": "development", "source": "calibration", "phase": "calibration",
         "task_id": DEV[0], "stage": None, "arm": None},
    )  # fmt: skip
    w.put("trial-metrics", new_id("tm"), {**template, "split": "holdout", "task_id": HOLD[0]})
    breadth, depth = turns([])
    ens = ensemble(w, tmp_path, breadth, depth)
    inputs = ens.inputs(CELL)
    rows = inputs["metrics"]
    assert len(rows) == expected + 1  # the stage rows and the development calibration row
    assert {r["task_id"] for r in rows} <= set(DEV)
    assert {r["source"] for r in rows} == {"experiment", "calibration"}
    flat = json.dumps(inputs)
    for task in (*VAL, *HOLD):
        assert task not in flat
    assert pid  # the proposal exists: its development results are the rows above


def test_rows_of_another_cell_are_not_inputs(w: World, tmp_path: Path) -> None:
    screened_proposal(w)
    template = dict(w.objects("trial-metrics")[0][1])
    w.put("trial-metrics", new_id("tm"), {**template, "cell_id": "claude-cli"})
    breadth, depth = turns([])
    assert len(ensemble(w, tmp_path, breadth, depth).inputs(CELL)["metrics"]) == 24


def test_metric_rows_hold_no_guard_signals(w: World, tmp_path: Path) -> None:
    """Hack-guard signals reject only, never a score (§9.10): not in the proposer's rows."""
    screened_proposal(w)
    breadth, depth = turns([])
    rows = ensemble(w, tmp_path, breadth, depth).inputs(CELL)["metrics"]
    assert rows
    for row in rows:
        assert "guards" not in row and "verified_hidden_fail" not in row
        assert "split" not in row  # nothing but development ever reaches this list


def test_inputs_list_the_development_tasks_only(w: World, tmp_path: Path) -> None:
    breadth, depth = turns([])
    inputs = ensemble(w, tmp_path, breadth, depth).inputs(CELL)
    assert [t["task_id"] for t in inputs["development_tasks"]] == sorted(DEV)
    assert all(t["domain"] for t in inputs["development_tasks"])


def test_inputs_hold_the_catalogue_and_the_champions_components(w: World, tmp_path: Path) -> None:
    breadth, depth = turns([])
    inputs = ensemble(w, tmp_path, breadth, depth).inputs(CELL)
    assert set(inputs["catalogue"]) == set(proposer.PROPOSER_KINDS)
    assert inputs["catalogue"]["env_bootstrap"]["v1"]["enabled"] is False
    assert inputs["champion"]["composition_ref"] == w.ops.app.compositions[CELL]
    assert "role_prompt" in json.dumps(inputs["champion"]["manifest"])
    assert inputs["parents"][0]["composition_ref"] == inputs["champion"]["composition_ref"]


def test_traces_are_development_only_failures_first_at_most_twelve(
    w: World, tmp_path: Path
) -> None:
    runs = [(f"run-d{i:02d}", False if i < 5 else (None if i < 10 else True)) for i in range(15)]
    for run_id, success in runs:
        admit(w, run_id, texts=(f"development trace {run_id}",))
        with_outcome(w, run_id, success)
    for split in ("validation", "holdout"):
        admit(w, f"run-{split[:3]}", split=split, texts=(f"{split.upper()}-TRACE-TEXT",))
    breadth, depth = turns([])
    ens = ensemble(w, tmp_path, breadth, depth)
    listing = ens.inputs(CELL)["traces"]
    assert len(listing) == 12
    assert [t["outcome"] for t in listing] == ["failed"] * 5 + ["unknown"] * 5 + ["passed"] * 2
    assert all(t["trace_id"].startswith("trace-run-d") for t in listing)
    assert all(t["file"] == f"traces/{t['trace_id']}.json" for t in listing)
    assert {t["task_id"] for t in listing} == {"bug-dev-00"}
    # the breadth turn sees those files and no others
    ens.run(cell_id=CELL, drafts=1, refine=0)
    (call,) = breadth.calls
    assert sorted(call["traces"]) == sorted(t["trace_id"] for t in listing)
    assert "VALIDATION-TRACE-TEXT" not in call["text"] and "HOLDOUT-TRACE-TEXT" not in call["text"]


def test_each_trace_is_cut_to_32_kib(w: World, tmp_path: Path) -> None:
    admit(w, "run-long", texts=tuple("long message " + "x" * 3900 for _ in range(40)))
    stored = TraceService(w.store, w.scope, w.artifacts).read(
        w.local.proposer, TraceService(w.store, w.scope, w.artifacts).list(
            w.local.proposer, cell_id=CELL)[0]
    )["body"]  # fmt: skip
    assert len(canonical(stored)) > 32 * 1024  # stored whole (up to 256 KiB)
    breadth, depth = turns([])
    ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=0)
    (body,) = breadth.calls[0]["traces"].values()
    assert len(canonical(body)) <= 32 * 1024 and body.get("cut") is True


def test_the_proposer_cannot_read_a_validation_trace_through_the_service(
    w: World, tmp_path: Path
) -> None:
    refs = seed_traces(w)
    breadth, depth = turns([])
    ens = ensemble(w, tmp_path, breadth, depth)
    assert ens.proposer.permissions == frozenset({"harness.propose"})
    for split in ("validation", "holdout"):
        hold("TRACE_ACL", ens.traces.read, ens.proposer, refs[split])
        hold("TRACE_ACL", ens.traces.list, ens.proposer, cell_id=CELL, split=split)
    assert [t["trace_id"] for t in ens.inputs(CELL)["traces"]] == [refs["development"]["id"]]


def test_the_archive_input_has_no_verdict_and_no_validation_numbers(
    w: World, tmp_path: Path
) -> None:
    """After the focused stage (validation split, 16 tasks x 2 repeats x arms) the archive holds the
    operator-only verdict; if validation rows reached the elites, domains with n >= 4 would
    place."""
    pid = screened_proposal(w, focused=True)
    breadth, depth = turns([])
    ens = ensemble(w, tmp_path, breadth, depth)
    inputs = ens.inputs(CELL)
    view = inputs["archive"]
    assert view == EliteArchive(w.store, w.scope).proposer_view(CELL)
    assert view["elites"] == []  # only 12 development trials per arm over five domains: n < 4
    assert all("verdict" not in entry for entry in view["lineage"])
    assert [e["proposal_id"] for e in view["lineage"]] == [pid]
    head = EliteArchive(w.store, w.scope).head(CELL)
    assert head["lineage"][0]["verdict"] == "focused:non_inferior"  # the operator's view keeps it
    flat = json.dumps(inputs)
    assert "non_inferior" not in flat and "focused:" not in flat


def test_earlier_proposals_show_their_screening_score_and_nothing_of_later_stages(
    w: World, tmp_path: Path
) -> None:
    pred = {"improve_task_ids": [DEV[0]], "regress_task_ids": [], "improve_buckets": [],
            "regress_buckets": [], "expected_delta": 0.1, "risk": "low"}  # fmt: skip
    pid = w.propose("a2", prediction=pred)
    w.script(pid)
    runner = w.runner()
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    runner.advance(pid)
    runner.approve_stage(pid, "focused")
    breadth, depth = turns([])
    entries = ensemble(w, tmp_path, breadth, depth).inputs(CELL)["proposals"]
    (entry,) = [e for e in entries if e["proposal_id"] == pid]
    assert set(entry) == {
        "proposal_id", "hypothesis", "component_changes", "prediction", "screening_score",
    }  # fmt: skip
    assert entry["screening_score"]["task_level"] is not None
    assert entry["prediction"]["improve_task_ids"] == [DEV[0]]
    flat = json.dumps(entry)
    for later in ("focused", "holdout", "bucket_level", "decision_class", "HACK_GUARD", "state"):
        assert later not in flat


def test_a_guard_rejected_proposal_shows_no_guard_finding_to_the_proposer(
    w: World, tmp_path: Path
) -> None:
    def outcome(role: str, case: str, repeat: int) -> Any:
        if role == "candidate" and case in ("bug-dev-00", "bug-dev-05"):
            return {"success": True, "goal_status": "verified", "hidden_passed": False}
        return True

    pid = screened_proposal(w, outcome)
    assert "HACK_GUARD" in w.stage_run(pid)["data"]["stages"]["screening"]["guard_findings"][0]
    breadth, depth = turns([])
    inputs = ensemble(w, tmp_path, breadth, depth).inputs(CELL)
    assert "HACK_GUARD" not in json.dumps(inputs) and "guard" not in json.dumps(inputs["metrics"])
    assert EliteArchive(w.store, w.scope).proposer_view(CELL)["elites"] == []


# ==================================================================================================
# scratch directory (IC-14)
# ==================================================================================================
def test_the_turns_run_on_an_empty_scratch_directory_holding_only_the_inputs(
    w: World, tmp_path: Path
) -> None:
    seed_traces(w)
    breadth, depth = turns([E_ROLE])
    ens = ensemble(w, tmp_path, breadth, depth)
    ens.run(cell_id=CELL, drafts=2, refine=1)
    for turn in (breadth, depth):
        (call,) = turn.calls
        workspace = call["workspace"]
        assert workspace.parent == (tmp_path / "scratch").resolve() or workspace.parent == (
            tmp_path / "scratch"
        )
        assert not workspace.resolve().is_relative_to(REPO)  # never a repository copy
        assert call["files"] == ["inputs.json", "traces/trace-run-dev.json"]
        assert call["dirs"] == ["traces"]
        assert call["mounts"] is None
        assert not workspace.exists()  # removed after the run
    assert list((tmp_path / "scratch").iterdir()) == []


def test_the_turns_never_see_corpus_material(w: World, tmp_path: Path) -> None:
    seed_traces(w)
    screened_proposal(w)
    breadth, depth = turns([E_ROLE])
    ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    text = "\n".join(c["text"] + c["prompt"] for c in (*breadth.calls, *depth.calls))
    for hidden in (
        *VAL,
        *HOLD,
        "zqx_carry_fold",
        "NovelAccumulator",
        "test_bug_val",
        "test_bug_hol",
        "pytest_hidden",
        "splits.json",
        "hidden_map",
        str(REPO),
    ):
        assert hidden not in text, hidden


# ==================================================================================================
# operator-made earlier proposals and every input: development only, leak-gated (§9.4, IC-11)
# ==================================================================================================
def operator_prediction(**over: Any) -> dict[str, Any]:
    """A prediction naming validation and holdout task ids (an operator's, unfiltered)."""
    pred = {
        "improve_task_ids": [DEV[0], VAL[0], DEV[0]],
        "regress_task_ids": [HOLD[0]],
        "improve_buckets": [],
        "regress_buckets": [],
        "expected_delta": 0.1,
        "risk": "low",
    }
    return {**pred, **over}


def seen_text(*turns_: Turn) -> str:
    return "\n".join(c["text"] + c["prompt"] for t in turns_ for c in t.calls)


def stored_prediction(w: World, prediction: dict[str, Any] | None, **over: Any) -> str:
    """A proposal whose ``pred-<id>`` record was stored without the propose-time leak gate (a
    record written before that gate, or by hand): the input builder filters it all the same."""
    pid = w.propose("a2", prediction=None, **over)
    if prediction is not None:
        w.put(
            PREDICTION_KIND, "pred-" + pid,
            {"schema": "amplai.proposal-prediction.v1", "scope": w.scope.wire(),
             "proposal_id": pid, **prediction, "made_at": now()},
        )  # fmt: skip
    return pid


def test_a_prediction_naming_validation_or_holdout_material_is_refused_at_propose(
    w: World,
) -> None:
    """§9.5, IC-11: a prediction names development task ids only; LEAK_GATE before any write."""
    before = {k: len(w.objects(k)) for k in ("harness-change-proposal", PREDICTION_KIND)}
    for pred in (
        operator_prediction(),  # a validation and a holdout id
        operator_prediction(improve_task_ids=[DEV[0]], regress_task_ids=[VAL[3].upper()]),
        operator_prediction(
            improve_task_ids=[], regress_task_ids=[], improve_buckets=[{"domain": "zqx_carry_fold"}]
        ),
    ):
        error = hold("LEAK_GATE", w.propose, "a2", prediction=pred)
        assert all(f["code"] == "LEAK_GATE" for f in error.details)
        assert not any(h.lower() in json.dumps(error.details).lower() for h in HIDDEN)
    assert {k: len(w.objects(k)) for k in before} == before  # nothing submitted or stored
    # development ids only pass
    pid = w.propose("a2", prediction=operator_prediction(improve_task_ids=[DEV[0]],
                                                         regress_task_ids=[DEV[1]]))  # fmt: skip
    assert any(r["id"] == "pred-" + pid for r, _ in w.objects(PREDICTION_KIND))


def test_screen_scans_the_stored_prediction_of_a_proposal(w: World) -> None:
    """The MetaHarness.screen hook (``meta_local.leak_subject``) reads the prediction the change
    artifact names: a stored prediction naming a holdout id holds LEAK_GATE at screen."""
    pid = w.propose("a2", prediction=operator_prediction(improve_task_ids=[DEV[0]],
                                                         regress_task_ids=[]))  # fmt: skip
    change = json.loads(w.artifacts.read(w.scope, w.proposal(pid)["change_artifact"]))
    stored = w.store.get(w.scope, PREDICTION_KIND, change["prediction_ref"])
    with w.store.tx() as db:  # a tampered prediction: the next revision names a holdout id
        ref = w.store.put(db, w.scope, PREDICTION_KIND, "pred-" + pid, 2,
                          {**stored, "regress_task_ids": [HOLD[0]]})  # fmt: skip
    from amplai_foundry.runtime.execution.meta_local import leak_subject

    leaky = {**w.proposal(pid)}
    subject = leak_subject(w.store, w.artifacts, w.scope, leaky)
    assert subject["prediction"]["regress_task_ids"] == []  # the change artifact names revision 1
    assert subject["prediction"]["improve_task_ids"] == [DEV[0]]
    assert w.local.leak_findings(w.scope, leaky) == []
    # a change artifact naming the leaky revision is refused at screen
    patched = {**json.loads(w.artifacts.read(w.scope, leaky["change_artifact"])),
               "prediction_ref": ref}  # fmt: skip
    leaky["change_artifact"] = w.artifacts.admit(
        w.scope, canonical(patched), "application/json", trust="operator"
    )
    findings = w.local.leak_findings(w.scope, leaky)
    assert findings and all(f["code"] == "LEAK_GATE" for f in findings)
    assert any("/prediction/regress_task_ids/0" in f["statement"] for f in findings)
    assert HOLD[0] not in json.dumps(findings)


def test_an_operator_prediction_shows_its_development_task_ids_only(
    w: World, tmp_path: Path
) -> None:
    pid = stored_prediction(w, operator_prediction())
    stored = next(v for r, v in w.objects(PREDICTION_KIND) if r["id"] == "pred-" + pid)
    assert VAL[0] in stored["improve_task_ids"] and stored["regress_task_ids"] == [HOLD[0]]
    breadth, depth = turns([])
    (entry,) = [
        e for e in ensemble(w, tmp_path, breadth, depth).inputs(CELL)["proposals"]
        if e["proposal_id"] == pid
    ]  # fmt: skip
    assert entry["prediction"]["improve_task_ids"] == [DEV[0]]
    assert entry["prediction"]["regress_task_ids"] == []
    assert VAL[0] not in json.dumps(entry) and HOLD[0] not in json.dumps(entry)


@pytest.mark.parametrize(
    "over",
    [
        {"hypothesis": "Fixes " + HOLD[1]},
        {"hypothesis": "Like " + VAL[2].upper()},  # task ids match case-insensitively
        {"prediction": operator_prediction(improve_buckets=[{"domain": "zqx_carry_fold"}])},
    ],
    ids=["holdout-id-in-hypothesis", "validation-id-in-hypothesis", "identifier-in-bucket"],
)
def test_an_earlier_proposal_naming_validation_or_holdout_material_is_left_out(
    w: World, tmp_path: Path, over: dict[str, Any]
) -> None:
    clean = stored_prediction(w, operator_prediction())
    rest = {k: v for k, v in over.items() if k != "prediction"}
    leaky = stored_prediction(w, over.get("prediction", operator_prediction()), **rest)
    breadth, depth = turns([E_ROLE])
    ens = ensemble(w, tmp_path, breadth, depth)
    inputs = ens.inputs(CELL)
    assert [e["proposal_id"] for e in inputs["proposals"]] == [clean]
    flat = json.dumps(inputs)
    assert leaky not in flat
    for hidden in HIDDEN:
        assert hidden.lower() not in flat.lower(), hidden
    ens.run(cell_id=CELL, drafts=1, refine=1)
    assert len(breadth.calls) == 1 and len(depth.calls) == 1
    assert [e["proposal_id"] for e in breadth.calls[0]["inputs"]["proposals"]] == [clean]
    text = seen_text(breadth, depth)
    for hidden in HIDDEN:
        assert hidden.lower() not in text.lower(), hidden
    assert ens.last is not None
    assert ens.last["inputs_left_out"] == {"traces": 0, "proposals": 1}


def test_a_development_trace_naming_holdout_material_is_left_out_and_frees_its_place(
    w: World, tmp_path: Path
) -> None:
    admit(w, "run-leaky", texts=("I called " + LEAK + " first.",))
    with_outcome(w, "run-leaky", False)  # a failure: it would be shown first
    for i in range(12):
        admit(w, f"run-c{i:02d}", texts=(f"clean development trace {i}",))
    breadth, depth = turns([])
    ens = ensemble(w, tmp_path, breadth, depth)
    listing = ens.inputs(CELL)["traces"]
    assert len(listing) == 12 and "trace-run-leaky" not in [t["trace_id"] for t in listing]
    ref = ens.run(cell_id=CELL, drafts=1, refine=0)
    (call,) = breadth.calls
    assert "trace-run-leaky" not in call["traces"] and len(call["traces"]) == 12
    for hidden in HIDDEN:
        assert hidden not in call["text"], hidden
    trace_ids = [r["id"] for r in run_record(w, ref)["inputs"]["trace_refs"]]
    assert "trace-run-leaky" not in trace_ids and len(trace_ids) == 12
    assert ens.last is not None
    assert ens.last["inputs_left_out"] == {"traces": 1, "proposals": 0}


def test_the_whole_inputs_are_leak_checked_before_any_turn_runs(
    w: World, tmp_path: Path, monkeypatch: Any
) -> None:
    """The last line of defence: material that reaches the inputs by another path holds
    LEAK_GATE before the scratch directory is written; the details never carry the token."""
    monkeypatch.setattr(
        proposer, "development_rows", lambda *args: [{"task_id": HOLD[0], "domain": "bug"}]
    )
    breadth, depth = turns([E_ROLE])
    ens = ensemble(w, tmp_path, breadth, depth)
    error = hold("LEAK_GATE", ens.run, cell_id=CELL, drafts=1, refine=1)
    assert breadth.calls == [] and depth.calls == []
    assert not (tmp_path / "scratch").exists()  # no scratch directory was made
    assert w.objects(RUN_KIND) == []
    assert {"kind": "task_id", "where": "/inputs.json/metrics/0/task_id"} in error.details
    assert HOLD[0] not in json.dumps(error.details)
    hold("LEAK_GATE", ens.inputs, CELL)


def test_a_scratch_root_inside_the_repository_is_refused(
    w: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_repo = tmp_path / "pretend-repository"
    fake_repo.mkdir()
    monkeypatch.setattr(proposer, "REPO_ROOT", fake_repo)
    breadth, depth = turns([E_ROLE])
    ens = ensemble(w, tmp_path, breadth, depth)
    ens.scratch_root = fake_repo / "scratch"
    error = hold("TURN_FAILED", ens.run, cell_id=CELL)
    assert "IC-14" in error.message
    assert breadth.calls == []
    assert list((fake_repo / "scratch").iterdir()) == []  # the refused directory was removed


def test_the_scratch_directory_is_removed_when_a_turn_fails(w: World, tmp_path: Path) -> None:
    def boom(seen: dict[str, Any], n: int) -> Exception:
        return Hold("TURN_TIMEOUT", "Read-only turn exceeded its time budget")

    breadth = Turn("cheap-cell", boom)
    ens = ensemble(w, tmp_path, breadth, Turn("strong-cell", refine_ok))
    hold("TURN_TIMEOUT", ens.run, cell_id=CELL)
    assert list((tmp_path / "scratch").iterdir()) == []


# ==================================================================================================
# the ensemble: breadth -> dedup -> leak gate -> depth -> submit (§9.4)
# ==================================================================================================
def test_the_breadth_turn_gets_the_proposal_schema_and_the_draft_budget(
    w: World, tmp_path: Path
) -> None:
    breadth, depth = turns([E_ROLE])
    ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=4, refine=1)
    (call,) = breadth.calls
    assert call["schema"] is proposer.PROPOSAL_SCHEMA
    assert "at most 4 edits" in call["prompt"]
    assert call["inputs"]["cell_id"] == CELL


def test_cheap_drafts_are_deduplicated_leak_gated_and_the_top_ones_refined(
    w: World, tmp_path: Path
) -> None:
    drafts = [E_ROLE, E_BOOT, E_NEAR_V1, E_SAME_AS_ROLE, E_LEAK, E_INVALID, E_DECIDER]
    breadth, depth = turns(drafts)
    ens = ensemble(w, tmp_path, breadth, depth)
    ref = ens.run(cell_id=CELL, drafts=6, refine=2)
    record = run_record(w, ref)
    # a run that submits writes revision 1 before its first submit and revision 2 after
    assert ref["id"].startswith("proprun-") and ref["revision"] == 2
    (first,) = [r for r, _ in w.objects(RUN_KIND) if r["id"] == ref["id"] and r["revision"] == 1]
    assert run_record(w, first)["submitted"] == []
    assert (record["cell_breadth"], record["cell_depth"]) == ("cheap-cell", "strong-cell")
    assert record["drafted"] == 6  # the seventh edit is beyond the draft budget
    assert record["deduplicated"] == 2  # a near-duplicate of v1 and a copy of another draft
    assert record["leak_refused"] == 1  # the planted leak
    assert record["refined"] == 2
    assert len(record["submitted"]) == 2
    # the strong cell refined the top two by expected_delta, one turn each, in rank order
    assert [draft_in(c["prompt"])["component_id"] for c in depth.calls] == [
        "role_prompt.s13a",
        "env_bootstrap.s13b",
    ]
    assert all(c["schema"] is proposer.PROPOSAL_SCHEMA for c in depth.calls)
    assert record["inputs"]["archive_ref"] is not None
    assert record["usage"]["turns"] == 3 and record["usage"]["input_tokens"] == 300
    assert ens.last is not None and ens.last["submitted"] == record["submitted"]
    assert ens.last["proposer_run_ref"] == ref
    # what was submitted is the refined edit, not the draft
    kinds = {}
    for proposal in proposals_of(w, record["submitted"]):
        change = json.loads(w.artifacts.read(w.scope, proposal["change_artifact"]))
        # §2.12: the change artifact names its proposer run and the leak scan of its content
        assert change["proposer_run_ref"] == first
        assert change["leak_scan"] == {"hits": 0, "index_ref": w.refs["leak_index_ref"]}
        assert change["origin"] is None
        for entry in change["component_changes"]:
            kinds[entry["kind"]] = entry["to"]
    assert set(kinds) == {"role_prompt", "env_bootstrap"}
    boot = w.store.get(w.scope, "harness-component", kinds["env_bootstrap"])
    assert boot["content"]["tree_depth"] == 2 and boot["content"]["facts"] == [
        "repo_tree", "tool_versions",
    ]  # fmt: skip
    assert boot["rationale"].startswith("refined: ")


def test_an_edit_equal_to_an_existing_version_of_the_kind_is_dropped(
    w: World, tmp_path: Path
) -> None:
    w.ops.components.register(
        w.local.proposer, component_id="env_bootstrap.seen", kind="env_bootstrap",
        content=bootstrap(depth=1, entries=50, chars=500), source="proposer", rationale="seen",
    )  # fmt: skip
    again = edit(
        "env_bootstrap", "other-name", bootstrap(depth=1, entries=50, chars=500), delta=0.4
    )
    fresh = edit("env_bootstrap", "fresh", bootstrap(depth=2, entries=75, chars=900), delta=0.1)
    breadth, depth = turns([again, fresh])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=2, refine=2)
    )
    assert record["deduplicated"] == 1 and record["drafted"] == 2
    assert [draft_in(c["prompt"])["component_id"] for c in depth.calls] == ["env_bootstrap.fresh"]


def test_a_near_duplicate_needs_a_word_set_similarity_of_at_least_0_9(
    w: World, tmp_path: Path
) -> None:
    near = {"implementer": [*prompts.IMPLEMENTER_BASELINE, "Good luck."]}
    far = role_content("Read the failing test, then change only the code it names.")
    words_near = proposer.words(near)
    assert (
        proposer.jaccard(
            words_near, proposer.words({"implementer": list(prompts.IMPLEMENTER_BASELINE)})
        )
        >= 0.9
    )
    assert proposer.jaccard(proposer.words(far), proposer.words(near)) < 0.9
    assert proposer.JACCARD_LIMIT == 0.9
    breadth, depth = turns(
        [edit("role_prompt", "n", near, delta=0.9), edit("role_prompt", "f", far)]
    )
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=2, refine=2)
    )
    assert record["deduplicated"] == 1
    assert [draft_in(c["prompt"])["component_id"] for c in depth.calls] == ["role_prompt.f"]


def test_the_content_digest_ignores_case_and_whitespace_only() -> None:
    a = {"implementer": ["Read the  test.", "Then FIX it"]}
    b = {"implementer": ["read the test.", "then fix it"]}
    c = {"implementer": ["read the test.", "then fix it now"]}
    assert proposer.content_digest(a) == proposer.content_digest(b)
    assert proposer.content_digest(a) != proposer.content_digest(c)


def test_only_non_proposer_kinds_and_invalid_edits_means_nothing_to_refine(
    w: World, tmp_path: Path
) -> None:
    breadth, depth = turns([E_DECIDER, E_INVALID, {"kind": "role_prompt"}, "not an edit"])
    ens = ensemble(w, tmp_path, breadth, depth)
    record = run_record(w, ens.run(cell_id=CELL, drafts=4, refine=2))
    assert record["drafted"] == 4 and record["refined"] == 0 and record["submitted"] == []
    assert depth.calls == []
    assert w.objects("harness-change-proposal") == []


def test_a_component_id_of_another_kind_is_not_a_draft(w: World, tmp_path: Path) -> None:
    wrong = {**E_BOOT, "component_id": "role_prompt.s13b"}
    breadth, depth = turns([wrong])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    )
    assert record["submitted"] == [] and depth.calls == []


# -- the leak gate ---------------------------------------------------------------------------
def test_a_planted_leak_in_a_draft_is_refused_before_the_strong_cell_sees_it(
    w: World, tmp_path: Path
) -> None:
    breadth, depth = turns([E_LEAK, E_BOOT])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=2, refine=2)
    )
    assert record["leak_refused"] == 1 and record["drafted"] == 2
    assert [draft_in(c["prompt"])["component_id"] for c in depth.calls] == ["env_bootstrap.s13b"]
    assert all(LEAK.split("(")[0] not in c["prompt"] for c in depth.calls)
    assert len(record["submitted"]) == 1
    # nothing that names a reference identifier is a component or a proposal
    for _ref, value in w.objects("harness-component"):
        assert "zqx_carry_fold" not in json.dumps(value["content"])


@pytest.mark.parametrize(
    "where",
    ["rationale-free content text", "a validation task id", "a hidden test name"],
)
def test_the_leak_gate_covers_every_string_of_an_edit(w: World, tmp_path: Path, where: str) -> None:
    leaked = {
        "rationale-free content text": role_content("Use zqx_carry_fold for the carry."),
        "a validation task id": role_content(f"This is like {VAL[0]}."),
        "a hidden test name": role_content("Mirror test_" + VAL[1].replace("-", "_") + "_ok."),
    }[where]
    gate = LeakGate(w.operator, w.store, w.refs["leak_index_ref"])
    probe = edit("role_prompt", "probe", leaked)
    if not gate.scan(probe):  # the index does not carry this token kind: nothing to assert
        pytest.skip(f"the leak index holds no token for: {where}")
    breadth, depth = turns([probe])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    )
    assert record["leak_refused"] == 1 and record["submitted"] == []


def test_a_leak_introduced_by_the_strong_cell_is_refused_too(w: World, tmp_path: Path) -> None:
    def leaky_refine(seen: dict[str, Any], n: int) -> dict[str, Any]:
        draft = draft_in(seen["prompt"])
        if draft["component_id"] == "role_prompt.s13a":
            draft["content"] = role_content("Finish with NovelAccumulator.")
        else:
            draft = refine_ok(seen, n)["edits"][0]
        return {"edits": [draft]}

    breadth, depth = turns([E_ROLE, E_BOOT], leaky_refine)
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=2, refine=2)
    )
    assert record["refined"] == 2 and record["leak_refused"] == 1 and len(record["submitted"]) == 1


def test_a_proposer_identity_cannot_open_the_leak_index(w: World) -> None:
    hold("LEAK_INDEX_ACL", LeakGate, w.local.proposer, w.store, w.refs["leak_index_ref"])


# -- refinement outcomes ---------------------------------------------------------------------
def test_a_failed_refinement_is_not_submitted_and_the_others_are(w: World, tmp_path: Path) -> None:
    def flaky(seen: dict[str, Any], n: int) -> Any:
        if draft_in(seen["prompt"])["component_id"] == "role_prompt.s13a":
            return Hold("TURN_TIMEOUT", "Read-only turn exceeded its time budget")
        return refine_ok(seen, n)

    breadth, depth = turns([E_ROLE, E_BOOT], flaky)
    ens = ensemble(w, tmp_path, breadth, depth)
    record = run_record(w, ens.run(cell_id=CELL, drafts=2, refine=2))
    assert record["refined"] == 1 and len(record["submitted"]) == 1
    assert ens.last is not None
    assert ens.last["failures"] == [{"component_id": "role_prompt.s13a", "code": "TURN_TIMEOUT"}]
    assert record["usage"]["turns"] == 2  # a failed turn reports no usage


@pytest.mark.parametrize("answer", ["two edits", "other kind", "no edits", "not json shape"])
def test_a_refinement_that_breaks_the_contract_is_not_submitted(
    w: World, tmp_path: Path, answer: str
) -> None:
    def bad(seen: dict[str, Any], n: int) -> dict[str, Any]:
        draft = draft_in(seen["prompt"])
        other = {**draft, "kind": "env_bootstrap", "component_id": "env_bootstrap.zzz",
                 "content": bootstrap(depth=2, entries=11, chars=77)}  # fmt: skip
        return {
            "two edits": {"edits": [draft, draft]},
            "other kind": {"edits": [other]},
            "no edits": {"edits": []},
            "not json shape": {"edits": ["x"]},
        }[answer]

    breadth, depth = turns([E_ROLE], bad)
    ens = ensemble(w, tmp_path, breadth, depth)
    record = run_record(w, ens.run(cell_id=CELL, drafts=1, refine=1))
    assert record["submitted"] == [] and record["refined"] == 0
    assert ens.last is not None and ens.last["failures"][0]["code"] == "TURN_OUTPUT"


def test_a_refinement_equal_to_an_existing_version_is_deduplicated(
    w: World, tmp_path: Path
) -> None:
    def same_as_v1(seen: dict[str, Any], n: int) -> dict[str, Any]:
        draft = draft_in(seen["prompt"])
        draft["content"] = {"implementer": list(prompts.IMPLEMENTER_BASELINE)}
        return {"edits": [draft]}

    breadth, depth = turns([E_ROLE], same_as_v1)
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    )
    assert record["deduplicated"] == 1 and record["submitted"] == []


@pytest.mark.parametrize(
    "drafts, refine",
    [(0, 0), (7, 1), (-1, 0), (True, 0), ("6", 1), (6, -1), (3, 4), (6, True), (6, "2")],
)
def test_run_arguments_are_bounded(w: World, tmp_path: Path, drafts: Any, refine: Any) -> None:
    breadth, depth = turns([E_ROLE])
    ens = ensemble(w, tmp_path, breadth, depth)
    fault("PROPOSER_RUN", ens.run, cell_id=CELL, drafts=drafts, refine=refine)
    assert breadth.calls == []


def test_an_unknown_cell_holds_cell_unknown(w: World, tmp_path: Path) -> None:
    breadth, depth = turns([E_ROLE])
    hold("CELL_UNKNOWN", ensemble(w, tmp_path, breadth, depth).run, cell_id="no-such-cell")
    assert breadth.calls == []


def test_a_breadth_turn_that_fails_stops_the_run(w: World, tmp_path: Path) -> None:
    breadth = Turn(
        "cheap-cell", lambda seen, n: Hold("TURN_OUTPUT", "Read-only turn reply is not JSON")
    )
    ens = ensemble(w, tmp_path, breadth, Turn("strong-cell", refine_ok))
    hold("TURN_OUTPUT", ens.run, cell_id=CELL)
    assert w.objects(RUN_KIND) == [] and w.objects("harness-change-proposal") == []


# -- what the proposer submits (§9.4 step 6, D-100) ------------------------------------------
def test_submitted_proposals_are_drafts_made_by_the_proposer_identity(
    w: World, tmp_path: Path
) -> None:
    seed_traces(w)
    before = pointer(w)
    breadth, depth = turns([E_ROLE, E_BOOT])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=2, refine=2)
    )
    assert len(record["submitted"]) == 2
    for pid, proposal in zip(
        record["submitted"], proposals_of(w, record["submitted"]), strict=True
    ):
        assert w.state(pid) == "draft"  # never screened, approved or promoted by the proposer
        assert proposal["proposer"] == w.local.proposer.wire()
        assert proposal["baseline_ref"] == w.ops.app.compositions[CELL]
        assert proposal["status"] == "draft"
        with pytest.raises(RuntimeFault) as missing:  # no stage run exists
            w.stage_run(pid)
        assert missing.value.code == "NOT_FOUND"
    assert pointer(w) == before  # the active release did not move
    assert w.objects("experiment-approval") == [] and w.objects("eval-experiment") == []
    for proposal in proposals_of(w, record["submitted"]):
        change = json.loads(w.artifacts.read(w.scope, proposal["change_artifact"]))
        for entry in change["component_changes"]:
            if entry["kind"] != "role_prompt":  # a role prompt becomes a prompt bundle
                assert (
                    w.store.get(w.scope, "harness-component", entry["to"])["source"] == "proposer"
                )


def test_the_prediction_of_a_submitted_edit_is_stored_without_non_development_ids(
    w: World, tmp_path: Path
) -> None:
    breadth, depth = turns([E_ROLE])  # E_ROLE predicts DEV[0], DEV[1] and VAL[0]
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    )
    (pid,) = record["submitted"]
    (stored,) = [v for r, v in w.objects(PREDICTION_KIND) if r["id"] == "pred-" + pid]
    assert stored["improve_task_ids"] == [DEV[0], DEV[1]]  # the validation id was dropped
    assert stored["improve_buckets"] == [{"domain": "bug"}]
    assert stored["risk"] == "medium" and stored["expected_delta"] == 0.3
    flat = json.dumps(stored)
    assert VAL[0] not in flat
    (component,) = [v for _r, v in w.objects("harness-component") if v["kind"] == "role_prompt"]
    assert (
        "1 prediction task ids outside the development split were dropped" in component["rationale"]
    )
    assert VAL[0] not in component["rationale"]  # the count, never the id


def test_the_observation_names_development_traces_only(w: World, tmp_path: Path) -> None:
    refs = seed_traces(w)
    breadth, depth = turns([E_ROLE])
    record = run_record(
        w, ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    )
    assert [r["id"] for r in record["inputs"]["trace_refs"]] == [refs["development"]["id"]]
    (proposal,) = proposals_of(w, record["submitted"])
    (observation_ref,) = proposal["observation_refs"]
    observation = w.store.get(w.scope, OBSERVATION_KIND, observation_ref)
    body = json.loads(w.artifacts.read(w.scope, observation["artifact"]))
    assert set(body) == {"issue", "trace_ids", "score_refs"}
    assert body["trace_ids"] == [refs["development"]["id"]]


def test_a_submitted_candidate_keeps_a_parent_when_it_edits_a_component_the_champion_has(
    w: World, tmp_path: Path
) -> None:
    first = w.component("env_bootstrap", "parented", enabled=True, tree_depth=3)
    pid = w.ops.propose_components(
        cell_id=CELL, changes={"env_bootstrap": first}, suffix="seedboot", hypothesis="h",
        expected_benefit="b", risks=["r"], observation_refs=[w.ops._observation("x")],
        prediction=None,
    )  # fmt: skip
    promote_directly(w, dict(w.proposal(pid)["candidate_ref"]))
    revised = edit(
        "env_bootstrap", "parented", bootstrap(depth=1, entries=60, chars=700), delta=0.2
    )
    breadth, depth = turns([revised], lambda seen, n: {"edits": [draft_in(seen["prompt"])]})
    ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    versions = w.ops.components.versions("env_bootstrap.parented")
    assert [v["version"] for _r, v in versions] == [1, 2]
    assert versions[1][1]["parent"] == versions[0][0]


def test_the_proposer_run_record_is_validated(w: World, tmp_path: Path) -> None:
    breadth, depth = turns([E_ROLE])
    ref = ensemble(w, tmp_path, breadth, depth).run(cell_id=CELL, drafts=1, refine=1)
    value = run_record(w, ref)
    proposer.validate_run(value)
    assert value["schema"] == "amplai.proposer-run.v1" and value["scope"] == w.scope.wire()
    for broken in ({"drafted": -1}, {"submitted": "x"}, {"inputs": {}}, {"extra": 1}):
        fault("PROPOSER_RUN", proposer.validate_run, {**value, **broken})


# ==================================================================================================
# dreaming (§9.7)
# ==================================================================================================
def night() -> str:
    return now()[:10]


def dev_traces_of_the_night(w: World, count: int = 3) -> list[dict[str, Any]]:
    return [admit(w, f"run-night{i}", texts=(f"night trace {i}",)) for i in range(count)]


def seeded_champion(w: World, notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Make a champion whose ``memory_notes`` hold these notes; the champion composition ref."""
    ref = w.ops.components.register(
        w.local.proposer, component_id="memory_notes.seed", kind="memory_notes",
        content={"enabled": True, "notes": notes}, source="proposer", rationale="seed notes",
    )  # fmt: skip
    pid = w.ops.propose_components(
        cell_id=CELL, changes={"memory_notes": ref}, suffix="seednotes", hypothesis="h",
        expected_benefit="b", risks=["r"], observation_refs=[w.ops._observation("x")],
        prediction=None,
    )  # fmt: skip
    composition = dict(w.proposal(pid)["candidate_ref"])
    promote_directly(w, composition)
    return composition


def note(
    text: str, task_class: str | None = None, evidence: tuple[str, ...] = ("trace-old",)
) -> Any:
    return {"text": text, "app": "app", "task_class": task_class, "evidence": list(evidence),
            "dated": "2026-09-01"}  # fmt: skip


def delta(op: str, text: str = "", targets: tuple[int, ...] = (), evidence: tuple[str, ...] = (),
          *, app: str = "app", task_class: str | None = None) -> dict[str, Any]:  # fmt: skip
    return {"op": op, "text": text, "targets": list(targets), "app": app,
            "task_class": task_class, "evidence": list(evidence)}  # fmt: skip


def dreamer(deltas: list[dict[str, Any]]) -> Turn:
    return Turn("cheap-cell", lambda seen, n: {"deltas": deltas})


def test_dreaming_proposes_add_deltas_with_evidence_and_the_absolute_date(
    w: World, tmp_path: Path
) -> None:
    traces = dev_traces_of_the_night(w)
    when = night()
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night0", "trace-run-night2"),
                          task_class="bug_fix")])  # fmt: skip
    proposal_id = dream(w.ops, cell_id=CELL, night=when, turn=turn)
    assert proposal_id is not None and w.state(proposal_id) == "draft"
    proposal = w.proposal(proposal_id)
    assert proposal["baseline_ref"] == w.ops.app.compositions[CELL]
    assert proposal["surface_class"] == "A"  # memory_notes is a class A kind
    assert proposal["proposer"] == w.local.proposer.wire()
    (component,) = [(r, v) for r, v in w.objects("harness-component") if v["source"] == "dreaming"]
    value = component[1]
    assert value["source"] == "dreaming" and value["component_id"] == "memory_notes.dream." + CELL
    assert value["content"] == {
        "enabled": True,
        "notes": [{
            "text": NIGHT_TEXT, "app": "app", "task_class": "bug_fix",
            "evidence": ["trace-run-night0", "trace-run-night2"], "dated": when,
        }],
    }  # fmt: skip
    assert value["content"]["notes"][0]["dated"] == when and len(when) == 10  # absolute, no "today"
    assert {t["id"] for t in traces} >= set(value["content"]["notes"][0]["evidence"])
    change = json.loads(w.artifacts.read(w.scope, proposal["change_artifact"]))
    assert [(c["kind"], c["component_id"]) for c in change["component_changes"]] == [
        ("memory_notes", "memory_notes.dream." + CELL)
    ]
    assert change["component_changes"][0]["to"] == component[0]
    # one read-only turn on an empty scratch directory holding the night's inputs
    (call,) = turn.calls
    assert call["schema"] is proposer.DREAM_SCHEMA
    assert call["files"][0] == "inputs.json" and sorted(call["traces"]) == sorted(
        t["id"] for t in traces
    )
    assert call["inputs"]["night"] == when and call["inputs"]["memory_notes"]["notes"] == []
    assert not call["workspace"].resolve().is_relative_to(REPO) and not call["workspace"].exists()


def test_dreaming_merges_and_deletes_notes_of_the_champion(w: World) -> None:
    dev_traces_of_the_night(w, 4)
    champion = seeded_champion(
        w, [note("keep the diff small"), note("rerun tests"), note("stale advice"), note("keep me")]
    )
    when = night()
    turn = dreamer([
        delta("merge", "Keep diffs small and rerun the tests.", targets=(0, 1),
              evidence=("trace-run-night0",)),
        delta("delete", targets=(2,), evidence=("trace-run-night1", "trace-run-night0")),
        delta("add", NIGHT_TEXT, evidence=("trace-run-night3",), task_class="refactor"),
    ])  # fmt: skip
    proposal_id = dream(w.ops, cell_id=CELL, night=when, turn=turn)
    assert proposal_id is not None
    assert w.proposal(proposal_id)["baseline_ref"] == champion
    ((_, seen),) = [(0, turn.calls[0])]
    assert [n["index"] for n in seen["inputs"]["memory_notes"]["notes"]] == [0, 1, 2, 3]
    component = next(v for _r, v in w.objects("harness-component") if v["source"] == "dreaming")
    texts = [n["text"] for n in component["content"]["notes"]]
    assert texts == ["keep me", "Keep diffs small and rerun the tests.", NIGHT_TEXT]
    dated = {n["text"]: n["dated"] for n in component["content"]["notes"]}
    assert dated["keep me"] == "2026-09-01"  # an untouched note keeps its own date
    assert dated[NIGHT_TEXT] == when and dated["Keep diffs small and rerun the tests."] == when
    # a dreamed version names the version it consolidates (merge_parents) and cites its traces
    assert component["merge_parents"] and all(n["evidence"] for n in component["content"]["notes"])


def test_apply_deltas_semantics() -> None:
    notes = [note("n0"), note("n1"), note("n2")]
    shown = {"t1", "t2"}
    apps = {"app"}
    out, refused = apply_deltas(
        notes,
        [
            delta("add", "new", evidence=("t1",)),
            delta("merge", "merged", targets=(0, 1), evidence=("t2",)),
            delta("delete", targets=(2,), evidence=("t1",)),
        ],
        night="2026-10-01", shown=shown, apps=apps,
    )  # fmt: skip
    assert refused == 0
    assert [n["text"] for n in out] == ["merged", "new"]
    assert all(n["dated"] == "2026-10-01" for n in out)
    assert out[0]["evidence"] == ["t2"]
    assert notes[0]["text"] == "n0"  # the input list is not mutated


@pytest.mark.parametrize(
    "bad",
    [
        delta("add", "x", evidence=()),  # no evidence
        delta("add", "x", evidence=("t-not-shown",)),  # evidence not among the traces shown
        delta("add", "x", evidence=tuple(f"t{i}" for i in range(9))),  # more than 8
        delta("add", "", evidence=("t1",)),
        delta("add", "x" * 301, evidence=("t1",)),
        delta("add", "x", evidence=("t1",), app="other-app"),
        delta("add", "x", evidence=("t1",), task_class="not_a_class"),
        delta("add", "x", targets=(0,), evidence=("t1",)),  # an add has no targets
        delta("merge", "x", targets=(), evidence=("t1",)),
        delta("merge", "x", targets=(7,), evidence=("t1",)),  # no such note
        delta("delete", targets=(-1,), evidence=("t1",)),
        delta("wipe", "x", evidence=("t1",)),
        "not a delta",
    ],
)
def test_a_bad_delta_is_refused_and_counted(bad: Any) -> None:
    out, refused = apply_deltas(
        [note("n0")],
        [bad],
        night="2026-10-01",
        shown={"t1", *[f"t{i}" for i in range(9)]},
        apps={"app"},
    )
    assert refused == 1 and [n["text"] for n in out] == ["n0"]


def test_a_note_is_the_target_of_one_delta_at_most_and_ten_deltas_are_the_limit() -> None:
    notes = [note("n0"), note("n1")]
    out, refused = apply_deltas(
        notes,
        [delta("delete", targets=(0,), evidence=("t1",)),
         delta("merge", "again", targets=(0,), evidence=("t1",))],
        night="2026-10-01", shown={"t1"}, apps={"app"},
    )  # fmt: skip
    assert refused == 1 and [n["text"] for n in out] == ["n1"]
    many = [delta("add", f"note {i}", evidence=("t1",)) for i in range(12)]
    out, refused = apply_deltas([], many, night="2026-10-01", shown={"t1"}, apps={"app"})
    assert len(out) == 10 and refused == 2


def test_dreaming_with_evidence_of_a_hidden_split_is_refused(w: World) -> None:
    dev_traces_of_the_night(w, 1)
    admit(w, "run-val-night", split="validation", texts=("validation night",))
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-val-night",))])
    assert dream(w.ops, cell_id=CELL, night=night(), turn=turn) is None  # refused: nothing left
    assert "trace-run-val-night" not in turn.calls[0]["text"]  # the turn never saw that trace
    assert [v for _r, v in w.objects("harness-component") if v["source"] == "dreaming"] == []


def test_dreaming_has_nothing_to_do_without_traces_of_that_night(w: World) -> None:
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night0",))])
    assert dream(w.ops, cell_id=CELL, night=night(), turn=turn) is None
    assert turn.calls == []
    admit(w, "run-other-night", texts=("another night",))
    assert dream(w.ops, cell_id=CELL, night="2020-01-01", turn=turn) is None
    assert turn.calls == []


def test_dreaming_without_an_accepted_delta_proposes_nothing(w: World) -> None:
    dev_traces_of_the_night(w, 1)
    for deltas in ([], [delta("delete", targets=(0,), evidence=("trace-run-night0",))]):
        assert dream(w.ops, cell_id=CELL, night=night(), turn=dreamer(deltas)) is None
    assert w.objects("harness-change-proposal") == []


def test_dreaming_can_name_the_traces_of_the_night_explicitly(w: World) -> None:
    refs = dev_traces_of_the_night(w, 3)
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night1",))])
    assert dream(w.ops, cell_id=CELL, night="2020-01-01", turn=turn, trace_refs=[refs[1]])
    assert sorted(turn.calls[0]["traces"]) == ["trace-run-night1"]


def test_dreamed_notes_that_name_validation_or_holdout_material_hold_the_leak_gate(
    w: World,
) -> None:
    dev_traces_of_the_night(w, 1)
    turn = dreamer([delta("add", "Use " + LEAK, evidence=("trace-run-night0",))])
    error = hold("LEAK_GATE", dream, w.ops, cell_id=CELL, night=night(), turn=turn)
    assert "zqx_carry_fold" not in json.dumps(
        error.details
    )  # the finding names kind and place only
    assert w.objects("harness-change-proposal") == []
    assert [v for _r, v in w.objects("harness-component") if v["source"] == "dreaming"] == []


def test_dreaming_leaves_out_a_trace_naming_holdout_material(w: World) -> None:
    dev_traces_of_the_night(w, 1)
    admit(w, "run-night-leak", texts=("I called " + LEAK + " first.",))
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night-leak",))])
    assert dream(w.ops, cell_id=CELL, night=night(), turn=turn) is None  # evidence not shown
    (call,) = turn.calls
    assert sorted(call["traces"]) == ["trace-run-night0"]
    assert [t["trace_id"] for t in call["inputs"]["traces"]] == ["trace-run-night0"]
    for hidden in HIDDEN:
        assert hidden not in call["text"], hidden


def test_dreaming_over_leaky_traces_only_runs_no_turn(w: World) -> None:
    admit(w, "run-night-leak", texts=("I called " + LEAK + " first.",))
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night-leak",))])
    assert dream(w.ops, cell_id=CELL, night=night(), turn=turn) is None
    assert turn.calls == []


def test_dreaming_inputs_are_leak_checked_before_the_turn(w: World) -> None:
    dev_traces_of_the_night(w, 1)
    seeded_champion(w, [note("Mirror " + LEAK)])  # notes that never passed the leak gate
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night0",))])
    error = hold("LEAK_GATE", dream, w.ops, cell_id=CELL, night=night(), turn=turn)
    assert turn.calls == []
    assert all(d["where"].startswith("/inputs.json/memory_notes/") for d in error.details)
    assert "zqx_carry_fold" not in json.dumps(error.details)
    assert [v for _r, v in w.objects("harness-component") if v["source"] == "dreaming"] == []


@pytest.mark.parametrize(
    "bad_night", ["yesterday", "2026-13-40", "2026-1-1", "", "2026-10-01T00:00Z"]
)
def test_the_night_is_a_calendar_date(w: World, bad_night: str) -> None:
    fault("DREAM_NIGHT", dream, w.ops, cell_id=CELL, night=bad_night, turn=dreamer([]))


def test_dreaming_an_unknown_cell_holds_cell_unknown(w: World) -> None:
    admit(
        w, "run-night0", cell="claude-cli", texts=("night trace",)
    )  # a trace of a cell not installed
    turn = dreamer([delta("add", NIGHT_TEXT, evidence=("trace-run-night0",))])
    hold("CELL_UNKNOWN", dream, w.ops, cell_id="claude-cli", night=night(), turn=turn)


# ==================================================================================================
# removal sweep (§9.8)
# ==================================================================================================
def champion_with_two_changes(w: World) -> dict[str, Any]:
    pid = w.propose("a2")
    composition = dict(w.proposal(pid)["candidate_ref"])
    promote_directly(w, composition)
    return composition


def test_nothing_differs_from_v1_means_nothing_to_sweep(w: World) -> None:
    assert removal_targets(w.ops, CELL) == []
    assert removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed") == []
    assert w.objects("harness-change-proposal") == []


def test_a_sweep_proposes_one_leave_one_out_candidate_per_non_v1_component(w: World) -> None:
    champion = champion_with_two_changes(w)
    targets = removal_targets(w.ops, CELL)
    assert {t["kind"] for t in targets} == {"role_prompt", "env_bootstrap"}
    ids = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    assert len(ids) == 2
    installed = w.ops.manifests.of_composition(w.ops.app.compositions[CELL]).flat()
    champ_flat = w.ops.manifests.of_composition(champion).flat()
    removed_slots = set()
    order: list[str] = []
    for pid in ids:
        proposal = w.proposal(pid)
        assert proposal["baseline_ref"] == champion  # champion against ...
        candidate = w.ops.manifests.of_composition(proposal["candidate_ref"]).flat()
        # ... champion with exactly one component back to v1
        differing = {slot for slot in candidate if candidate[slot] != champ_flat.get(slot)}
        assert len(differing) == 1
        (slot,) = differing
        # IC-24: the component's v1 content (policies.V1), here equal to the installed one's
        kind = "role_prompt" if slot == "role_prompt" else slot
        assert proposer._content_of(w.ops, candidate[slot]) == policies.V1[kind]
        assert proposer._content_of(w.ops, candidate[slot]) == proposer._content_of(
            w.ops, installed[slot]
        )
        change = json.loads(w.artifacts.read(w.scope, proposal["change_artifact"]))
        assert change["origin"] == "removal_sweep"
        removed_slots.add(slot)
        order.append(slot)
        assert w.state(pid) == "draft"
    assert len(removed_slots) == 2
    # proposals come in the order removal_targets lists the slots
    from amplai_foundry.meta_harness.stages import slot_of

    assert [slot_of(name) for name in order] == [t["slot"] for t in targets]


def test_a_sweep_never_screens_approves_or_promotes(w: World) -> None:
    champion_with_two_changes(w)
    before = pointer(w)
    ids = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    assert pointer(w) == before
    for pid in ids:
        assert w.state(pid) == "draft"
        with pytest.raises(RuntimeFault) as missing:
            w.stage_run(pid)
        assert missing.value.code == "NOT_FOUND"
    assert w.objects("eval-experiment") == []


def test_a_sweep_is_not_repeated_while_its_proposals_are_open(w: World) -> None:
    champion_with_two_changes(w)
    first = removal_sweep(w.ops, cell_id=CELL, reason="drift detected")
    assert len(first) == 2
    assert removal_sweep(w.ops, cell_id=CELL, reason="drift detected") == []
    assert len(w.objects("harness-change-proposal")) == 1 + 2  # the champion's own + the sweep


def test_a_sweep_states_why_it_runs(w: World) -> None:
    champion_with_two_changes(w)
    for reason in ("", "   ", None, 5):
        fault("SWEEP_REASON", removal_sweep, w.ops, cell_id=CELL, reason=reason)  # type: ignore[arg-type]
    assert len(w.objects("harness-change-proposal")) == 1


def test_a_sweep_of_an_unknown_cell_holds_cell_unknown(w: World) -> None:
    hold("CELL_UNKNOWN", removal_sweep, w.ops, cell_id="no-such-cell", reason="x")
    hold("CELL_UNKNOWN", removal_targets, w.ops, "no-such-cell")


def test_a_sweep_proposal_goes_through_the_ordinary_stages(w: World) -> None:
    champion_with_two_changes(w)
    (first, _second) = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    w.script(first)
    runner = w.runner()
    runner.plan(first, cell_id=CELL, root_budget=BUDGET)
    steps = runner.advance(first)
    assert w.step(steps, "screening").state == "passed"  # screened like any proposal
    assert w.step(steps, "focused").waiting_for == "approve-stage focused"  # the operator's gate


def test_a_sweep_variant_has_no_removal_verdict_before_focused(w: World) -> None:
    champion_with_two_changes(w)
    (first, second) = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    verdict = proposer.removal_verdict(w.store, w.scope, first)
    assert verdict["removal_candidate"] is None and verdict["stage"] == "focused"
    w.script(first)
    runner = w.runner()
    runner.plan(first, cell_id=CELL, root_budget=BUDGET)
    runner.advance(first)  # screening passed, focused waits for the operator
    assert proposer.removal_verdict(w.store, w.scope, first)["removal_candidate"] is None
    listed = proposer.sweep_proposals(w.ops, CELL)
    assert [p["proposal_id"] for p in listed] == sorted([first, second])
    assert {p["state"] for p in listed} <= {"draft", "screened"}
    assert all(p["removal"]["removal_candidate"] is None for p in listed)


@pytest.mark.parametrize(
    "baseline, candidate, expected",
    [
        ((4, 4), (2, 2), True),  # non-inferior and fewer tokens per solved task
        ((2, 2), (4, 4), False),  # non-inferior but dearer: not a removal candidate
        ((2, 2), (2, 2), False),  # non-inferior at the same cost: not fewer
    ],
)
def test_the_removal_verdict_is_the_section_9_8_criterion(
    w: World, baseline: tuple[int, int], candidate: tuple[int, int], expected: bool
) -> None:
    champion_with_two_changes(w)
    (first, _second) = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    w.script(first, baseline=baseline, candidate=candidate)
    runner = w.runner()
    runner.plan(first, cell_id=CELL, root_budget=BUDGET)
    runner.advance(first)
    focused = runner.approve_stage(first, "focused")
    verdict = proposer.removal_verdict(w.store, w.scope, first)
    # every arm passes every task: the class is non_inferior, so the token count decides
    assert verdict["decision_class"] == focused.decision_class == "non_inferior"
    per = verdict["tokens_per_solved"]
    assert (per["baseline"], per["candidate"]) == (float(sum(baseline)), float(sum(candidate)))
    assert verdict["removal_candidate"] is expected
    (listed,) = [p for p in proposer.sweep_proposals(w.ops, CELL) if p["proposal_id"] == first]
    assert listed["removal"] == verdict


def test_unknown_usage_is_never_fewer_tokens_per_solved_task() -> None:
    trials = [
        {"arm": "baseline", "success": True, "input_tokens": 4, "output_tokens": 4},
        {"arm": "candidate", "success": True, "input_tokens": None, "output_tokens": 1},
        {"arm": "candidate", "success": True, "input_tokens": 1, "output_tokens": 1},
    ]
    assert proposer._tokens_per_solved(trials, "baseline") == 8.0
    assert proposer._tokens_per_solved(trials, "candidate") is None
    unsolved = [{"arm": "baseline", "success": False, "input_tokens": 1, "output_tokens": 1}]
    assert proposer._tokens_per_solved(unsolved, "baseline") is None


# ==================================================================================================
# IC-24 (provisional): the removal gate holds holdout and approvals unless the sweep is a removal
# ==================================================================================================
def sweep_through_focused(
    w: World, baseline: tuple[int, int], candidate: tuple[int, int]
) -> tuple[str, Any]:
    """A sweep proposal through screening and its focused stage (every arm passes every task:
    the class is non_inferior, so tokens per solved task decide)."""
    champion_with_two_changes(w)
    (first, _second) = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    w.script(first, baseline=baseline, candidate=candidate)
    runner = w.runner()
    runner.plan(first, cell_id=CELL, root_budget=BUDGET)
    runner.advance(first)
    runner.approve_stage(first, "focused")
    steps = runner.advance(first)  # one changed component: nothing to ablate
    assert w.step(steps, "holdout").waiting_for == "approve-stage holdout"
    return first, runner


@pytest.mark.parametrize(
    "baseline, candidate",
    [((2, 2), (4, 4)), ((2, 2), (2, 2))],
    ids=["dearer", "same-cost"],
)
def test_a_sweep_that_is_no_removal_is_held_before_holdout_and_approvals(
    w: World, baseline: tuple[int, int], candidate: tuple[int, int]
) -> None:
    first, runner = sweep_through_focused(w, baseline, candidate)
    experiments = len(w.objects("eval-experiment"))
    error = hold("NOT_A_REMOVAL", runner.approve_stage, first, "holdout")
    assert error.details["removal_candidate"] is False
    assert error.details["decision_class"] == "non_inferior"
    # nothing of the holdout ran: no freeze, no approval, the candidate still screened
    assert len(w.objects("eval-experiment")) == experiments
    assert w.stage_run(first)["data"]["stages"]["holdout"]["state"] == "waiting_approval"
    assert w.state(first) == "screened"
    # the legacy offline approval of the evolution machine is held the same way
    hold("NOT_A_REMOVAL", w.ops.approve_experiment, first, max_tokens=1000,
         max_wall_seconds=600)  # fmt: skip
    assert w.state(first) == "screened"


def test_a_sweep_that_is_a_removal_passes_holdout_and_the_canary_approval(w: World) -> None:
    first, runner = sweep_through_focused(w, (4, 4), (2, 2))  # fewer tokens per solved task
    assert proposer.removal_verdict(w.store, w.scope, first)["removal_candidate"] is True
    holdout = runner.approve_stage(first, "holdout")
    assert holdout.state == "passed" and w.state(first) == "offline_evaluated"
    canary = w.ops.approve_canary(first, ["t0"], max_trial_tokens=10)
    assert canary["tasks"] == ["t0"]


def test_the_removal_gate_holds_a_sweep_before_its_focused_stage(w: World) -> None:
    from amplai_foundry.meta_harness.stages import check_removal

    champion_with_two_changes(w)
    (first, _second) = removal_sweep(w.ops, cell_id=CELL, reason="model snapshot changed")
    error = hold("NOT_A_REMOVAL", check_removal, w.store, w.scope, w.artifacts, first)
    assert error.details["removal_candidate"] is None  # no focused report yet


def test_the_removal_gate_never_holds_an_ordinary_proposal(w: World) -> None:
    from amplai_foundry.meta_harness.stages import check_removal

    pid = w.propose("a2")
    check_removal(w.store, w.scope, w.artifacts, pid)  # no focused report, no hold
    change = json.loads(w.artifacts.read(w.scope, w.proposal(pid)["change_artifact"]))
    assert change["origin"] is None


def test_propose_components_refuses_an_unknown_origin_and_a_malformed_leak_scan(
    w: World,
) -> None:
    count = len(w.objects("harness-change-proposal"))
    fault("PROPOSAL_ORIGIN", w.propose, "a1", origin="dreaming")
    fault("LEAK_SCAN", w.propose, "a1", leak_scan={"hits": -1, "index_ref": {}})
    fault("LEAK_SCAN", w.propose, "a1", leak_scan={"hits": 0})
    fault("PROPOSER_RUN", w.propose, "a1", proposer_run_ref={"id": "proprun-x"})
    assert len(w.objects("harness-change-proposal")) == count
    scan = {"hits": 0, "index_ref": w.refs["leak_index_ref"]}
    pid = w.propose("a1", leak_scan=scan)
    change = json.loads(w.artifacts.read(w.scope, w.proposal(pid)["change_artifact"]))
    assert change["leak_scan"] == scan and change["proposer_run_ref"] is None


def test_a_sweep_puts_a_component_back_to_policies_v1_not_to_the_installed_one(
    w: World,
) -> None:
    """IC-24: v1 is ``policies.V1``. A champion slot without v1 content (driver_options) goes
    back to the empty slot; a kind whose v1 content no install version holds gets a ``<kind>.v1``
    component (source ``sweep``)."""
    options = w.ops.components.register(
        w.local.proposer, component_id="driver_options.s13", kind="driver_options",
        content={"claude": {"max_turns": None, "append_system_prompt": None, "allowed_tools": None},
                 "codex": {"config": []}},
        source="proposer", rationale="S13 sweep test",
    )  # fmt: skip
    pid = w.propose("a1", changes={"driver_options": options, "role_prompt": w.role_prompt("x")})
    promote_directly(w, dict(w.proposal(pid)["candidate_ref"]))
    targets = {t["kind"]: t for t in removal_targets(w.ops, CELL)}
    assert set(targets) == {"role_prompt", "driver_options"}
    assert targets["driver_options"]["v1"] is None
    assert targets["role_prompt"]["v1"] == policies.V1["role_prompt"]
    ids = removal_sweep(w.ops, cell_id=CELL, reason="drift detected")
    assert len(ids) == 2
    flats = {p: w.ops.manifests.of_composition(w.proposal(p)["candidate_ref"]).flat() for p in ids}
    assert any(f["driver_options"] is None for f in flats.values())
    assert any(
        proposer._content_of(w.ops, f["role_prompt"]) == policies.V1["role_prompt"]
        for f in flats.values()
    )


# ==================================================================================================
# the commands (§12.1)
# ==================================================================================================
PLAIN = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "250"}


def help_of(*args: str) -> str:
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["meta", *args, "--help"], env=PLAIN)
    assert result.exit_code == 0, result.output
    return result.output


def test_the_meta_group_lists_the_trace_and_proposer_command_groups() -> None:
    out = help_of()
    assert "trace" in out and "proposer" in out
    assert "approve" not in help_of("proposer") and "promote" not in help_of("proposer")


@pytest.mark.parametrize(
    "path, commands, options",
    [
        (("trace",), ("list", "show"), ()),
        (("trace", "list"), (), ("--cell", "--split", "--config")),
        (("trace", "show"), (), ("--config",)),
        (("proposer",), ("run", "dream", "sweep", "score"), ()),
        (("proposer", "score"), (), ("--stage", "--config")),
        (
            ("proposer", "run"),
            (),
            ("--cell", "--drafts", "--refine", "--config", "--corpus", "--app"),
        ),
        (("proposer", "dream"), (), ("--cell", "--night", "--turn-cell", "--config", "--corpus")),
        (("proposer", "sweep"), (), ("--cell", "--reason", "--config", "--corpus", "--app")),
    ],
)
def test_the_commands_take_their_documented_options(
    path: tuple[str, ...], commands: tuple[str, ...], options: tuple[str, ...]
) -> None:
    out = help_of(*path)
    for name in (*commands, *options):
        assert name in out, name


def test_proposer_cells_need_a_cheap_and_a_strong_cell() -> None:
    from types import SimpleNamespace

    from amplai_foundry.runtime.meta_commands import proposer as command

    def ops(cells: list[str]) -> Any:
        return SimpleNamespace(dep=SimpleNamespace(config=SimpleNamespace(
            roles=SimpleNamespace(proposer=cells))))  # fmt: skip

    assert command.proposer_cells(ops(["cheap", "strong"])) == ("cheap", "strong")
    for cells in ([], ["only-one"]):
        error = hold("CELL_UNKNOWN", command.proposer_cells, ops(cells))
        assert "roles.proposer" in error.message
