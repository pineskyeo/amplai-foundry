"""Work 033 S13 wiring: corpus v2 stage and calibration trials capture (interfaces.md §9.1,
§9.3, §2.10 ``trace_ref``, D-100).

``amplai meta`` commands of Work 033 open the deployment through ``meta_commands.opened_v2``; its
trial executor gets the deployment's ``TraceService`` (``meta_cli.trial_traces``, as the Work 030
gates' ``meta_cli.opened``), so a development trial of a stage stores its run's sanitized trace and
names it in the receipt, while a validation or holdout trial and a real goal store none.

Real: ``opened_v2`` with ``load_corpus`` and ``LocalTrialExecutor``, the rc06 product rig (store,
runtime, worker coordinator with the deployment trace sink ``traces.trace_sink``, execution loop,
git workspaces, protected verifier), ``CodexCliDriver`` with its trace buffer, ``TraceService``.
Stand-ins: the deployment object handed to ``opened_v2`` (the rig's pieces in place of a
``LocalProductDeployment`` booted from ``local.json``), ``LocalMetaOps`` (a recorder: the commands
are not run here, the executor ``opened_v2`` built is), the dispatching eval-trial heads a stage
writes before calling the executor, and the rc06 host-process agent that prints Codex events. No
docker, provider or network.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from amplai_foundry.meta_harness.traces import DROP_KIND, TraceService, trace_sink
from amplai_foundry.runtime import meta_commands
from amplai_foundry.runtime.contracts.authority import Actor
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import meta_ops
from amplai_foundry.runtime.execution.product import AppConfig, VerifierCommand
from rc06_rig import AGENT, CHECK, git, rig_with_codex, submit

TASK = "bug-01-value"
SAID = "patched value() to return 2"
TRACED_AGENT = (
    AGENT
    + r"""
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                             "text": "patched value() to return 2"}}) + "\n")
sys.stdout.write(json.dumps({"type": "item.completed", "item": {"type": "reasoning",
                             "text": "PRIVATE-REASONING"}}) + "\n")
"""
)
VISIBLE = (
    "from app import value\n\n\ndef test_value_is_an_int() -> None:\n"
    "    assert isinstance(value(), int)\n"
)
HIDDEN = "from app import value\n\n\ndef test_two() -> None:\n    assert value() == 2\n"


def write_corpus(root: Path, base_commit: str) -> Path:
    """A one-task corpus v2 on the rig's repository (the S8 task shape)."""
    bench = root / "bases" / "bench"
    (bench / "tests").mkdir(parents=True)
    (bench / "app.py").write_text("def value():\n    return 2\n")
    (bench / "tests" / "test_visible.py").write_text(VISIBLE)
    bases = {"bench": {"app_id": "app", "dir": "bases/bench", "base_commit": base_commit}}
    (root / "manifest.json").write_text(
        json.dumps({"corpus_id": "s13-capture", "version": "1.0.0", "split_seed": None,
                    "bases": bases})
    )  # fmt: skip
    folder = root / "tasks" / TASK
    (folder / "hidden").mkdir(parents=True)
    (folder / "reference").mkdir()
    meta = {
        "task_id": TASK, "version": 2, "domain": "bug", "subdomain": None, "set": "main",
        "split": None,
        "source": {"kind": "own", "ref": "authored", "author": "test", "created": "2026-10-01"},
        "license": "LicenseRef-amplai-internal", "base": "bench", "environment": "app",
        "grading": "pytest_hidden", "objective": "Make value() return 2.",
        "acceptance": ["value() returns 2"], "hidden_map": {"test_two": 0}, "ambiguity": None,
        "difficulty_declared": None,
    }  # fmt: skip
    (folder / "task.json").write_text(json.dumps(meta))
    (folder / "hidden" / "test_hidden.py").write_text(HIDDEN)
    (folder / "reference" / "app.py").write_text("def value():\n    return 2\n")
    return root


class World:
    def __init__(self, deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.d = deployment
        rig, loop, container = rig_with_codex(deployment, tmp_path, "right")
        self.rig, self.loop, self.container = rig, loop, container
        # corpus tasks are graded by the app's behaviour verifier "unit" (TrialPlanner default)
        rig.service.install(AppConfig("app", rig.repo, (VerifierCommand("unit", CHECK, "v", 60),)))
        (rig.repo / "tests").mkdir()
        (rig.repo / "tests" / "test_visible.py").write_text(VISIBLE)
        git(rig.repo, "add", "-A")
        git(rig.repo, "commit", "-q", "-m", "visible test")
        base_commit = git(rig.repo, "rev-parse", "HEAD").strip()
        self.corpus_root = write_corpus(tmp_path / "corpus", base_commit)
        original = container.command

        def traced(argv: list[str], workspace: Path, run_name: str, **kw: Any) -> list[str]:
            command: list[str] = original(argv, workspace, run_name, **kw)
            command[2] = TRACED_AGENT  # the scripted agent also prints provider items
            return command

        container.command = traced  # type: ignore[method-assign]
        # the deployment's coordinator sink (local_deployment._trace_sink)
        loop.coordinator.trace_sink = trace_sink(
            deployment.store, deployment.scope, deployment.artifacts,
            lambda goal_id: rig.service.plan_record(goal_id),
        )  # fmt: skip
        self.meta: Any = None  # local.json "meta" absent: the default (capture on)
        self.opened: list[dict[str, Any]] = []
        world = self

        @contextmanager
        def opened_deployment(config: Path) -> Any:
            yield SimpleNamespace(
                service=rig.service, coordinator=loop.coordinator, goals=deployment.goals,
                operator=lambda: rig.operator, config=SimpleNamespace(meta=world.meta),
                store=deployment.store, scope=deployment.scope, artifacts=deployment.artifacts,
            )  # fmt: skip

        class Ops:  # LocalMetaOps' place: records what opened_v2 built
            def __init__(self, dep: Any, corpus: Any, executor: Any, **kw: Any) -> None:
                world.opened.append({"dep": dep, "corpus": corpus, "executor": executor, **kw})
                self.executor = executor

        monkeypatch.setattr(meta_commands, "opened_deployment", opened_deployment)
        monkeypatch.setattr(meta_ops, "LocalMetaOps", Ops)

    @property
    def traces(self) -> TraceService:
        return TraceService(self.d.store, self.d.scope, self.d.artifacts)

    @property
    def baseline(self) -> dict[str, Any]:
        ref: dict[str, Any] = self.rig.service.apps["app"].compositions["codex-cli"]
        return ref

    def executor(self) -> Any:
        with meta_commands.opened_v2(Path("unused.json"), self.corpus_root) as ops:
            return ops.executor

    def dispatching(self, trial_id: str, repeat: int) -> None:
        """The eval-trial head (and experiment) a stage writes before calling the executor."""
        store, scope = self.d.store, self.d.scope
        experiment = {
            "scope": scope.wire(), "experiment_id": "exp-capture", "baseline_ref": self.baseline,
            "candidate_ref": self.baseline, "analysis_plan_ref": self.baseline,
        }  # fmt: skip
        with store.tx() as db:
            try:
                ref = store.put(db, scope, "eval-experiment", "exp-capture", 1, experiment)
            except Exception:  # written by an earlier call of this test
                ref = None
        if ref is None:
            ref = next(r for r, _v in store.list_objects(scope, "eval-experiment"))
        with store.tx() as db:
            store.cas(
                db, scope, "eval-trial", trial_id, 0, "dispatching",
                {"experiment_ref": ref, "task_id": TASK, "arm": "baseline", "repeat": repeat,
                 "owner_epoch": store.epoch},
            )  # fmt: skip

    def trial(self, executor: Any, split: str, repeat: int) -> tuple[Any, dict[str, Any]]:
        self.dispatching(f"trial-{split}", repeat)
        case = {"case_id": TASK, "split": split}
        obs = executor(self.baseline, case, repeat, "sandbox_rerun")
        receipt = json.loads(self.d.artifacts.read(self.d.scope, obs.artifact_refs[0]))
        return obs, receipt


@pytest.fixture
def world(deployment: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(deployment, tmp_path, monkeypatch)


def proposer(world: World) -> Actor:
    return Actor("meta-proposer", world.d.scope, frozenset({"harness.propose"}), "service")


def test_a_development_stage_trial_stores_its_run_trace_and_names_it(world: World) -> None:
    executor = world.executor()
    assert isinstance(executor.traces, TraceService)  # opened_v2 wired the trace service
    obs, receipt = world.trial(executor, "development", 0)
    assert obs.success is True and receipt["goal_status"] == "verified"
    plan = world.rig.service.plan_record(receipt["goal_id"])
    assert plan["trial"]["capture_trace"] is True and plan["trial"]["domain"] == "bug"
    records = [v for _r, v in world.traces.records()]
    assert len(records) == 1
    record = records[0]
    assert record["goal_id"] == receipt["goal_id"] and record["split"] == "development"
    assert record["task_id"] == TASK and record["arm"] == "baseline"
    assert record["run_id"] == plan["attempts"][-1]["run_id"]
    # §2.10: the receipt names the graded run's trace; §6.8: and the goal's decisions
    assert receipt["trace_ref"] == world.traces.of_run(record["run_id"])
    assert receipt["trace_ref"] is not None and receipt["decisions"] == plan["decisions"]
    body = world.traces.read(proposer(world), receipt["trace_ref"])["body"]
    assert SAID in json.dumps(body) and "PRIVATE-REASONING" not in json.dumps(body)


@pytest.mark.parametrize(("split", "repeat"), [("validation", 1), ("holdout", 2)])
def test_a_validation_or_holdout_trial_stores_no_trace(
    world: World, split: str, repeat: int
) -> None:
    executor = world.executor()
    _obs, receipt = world.trial(executor, split, repeat)
    plan = world.rig.service.plan_record(receipt["goal_id"])
    assert plan["trial"]["capture_trace"] is False and plan["trial"]["split"] == split
    assert receipt["trace_ref"] is None
    assert world.traces.records() == []
    assert list(world.d.store.list_objects(world.d.scope, DROP_KIND)) == []  # nothing captured
    assert world.container.driver._traces == {}  # the driver kept no buffer either


def test_a_real_goal_stores_no_trace(world: World) -> None:
    world.executor()  # the meta command's executor exists; the product goal is separate
    # the rig planner's draft names the app's behaviour verifier, installed here as "unit"
    world.rig.planner.draft_value["acceptance"] = [
        {"statement": "value() returns 2", "verifier": "unit"}
    ]
    goal = submit(world.rig, "real goal: make value return 2")
    world.rig.service.plan(goal)
    world.rig.service.approve(world.rig.operator, goal)
    assert world.loop.run_goal(goal)["status"] == "published"
    assert world.traces.records() == []
    assert list(world.d.store.list_objects(world.d.scope, DROP_KIND)) == []


def test_trace_capture_off_gives_the_stage_executor_no_trace_service(world: World) -> None:
    world.meta = SimpleNamespace(trace_capture=False)
    executor = world.executor()
    assert executor.traces is None
    _obs, receipt = world.trial(executor, "development", 3)
    assert receipt["trace_ref"] is None and world.traces.records() == []


def test_a_trial_whose_corpus_names_no_installed_app_is_held_before_any_capture(
    world: World,
) -> None:
    manifest = world.corpus_root / "manifest.json"
    value = json.loads(manifest.read_text())
    value["bases"]["bench"]["app_id"] = "ghost-app"
    manifest.write_text(json.dumps(value))
    executor = world.executor()
    with pytest.raises(Hold) as held:
        executor(world.baseline, {"case_id": TASK, "split": "development"}, 0, "sandbox_rerun")
    assert held.value.code == "TARGET_UNKNOWN"
    assert world.traces.records() == [] and world.container.prompts == []
