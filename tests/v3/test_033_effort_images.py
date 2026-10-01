"""Work 033 S7b: effort probes keyed by (cell, task environment) (W2 part 1 clarification).

An effort cell installs in a TB2 task environment only with an accepted probe of that exact
(cell, environment) pair whose image and driver version are the environment's current ones.
The app-image probe (`probe-<cell hash>`, §2.4) is unchanged and never counts for a task
environment; another environment's probe never counts either; probes never run at boot.

Real: `amplai ops local-init` / `local-cell add` output, `LocalConfig`, `probe_local_cell`, the
`LocalProductDeployment` boot (`_task_environments`, `CellInstaller.profile`). Stand-ins: scripted
probe turns (no docker, no provider, no credential) and fake pinned task images.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.cells import (
    PROBE_KIND,
    Cell,
    latest_probe,
    probe_id,
    run_probe,
    store_probe,
)
from amplai_foundry.runtime.execution.codex import AUTH, CodexProfileInputs
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.local_deployment import LocalProductDeployment, probe_local_cell
from rc06_rig import IMAGE, codex_inputs, make_repo

MODEL = "gpt-5.6-sol"
HIGH = "codex-cli.gpt-5.6-sol.high"
ALPHA, BETA = "tb2-alpha", "tb2-beta"


class Turn:
    """A scripted probe turn: completes, or fails like a provider refusing the flag."""

    cell_id = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def run(self, **_: Any) -> TurnResult:
        if self.fail:
            raise Hold("TURN_FAILED", "provider refused the effort")
        return TurnResult(
            {"ok": True}, {"input_tokens": 2, "output_tokens": 1}, 0.0, "sha256:" + "0" * 64
        )


def run(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    parsed: dict[str, Any] = json.loads(result.output)
    return parsed


def image_of(tag: str, char: str) -> str:
    return f"localhost:5000/amplai-tb2-{tag}@sha256:" + char * 64


def write_image(root: Path, tag: str, char: str) -> CodexProfileInputs:
    """A task image's container profile and passing codex report (the `container_qualify.py`
    shape), pinned by digest; rewriting it with another char is a rebuilt image."""
    root.mkdir(parents=True, exist_ok=True)
    inputs = codex_inputs(root)
    image = image_of(tag, char)
    container = json.loads(inputs.container_profile.read_text())
    inputs.container_profile.write_text(json.dumps({**container, "image": image}))
    doc = json.loads(inputs.qualification_report.read_text())
    doc["container_image"] = image
    doc["reports"]["codex-cli"].update(qualification_id=f"qual-{tag}", model=MODEL)
    inputs.qualification_report.write_text(json.dumps(doc))
    out: CodexProfileInputs = replace(inputs, model=MODEL)
    return out


class World:
    def __init__(self, tmp_path: Path, envs: dict[str, str]) -> None:
        self.tmp = tmp_path
        repo = make_repo(tmp_path)
        inputs = codex_inputs(tmp_path)
        codex_home = tmp_path / "codex-home"
        (codex_home / ".codex").mkdir(parents=True)
        (codex_home / AUTH).write_text('{"tokens": "x"}')
        home = tmp_path / "amplai"
        run(
            "ops", "local-init", "--repo", str(repo), "--app", "app",
            "--codex-home", str(codex_home),
            "--container-profile", str(inputs.container_profile),
            "--qualification-report", str(inputs.qualification_report),
            "--egress-profile", str(inputs.egress_profile),
            "--egress-qualification", str(inputs.egress_qualification),
            "--verifier", "check=python3 -c 'import app' | app imports",
            "--home", str(home), "--operator", "pinesky",
        )  # fmt: skip
        self.config = home / "local.json"
        self.envs: dict[str, CodexProfileInputs] = {
            env_id: write_image(tmp_path / f"in-{env_id}", env_id, char)
            for env_id, char in envs.items()
        }
        doc = json.loads(self.config.read_text())
        doc["apps"][0]["environments"] = [
            {
                "environment_id": env_id,
                "container_profile": str(e.container_profile),
                "qualification_reports": {"codex-cli": str(e.qualification_report)},
            }
            for env_id, e in self.envs.items()
        ]
        self.config.write_text(json.dumps(doc))
        os.chmod(self.config, 0o600)
        run(
            "ops", "local-cell", "add", "--driver", "codex-cli", "--model", MODEL,
            "--effort", "high", "--config", str(self.config),
        )  # fmt: skip
        self.turns: list[tuple[str, Path]] = []  # (cell id, container profile) per probe turn

    def factory(self, fail: bool = False) -> Any:
        def make(cell: Cell, inputs: CodexProfileInputs, scratch_root: Path) -> Turn:
            self.turns.append((cell.cell_id, inputs.container_profile))
            return Turn(fail)

        return make

    def probe(
        self, environment_id: str | None = None, *, fail: bool = False, **kw: Any
    ) -> dict[str, Any]:
        return probe_local_cell(
            self.config, HIGH, environment_id=environment_id, turn_factory=self.factory(fail), **kw
        )

    def rebuild(self, env_id: str, char: str) -> None:
        """The task image is rebuilt: same profile paths, another pinned digest."""
        write_image(self.tmp / f"in-{env_id}", env_id, char)

    def boot(self) -> LocalProductDeployment:
        return LocalProductDeployment(self.config, start_loop=False)


def skips_of(dep: LocalProductDeployment, cell_id: str) -> list[dict[str, Any]]:
    return [s for s in dep.environment_skips if s["cell_id"] == cell_id]


def env_image(dep: LocalProductDeployment, env_id: str, cell_id: str) -> str:
    ref = dep.service.apps["app"].env_compositions[env_id][cell_id]
    value = dep.store.get(dep.scope, "harness-composition", ref)
    image: str = dep.store.get(dep.scope, "environment", value["sandbox_profile_ref"])["image"]
    return image


# -- a probed (cell, environment) pair installs ----------------------------------------------------
def test_a_probed_cell_environment_pair_installs_in_that_task_image(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    assert w.probe()["outcome"] == "accepted"  # the app image, as before
    out = w.probe(ALPHA)
    assert out["outcome"] == "accepted" and out["app"] == "app"
    assert out["environment_id"] == ALPHA and out["image"] == image_of(ALPHA, "e")
    # the turn ran in the task image's container profile, not the app's
    assert w.turns[-1] == (HIGH, w.envs[ALPHA].container_profile)
    dep = w.boot()
    try:
        assert skips_of(dep, HIGH) == []
        env = dep.service.apps["app"].env_compositions[ALPHA]
        assert set(env) == {"codex-cli", HIGH}
        assert env_image(dep, ALPHA, HIGH) == image_of(ALPHA, "e")
        found = latest_probe(dep.store, dep.scope, HIGH, ALPHA)
        assert found is not None
        assert found[0]["id"] == probe_id(HIGH, ALPHA) != probe_id(HIGH)
        assert found[1]["environment_id"] == ALPHA and found[1]["image"] == image_of(ALPHA, "e")
    finally:
        dep.close()


# -- unprobed: skipped with the environment named --------------------------------------------------
def test_an_unprobed_pair_is_skipped_with_the_environment_named(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    assert w.probe()["outcome"] == "accepted"  # only the app image
    dep = w.boot()
    try:
        app = dep.service.apps["app"]
        assert HIGH in app.compositions  # the app image keeps the cell
        assert set(app.env_compositions[ALPHA]) == {"codex-cli"}  # the legacy cell still runs
        (skip,) = skips_of(dep, HIGH)
        assert skip["app"] == "app" and skip["environment_id"] == ALPHA
        assert skip["code"] == "EFFORT_UNPROBED"
        assert f"amplai ops local-cell probe {HIGH} --environment {ALPHA}" in skip["reason"]
    finally:
        dep.close()


def test_a_refused_pair_probe_is_skipped_as_refused(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    w.probe()
    assert w.probe(ALPHA, fail=True)["outcome"] == "refused"
    dep = w.boot()
    try:
        (skip,) = skips_of(dep, HIGH)
        assert (skip["environment_id"], skip["code"]) == (ALPHA, "EFFORT_REFUSED")
        assert HIGH not in dep.service.apps["app"].env_compositions[ALPHA]
    finally:
        dep.close()


# -- another environment's probe does not count ----------------------------------------------------
def test_a_probe_of_another_environment_does_not_count(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e", BETA: "f"})
    w.probe()
    assert w.probe(BETA)["outcome"] == "accepted"
    dep = w.boot()
    try:
        envs = dep.service.apps["app"].env_compositions
        assert HIGH in envs[BETA] and HIGH not in envs[ALPHA]
        assert env_image(dep, BETA, HIGH) == image_of(BETA, "f")
        assert [(s["environment_id"], s["code"]) for s in skips_of(dep, HIGH)] == [
            (ALPHA, "EFFORT_UNPROBED")
        ]
        assert latest_probe(dep.store, dep.scope, HIGH, ALPHA) is None
    finally:
        dep.close()


def test_an_environment_probe_with_the_same_image_as_another_does_not_count(
    tmp_path: Path,
) -> None:
    """Even an identical task image is keyed by environment: the pair is the key, not the image."""
    w = World(tmp_path, {ALPHA: "e", BETA: "e"})
    w.probe()
    w.probe(BETA)
    dep = w.boot()
    try:
        envs = dep.service.apps["app"].env_compositions
        assert HIGH in envs[BETA] and HIGH not in envs[ALPHA]
    finally:
        dep.close()


# -- a changed image digest invalidates the probe --------------------------------------------------
def test_a_rebuilt_task_image_invalidates_the_pair_probe_until_probed_again(
    tmp_path: Path,
) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    w.probe()
    w.probe(ALPHA)
    w.rebuild(ALPHA, "9")  # another digest, same profile and report paths
    dep = w.boot()
    try:
        (skip,) = skips_of(dep, HIGH)
        assert (skip["environment_id"], skip["code"]) == (ALPHA, "EFFORT_UNPROBED")
        assert "another image" in skip["reason"]
        assert set(dep.service.apps["app"].env_compositions[ALPHA]) == {"codex-cli"}
    finally:
        dep.close()
    out = w.probe(ALPHA)  # probe again in the rebuilt image
    assert out["image"] == image_of(ALPHA, "9")
    assert out["probe_ref"]["revision"] == 2  # the latest probe of the pair counts
    dep = w.boot()
    try:
        assert skips_of(dep, HIGH) == []
        assert env_image(dep, ALPHA, HIGH) == image_of(ALPHA, "9")
    finally:
        dep.close()


# -- probes never run at boot; records are idempotent ---------------------------------------------
def test_boot_runs_no_probe_and_a_reboot_writes_no_probe_revision(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    w.probe()
    w.probe(ALPHA)
    turns = len(w.turns)
    rows: list[list[Any]] = []
    for _ in range(2):
        dep = w.boot()
        try:
            rows.append(sorted(
                (r["id"], r["revision"]) for r, _ in dep.store.list_objects(dep.scope, PROBE_KIND)
            ))  # fmt: skip
            assert HIGH in dep.service.apps["app"].env_compositions[ALPHA]
        finally:
            dep.close()
    assert len(w.turns) == turns  # no turn at boot
    assert rows[0] == rows[1] == sorted([(probe_id(HIGH), 1), (probe_id(HIGH, ALPHA), 1)])


def test_storing_an_identical_pair_probe_is_no_new_revision(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    w.probe()
    w.probe(ALPHA)
    dep = w.boot()
    try:
        ref, value = latest_probe(dep.store, dep.scope, HIGH, ALPHA)  # type: ignore[misc]
        assert store_probe(dep.store, dep.scope, value) == ref
    finally:
        dep.close()


# -- the app image behaviour is unchanged ---------------------------------------------------------
def test_the_app_image_probe_is_unchanged(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    out = w.probe()
    assert set(out) == {"cell_id", "app", "outcome", "probe_ref", "next"}  # as before S7b
    assert out["probe_ref"]["id"] == probe_id(HIGH) == "probe-" + digest(HIGH)[7:31]
    assert w.turns[-1][1] != w.envs[ALPHA].container_profile  # the app's own image
    dep = w.boot()
    try:
        ref, value = latest_probe(dep.store, dep.scope, HIGH)  # type: ignore[misc]
        assert ref["id"] == probe_id(HIGH)
        assert "environment_id" not in value and value["image"] == IMAGE
        assert HIGH in dep.service.apps["app"].compositions
    finally:
        dep.close()


def test_a_task_environment_probe_never_admits_the_cell_in_the_app_image(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    assert w.probe(ALPHA)["outcome"] == "accepted"
    with pytest.raises(Hold) as held:  # as before: the app image needs its own probe
        w.boot()
    assert held.value.code == "EFFORT_UNPROBED"


# -- the probe command's refusals ------------------------------------------------------------------
def test_an_unknown_environment_or_one_without_a_driver_report_holds(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    for env_id, app_id in (("tb2-nowhere", None), (ALPHA, "other-app")):
        with pytest.raises(Hold) as held:
            w.probe(env_id, app_id=app_id)
        assert held.value.code == "ENVIRONMENT_UNQUALIFIED"
    doc = json.loads(w.config.read_text())
    doc["apps"][0]["environments"][0]["qualification_reports"] = {}
    w.config.write_text(json.dumps(doc))
    with pytest.raises(Hold) as held:
        w.probe(ALPHA)
    assert held.value.code == "ENVIRONMENT_UNQUALIFIED"
    assert held.value.details == {"environment_id": ALPHA, "driver_id": "codex-cli"}
    assert w.turns == []  # no turn was spent


def test_the_cli_passes_the_environment_and_holds_an_unknown_one(tmp_path: Path) -> None:
    w = World(tmp_path, {ALPHA: "e"})
    result = CliRunner().invoke(
        cli.app,
        ["ops", "local-cell", "probe", HIGH, "--environment", "tb2-nowhere",
         "--config", str(w.config)],
    )  # fmt: skip
    assert result.exit_code == 3, result.output
    assert json.loads(result.output)["code"] == "ENVIRONMENT_UNQUALIFIED"


def test_a_probe_record_refuses_a_malformed_environment_id(tmp_path: Path) -> None:
    from amplai_foundry.runtime.storage.store import Scope

    scratch = tmp_path / "s"
    scratch.mkdir()
    cell = Cell(HIGH, "codex-cli", MODEL, "high", (), legacy=False)
    scope = Scope.parse({"tenant_id": "t", "project_id": "p"})
    with pytest.raises(RuntimeFault) as fault:
        run_probe(
            scope, cell, Turn(), scratch, driver_version="0.155.1",
            image=IMAGE, argv=["codex"], environment_id="bad/id",
        )  # fmt: skip
    assert fault.value.code == "SCHEMA_INVALID"
