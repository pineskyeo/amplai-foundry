"""Work 033 S4: cells in the local deployment, from `local.json` to selection (AC-03 unit part).

Real: `amplai ops local-init` / `local-claude` / `local-cell` output, `LocalConfig`, the
`LocalProductDeployment` boot (store, cell installer, registry, compositions, planners, ports),
`select_composition` and `releases`. Stand-ins: a fake read-only turn for the probe (no docker, no
provider, no credential) and a fixed planner. IC-07: the legacy cell id is the driver id.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution import releases
from amplai_foundry.runtime.execution.cells import (
    CELL_KIND,
    REGISTRY_ID,
    REGISTRY_KIND,
    Cell,
    latest_probe,
)
from amplai_foundry.runtime.execution.codex import AUTH, SeededCodexPort
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.local_deployment import LocalProductDeployment, probe_local_cell
from rc06_rig import FixedPlanner, codex_inputs, make_repo

MODEL, CLAUDE_MODEL = "gpt-5.6-sol", "claude-sonnet-5"
OTHER_MODEL = "gpt-5.7-terra"  # 확인 필요: a stand-in name for "another model", not a real id
HIGH, LOW = "codex-cli.gpt-5.6-sol.high", "codex-cli.gpt-5.6-sol.low"
CLAUDE_MAX = "claude-cli.claude-sonnet-5.max"
OTHER_HIGH = "codex-cli.gpt-5.7-terra.high"


class Turn:
    """A probe turn: completes, or fails like a provider refusing the flag."""

    cell_id = "fake"
    # operator decision 2026-10-08: a probe turn declares how its web tools are off
    offline_tools: ClassVar[dict[str, list[str]] | None] = {"argv": ["--web-off"]}

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def run(self, **_: Any) -> TurnResult:
        if self.fail:
            raise Hold("TURN_FAILED", "provider refused the effort")
        return TurnResult(
            {"ok": True}, {"input_tokens": 2, "output_tokens": 1}, 0.0, "sha256:" + "0" * 64
        )


def factory(fail: bool = False) -> Any:
    return lambda cell, inputs, scratch_root: Turn(fail)


def run(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    parsed: dict[str, Any] = json.loads(result.output)
    return parsed


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        repo = make_repo(tmp_path)
        self.inputs = codex_inputs(tmp_path)
        codex_home = tmp_path / "codex-home"
        (codex_home / ".codex").mkdir(parents=True)
        (codex_home / AUTH).write_text('{"tokens": "x"}')
        home = tmp_path / "amplai"
        run(
            "ops", "local-init", "--repo", str(repo), "--app", "app",
            "--codex-home", str(codex_home),
            "--container-profile", str(self.inputs.container_profile),
            "--qualification-report", str(self.inputs.qualification_report),
            "--egress-profile", str(self.inputs.egress_profile),
            "--egress-qualification", str(self.inputs.egress_qualification),
            "--verifier", "check=python3 -c 'import app' | app imports",
            "--home", str(home), "--operator", "pinesky",
        )  # fmt: skip
        self.config = home / "local.json"
        token = tmp_path / "claude.env"
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("CLAUDE_CODE_OAUTH_TOKEN=test-token-not-a-secret\n")
        run(
            "ops", "local-claude", "--token-file", str(token),
            "--qualification-report", str(self.inputs.qualification_report),
            "--config", str(self.config),
        )  # fmt: skip
        # the second model has its own passing report in the same image (D-097)
        doc = json.loads(self.inputs.qualification_report.read_text())
        other = dict(
            doc["reports"]["codex-cli"], model=OTHER_MODEL, qualification_id="qualification-other"
        )
        self.other_report = tmp_path / "qual-other.json"
        self.other_report.write_text(json.dumps({**doc, "reports": {"codex-cli": other}}))

    def add(self, driver: str, model: str, effort: str, report: Path | None = None) -> None:
        extra = ["--qualification-report", str(report)] if report else []
        run(
            "ops", "local-cell", "add", "--driver", driver, "--model", model, "--effort", effort,
            *extra, "--config", str(self.config),
        )  # fmt: skip

    def probe(self, cell_id: str, *, fail: bool = False) -> dict[str, Any]:
        return probe_local_cell(self.config, cell_id, turn_factory=factory(fail))

    def boot(self) -> LocalProductDeployment:
        return LocalProductDeployment(self.config, start_loop=False)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def probed_world(world: World) -> World:
    world.add("codex-cli", MODEL, "high")
    world.add("codex-cli", MODEL, "low")
    world.add("claude-cli", CLAUDE_MODEL, "max")
    world.add("codex-cli", OTHER_MODEL, "high", world.other_report)
    for cell_id in (HIGH, LOW, CLAUDE_MAX, OTHER_HIGH):
        assert world.probe(cell_id)["outcome"] == "accepted"
    return world


# -- an effort is never installed without an accepted probe
def test_boot_holds_until_the_effort_cell_has_an_accepted_probe(world: World) -> None:
    world.add("codex-cli", MODEL, "high")
    with pytest.raises(Hold) as held:
        world.boot()
    assert held.value.code == "EFFORT_UNPROBED"
    # a refused probe keeps it out, and says so
    assert world.probe(HIGH, fail=True)["outcome"] == "refused"
    with pytest.raises(Hold) as held:
        world.boot()
    assert held.value.code == "EFFORT_REFUSED"
    # the latest probe counts: an accepted one afterwards lets the deployment boot
    assert world.probe(HIGH)["outcome"] == "accepted"
    dep = world.boot()
    try:
        assert HIGH in dep.service.apps["app"].compositions
    finally:
        dep.close()


# -- installed records
def test_cells_install_beside_the_legacy_cells(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        # IC-07: compositions, planners and driver refs are keyed by cell id; legacy = driver id
        assert list(installed.compositions) == [
            "codex-cli",
            "claude-cli",
            HIGH,
            LOW,
            CLAUDE_MAX,
            OTHER_HIGH,
        ]
        assert set(installed.planners) == set(installed.compositions)
        assert set(installed.driver_refs) == set(installed.compositions)
        assert set(installed.cells) == {HIGH, LOW, CLAUDE_MAX, OTHER_HIGH}  # non-legacy only
        assert installed.cells[HIGH] == Cell(HIGH, "codex-cli", MODEL, "high", (), legacy=False)
        # composition ids: legacy unchanged, others <app>-<driver short>-<model_slug>-<effort>
        names = {k: dep.store.get(dep.scope, "harness-composition", r)["composition_id"]
                 for k, r in installed.compositions.items()}  # fmt: skip
        assert names["codex-cli"] == "app-codex" and names["claude-cli"] == "app-claude"
        assert names[HIGH] == "app-codex-gpt-5.6-sol-high"
        assert names[CLAUDE_MAX] == "app-claude-claude-sonnet-5-max"
        assert names[OTHER_HIGH] == "app-codex-gpt-5.7-terra-high"
    finally:
        dep.close()


def test_effort_variants_share_one_qualification_and_another_model_does_not(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        head = dep.store.head(dep.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        assert head["order"] == ["codex-cli", "claude-cli", HIGH, LOW, CLAUDE_MAX, OTHER_HIGH]
        records = {c: dep.store.get(dep.scope, CELL_KIND, r) for c, r in head["cells"].items()}
        qual = {c: json.dumps(v["qualification_ref"], sort_keys=True) for c, v in records.items()}
        assert qual["codex-cli"] == qual[HIGH] == qual[LOW]  # one report for the model (D-097)
        assert qual[OTHER_HIGH] != qual["codex-cli"] and qual[CLAUDE_MAX] == qual["claude-cli"]
        assert records["codex-cli"]["legacy"] is True and records[HIGH]["legacy"] is False
        assert (
            records[HIGH]["effort_probe_ref"] and records["codex-cli"]["effort_probe_ref"] is None
        )
        # the model profile carries the effort; driver records are per model
        profiles = {c: dep.store.get(dep.scope, "model-profile", r["model_profile_ref"])
                    for c, r in records.items()}  # fmt: skip
        assert {c: p["reasoning_profile"] for c, p in profiles.items()} == {
            "codex-cli": "provider-default", "claude-cli": "provider-default", HIGH: "high",
            LOW: "low", CLAUDE_MAX: "max", OTHER_HIGH: "high",
        }  # fmt: skip
        drivers = {c: r["driver_profile_ref"]["id"] for c, r in records.items()}
        assert drivers["codex-cli"] == drivers[HIGH] == drivers[LOW]
        assert drivers[OTHER_HIGH] == drivers["codex-cli"] + "-" + OTHER_MODEL
        # one port per driver record: cells of one model share it (registered once)
        ports = {p for _, p in dep.coordinator.registry.entries.values()}
        assert len(ports) == 3 and all(isinstance(p, SeededCodexPort) or p for p in ports)
    finally:
        dep.close()


def test_a_reboot_repeats_the_install_without_new_revisions(world: World) -> None:
    probed_world(world)
    first = world.boot()
    try:
        before = first.store.head(first.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        compositions = dict(first.service.apps["app"].compositions)
    finally:
        first.close()
    second = world.boot()
    try:
        assert second.store.head(second.scope, REGISTRY_KIND, REGISTRY_ID)["data"] == before
        assert second.service.apps["app"].compositions == compositions
    finally:
        second.close()


def test_the_probe_is_stored_as_a_cell_effort_probe(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        ref, value = latest_probe(dep.store, dep.scope, HIGH)  # type: ignore[misc]
        assert (
            value["cell_id"] == HIGH
            and value["effort"] == "high"
            and value["outcome"] == "accepted"
        )
        assert value["driver_version"] == "0.155.1" and value["image"].endswith("c" * 64)
        assert (
            value["stream_evidence"]["effort_reported"] is None
        )  # applied effort unobservable (Q2)
        assert ref["revision"] == 1
    finally:
        dep.close()


# -- selection, pins and the router
def test_the_legacy_cell_stays_first_and_the_cells_are_candidates(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        chosen = dep.service.select_composition(installed)
        assert chosen["cell_id"] == "codex-cli" and chosen["driver_id"] == "codex-cli"
        assert chosen["rank"] == 1 and chosen["role"] == "executor" and chosen["pinned"] is False
        assert [(c["driver_id"], c["model"]) for c in chosen["candidates"]] == [
            ("codex-cli", MODEL),
            ("claude-cli", CLAUDE_MODEL),
            ("codex-cli", MODEL),
            ("codex-cli", MODEL),
            ("claude-cli", CLAUDE_MODEL),
            ("codex-cli", OTHER_MODEL),
        ]  # the legacy order first, then the configured cells in config order
        assert all(c["eligible"] for c in chosen["candidates"])
        router = releases.router_ref(dep.store, dep.scope, installed)
        assert chosen["router_ref"] == router == chosen["policy_ref"]
    finally:
        dep.close()


def test_a_pin_selects_the_cell_and_its_effort_profile(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        for cell_id in (HIGH, LOW, CLAUDE_MAX, OTHER_HIGH, "claude-cli"):
            ref = installed.compositions[cell_id]
            chosen = dep.service.select_composition(installed, pin=ref)
            assert (
                chosen["cell_id"] == cell_id and chosen["pinned"] is True and chosen["ref"] == ref
            )
            assert (
                releases.pin_allowed(dep.store, dep.scope, installed.compositions, ref) == cell_id
            )
        profile = dep.store.get(
            dep.scope,
            "model-profile",
            dep.store.get(
                dep.scope,
                CELL_KIND,
                dep.store.head(dep.scope, REGISTRY_KIND, REGISTRY_ID)["data"]["cells"][HIGH],
            )["model_profile_ref"],
        )
        assert profile["reasoning_profile"] == "high"
    finally:
        dep.close()


def test_a_cell_sibling_pins_to_its_own_cell_and_a_stranger_is_refused(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        base = installed.compositions[HIGH]
        value = dep.store.get(dep.scope, "harness-composition", base)
        sibling = dep.service._put_composition(
            value["composition_id"] + "__cand1",
            {**value, "composition_id": value["composition_id"] + "__cand1"},
        )
        assert releases.pin_allowed(dep.store, dep.scope, installed.compositions, sibling) == HIGH
        chosen = dep.service.select_composition(installed, pin=sibling)
        assert chosen["cell_id"] == HIGH
        # another cell's profiles under this cell's name are not a sibling of it
        other = dep.store.get(dep.scope, "harness-composition", installed.compositions[LOW])
        forged = dep.service._put_composition(
            value["composition_id"] + "__forged",
            {**other, "composition_id": value["composition_id"] + "__forged"},
        )
        assert releases.pin_allowed(dep.store, dep.scope, installed.compositions, forged) is None
        with pytest.raises(Hold) as held:
            dep.service.select_composition(installed, pin=forged)
        assert held.value.code == "COMPOSITION_PIN"
        stranger = {"id": "no-such-composition", "revision": 1, "digest": "sha256:" + "0" * 64}
        assert releases.pin_allowed(dep.store, dep.scope, installed.compositions, stranger) is None
    finally:
        dep.close()


def test_routers_that_differ_hold_router_inconsistent(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        releases.router_ref(dep.store, dep.scope, installed)  # consistent after a boot
        value = dep.store.get(dep.scope, "harness-composition", installed.compositions[LOW])
        other_router = dict(value["context_policy_ref"])  # any other ref: only the digest differs
        stray = dep.service._put_composition(
            "app-codex-gpt-5.6-sol-low", {**value, "router_policy_ref": other_router}
        )
        installed.compositions[LOW] = stray
        with pytest.raises(Hold) as held:
            releases.router_ref(dep.store, dep.scope, installed)
        assert held.value.code == "ROUTER_INCONSISTENT"
        with pytest.raises(Hold):
            dep.service.select_composition(installed)
    finally:
        dep.close()


def test_a_disabled_cell_is_filtered_out_of_selection(world: World) -> None:
    probed_world(world)
    cfg = json.loads(world.config.read_text())
    for c in cfg["cells"]:
        if c["effort"] == "low":
            c["enabled"] = False
    world.config.write_text(json.dumps(cfg))
    os.chmod(world.config, 0o600)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        chosen = dep.service.select_composition(installed)
        rows = chosen["candidates"]
        assert [r["eligible"] for r in rows] == [True, True, True, False, True, True]
        assert rows[3]["reasons"] == ["model disabled"]  # the `low` cell, fourth in the order
        with pytest.raises(Hold) as held:  # a pin never overrides eligibility
            dep.service.select_composition(installed, pin=installed.compositions[LOW])
        assert held.value.code == "NO_COMPOSITION"
    finally:
        dep.close()


@pytest.mark.parametrize(
    ("driver", "driver_id"), [("claude", "claude-cli"), ("codex", "codex-cli")]
)
def test_local_driver_disable_filters_every_cell_of_that_driver(
    world: World, driver: str, driver_id: str
) -> None:
    # `ops local-driver <d> --disable` is the per-driver kill switch (S4 security review; §12.2
    # leaves the combination open): the legacy cell and every configured cell of <d> leave selection
    probed_world(world)
    run("ops", "local-driver", driver, "--disable", "--config", str(world.config))
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        chosen = dep.service.select_composition(installed)
        rows = chosen["candidates"]
        assert len(rows) == 6
        for row in rows:
            assert row["eligible"] is (row["driver_id"] != driver_id), row
            if row["driver_id"] == driver_id:
                assert row["reasons"] == ["model disabled"]
        assert chosen["driver_id"] != driver_id
        for cell_id in (HIGH, LOW, CLAUDE_MAX, OTHER_HIGH):
            if cell_id.startswith(driver_id):
                with pytest.raises(Hold) as held:  # a pin never overrides the switch
                    dep.service.select_composition(installed, pin=installed.compositions[cell_id])
                assert held.value.code == "NO_COMPOSITION"
    finally:
        dep.close()


# -- planners per cell
def test_each_cell_has_a_planner_with_its_effort(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        planners = dep.service.apps["app"].planners
        assert planners["codex-cli"].effort is None and planners["codex-cli"].cell_id == "codex-cli"
        assert planners[HIGH].effort == "high" and planners[HIGH].cell_id == HIGH
        assert planners[LOW].effort == "low" and planners[CLAUDE_MAX].effort == "max"
        assert planners[OTHER_HIGH].model == OTHER_MODEL
        assert "model_reasoning_effort=high" in planners[HIGH].argv("p")
        assert "model_reasoning_effort" not in " ".join(planners["codex-cli"].argv("p"))
        claude_argv = planners[CLAUDE_MAX].claude_argv("p", {"type": "object"})
        assert claude_argv[claude_argv.index("--effort") + 1] == "max"
        assert "--effort" not in planners["claude-cli"].claude_argv("p", {"type": "object"})
    finally:
        dep.close()


def test_a_goal_pinned_to_an_effort_cell_is_planned_by_that_cell(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        seen: list[str] = []

        class Recording(FixedPlanner):
            def __init__(self, name: str) -> None:
                super().__init__()
                self.name = name

            def draft(self, *a: Any, **k: Any) -> dict[str, Any]:
                seen.append(self.name)
                return super().draft(*a, **k)

        for key in (HIGH, "codex-cli"):
            installed.planners[key] = Recording(key)
        actors = dep.service.actors
        goal = dep.goals.submit(actors.service, text="make value return 2", key="k-cell")["goal_id"]
        plan = dep.service.plan(goal, composition=installed.compositions[HIGH])
        assert seen == [HIGH]  # the planner of the selected cell, not the legacy one
        assert plan["composition"]["cell_id"] == HIGH and plan["composition"]["pinned"] is True
        assert plan["planned_with"]["driver_id"] == "codex-cli"
        approved = dep.service.approve(dep.operator(), goal)
        assert approved["status"] == "approved"
        # the activated profile of the goal is the effort cell's model profile
        profile = dep.store.head(dep.scope, "goal", goal)["data"]["profile"]
        model = dep.store.get(dep.scope, "model-profile", profile["model_profile_ref"])
        assert model["reasoning_profile"] == "high" and model["provider_model_id"] == MODEL
    finally:
        dep.close()


def test_a_cell_without_a_planner_holds_planner_unavailable(world: World) -> None:
    probed_world(world)
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        del installed.planners[LOW]
        goal = dep.goals.submit(dep.service.actors.service, text="a goal", key="k-none")["goal_id"]
        with pytest.raises(Hold) as held:
            dep.service.plan(goal, composition=installed.compositions[LOW])
        assert held.value.code == "PLANNER_UNAVAILABLE"
    finally:
        dep.close()


# -- install input checks
def test_install_refuses_a_driver_ref_without_its_cell(world: World) -> None:
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        with pytest.raises(Hold) as held:
            dep.service.install(
                installed.config,
                driver_refs={
                    **installed.driver_refs,
                    "codex-cli.x.high": installed.driver_refs["codex-cli"],
                },
            )
        assert held.value.code == "CELL_UNKNOWN"
    finally:
        dep.close()


def test_the_legacy_install_is_unchanged_without_cells(world: World) -> None:
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        assert list(installed.compositions) == ["codex-cli", "claude-cli"] and installed.cells == {}
        chosen = dep.service.select_composition(installed)
        assert chosen["cell_id"] == "codex-cli" and len(chosen["candidates"]) == 2
        head = dep.store.head(dep.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        assert head["order"] == ["codex-cli", "claude-cli"]  # legacy cells = driver ids
    finally:
        dep.close()


def test_a_cell_without_a_qualification_report_for_the_app_is_skipped(world: World) -> None:
    # `--qualification-report` with an unknown --app records none for the real app: the cell has
    # no report in this image, so the app does not install it (not qualified there)
    world.add("codex-cli", MODEL, "high")
    cfg = json.loads(world.config.read_text())
    cfg["cells"][0]["qualification_reports"] = {}
    world.config.write_text(json.dumps(cfg))
    os.chmod(world.config, 0o600)
    dep = world.boot()
    try:
        assert HIGH not in dep.service.apps["app"].compositions
    finally:
        dep.close()


# -- a cell in apps with different images (S4 review: one probe per cell, §2.4 one image)
OTHER_IMAGE = "localhost:5000/amplai-worker-app-two@sha256:" + "d" * 64


def second_app(world: World) -> None:
    """A second app in another image with its own passing codex report (no Claude report)."""
    repo = make_repo(world.tmp / "two")
    container = json.loads(world.inputs.container_profile.read_text())
    profile = world.tmp / "container-two.json"
    profile.write_text(json.dumps({**container, "image": OTHER_IMAGE}))
    doc = json.loads(world.inputs.qualification_report.read_text())
    report = world.tmp / "qual-two.json"
    report.write_text(json.dumps({**doc, "container_image": OTHER_IMAGE}))
    cfg = json.loads(world.config.read_text())
    first = cfg["apps"][0]
    cfg["apps"].append({
        **{k: v for k, v in first.items() if k != "claude_qualification_report"},
        "app_id": "two", "repo": str(repo), "container_profile": str(profile),
        "qualification_report": str(report), "aliases": [],
    })  # fmt: skip
    world.config.write_text(json.dumps(cfg))
    os.chmod(world.config, 0o600)


def test_an_effort_cell_installs_only_in_the_image_its_probe_ran_on(world: World) -> None:
    second_app(world)
    world.add("codex-cli", MODEL, "high")  # no reports: each app's own (§12.2)
    assert world.probe(HIGH)["app"] == "app"  # the first app with a report, image c…
    dep = world.boot()  # the other image skips the cell instead of holding the boot
    try:
        assert HIGH in dep.service.apps["app"].compositions
        assert HIGH not in dep.service.apps["two"].compositions
        assert [(s["cell_id"], s["app"], s["code"]) for s in dep.cell_skips] == [
            (HIGH, "two", "EFFORT_UNPROBED")
        ]
        head = dep.store.head(dep.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        assert dep.store.get(dep.scope, CELL_KIND, head["cells"][HIGH])["image"].endswith("c" * 64)
    finally:
        dep.close()
    # a probe in the other app moves the cell to that image (the latest probe counts)
    probed = probe_local_cell(world.config, HIGH, app_id="two", turn_factory=factory())
    assert probed["app"] == "two" and probed["outcome"] == "accepted"
    dep = world.boot()
    try:
        assert HIGH in dep.service.apps["two"].compositions
        assert HIGH not in dep.service.apps["app"].compositions
        head = dep.store.head(dep.scope, REGISTRY_KIND, REGISTRY_ID)["data"]
        assert dep.store.get(dep.scope, CELL_KIND, head["cells"][HIGH])["image"] == OTHER_IMAGE
    finally:
        dep.close()


def test_a_probe_from_an_image_no_app_runs_still_holds_the_boot(world: World) -> None:
    world.add("codex-cli", MODEL, "high")
    assert world.probe(HIGH)["outcome"] == "accepted"
    # the app moves to another image: its report is for that image, the probe is not
    container = json.loads(world.inputs.container_profile.read_text())
    world.inputs.container_profile.write_text(json.dumps({**container, "image": OTHER_IMAGE}))
    doc = json.loads(world.inputs.qualification_report.read_text())
    world.inputs.qualification_report.write_text(
        json.dumps({**doc, "container_image": OTHER_IMAGE})
    )
    with pytest.raises(Hold) as held:
        world.boot()
    assert held.value.code == "EFFORT_UNPROBED"
    assert [d["cell_id"] for d in held.value.details] == [HIGH]  # type: ignore[union-attr]


# -- a promoted release and a cell added afterwards (S4 review)
def test_a_cell_added_after_a_promotion_holds_router_inconsistent_and_names_the_release(
    world: World,
) -> None:
    from amplai_foundry.runtime.execution import prompts

    dep = world.boot()
    try:  # promote a class-A candidate of the legacy codex composition (pointer CAS as in rc08)
        installed = dep.service.apps["app"]
        base = dep.store.get(dep.scope, "harness-composition", installed.compositions["codex-cli"])
        bundle = dep.service._put(
            prompts.KIND, "implementer-cand",
            prompts.bundle("implementer-cand", ["Implement it in {app_id}."], "test"),
        )  # fmt: skip
        cand = dep.service._put_composition(
            base["composition_id"] + "__cand",
            {**base, "composition_id": base["composition_id"] + "__cand",
             "prompt_bundle_ref": bundle},
        )  # fmt: skip
        release = releases.build(
            dep.store, dep.scope, dep.contracts, "local-candidate-test", [cand],
            signer=dep._release_signer, key_id="local-authority", basis="test",
        )  # fmt: skip
        with dep.store.tx() as db:
            head = dep.store.head(dep.scope, "release-pointer", "active", db=db)
            baseline = head["data"]["release_ref"]
            dep.store.cas(db, dep.scope, "release-pointer", "active", head["row_version"],
                          "active", {"release_ref": release})  # fmt: skip
        assert dep.service.select_composition(installed)["ref"] == cand
    finally:
        dep.close()
    # a cell added afterwards extends the app's route order: a new router for the installed
    # compositions, while the promoted candidate keeps the one it was copied with
    world.add("codex-cli", MODEL, "high")
    assert world.probe(HIGH)["outcome"] == "accepted"
    dep = world.boot()
    try:
        installed = dep.service.apps["app"]
        pointer = dep.store.head(dep.scope, "release-pointer", "active")["data"]
        assert pointer["release_ref"] == release  # a promoted release stays active
        with pytest.raises(Hold) as held:
            dep.service.select_composition(installed)
        assert held.value.code == "ROUTER_INCONSISTENT"
        details: Any = held.value.details
        assert details["active_release"] == "local-candidate-test" and len(details["routers"]) == 2
        assert "amplai meta rollback" in details["remedy"]
        # the remedy: back on the prior release, the installed compositions agree again
        with dep.store.tx() as db:
            head = dep.store.head(dep.scope, "release-pointer", "active", db=db)
            dep.store.cas(db, dep.scope, "release-pointer", "active", head["row_version"],
                          "active", {"release_ref": baseline})  # fmt: skip
        assert dep.service.select_composition(installed)["cell_id"] == "codex-cli"
    finally:
        dep.close()
