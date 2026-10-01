"""Work 033 S7b: TB2 execution wiring, the environment siblings (IC-12, interfaces.md §10.5 step 6).

Contract: specs/033-harness-taxonomy/interfaces.md §0 IC-12, §2.9 (`environment_digests`), §2.10
(receipt `environment_binding`, `environment_drift`), §3.2 (`env_sibling`), §3.3 (releases pin),
§10.5 steps 3-6, the S7b row of §13 and the Clarifications ("Open (S7b)"; provisional IC-31 and
IC-32 with the Clarifications After S7b: the stage plan's `app_id`, the `:env-` sibling ids).

Real: the rc06 local product (store, runtime, goals, worker coordinator, execution loop, the
protected verification service, git workspaces), `LocalExecutionService.install` with task
environments, `releases`, `manifest.env_sibling`, `LocalTrialExecutor`, `StageRunner` and
`LocalProductDeployment` (boot only). Stand-ins: the scripted host-process "container" of the rc06
rig (an agent that writes `value()` returning 2), a host verifier sandbox, scripted environment
records for the task images
(`install_codex_profile` over synthetic container profiles and qualification reports), and for the
stage part a scripted trial executor. No docker, no provider, no network.

Not covered here, because the contract leaves it open (§14 Q7, 확인 필요): the TB2 working
directory and the test entry command. A verified TB2 trial is reported `success` None (not graded by
a guessed command); the tests assert that and never a grade.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "v3"))

from test_033_s11_stages import BUDGET, CELL, SHA, Fake, build_world

from amplai_foundry.agent_drivers.cli import CodexCliDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.evaluation.receipts import read_receipt
from amplai_foundry.meta_harness import corpus_v2, manifest
from amplai_foundry.meta_harness.local_executor import (
    LocalTrialExecutor,
    manifest_digest,
    task_environment_digest,
)
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution import releases
from amplai_foundry.runtime.execution.codex import (
    AUTH,
    SeededCodexPort,
    install_codex_profile,
)
from amplai_foundry.runtime.execution.loop import ExecutionLoop
from amplai_foundry.runtime.execution.product import (
    AppConfig,
    TaskEnvironment,
    VerifierCommand,
    app_capabilities,
)
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.runtime.goals.validation import validate_bindings
from amplai_foundry.verification.runtime.patch_commands import SuiteVerifier
from rc06_rig import CHECK, HostSandbox, ScriptContainer, build_rig, codex_inputs, git, make_repo

TB2_APP = "amplai-tb2"
MODEL = "gpt-5.6-sol"
OTHER_MODEL = "gpt-5.7-terra"  # 확인 필요: a stand-in name for "another model", not a real id
ALPHA, BETA, GAMMA, DELTA = "tb2-alpha", "tb2-beta", "tb2-gamma", "tb2-delta"
BENCH_TASK = "bug-01-value"
TB2_TASK = {ALPHA: "tb2-alpha-task", BETA: "tb2-beta-task", GAMMA: "tb2-gamma-task",
            DELTA: "tb2-delta-task"}  # fmt: skip
VERIFIER = "check"
VISIBLE = (
    "from app import value\n\n\ndef test_value_is_an_int() -> None:\n"
    "    assert isinstance(value(), int)\n"
)
HIDDEN_TWO = "from app import value\n\n\ndef test_two() -> None:\n    assert value() == 2\n"
REF_TWO = "def value():\n    return 2\n"


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(Hold) as held:
        fn(*args, **kwargs)
    return str(held.value.code)


def hold_of(fn: Any, *args: Any, **kwargs: Any) -> Hold:
    with pytest.raises(Hold) as held:
        fn(*args, **kwargs)
    return held.value


# -- scripted records of a task image ------------------------------------------------------------
def inputs_in(root: Path, tag: str, char: str, qual_id: str, model: str = MODEL) -> Any:
    """Codex profile inputs for one image: its container profile and a passing qualification report
    (the shape `container_qualify.py` writes), distinct per image so records never collide."""
    root.mkdir(parents=True, exist_ok=True)
    inputs = codex_inputs(root)
    image = f"localhost:5000/amplai-tb2-{tag}@sha256:" + char * 64
    container = json.loads(inputs.container_profile.read_text())
    container["image"] = image
    inputs.container_profile.write_text(json.dumps(container))
    doc = json.loads(inputs.qualification_report.read_text())
    doc["container_image"] = image
    doc["reports"]["codex-cli"].update(qualification_id=qual_id, model=model)
    inputs.qualification_report.write_text(json.dumps(doc))
    return replace(inputs, model=model)


class Recorder:
    """A verifier runner that counts the changes it was asked to check."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.calls = inner, 0

    def __call__(self, raw: bytes) -> Any:
        self.calls += 1
        return self.inner(raw)


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def _task_json(task_id: str, **over: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "task_id": task_id,
        "version": 2,
        "domain": "bug",
        "subdomain": None,
        "set": "main",
        "split": None,
        "source": {"kind": "own", "ref": "authored", "author": "test", "created": "2026-09-30"},
        "license": "LicenseRef-amplai-internal",
        "base": "bench",
        "environment": "app",
        "grading": "pytest_hidden",
        "objective": f"Make value() return 2 ({task_id}).",
        "acceptance": [f"value() returns 2 ({task_id})"],
        "hidden_map": {"test_two": 0},
        "ambiguity": None,
        "difficulty_declared": None,
    }
    value.update(over)
    return value


def write_tb2_task(root: Path, task_id: str, environment: str, **over: Any) -> None:
    """A TB2-shaped task (`tb2/<name>/`: task.json, tests/, solution/; §10.5 step 5)."""
    folder = root / "tb2" / task_id
    meta = _task_json(
        task_id,
        domain="terminal",
        subdomain="software-engineering",
        source={
            "kind": "tb2",
            "ref": f"tb2-repo@abc:{task_id}",
            "author": "tb2",
            "created": "2026-09-30",
        },
        license="Apache-2.0",
        base="tb2",
        environment=environment,
        grading="tb2_tests",
        hidden_map={},
        objective=f"Make value() return 2 ({task_id}).",
        acceptance=["The task tests pass"],
        **over,
    )
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(meta))
    _write(folder / "tests", {"test.sh": "exit 0\n"})
    _write(folder / "solution", {"solve.sh": "exit 0\n"})


def write_corpus(root: Path, bench_commit: str, tb2_commit: str) -> corpus_v2.CorpusV2:
    """One bench-app task and four TB2 tasks (alpha, beta, gamma: not installed, delta: an
    environment installed with no qualified cell), plus a `tb2_tests` task claiming the app's own
    environment and a TB2 task whose environment pair is missing."""
    root.mkdir(parents=True)
    bases = {
        "bench": {"app_id": "app", "dir": "bases/bench", "base_commit": bench_commit},
        "tb2": {"app_id": TB2_APP, "dir": "bases/tb2", "base_commit": tb2_commit},
    }
    (root / "manifest.json").write_text(
        json.dumps(
            {"corpus_id": "s7b-mini", "version": "1.0.0", "split_seed": None, "bases": bases}
        )
    )
    for name in ("bench", "tb2"):
        _write(root / "bases" / name, {"app.py": REF_TWO, "tests/test_visible.py": VISIBLE})
    folder = root / "tasks" / BENCH_TASK
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(_task_json(BENCH_TASK)))
    _write(folder / "hidden", {"test_hidden.py": HIDDEN_TWO})
    _write(folder / "reference", {"app.py": REF_TWO})
    for env_id, task_id in TB2_TASK.items():
        write_tb2_task(root, task_id, env_id)
    write_tb2_task(root, "tb2-appenv-task", "app")  # tb2_tests claiming the app's own environment
    return corpus_v2.load(root)


# -- the world ------------------------------------------------------------------------------------
@dataclass
class World:
    d: Any
    rig: Any
    container: ScriptContainer
    corpus: corpus_v2.CorpusV2
    executor: LocalTrialExecutor
    probe_state: dict[str, str]
    recorders: dict[str, Recorder]
    env_records: dict[str, dict[str, Any]]
    tb2_cfg: AppConfig
    tb2_refs: dict[str, Any]
    task_envs: list[TaskEnvironment]
    binding_before: dict[str, Any]
    registry: DriverRegistry
    port: Any

    @property
    def store(self) -> Any:
        return self.d.store

    @property
    def scope(self) -> Any:
        return self.d.scope

    @property
    def service(self) -> Any:
        return self.rig.service

    @property
    def tb2(self) -> Any:
        return self.service.apps[TB2_APP]

    @property
    def arm(self) -> dict[str, Any]:
        """The baseline composition of the TB2 app (the arm composition of its trials)."""
        ref: dict[str, Any] = self.tb2.compositions["codex-cli"]
        return ref

    def comp(self, ref: dict[str, Any]) -> dict[str, Any]:
        value: dict[str, Any] = self.store.get(self.scope, "harness-composition", ref)
        return value

    def run(self, task_id: str, composition: dict[str, Any] | None = None, repeat: int = 0) -> Any:
        case = {"case_id": task_id, "split": "development"}
        return self.executor(composition or self.arm, case, repeat, "sandbox_rerun")

    def receipt(self, obs: Any) -> dict[str, Any]:
        return read_receipt(self.d.artifacts.read(self.scope, obs.artifact_refs[0], trusted=True))

    def proof(self, obs: Any) -> dict[str, Any]:
        return read_receipt(self.d.artifacts.read(self.scope, obs.artifact_refs[1], trusted=True))

    def goals(self) -> int:
        return len(list(self.store.list_objects(self.scope, "goal-intake")))

    def pins(self, *envs: str) -> dict[str, str]:
        return {e: self.executor.task_environment_digest(self.tb2, e) for e in envs}

    def candidate(self, suffix: str = "cand") -> dict[str, Any]:
        """A class-A candidate of the TB2 app's baseline (another prompt bundle)."""
        from amplai_foundry.runtime.execution import prompts

        bundle = self.service._put(
            prompts.KIND,
            f"implementer-{suffix}",
            prompts.bundle(f"implementer-{suffix}", ["Implement it in {app_id}."], "test"),
        )
        base = self.comp(self.arm)
        value = {
            **base,
            "composition_id": base["composition_id"] + "__" + suffix,
            "prompt_bundle_ref": bundle,
        }
        ref: dict[str, Any] = self.service._put(
            "harness-composition", value["composition_id"], value
        )
        return ref

    def bindings(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        rows = [
            (r, v) for r, v in self.store.list_objects(self.scope, "app-binding")
            if r["id"] == TB2_APP
        ]  # fmt: skip
        return sorted(rows, key=lambda row: row[0]["revision"])


def make_world(deployment: Any, tmp_path: Path, mode: str = "right") -> World:
    d = deployment
    rig = build_rig(d, tmp_path)
    service = rig.service
    # the bench base and the TB2 app base: two repositories, one commit each
    _write(rig.repo, {"tests/test_visible.py": VISIBLE})
    git(rig.repo, "add", "-A")
    git(rig.repo, "commit", "-q", "-m", "visible test")
    bench_commit = git(rig.repo, "rev-parse", "HEAD").strip()
    tb2_repo = make_repo(tmp_path / "tb2-root")
    _write(tb2_repo, {"tests/test_visible.py": VISIBLE})
    git(tb2_repo, "add", "-A")
    git(tb2_repo, "commit", "-q", "-m", "visible test")
    tb2_commit = git(tb2_repo, "rev-parse", "HEAD").strip()
    rig.workspaces.repos[TB2_APP] = tb2_repo
    caps = app_capabilities(TB2_APP)

    # records: the TB2 app's own image, then one task image per environment (IC-12)
    tb2_refs = install_codex_profile(
        d.store, d.scope, inputs_in(tmp_path / "in-app", "app", "d", "qual-tb2-app"), caps
    )
    env_records = {
        ALPHA: install_codex_profile(
            d.store, d.scope, inputs_in(tmp_path / "in-a", "alpha", "e", "qual-tb2-alpha"), caps
        ),
        BETA: install_codex_profile(
            d.store, d.scope, inputs_in(tmp_path / "in-b", "beta", "f", "qual-tb2-beta"), caps
        ),
        DELTA: install_codex_profile(  # an image qualified for no cell of the app
            d.store, d.scope, inputs_in(tmp_path / "in-d", "delta", "9", "qual-tb2-delta"), caps
        ),
    }
    cfg = AppConfig(
        TB2_APP, tb2_repo, (VerifierCommand(VERIFIER, CHECK, "value() must return 2", 60),)
    )
    # revision 1 of the app-binding: the app image only (before S7b)
    service.install(cfg, driver_refs={"codex-cli": tb2_refs})
    binding_before = dict(
        service.store.get(d.scope, "app-binding", _latest(d, "app-binding", TB2_APP))
    )

    recorders: dict[str, Recorder] = {}

    def verifier() -> Recorder:
        return Recorder(
            SuiteVerifier(
                [(VERIFIER, list(CHECK), 60)],
                workspaces=rig.workspaces,
                scope=d.scope,
                sandbox=HostSandbox(),
            )
        )

    task_envs = []
    for env_id in (ALPHA, BETA):
        recorders[env_id] = verifier()
        task_envs.append(
            TaskEnvironment(
                env_id,
                env_records[env_id]["environment"],
                {"codex-cli": env_records[env_id]},
                recorders[env_id],
            )
        )
    recorders[DELTA] = verifier()
    task_envs.append(  # installed, but qualified for no cell of the app: the missing pair
        TaskEnvironment(DELTA, env_records[DELTA]["environment"], {}, recorders[DELTA])
    )
    service.install(cfg, driver_refs={"codex-cli": tb2_refs}, environments=task_envs)
    # the TB2 app's own suite counts the changes it verifies
    app_suite = service.apps[TB2_APP].verifier_refs[VERIFIER]
    recorders["app"] = Recorder(service.verification.runners[digest(app_suite)])
    service.verification.runners[digest(app_suite)] = recorders["app"]

    container = ScriptContainer(mode)
    driver = CodexCliDriver(
        "0.155.1",
        container,  # type: ignore[arg-type]  # host stand-in
        SessionJournal(tmp_path / "journal"),
        model=MODEL,
        qualified=True,
    )
    container.driver = driver
    home = tmp_path / "scoped-codex"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "original"}')
    registry = DriverRegistry(d.store)
    port = SeededCodexPort(driver, home)
    for refs in (rig.codex_refs, tb2_refs, *env_records.values()):
        registry.register(d.actor, refs["driver"], port)
    coordinator = WorkCoordinator(d.runtime, registry, rig.workspaces, poll_seconds=0.05)
    loop = ExecutionLoop(service, coordinator, publisher=None)
    corpus = write_corpus(tmp_path / "corpus", bench_commit, tb2_commit)
    probe_state: dict[str, str] = {}  # task environment id -> what its probe measures
    task_env_of = {
        d.store.get(d.scope, "environment", env_records[e]["environment"])["environment_id"]: e
        for e in env_records
    }

    def probe(env: dict[str, Any]) -> dict[str, Any]:
        """The evaluation service's probe stand-in: the environment plus what a probe measured."""
        return {**env, "probed": probe_state.get(task_env_of.get(env["environment_id"]), "same")}

    executor = LocalTrialExecutor(
        service,
        loop,
        d.goals,
        rig.operator,
        corpus,
        behaviour_verifier=VERIFIER,
        environment_probe=probe,
    )
    return World(d, rig, container, corpus, executor, probe_state, recorders, env_records, cfg,
                 tb2_refs, task_envs, binding_before, registry, port)  # fmt: skip


def _latest(d: Any, kind: str, object_id: str) -> dict[str, Any]:
    refs = [r for r, _ in d.store.list_objects(d.scope, kind) if r["id"] == object_id]
    return max(refs, key=lambda r: r["revision"])


@pytest.fixture
def world(deployment: Any, tmp_path: Path) -> World:
    return make_world(deployment, tmp_path)


# ==================================================================================================
# IC-12: a TB2 task runs in the environment sibling and verifies in that environment
# ==================================================================================================
def test_ic12_a_tb2_task_runs_in_the_environment_sibling_and_the_three_environments_are_equal(
    world: World,
) -> None:
    obs = world.run(TB2_TASK[ALPHA])
    receipt, proof = world.receipt(obs), world.proof(obs)
    assert receipt["goal_status"] == "verified"
    # §10.5: graded by the task tests in the task image, whose entry command is 확인 필요 (§14 Q7)
    assert obs.success is None and receipt["success"] is None
    assert receipt["grading"] == "tb2_tests"
    env = world.env_records[ALPHA]
    arm, sibling_ref = world.comp(world.arm), receipt["executed_composition_ref"]
    sibling = world.comp(sibling_ref)
    # the sibling is the arm composition's manifest in the task image's records (IC-12)
    assert sibling_ref != world.arm
    assert sibling["composition_id"] == arm["composition_id"] + releases.ENV_SEP + releases.env12(
        ALPHA
    )
    assert {k: sibling[k] for k in manifest.SIBLING_KEEPS} == {
        k: arm[k] for k in manifest.SIBLING_KEEPS
    }
    assert sibling["sandbox_profile_ref"] == env["environment"]
    assert sibling["driver_profile_ref"] == env["driver"]
    assert sibling["model_profile_ref"] == env["model"]
    assert sibling["qualification_ref"] == env["qualification"]
    assert receipt["composition_ref"] == world.arm  # the arm composition stays what was asked
    assert receipt["environment_binding"] == {
        "environment_id": ALPHA,
        "manifest_digest": manifest_digest(arm),
    }
    assert manifest_digest(sibling) == manifest_digest(arm)  # same manifest
    assert receipt["environment_drift"] is False
    # the run's, the driver's and the verifier's environment are one (service.py:136-137,
    # verification/runtime/service.py:309-313)
    plan = world.service.plan_record(receipt["goal_id"])
    goal = world.store.head(world.scope, "goal", receipt["goal_id"])["data"]
    run_environment = goal["profile"]["environment_ref"]
    driver = world.store.get(world.scope, "driver-capabilities", sibling["driver_profile_ref"])
    assert run_environment == env["environment"] == driver["environment_ref"]
    contract = world.store.get(world.scope, "goal-contract", plan["contract_ref"])
    vplan = world.store.get(world.scope, "verification-plan", contract["verification_plan_ref"])
    suite = world.tb2.env_verifier_refs[ALPHA][VERIFIER]
    assert [c["verifier_ref"] for c in contract["acceptance"]] == [suite] * len(
        contract["acceptance"]
    )
    assert [b["environment_ref"] for b in vplan["bindings"]] == (
        [run_environment] * len(vplan["bindings"])
    )
    assert (
        world.store.get(world.scope, "verifier-profile", suite)["environment_ref"]
        == run_environment
    )
    # the verifier that ran is the task image's, not the app image's
    assert world.recorders[ALPHA].calls >= 1 and world.recorders["app"].calls == 0
    assert world.recorders[BETA].calls == 0
    assert proof["task_id"] == TB2_TASK[ALPHA] and proof["goal_id"] == receipt["goal_id"]


def test_two_task_environments_run_in_their_own_siblings_and_verifiers(world: World) -> None:
    alpha, beta = world.run(TB2_TASK[ALPHA]), world.run(TB2_TASK[BETA])
    ra, rb = world.receipt(alpha), world.receipt(beta)
    assert ra["environment_binding"]["environment_id"] == ALPHA
    assert rb["environment_binding"]["environment_id"] == BETA
    assert ra["executed_composition_ref"] != rb["executed_composition_ref"]
    assert (
        world.comp(rb["executed_composition_ref"])["sandbox_profile_ref"]
        == (world.env_records[BETA]["environment"])
    )
    assert world.env_records[ALPHA]["environment"] != world.env_records[BETA]["environment"]
    assert world.recorders[ALPHA].calls >= 1 and world.recorders[BETA].calls >= 1
    assert world.recorders["app"].calls == 0
    # one composition, two environments: a repeat of the first reuses its sibling revision
    again = world.receipt(world.run(TB2_TASK[ALPHA], repeat=1))
    assert again["executed_composition_ref"] == ra["executed_composition_ref"]


def test_a_class_a_candidate_gets_a_sibling_that_keeps_its_carriers_and_suffix(
    world: World,
) -> None:
    candidate = world.candidate()
    receipt = world.receipt(world.run(TB2_TASK[ALPHA], candidate))
    arm, sibling = world.comp(candidate), world.comp(receipt["executed_composition_ref"])
    assert receipt["composition_ref"] == candidate
    assert sibling["composition_id"] == (
        world.comp(world.arm)["composition_id"]
        + releases.ENV_SEP
        + releases.env12(ALPHA)
        + "__cand"
    )
    assert sibling["prompt_bundle_ref"] == arm["prompt_bundle_ref"]  # the candidate's carriers
    assert sibling["sandbox_profile_ref"] == world.env_records[ALPHA]["environment"]
    assert receipt["environment_binding"]["manifest_digest"] == manifest_digest(arm)
    assert manifest_digest(arm) != manifest_digest(world.comp(world.arm))
    # the candidate's sibling is not the baseline's sibling
    base = world.receipt(world.run(TB2_TASK[ALPHA]))
    assert base["executed_composition_ref"] != receipt["executed_composition_ref"]


def test_a_bench_app_task_is_unchanged_by_the_environment_siblings(world: World) -> None:
    baseline: dict[str, Any] = world.service.apps["app"].compositions["codex-cli"]
    obs = world.run(BENCH_TASK, baseline)
    receipt = world.receipt(obs)
    assert obs.success is True and receipt["goal_status"] == "verified"
    assert receipt["environment_binding"] is None and receipt["environment_drift"] is False
    assert receipt["executed_composition_ref"] == baseline == receipt["composition_ref"]
    assert releases.ENV_SEP not in world.comp(baseline)["composition_id"]
    assert world.recorders[ALPHA].calls == 0 and world.recorders["app"].calls == 0


# ==================================================================================================
# the amplai-tb2 app-binding revision (runtime/goals/validation.py:40-60)
# ==================================================================================================
def test_the_new_app_binding_revision_lists_every_admitted_environment_and_verifier(
    world: World,
) -> None:
    (old_ref, old), (new_ref, new) = world.bindings()[-2:]
    assert new_ref["revision"] == old_ref["revision"] + 1
    assert new["registry_revision"] == old["registry_revision"] + 1
    # before: the one app image; after: it plus every task environment installed
    assert world.binding_before["environment_refs"] == old["environment_refs"]
    assert len(old["environment_refs"]) == 1
    wanted = [world.env_records[e]["environment"] for e in (ALPHA, BETA, DELTA)]
    assert new["environment_refs"][:1] == old["environment_refs"]
    assert new["environment_refs"][1:] == wanted
    suites = [world.tb2.env_verifier_refs[e][VERIFIER] for e in (ALPHA, BETA, DELTA)]
    assert (
        new["verifier_profile_refs"][: len(old["verifier_profile_refs"])]
        == old["verifier_profile_refs"]
    )
    assert new["verifier_profile_refs"][len(old["verifier_profile_refs"]) :] == suites
    # each per-environment verifier profile names its own environment
    for env_id, suite in zip((ALPHA, BETA, DELTA), suites, strict=True):
        profile = world.store.get(world.scope, "verifier-profile", suite)
        assert profile["environment_ref"] == world.env_records[env_id]["environment"]
        assert profile["profile_id"] == f"{TB2_APP}-suite{releases.ENV_SEP}{releases.env12(env_id)}"


def test_goal_validation_accepts_the_per_task_environment_only_in_the_new_revision(
    world: World,
) -> None:
    receipt = world.receipt(world.run(TB2_TASK[ALPHA]))
    plan = world.service.plan_record(receipt["goal_id"])
    contract = world.store.get(world.scope, "goal-contract", plan["contract_ref"])
    (old_ref, _), (new_ref, _) = world.bindings()[-2:]
    assert contract["targets"] == [new_ref]
    validate_bindings(world.store, world.d.contracts, world.scope, contract)  # accepted
    # the same contract bound to the binding before S7b: the verifier is not pinned by it
    stale = {**contract, "targets": [old_ref]}
    assert (
        hold_code(validate_bindings, world.store, world.d.contracts, world.scope, stale)
        == "VERIFIER_NOT_INSTALLED"
    )


# ==================================================================================================
# releases pin the siblings (§3.3)
# ==================================================================================================
def test_releases_pin_the_environment_siblings_of_the_installed_compositions(world: World) -> None:
    installed = world.tb2.compositions
    sibling = world.service.env_sibling(world.tb2, world.arm, ALPHA)
    assert releases.pin_allowed(world.store, world.scope, installed, sibling) == "codex-cli"
    assert releases.pin_allowed(world.store, world.scope, installed, world.arm) == "codex-cli"
    # a class-A candidate's sibling is pinned too
    cand_sibling = world.service.env_sibling(world.tb2, world.candidate(), BETA)
    assert releases.pin_allowed(world.store, world.scope, installed, cand_sibling) == "codex-cli"
    # and the app's select_composition accepts it as a pin (the plan of a trial goal)
    chosen = world.service.select_composition(world.tb2, "logic_change", pin=sibling)
    assert (
        chosen["ref"] == sibling and chosen["pinned"] is True and chosen["cell_id"] == "codex-cli"
    )


def test_releases_refuse_a_sibling_whose_class_a_fields_were_changed(world: World) -> None:
    installed = world.tb2.compositions
    sibling = world.service.env_sibling(world.tb2, world.arm, ALPHA)
    forged = {
        **world.comp(sibling),
        "composition_id": world.comp(sibling)["composition_id"] + "__forged",
        "driver_profile_ref": world.env_records[BETA]["driver"],
        "sandbox_profile_ref": world.env_records[BETA]["environment"],
        "model_profile_ref": world.env_records[BETA]["model"],
        "qualification_ref": world.env_records[BETA]["qualification"],
    }
    forged_ref = world.service._put("harness-composition", forged["composition_id"], forged)
    assert releases.pin_allowed(world.store, world.scope, installed, forged_ref) is None
    hold = hold_of(world.service.select_composition, world.tb2, None, pin=forged_ref)
    assert hold.code == "COMPOSITION_PIN"


def test_releases_refuse_a_composition_named_as_a_sibling_of_an_environment_nothing_installed(
    world: World,
) -> None:
    installed = world.tb2.compositions
    # a composition named like a sibling of an environment nothing installed, with another
    # driver, model and sandbox (a class B change), is no sibling: no installed environment
    # composition of that name exists to equal (§3.3: "the installed environment composition of
    # that name exists")
    beta = world.env_records[BETA]
    stranger = {
        **world.comp(world.arm),
        "composition_id": world.comp(world.arm)["composition_id"] + releases.ENV_SEP + "1" * 12,
        "driver_profile_ref": beta["driver"],
        "sandbox_profile_ref": beta["environment"],
        "model_profile_ref": beta["model"],
        "qualification_ref": beta["qualification"],
    }
    stranger_ref = manifest.put_composition(  # the profile checks of CompositionService.register
        world.store, world.scope, world.d.contracts, stranger["composition_id"], stranger
    )
    assert releases.pin_allowed(world.store, world.scope, installed, stranger_ref) is None
    hold = hold_of(world.service.select_composition, world.tb2, None, pin=stranger_ref)
    assert hold.code == "COMPOSITION_PIN"


def test_a_sibling_of_another_apps_composition_is_no_cell_of_the_app(world: World) -> None:
    bench: dict[str, Any] = world.service.apps["app"].compositions["codex-cli"]
    assert hold_code(world.service.env_sibling, world.tb2, bench, ALPHA) == "CELL_UNKNOWN"
    # the executor refuses it before any goal (a pin that is no installed composition of the app)
    assert hold_code(world.run, TB2_TASK[ALPHA], bench) == "COMPOSITION_PIN"


# ==================================================================================================
# env_sibling rules (§3.2) and the refusals before any claim
# ==================================================================================================
def test_env_sibling_is_idempotent_and_a_sibling_stays_in_its_environment(world: World) -> None:
    first = world.service.env_sibling(world.tb2, world.arm, ALPHA)
    assert world.service.env_sibling(world.tb2, world.arm, ALPHA) == first
    assert manifest.environment_of(world.store, world.scope, first) == releases.env12(ALPHA)
    assert manifest.environment_of(world.store, world.scope, world.arm) is None
    # the sibling of the sibling in the same environment is itself ...
    assert manifest.env_sibling(world.store, world.scope, world.d.contracts, first, ALPHA) == first
    # ... and in another environment it is refused: a sibling is of one environment (§3.2)
    error = hold_of(manifest.env_sibling, world.store, world.scope, world.d.contracts, first, BETA)
    assert error.code == "ENVIRONMENT_UNQUALIFIED"
    assert error.details["environment_id"] == BETA


def test_a_missing_pair_holds_before_any_goal_exists(world: World) -> None:
    """An environment the app installed but qualified for no cell of the arm (missing pair)."""
    before, prompts = world.goals(), list(world.container.prompts)
    error = hold_of(world.run, TB2_TASK[DELTA])
    assert error.code == "ENVIRONMENT_UNQUALIFIED"
    assert error.details["environment_id"] == DELTA
    assert world.goals() == before and world.container.prompts == prompts
    assert world.recorders[DELTA].calls == 0
    # it is the same refusal at the service and in the manifest layer
    assert (
        hold_code(world.service.env_sibling, world.tb2, world.arm, DELTA)
        == "ENVIRONMENT_UNQUALIFIED"
    )


def test_an_environment_the_app_did_not_install_holds_before_any_goal(world: World) -> None:
    before = world.goals()
    error = hold_of(world.run, TB2_TASK[GAMMA])
    assert error.code == "ENVIRONMENT_UNQUALIFIED"
    assert error.details == {"app": TB2_APP, "environment_id": GAMMA}
    assert world.goals() == before and world.container.prompts == []
    # the bench app installed no task environment at all
    bench: dict[str, Any] = world.service.apps["app"].compositions["codex-cli"]
    assert hold_code(world.service.env_sibling, world.service.apps["app"], bench, ALPHA) == (
        "ENVIRONMENT_UNQUALIFIED"
    )


def test_a_tb2_task_claiming_the_apps_own_environment_is_held(world: World) -> None:
    error = hold_of(world.run, "tb2-appenv-task")
    assert error.code == "TRIAL_ENVIRONMENT"
    assert error.details == {"environment_id": "app", "grading": "tb2_tests"}
    assert world.container.prompts == [] and world.goals() == 0


def test_an_environment_qualified_for_another_model_is_no_sibling(
    deployment: Any, tmp_path: Path
) -> None:
    """The task image's model record must be the arm's `provider_model_id` and effort (§3.2)."""
    w = make_world(deployment, tmp_path)
    caps = app_capabilities(TB2_APP)
    other = install_codex_profile(
        w.store,
        w.scope,
        inputs_in(tmp_path / "in-o", "other", "8", "qual-tb2-other", model=OTHER_MODEL),
        caps,
    )
    w.service.install(
        w.tb2_cfg,
        driver_refs={"codex-cli": w.tb2_refs},
        environments=[
            TaskEnvironment(
                "tb2-other", other["environment"], {"codex-cli": other}, w.recorders[ALPHA]
            )
        ],
    )
    error = hold_of(w.service.env_sibling, w.tb2, w.arm, "tb2-other")
    assert error.code == "ENVIRONMENT_UNQUALIFIED"
    assert error.details["field"] == "provider_model_id"
    assert error.details["composition"] == MODEL and error.details["environment"] == OTHER_MODEL


def test_install_refuses_records_of_another_environment_and_unknown_cells(world: World) -> None:
    alpha, beta = world.env_records[ALPHA], world.env_records[BETA]
    wrong = TaskEnvironment("tb2-wrong", alpha["environment"], {"codex-cli": beta},
                            world.recorders[ALPHA])  # fmt: skip
    error = hold_of(world.service.install, world.tb2_cfg, driver_refs={"codex-cli": world.tb2_refs},
                    environments=[wrong])  # fmt: skip
    assert error.code == "ENVIRONMENT_UNQUALIFIED"
    assert error.details == {"environment_id": "tb2-wrong", "cells": ["codex-cli"]}
    unknown = TaskEnvironment("tb2-cells", alpha["environment"], {"claude-cli": alpha},
                              world.recorders[ALPHA])  # fmt: skip
    error = hold_of(world.service.install, world.tb2_cfg, driver_refs={"codex-cli": world.tb2_refs},
                    environments=[unknown])  # fmt: skip
    assert error.code == "CELL_UNKNOWN" and error.details["cells"] == ["claude-cli"]


@pytest.mark.parametrize("bad", ["app", "has space", "", "-lead"])
def test_install_refuses_a_task_environment_id_that_is_app_or_malformed(
    world: World, bad: str
) -> None:
    alpha = world.env_records[ALPHA]
    env = TaskEnvironment(bad, alpha["environment"], {"codex-cli": alpha}, world.recorders[ALPHA])
    with pytest.raises(RuntimeFault) as fault:
        world.service.install(world.tb2_cfg, driver_refs={"codex-cli": world.tb2_refs},
                              environments=[env])  # fmt: skip
    assert fault.value.code == "ENVIRONMENT_ID" and not isinstance(fault.value, Hold)


def test_install_refuses_a_task_environment_listed_twice(world: World) -> None:
    alpha = world.env_records[ALPHA]
    env = TaskEnvironment(ALPHA, alpha["environment"], {"codex-cli": alpha}, world.recorders[ALPHA])
    with pytest.raises(RuntimeFault) as fault:
        world.service.install(world.tb2_cfg, driver_refs={"codex-cli": world.tb2_refs},
                              environments=[env, env])  # fmt: skip
    assert fault.value.code == "ENVIRONMENT_ID"


# ==================================================================================================
# drift: the task environment's digest against the stage plan's pin (§10.5 step 6, §2.10)
# ==================================================================================================
def test_an_unchanged_task_environment_runs_under_its_pin(world: World) -> None:
    world.executor.environment_digests = world.pins(ALPHA, BETA)
    obs = world.run(TB2_TASK[ALPHA])
    receipt = world.receipt(obs)
    assert receipt["environment_drift"] is False and receipt["goal_status"] == "verified"
    assert receipt["environment_binding"]["environment_id"] == ALPHA


def test_a_changed_task_environment_runs_nothing_and_is_missing(world: World) -> None:
    world.executor.environment_digests = world.pins(ALPHA, BETA)
    world.probe_state[ALPHA] = "rebuilt"  # the probe now measures another image state
    before, prompts = world.goals(), list(world.container.prompts)
    obs = world.run(TB2_TASK[ALPHA])
    receipt, proof = world.receipt(obs), world.proof(obs)
    assert obs.success is None and receipt["success"] is None  # counts as missing, not a failure
    assert receipt["environment_drift"] is True
    assert receipt["environment_binding"] == {
        "environment_id": ALPHA,
        "manifest_digest": manifest_digest(world.comp(world.arm)),
    }
    assert receipt["goal_id"] is None and receipt["goal_status"] is None
    assert receipt["goal_reason"] == "environment_drift"
    assert receipt["executed_composition_ref"] == world.arm  # nothing ran in a sibling
    assert (obs.safety_failures, obs.unknown_effects, obs.cost_microunits) == (0, 0, 0)
    assert (obs.input_tokens, obs.output_tokens) == (0, 0)
    assert proof["environment_drift"] == {
        "environment_id": ALPHA,
        "pinned": world.executor.environment_digests[ALPHA],
    }
    assert world.goals() == before and world.container.prompts == prompts  # nothing ran
    assert world.recorders[ALPHA].calls == 0
    # the other environment is not drifted and still runs
    assert world.receipt(world.run(TB2_TASK[BETA]))["environment_drift"] is False


def test_drift_is_judged_per_trial_against_the_pin_with_the_same_probe(world: World) -> None:
    pins = world.pins(ALPHA)
    world.executor.environment_digests = pins
    # the pin is the digest of the probed record, as EvaluationService.freeze computes it
    record = world.store.get(world.scope, "environment", world.env_records[ALPHA]["environment"])
    assert pins[ALPHA] == digest(world.executor.environment_probe(record))
    assert pins[ALPHA] == task_environment_digest(
        world.store,
        world.scope,
        world.env_records[ALPHA]["environment"],
        world.executor.environment_probe,
    )
    assert pins[ALPHA] != task_environment_digest(
        world.store, world.scope, world.env_records[ALPHA]["environment"], None
    )  # another probe, another digest: the check uses the evaluation service's
    world.probe_state[ALPHA] = "rebuilt"
    assert world.receipt(world.run(TB2_TASK[ALPHA], repeat=0))["environment_drift"] is True
    world.probe_state[ALPHA] = "same"  # restored: the next trial runs again
    assert world.receipt(world.run(TB2_TASK[ALPHA], repeat=1))["environment_drift"] is False


def test_with_pins_in_force_an_unpinned_task_environment_counts_as_drifted(world: World) -> None:
    """`environment_digests` {} means the pins are in force and name no task environment
    (local_executor.py:274-279); None means no check (calibration)."""
    world.executor.environment_digests = {}
    receipt = world.receipt(world.run(TB2_TASK[ALPHA]))
    assert receipt["environment_drift"] is True and receipt["goal_id"] is None
    assert world.receipt(world.run(TB2_TASK[BETA], repeat=1))["environment_drift"] is True
    world.executor.environment_digests = None
    assert world.receipt(world.run(TB2_TASK[ALPHA], repeat=2))["environment_drift"] is False


def test_a_missing_pair_is_held_before_the_drift_check_runs_anything(world: World) -> None:
    """An uninstalled or pair-less environment is a Hold, not a drift: no receipt, no goal."""
    world.executor.environment_digests = {}
    assert hold_code(world.run, TB2_TASK[GAMMA]) == "ENVIRONMENT_UNQUALIFIED"
    world.executor.environment_digests = world.pins(DELTA)
    assert hold_code(world.run, TB2_TASK[DELTA]) == "ENVIRONMENT_UNQUALIFIED"
    assert world.goals() == 0


def test_the_bench_app_is_never_checked_for_drift(world: World) -> None:
    world.executor.environment_digests = {}
    baseline: dict[str, Any] = world.service.apps["app"].compositions["codex-cli"]
    receipt = world.receipt(world.run(BENCH_TASK, baseline))
    assert receipt["environment_drift"] is False and receipt["environment_binding"] is None
    assert receipt["success"] is True


# ==================================================================================================
# the local deployment: per-environment profiles and ports from `local.json` (§12.2)
# ==================================================================================================
def run_cli(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    parsed: dict[str, Any] = json.loads(result.output)
    return parsed


def local_config(tmp_path: Path, environments: list[dict[str, Any]]) -> Path:
    """`amplai ops local-init` output with `apps[0].environments` set (task images, §12.2)."""
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
    run_cli(
        "ops", "local-init", "--repo", str(repo), "--app", "app",
        "--codex-home", str(codex_home),
        "--container-profile", str(inputs.container_profile),
        "--qualification-report", str(inputs.qualification_report),
        "--egress-profile", str(inputs.egress_profile),
        "--egress-qualification", str(inputs.egress_qualification),
        "--verifier", "check=python3 -c 'import app' | app imports",
        "--home", str(home), "--operator", "pinesky",
    )  # fmt: skip
    config = home / "local.json"
    doc = json.loads(config.read_text())
    doc["apps"][0]["environments"] = environments
    config.write_text(json.dumps(doc))
    return config


def env_entry(tmp_path: Path, env_id: str, char: str, *, report: bool = True) -> dict[str, Any]:
    inputs = inputs_in(tmp_path / f"in-{env_id}", env_id, char, f"qual-{env_id}")
    return {
        "environment_id": env_id,
        "container_profile": str(inputs.container_profile),
        "qualification_reports": {"codex-cli": str(inputs.qualification_report)} if report else {},
    }


def test_the_local_deployment_installs_each_task_environment_with_its_records_and_port(
    tmp_path: Path,
) -> None:
    from amplai_foundry.runtime.local_deployment import LocalProductDeployment

    config = local_config(
        tmp_path, [env_entry(tmp_path, ALPHA, "e"), env_entry(tmp_path, BETA, "f")]
    )
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        installed = dep.service.apps["app"]
        assert list(installed.env_compositions) == [ALPHA, BETA]
        assert list(installed.env_verifier_refs) == [ALPHA, BETA]
        assert dep.environment_skips == []
        store, scope = dep.store, dep.scope
        base = store.get(scope, "harness-composition", installed.compositions["codex-cli"])
        for env_id, char in ((ALPHA, "e"), (BETA, "f")):
            ref = installed.env_compositions[env_id]["codex-cli"]
            value = store.get(scope, "harness-composition", ref)
            assert value["composition_id"] == releases.env_composition_id(
                base["composition_id"], env_id
            )
            environment = store.get(scope, "environment", value["sandbox_profile_ref"])
            assert environment["image"].endswith("@sha256:" + char * 64)  # the task image
            driver = store.get(scope, "driver-capabilities", value["driver_profile_ref"])
            assert driver["environment_ref"] == value["sandbox_profile_ref"]
            suite_ref = installed.env_verifier_refs[env_id]["check"]
            suite = store.get(scope, "verifier-profile", suite_ref)
            assert suite["environment_ref"] == value["sandbox_profile_ref"]
            # each environment's driver has its own port in the registry
            key = (*scope.keys(), digest(value["driver_profile_ref"]))
            assert key in dep.coordinator.registry.entries
            # and the sibling of the app's installed composition is pinned by the releases rule
            arm = installed.compositions["codex-cli"]
            sibling = dep.service.env_sibling(installed, arm, env_id)
            assert sibling == ref
            assert (
                releases.pin_allowed(store, scope, installed.compositions, sibling) == "codex-cli"
            )
        # the app-binding of the app lists both task images and their verifier profiles
        refs = [r for r, _ in store.list_objects(scope, "app-binding") if r["id"] == "app"]
        binding = store.get(scope, "app-binding", max(refs, key=lambda r: r["revision"]))
        for env_id in (ALPHA, BETA):
            record = installed.env_compositions[env_id]["codex-cli"]
            assert (
                store.get(scope, "harness-composition", record)["sandbox_profile_ref"]
                in (binding["environment_refs"])
            )
            assert installed.env_verifier_refs[env_id]["check"] in binding["verifier_profile_refs"]
        assert len(binding["environment_refs"]) == 3  # the app image and two task images
    finally:
        dep.close()


def test_an_environment_without_a_report_for_any_driver_is_skipped_with_its_reason(
    tmp_path: Path,
) -> None:
    from amplai_foundry.runtime.local_deployment import LocalProductDeployment

    config = local_config(
        tmp_path, [env_entry(tmp_path, ALPHA, "e"), env_entry(tmp_path, BETA, "f", report=False)]
    )
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        installed = dep.service.apps["app"]
        assert list(installed.env_compositions) == [ALPHA]  # no pair for beta: not installed
        assert BETA not in installed.env_verifier_refs
        assert dep.environment_skips == [
            {"app": "app", "environment_id": BETA, "cell_id": "codex-cli", "code": None,
             "reason": "no qualification report for this driver"},
            {"app": "app", "environment_id": BETA, "cell_id": None,
             "code": "ENVIRONMENT_UNQUALIFIED", "reason": "no cell is qualified in it"},
        ]  # fmt: skip
        # its trials hold before any claim
        arm = installed.compositions["codex-cli"]
        assert hold_code(dep.service.env_sibling, installed, arm, BETA) == "ENVIRONMENT_UNQUALIFIED"
        assert hold_code(dep.service.environment_ref, installed, BETA) == "ENVIRONMENT_UNQUALIFIED"
    finally:
        dep.close()


@pytest.mark.parametrize("ids", [["app"], [ALPHA, ALPHA]])
def test_local_config_refuses_the_app_environment_and_duplicate_task_environments(
    tmp_path: Path, ids: list[str]
) -> None:
    from pydantic import ValidationError

    from amplai_foundry.runtime.local_deployment import LocalConfig

    config = local_config(tmp_path, [env_entry(tmp_path, GAMMA, "9")])
    doc = json.loads(config.read_text())
    doc["apps"][0]["environments"] = [env_entry(tmp_path, i, "e") for i in ids]
    with pytest.raises(ValidationError, match="each task environment id once, never 'app'"):
        LocalConfig.model_validate(doc)


# ==================================================================================================
# StageRunner: the plan pins the task environments, the executor checks them (§2.9, §10.5 step 6)
# ==================================================================================================
TB2_DEV, TB2_VAL = 12, 16  # a screening stage (12 exploratory) and a focused one (16 confirmatory)


class EnvFake(Fake):
    """The S11 scripted trial executor with the task-environment check of `LocalTrialExecutor`.

    `StageRunner` sets `environment_digests` and `environment_probe` on the executor while a stage
    experiment runs. This stand-in digests a TB2 task's environment with the real
    `task_environment_digest` and, on a mismatch with the pin, returns the receipt shape of a
    drifted trial (success None, `environment_drift` true, no usage)."""

    def __init__(self, world: Any, app_id: str, env_of: dict[str, str]) -> None:
        super().__init__(world)
        self.app_id, self.env_of = app_id, env_of
        self.environment_digests: dict[str, str] | None = None
        self.environment_probe: Any = None
        self.seen: list[tuple[str, dict[str, str] | None, Any]] = []

    def __call__(
        self, composition: dict[str, Any], case: dict[str, Any], repeat: int, mode: str
    ) -> Any:
        from amplai_foundry.evaluation.service import TrialObservation
        from amplai_foundry.runtime.contracts.identity import canonical

        env_id = self.env_of.get(case["case_id"])
        pins = None if self.environment_digests is None else dict(self.environment_digests)
        self.seen.append((case["case_id"], pins, self.environment_probe))
        if env_id is None or pins is None:
            return super().__call__(composition, case, repeat, mode)
        d = self.world
        installed = d.rig.service.apps[self.app_id]
        now_digest = task_environment_digest(
            d.store,
            d.scope,
            d.rig.service.environment_ref(installed, env_id),
            self.environment_probe,
        )
        if pins.get(env_id) == now_digest:
            return super().__call__(composition, case, repeat, mode)
        receipt = {
            "success": None, "safety_failures": 0, "unknown_effects": 0, "cost_microunits": 0,
            "input_tokens": 0, "output_tokens": 0, "usage_status": "measured", "mode": mode,
            "composition_ref": composition, "task_id": case["case_id"], "repeat": repeat,
            "scope": d.scope.wire(), "goal_id": None, "goal_status": None,
            "environment_binding": {
                "environment_id": env_id,
                "manifest_digest": "sha256:" + "0" * 64,
            },
            "environment_drift": True, "cell_id": CELL, "strategy": "repair_loop",
            "corpus_version": self.corpus_version, "split": case["split"],
            "harness_sha": self.harness_sha, "model_snapshot": self.snapshot,
            "cache_key": self.cache_key(composition, case, repeat),
        }  # fmt: skip
        artifact = d.artifacts.admit(
            d.scope, canonical(receipt), "application/json", trust="verifier"
        )
        return TrialObservation(None, (artifact,), cost_microunits=0, input_tokens=0,
                                output_tokens=0)  # fmt: skip


def tb2_stage_world(tmp_path: Path, stack: Any, *, holdout: bool = True) -> Any:
    """The S11 world with the `amplai-tb2` app installed beside the bench app (task environments
    alpha and beta), a frozen corpus of version 2.0.0 whose main set adds TB2 tasks of the app
    (alternating alpha and beta; one in the uninstalled gamma), and a stage runner over them.

    Real TB2 tasks never go to holdout (§10.2: `corpus_v2` refuses it), while every confirmatory
    stage plan needs 16 holdout tasks of its app (IC-09). `holdout` adds 16 own-source tasks of the
    `tb2` base in the alpha environment as stand-ins so a plan can be written; without them the
    plan is refused (`test_a_tb2_app_without_holdout_tasks_cannot_plan_stages`)."""
    w = stack.enter_context(build_world(tmp_path))
    service, d = w.rig.service, w.d
    repo = make_repo(tmp_path / "tb2-root")
    w.rig.workspaces.repos[TB2_APP] = repo
    caps = app_capabilities(TB2_APP)
    refs = install_codex_profile(
        d.store, d.scope, inputs_in(tmp_path / "in-app", "app", "d", "qual-tb2-app"), caps
    )
    records = {
        env_id: install_codex_profile(
            d.store,
            d.scope,
            inputs_in(tmp_path / f"in-{env_id}", env_id, char, f"qual-{env_id}"),
            caps,
        )
        for env_id, char in ((ALPHA, "e"), (BETA, "f"))
    }
    verifier = SuiteVerifier(
        [(VERIFIER, list(CHECK), 60)],
        workspaces=w.rig.workspaces,
        scope=d.scope,
        sandbox=HostSandbox(),
    )
    service.install(
        AppConfig(TB2_APP, repo, service.apps["app"].config.verifiers),
        driver_refs={"codex-cli": refs},
        environments=[TaskEnvironment(e, r["environment"], {"codex-cli": r}, verifier)
                      for e, r in records.items()],
    )  # fmt: skip
    dev = [t for t in w.corpus.tasks if t.split == "development"][:TB2_DEV]
    val = [t for t in w.corpus.tasks if t.split == "validation"][: TB2_VAL + 1]
    env_of: dict[str, str] = {}
    tb2_tasks = []
    for i, source in enumerate([*dev, *val]):
        task_id = "tb2-" + source.task_id
        env_id = GAMMA if source is val[-1] else (ALPHA, BETA)[i % 2]
        env_of[task_id] = env_id
        tb2_tasks.append(
            replace(
                source,
                task_id=task_id,
                domain="terminal",
                base_id="tb2",
                environment_id=env_id,
                grading="tb2_tests",
                source={
                    "kind": "tb2",
                    "ref": f"tb2@abc:{task_id}",
                    "author": "tb2",
                    "created": "2026-09-30",
                },
                license="Apache-2.0",
                hidden_map={},
                reference={},
            )
        )
    standins = [
        replace(source, task_id="own-" + source.task_id, base_id="tb2", environment_id=ALPHA)
        for source in w.corpus.tasks if source.split == "holdout"
    ][:16] if holdout else []  # fmt: skip
    bases = {**w.corpus.bases, "tb2": {"dir": "bases/bench", "base_commit": SHA, "app_id": TB2_APP}}
    corpus = replace(
        w.corpus, version="2.0.0", bases=bases, tasks=(*w.corpus.tasks, *tb2_tasks, *standins)
    )
    w.refs = corpus_v2.freeze(w.operator, w.store, w.artifacts, corpus, holdout_use_limit=50)
    w.version_ref = w.evaluator()
    informative = tuple(t for t in env_of if env_of[t] != GAMMA)  # gamma: not informative
    # a calibration of the TB2 app: only its tasks are informative (the bench tasks are not its)
    w.summary_ref = w.summary({"development": 0, "validation": 0}, extra=informative)
    from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps

    executor = EnvFake(w, TB2_APP, env_of)
    executor.corpus_version = "2.0.0"
    w.executor = executor
    w.ops = LocalMetaOps(w.dep, corpus, executor, driver=CELL, app_id=TB2_APP)  # type: ignore[arg-type]
    w.tb2_ids, w.env_of = list(env_of), env_of
    w.records = records
    return w


@pytest.fixture
def stage(tmp_path: Path) -> Any:
    import contextlib

    with contextlib.ExitStack() as stack:
        yield tb2_stage_world(tmp_path, stack)


def test_a_tb2_app_without_holdout_tasks_cannot_plan_stages(tmp_path: Path) -> None:
    """Observed rule, not a decision of this slice: TB2 tasks never go to holdout (§10.2) and a
    stage plan needs 16 holdout tasks of its app (IC-09), so the plan of an app whose main set
    is TB2 tasks only is refused. Whether `amplai-tb2` stages need a holdout is not in the
    contract (확인 필요)."""
    import contextlib

    with contextlib.ExitStack() as stack:
        w = tb2_stage_world(tmp_path, stack, holdout=False)
        pid = w.propose("a2")
        w.script(pid)
        error = hold_of(w.ops.search, pid, cell_id=CELL, root_budget=BUDGET)
        assert error.code == "SAMPLE_UNDERPOWERED"
        assert error.details == {"stage": "holdout", "task_count": 0, "need": 16}
        assert w.objects("stage-plan") == [] and w.executor.calls == []


def stage_plan(w: Any, pid: str) -> dict[str, Any]:
    head = w.store.head(w.scope, "stage-run", "stagerun-" + pid)
    plan: dict[str, Any] = w.store.get(w.scope, "stage-plan", head["data"]["plan_ref"])
    return plan


def test_the_stage_plan_pins_the_digest_of_every_installed_task_environment(stage: Any) -> None:
    pid = stage.propose("a2")
    stage.script(pid)
    result = stage.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    plan = stage_plan(stage, pid)
    assert plan["app_id"] == TB2_APP
    assert set(plan["environment_digests"]) == {ALPHA, BETA}  # gamma: not installed, not pinned
    app = stage.rig.service.apps[TB2_APP]
    for env_id, pinned in plan["environment_digests"].items():
        record = stage.store.get(stage.scope, "environment", stage.records[env_id]["environment"])
        assert pinned == digest(record)  # the evaluation service has no probe in this world
        assert pinned == task_environment_digest(
            stage.store, stage.scope, stage.rig.service.environment_ref(app, env_id), None
        )
    assert plan["environment_digests"][ALPHA] != plan["environment_digests"][BETA]
    assert result["findings"] == {}  # no drift: nothing found


def test_every_stage_run_gives_the_executor_the_pins_and_the_probe_then_takes_them_back(
    stage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(env: dict[str, Any]) -> dict[str, Any]:
        return {**env, "probed": True}

    monkeypatch.setattr(stage.local.evaluation, "environment_probe", probe)
    pid = stage.propose("a2")
    stage.script(pid)
    stage.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    plan = stage_plan(stage, pid)
    seen = stage.executor.seen
    assert seen and {case for case, _, _ in seen} <= set(stage.tb2_ids)
    assert all(pins == plan["environment_digests"] and p is probe for _, pins, p in seen)
    # the pins were computed under the evaluation service's probe (the same one as the checks)
    record = stage.store.get(stage.scope, "environment", stage.records[ALPHA]["environment"])
    assert plan["environment_digests"][ALPHA] == digest(probe(record))
    # outside a stage run the executor carries none (a calibration is never drift-checked)
    assert stage.executor.environment_digests is None and stage.executor.environment_probe is None


def test_a_drifted_task_environment_adds_task_environment_drift_to_the_stage_record(
    stage: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplai_foundry.meta_harness import stages

    assert stages.TASK_ENVIRONMENT_DRIFT == "task_environment_drift"
    pid = stage.propose("a2")
    stage.script(pid)
    first = stage.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    assert first["findings"] == {}  # screening ran against unchanged environments
    alpha_image = stage.store.get(stage.scope, "environment", stage.records[ALPHA]["environment"])[
        "environment_id"
    ]

    def rebuilt(env: dict[str, Any]) -> dict[str, Any]:
        """The alpha task image was rebuilt: its probe now measures another state."""
        return {**env, "probed": "rebuilt"} if env["environment_id"] == alpha_image else env

    monkeypatch.setattr(stage.local.evaluation, "environment_probe", rebuilt)
    stage.ops.approve_stage(pid, "focused")
    findings = stage.ops.stages(pid)["findings"]
    assert stages.TASK_ENVIRONMENT_DRIFT in findings["focused"]
    assert "screening" not in findings
    # the drifted trials carry a receipt that says so and count as missing
    trials = [t for t in stage.objects("eval-trial") if "artifact_refs" in t[1]]
    receipts = [
        read_receipt(stage.artifacts.read(stage.scope, t[1]["artifact_refs"][0], trusted=True))
        for t in trials
    ]
    drifted = [r for r in receipts if r.get("environment_drift")]
    assert drifted and all(r["success"] is None and r["goal_id"] is None for r in drifted)
    assert {r["environment_binding"]["environment_id"] for r in drifted} == {ALPHA}
    # the report's own `environment_drift` reason stays tied to the experiment's environment_ref
    step = next(s for s in stage.ops.stages(pid)["steps"] if s["stage"] == "focused")
    report = stage.store.get(stage.scope, "eval-report", step["report_ref"])
    assert "environment_drift" not in json.dumps(report.get("reasons") or [])


def test_sampling_changed_holds_per_app_when_a_summary_covers_both_apps(stage: Any) -> None:
    """The experiment freeze recomputes the subset over the whole split (IC-15,
    `EvaluationService._cases`); the stage pins the plan app's cases. A calibration summary
    that calls both apps' tasks informative therefore gives two subsets, and the freeze holds
    SAMPLING_CHANGED before any trial runs: a calibration of one app (S7b) is the one a stage of
    that app reads."""
    both = stage.summary(extra=tuple(t for t in stage.tb2_ids if stage.env_of[t] != GAMMA))
    pid = stage.propose("a2")
    stage.script(pid)
    runner = stage.ops.stage_runner(pid, cell_id=CELL)
    runner.summary_ref = both
    runner.plan(pid, cell_id=CELL, root_budget=BUDGET)
    assert hold_code(runner.advance, pid) == "SAMPLING_CHANGED"
    assert stage.executor.calls == [] and stage.objects("eval-trial") == []


def test_the_plan_keeps_its_app_whatever_app_a_later_operation_names(stage: Any) -> None:
    """The stage plan stores its app (S7b): a different `--app` later does not change the cases
    the stages select, so the IC-15 recomputation (SAMPLING_CHANGED) holds per app."""
    from amplai_foundry.runtime.execution.meta_ops import LocalMetaOps

    pid = stage.propose("a2")
    stage.script(pid)
    stage.ops.search(pid, cell_id=CELL, root_budget=BUDGET)
    sampled = {case for case, _, _ in stage.executor.seen}
    assert sampled and sampled <= set(stage.tb2_ids)
    bench = LocalMetaOps(stage.dep, stage.ops.corpus, stage.executor, driver=CELL, app_id="app")  # type: ignore[arg-type]
    stage.executor.seen.clear()
    assert bench.approve_stage(pid, "focused")["state"] == "passed"
    focused = {case for case, _, _ in stage.executor.seen}
    assert focused and focused <= set(stage.tb2_ids)  # still the plan's app, not --app app
    assert not {c for c in focused if not c.startswith("tb2-")}
