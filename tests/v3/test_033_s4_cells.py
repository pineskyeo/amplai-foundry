"""Work 033 S4 (AC-03 unit part): cells, effort probes, shared qualification, local-cell commands.

interfaces.md 2.4 (ids, records), 3.4 (`cells.py`), 12.1 (`amplai ops local-cell`), 12.2
(`local.json` keys), IC-07 (legacy cell id = driver id). Real: the store, `CellInstaller`, the
CLI and `LocalConfig`. Stand-ins: a fake read-only turn for probes (no docker, no provider).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.execution.cells import (
    CELL_KIND,
    EFFORT_SYNTAX,
    LEGACY_EFFORT,
    PROBE_KIND,
    Cell,
    CellInstaller,
    DispatchOptions,
    latest_probe,
    probe_id,
    run_probe,
    store_probe,
    validate_cell_record,
    validate_effort,
)
from amplai_foundry.runtime.execution.codex import AUTH, CodexProfileInputs
from amplai_foundry.runtime.execution.policies import v1_budget
from amplai_foundry.runtime.execution.product import app_capabilities
from amplai_foundry.runtime.execution.readonly_turn import TurnResult
from amplai_foundry.runtime.local_deployment import LocalConfig, probe_local_cell
from rc06_rig import IMAGE, codex_inputs, make_repo

CODEX, CLAUDE = "codex-cli", "claude-cli"
CODEX_MODEL = "gpt-5.6-sol"  # the model of the rig's qualification report
OTHER_MODEL = "gpt-5.7-terra"  # 확인 필요: a stand-in name for "another model", not a real id
IMAGE12 = IMAGE.rsplit("@sha256:", 1)[-1][:12]


def hold_code(fn: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(RuntimeFault) as held:
        fn(*args, **kwargs)
    return str(held.value.code)


def cell(driver: str, model: str, effort: str, *, legacy: bool = False) -> Cell:
    cell_id = Cell.make_id(driver, model, effort, legacy=legacy)
    return Cell(cell_id, driver, model, effort, (), legacy=legacy)  # type: ignore[arg-type]


# -- ids (IC-07) ---------------------------------------------------------------------------------
def test_the_legacy_cell_id_is_the_driver_id() -> None:
    for driver in (CODEX, CLAUDE, "opencode-server"):
        assert Cell.make_id(driver, "whatever-model", LEGACY_EFFORT, legacy=True) == driver
    legacy = cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True)
    assert legacy.cell_id == CODEX and legacy.legacy and legacy.provider == "codex"


def test_other_cell_ids_are_driver_model_slug_effort() -> None:
    assert Cell.make_id(CODEX, CODEX_MODEL, "high", legacy=False) == "codex-cli.gpt-5.6-sol.high"
    assert Cell.make_id(CLAUDE, "claude-opus-5-5", "xhigh", legacy=False) == (
        "claude-cli.claude-opus-5-5.xhigh"
    )
    # model_slug: "/" becomes "." (a record id has no "/")
    made = Cell.make_id("opencode-server", "opencode-go/glm-5.3-flash", LEGACY_EFFORT, legacy=False)
    assert made == "opencode-server.opencode-go.glm-5.3-flash.provider-default"


def test_a_cell_whose_id_does_not_follow_ic07_is_refused() -> None:
    assert hold_code(Cell, "codex-cli.x.high", CODEX, CODEX_MODEL, "high", (), False) == (
        "CELL_UNKNOWN"
    )
    # a legacy flag needs the driver id as the cell id, and the provider-default effort
    assert hold_code(Cell, "codex-cli.gpt-5.6-sol.high", CODEX, CODEX_MODEL, "high", (), True) == (
        "EFFORT_UNSUPPORTED"
    )
    code = hold_code(
        Cell, "codex-cli.gpt-5.6-sol.high", CODEX, CODEX_MODEL, LEGACY_EFFORT, (), True
    )
    assert code == "CELL_UNKNOWN"


def test_cell_construction_refuses_unknown_drivers_and_unpinned_models() -> None:
    assert hold_code(Cell, "x", "gemini-cli", "m", "high", (), False) == "DRIVER_KIND"
    for model in ("latest", "default", "auto"):
        assert hold_code(cell, CODEX, model, "high") == "MODEL_UNPINNED"
    assert hold_code(cell, CODEX, CODEX_MODEL, "High") == "EFFORT_UNSUPPORTED"


def test_the_report_of_a_cell_is_looked_up_by_app() -> None:
    value = Cell(
        "codex-cli.gpt-5.6-sol.high", CODEX, CODEX_MODEL, "high",
        (("app", "/q/app.json"), ("other", "/q/other.json")), legacy=False,
    )  # fmt: skip
    assert value.report_for("app") == "/q/app.json" and value.report_for("nope") is None


# -- validate_effort: refused, never substituted ---------------------------------------------------
def accepted(c: Cell, outcome: str = "accepted") -> dict[str, Any]:
    return {"cell_id": c.cell_id, "driver_id": c.driver_id, "model": c.model,
            "effort": c.effort, "outcome": outcome}  # fmt: skip


def test_provider_default_needs_no_probe() -> None:
    validate_effort(cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True), None)
    validate_effort(cell("opencode-server", "opencode-go/m", LEGACY_EFFORT), None)


def test_a_documented_effort_needs_an_accepted_probe_of_that_exact_cell() -> None:
    high = cell(CODEX, CODEX_MODEL, "high")
    assert hold_code(validate_effort, high, None) == "EFFORT_UNPROBED"
    validate_effort(high, accepted(high))
    # a probe of another effort, model or driver does not count
    for other in (
        cell(CODEX, CODEX_MODEL, "low"),
        cell(CODEX, OTHER_MODEL, "high"),
        cell(CLAUDE, CODEX_MODEL, "high"),
    ):
        assert hold_code(validate_effort, high, accepted(other)) == "EFFORT_UNPROBED"


@pytest.mark.parametrize("outcome", ["refused", "error"])
def test_a_probe_that_was_not_accepted_refuses_the_cell(outcome: str) -> None:
    high = cell(CODEX, CODEX_MODEL, "high")
    assert hold_code(validate_effort, high, accepted(high, outcome)) == "EFFORT_REFUSED"


@pytest.mark.parametrize("driver", [CODEX, CLAUDE])
def test_an_undocumented_effort_is_refused_even_with_an_accepted_probe(driver: str) -> None:
    model = CODEX_MODEL if driver == CODEX else "claude-sonnet-5"
    for effort in ("turbo", "ultracode", "minimal", "none"):
        c = cell(driver, model, effort)
        assert effort not in EFFORT_SYNTAX[driver]
        assert hold_code(validate_effort, c, None) == "EFFORT_UNSUPPORTED"
        assert hold_code(validate_effort, c, accepted(c)) == "EFFORT_UNSUPPORTED"


def test_the_documented_values_come_from_the_measured_facts() -> None:
    # specs/033-harness-taxonomy/runs/cli-effort-facts.md
    assert EFFORT_SYNTAX["claude-cli"] == ("low", "medium", "high", "xhigh", "max")
    assert EFFORT_SYNTAX["codex-cli"] == ("low", "medium", "high", "xhigh", "max", "ultra")
    assert EFFORT_SYNTAX["opencode-server"] == ()


# -- run_probe ------------------------------------------------------------------------------------
class FakeTurn:
    cell_id = "fake"

    def __init__(self, result: Hold | dict[str, Any] | None = None) -> None:
        self.result, self.calls = result, []  # type: ignore[var-annotated]

    def run(
        self, *, prompt: str, schema: dict[str, Any], workspace: Path, mounts: Any = None
    ) -> Any:
        self.calls.append({"prompt": prompt, "schema": schema, "workspace": workspace})
        if isinstance(self.result, Hold):
            raise self.result
        return TurnResult(
            self.result or {"ok": True},
            {"input_tokens": 3, "output_tokens": 1},
            0.1,
            "sha256:" + "0" * 64,
        )


def probe_of(d: Any, c: Cell, turn: FakeTurn, scratch: Path) -> dict[str, Any]:
    scratch.mkdir(parents=True, exist_ok=True)
    return run_probe(
        d.scope, c, turn, scratch, driver_version="0.155.1", image=IMAGE, argv=["codex", c.effort]
    )


def test_a_probe_turn_runs_on_an_empty_scratch_directory(deployment: Any, tmp_path: Path) -> None:
    c = cell(CODEX, CODEX_MODEL, "high")
    turn = FakeTurn()
    value = probe_of(deployment, c, turn, tmp_path / "s")
    assert value["outcome"] == "accepted" and value["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
    }
    assert [value[k] for k in ("cell_id", "driver_id", "model", "effort")] == [
        c.cell_id, CODEX, CODEX_MODEL, "high",
    ]  # fmt: skip
    # the effort was not observed to be applied (14 Q2): nothing is claimed
    assert value["stream_evidence"] == {"effort_reported": None, "error_text_digest": None}
    assert turn.calls[0]["workspace"] == tmp_path / "s"


def test_a_probe_outcome_follows_the_turn_failure_code(deployment: Any, tmp_path: Path) -> None:
    c = cell(CODEX, CODEX_MODEL, "high")
    for n, (code, outcome) in enumerate(
        (("TURN_FAILED", "refused"), ("TURN_TIMEOUT", "error"), ("TURN_OUTPUT", "error"))
    ):
        turn = FakeTurn(Hold(code, "x", details="stderr text"))
        value = probe_of(deployment, c, turn, tmp_path / f"s{n}")
        assert value["outcome"] == outcome
        assert value["stream_evidence"]["error_text_digest"].startswith("sha256:")
        assert "stderr text" not in json.dumps(value)  # error text is stored as a digest only


def test_a_probe_refuses_a_non_empty_or_linked_scratch_directory(
    deployment: Any, tmp_path: Path
) -> None:
    c = cell(CODEX, CODEX_MODEL, "high")
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("secret")
    turn = FakeTurn()
    assert hold_code(probe_of, deployment, c, turn, full) == "TURN_FAILED"
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    assert hold_code(probe_of, deployment, c, turn, tmp_path / "link") == "TURN_FAILED"
    assert turn.calls == []  # no provider turn was spent


def test_nothing_undocumented_is_ever_sent_to_a_provider(deployment: Any, tmp_path: Path) -> None:
    turn = FakeTurn()
    for bad in (
        cell(CODEX, CODEX_MODEL, "turbo"),
        cell(CODEX, CODEX_MODEL, LEGACY_EFFORT),
        cell("opencode-server", "opencode-go/m", "high"),
    ):
        assert hold_code(probe_of, deployment, bad, turn, tmp_path / bad.cell_id) == (
            "EFFORT_UNSUPPORTED"
        )
    assert turn.calls == []


def test_the_latest_probe_of_a_cell_counts(deployment: Any, tmp_path: Path) -> None:
    c = cell(CODEX, CODEX_MODEL, "high")
    assert latest_probe(deployment.store, deployment.scope, c.cell_id) is None
    first = probe_of(deployment, c, FakeTurn(Hold("TURN_FAILED", "x")), tmp_path / "a")
    ref1 = store_probe(deployment.store, deployment.scope, first)
    assert ref1["id"] == probe_id(c.cell_id) and ref1["revision"] == 1
    assert store_probe(deployment.store, deployment.scope, first) == ref1  # identical: no revision
    second = probe_of(deployment, c, FakeTurn(), tmp_path / "b")
    ref2 = store_probe(deployment.store, deployment.scope, second)
    assert ref2["revision"] == 2
    ref, value = latest_probe(deployment.store, deployment.scope, c.cell_id)  # type: ignore[misc]
    assert ref == ref2 and value["outcome"] == "accepted"
    assert PROBE_KIND == "cell-effort-probe"


# -- CellInstaller --------------------------------------------------------------------------------
def other_model_inputs(tmp_path: Path) -> CodexProfileInputs:
    """A second model with its own passing report in the same image (D-097: qualification is per
    model). The model name is a stand-in."""
    inputs = codex_inputs(tmp_path)
    doc = json.loads(inputs.qualification_report.read_text())
    report = dict(doc["reports"][CODEX], model=OTHER_MODEL, qualification_id="qualification-other")
    (tmp_path / "qual-other.json").write_text(json.dumps({**doc, "reports": {CODEX: report}}))
    return replace(inputs, qualification_report=tmp_path / "qual-other.json", model=OTHER_MODEL)


def installed_cell(
    d: Any, tmp_path: Path, c: Cell, inputs: CodexProfileInputs | None = None
) -> dict[str, Any]:
    inputs = inputs or replace(codex_inputs(tmp_path), model=c.model)
    probe = None
    if c.effort != LEGACY_EFFORT:
        # a boot reuses the stored probe (local_deployment.install_cell); a new probe is a new
        # revision of the cell record
        found = latest_probe(d.store, d.scope, c.cell_id)
        probe = found[1] if found else probe_of(d, c, FakeTurn(), tmp_path / ("s-" + c.cell_id))
        if not found:
            store_probe(d.store, d.scope, probe)
    return CellInstaller(d.store, d.scope).install(c, inputs, app_capabilities("app"), probe=probe)


def test_effort_variants_of_one_model_share_one_qualification(
    deployment: Any, tmp_path: Path
) -> None:
    d = deployment
    legacy = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True))
    high = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, "high"))
    low = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, "low"))
    assert set(legacy) == {"environment", "qualification", "driver", "model", "cell"}
    # one measured qualification report, one environment and one driver record for the model
    assert legacy["qualification"] == high["qualification"] == low["qualification"]
    assert legacy["environment"] == high["environment"] == low["environment"]
    assert legacy["driver"] == high["driver"] == low["driver"]
    # the harness-cell records point at that one qualification (D-097)
    values = {
        n: d.store.get(d.scope, CELL_KIND, r["cell"])
        for n, r in (("legacy", legacy), ("high", high), ("low", low))
    }
    assert len({json.dumps(v["qualification_ref"], sort_keys=True) for v in values.values()}) == 1
    # only the model profile differs: reasoning_profile is the effort (2.4)
    models = {
        n: d.store.get(d.scope, "model-profile", r["model"])
        for n, r in (("legacy", legacy), ("high", high), ("low", low))
    }
    assert [models[n]["reasoning_profile"] for n in ("legacy", "high", "low")] == [
        LEGACY_EFFORT, "high", "low",
    ]  # fmt: skip
    assert len({m["profile_id"] for m in models.values()}) == 3
    assert {m["provider_model_id"] for m in models.values()} == {CODEX_MODEL}


def test_record_ids_follow_the_spec(deployment: Any, tmp_path: Path) -> None:
    d = deployment
    legacy = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True))
    high = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, "high"))
    assert legacy["model"]["id"] == f"codex-{CODEX_MODEL}-{IMAGE12}"  # unchanged
    assert high["model"]["id"] == f"codex-{CODEX_MODEL}-high-{IMAGE12}"
    assert legacy["driver"]["id"] == f"codex-cli-{IMAGE12}"  # unchanged, and the same model
    assert (
        legacy["cell"]["id"] == "codex-cli" and high["cell"]["id"] == "codex-cli.gpt-5.6-sol.high"
    )
    value = d.store.get(d.scope, CELL_KIND, high["cell"])
    validate_cell_record(value)
    assert value["legacy"] is False and value["effort"] == "high" and value["effort_probe_ref"]
    assert value["driver_id"] == CODEX and value["provider_model_id"] == CODEX_MODEL
    assert value["image"] == IMAGE and value["enabled"] is True
    legacy_value = d.store.get(d.scope, CELL_KIND, legacy["cell"])
    assert legacy_value["legacy"] is True and legacy_value["effort_probe_ref"] is None


def test_another_model_gets_its_own_driver_record_and_qualification(
    deployment: Any, tmp_path: Path
) -> None:
    d = deployment
    legacy = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True))
    other = installed_cell(
        d, tmp_path, cell(CODEX, OTHER_MODEL, "high"), other_model_inputs(tmp_path)
    )
    assert other["driver"]["id"] == f"codex-cli-{IMAGE12}-{OTHER_MODEL}"
    assert other["driver"]["id"] != legacy["driver"]["id"]  # no alternating revisions of one id
    assert other["qualification"] != legacy["qualification"]
    # the legacy driver record kept revision 1 (the second model never rewrote it)
    assert legacy["driver"]["revision"] == 1
    drivers = [r for r, _ in d.store.list_objects(d.scope, "driver-capabilities")]
    assert {r["id"]: r["revision"] for r in drivers}[legacy["driver"]["id"]] == 1


def test_the_registry_lists_cells_in_install_order_and_installs_are_idempotent(
    deployment: Any, tmp_path: Path
) -> None:
    d = deployment
    c0 = cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True)
    c1, c2 = cell(CODEX, CODEX_MODEL, "high"), cell(CODEX, CODEX_MODEL, "low")
    assert CellInstaller(d.store, d.scope).registry() == {"cells": {}, "order": []}
    refs = [installed_cell(d, tmp_path, c) for c in (c0, c1, c2)]
    registry = CellInstaller(d.store, d.scope).registry()
    assert registry["order"] == [c0.cell_id, c1.cell_id, c2.cell_id]
    assert registry["cells"] == {
        c.cell_id: r["cell"] for c, r in zip((c0, c1, c2), refs, strict=True)
    }
    again = [installed_cell(d, tmp_path, c) for c in (c0, c1, c2)]
    assert [r["cell"] for r in again] == [r["cell"] for r in refs]  # same revision: a boot repeats
    assert [r["model"] for r in again] == [r["model"] for r in refs]
    assert CellInstaller(d.store, d.scope).registry()["order"] == registry["order"]


def test_an_effort_cell_installs_only_after_an_accepted_probe(
    deployment: Any, tmp_path: Path
) -> None:
    d = deployment
    c = cell(CODEX, CODEX_MODEL, "high")
    inputs, caps = codex_inputs(tmp_path), app_capabilities("app")
    installer = CellInstaller(d.store, d.scope)
    assert hold_code(installer.install, c, inputs, caps, probe=None) == "EFFORT_UNPROBED"
    refused = probe_of(d, c, FakeTurn(Hold("TURN_FAILED", "x")), tmp_path / "r")
    assert hold_code(installer.install, c, inputs, caps, probe=refused) == "EFFORT_REFUSED"
    stale = {**probe_of(d, c, FakeTurn(), tmp_path / "s1"), "image": "other@sha256:" + "d" * 64}
    assert hold_code(installer.install, c, inputs, caps, probe=stale) == "EFFORT_UNPROBED"
    old_version = {**probe_of(d, c, FakeTurn(), tmp_path / "s2"), "driver_version": "0.1.0"}
    assert hold_code(installer.install, c, inputs, caps, probe=old_version) == "EFFORT_UNPROBED"
    bad = cell(CODEX, CODEX_MODEL, "turbo")
    assert hold_code(installer.install, bad, inputs, caps, probe=None) == "EFFORT_UNSUPPORTED"
    assert installer.registry() == {"cells": {}, "order": []}  # nothing was half installed
    assert d.store.list_objects(d.scope, "model-profile") == []


def test_driver_qualification_still_holds_for_a_model_without_a_passing_report(
    deployment: Any, tmp_path: Path
) -> None:
    c = cell(CODEX, OTHER_MODEL, "high")
    inputs = replace(
        codex_inputs(tmp_path), model=OTHER_MODEL
    )  # the rig's report names another model
    probe = probe_of(deployment, c, FakeTurn(), tmp_path / "s")
    installer = CellInstaller(deployment.store, deployment.scope)
    assert hold_code(installer.install, c, inputs, app_capabilities("app"), probe=probe) == (
        "DRIVER_UNQUALIFIED"
    )


def test_profile_inputs_of_another_driver_are_refused(deployment: Any, tmp_path: Path) -> None:
    c = cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True)
    inputs = replace(codex_inputs(tmp_path), provider="claude", model="claude-sonnet-5")
    installer = CellInstaller(deployment.store, deployment.scope)
    assert hold_code(installer.install, c, inputs, app_capabilities("app"), probe=None) == (
        "CELL_UNKNOWN"
    )


def test_a_cell_record_with_an_effort_needs_its_probe_ref(deployment: Any, tmp_path: Path) -> None:
    d = deployment
    refs = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, "high"))
    value = d.store.get(d.scope, CELL_KIND, refs["cell"])
    validate_cell_record(value)
    assert hold_code(validate_cell_record, {**value, "effort_probe_ref": None}) == "SCHEMA_INVALID"
    assert hold_code(validate_cell_record, {**value, "unknown": 1}) == "SCHEMA_INVALID"
    assert hold_code(validate_cell_record, {**value, "driver_id": "gemini-cli"}) == "SCHEMA_INVALID"


def test_resolve_options_reads_model_and_effort_from_the_activated_profile(
    deployment: Any, tmp_path: Path
) -> None:
    from amplai_foundry.runtime.execution.cells import resolve_options

    d = deployment
    legacy = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True))
    high = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, "high"))
    policy = v1_budget({"max_attempts": 3, "max_wall_seconds": 1800, "max_tokens": 100000})

    def profile(refs: dict[str, Any]) -> dict[str, Any]:
        return {"model_profile_ref": refs["model"], "driver_profile_ref": refs["driver"]}

    got = resolve_options(d.store, d.scope, profile(high), policy, capture_trace=False)
    assert got == DispatchOptions(CODEX_MODEL, "high")
    assert resolve_options(
        d.store, d.scope, profile(legacy), policy, capture_trace=False
    ).is_default()
    traced = resolve_options(d.store, d.scope, profile(high), policy, capture_trace=True)
    assert traced.capture_trace and traced.effort == "high"


def test_resolve_options_refuses_unmeasured_driver_options(deployment: Any, tmp_path: Path) -> None:
    from amplai_foundry.runtime.execution.cells import resolve_options

    d = deployment
    legacy = installed_cell(d, tmp_path, cell(CODEX, CODEX_MODEL, LEGACY_EFFORT, legacy=True))
    profile = {"model_profile_ref": legacy["model"], "driver_profile_ref": legacy["driver"]}
    base = v1_budget({"max_attempts": 3, "max_wall_seconds": 1800, "max_tokens": 100000})

    def policy(options: dict[str, Any]) -> Any:
        return replace(base, driver_options=options)

    # Claude options on a Codex profile: another driver's options
    claude = {"claude": {"max_turns": 5}, "codex": {"config": []}}
    assert hold_code(
        resolve_options, d.store, d.scope, profile, policy(claude), capture_trace=False
    ) == "DRIVER_OPTIONS_UNSUPPORTED"  # fmt: skip
    # a Codex config key on a Codex profile: the allowlist is empty until probed (14 Q1)
    codex = {"claude": {"max_turns": None}, "codex": {"config": [["some_key", "1"]]}}
    assert hold_code(
        resolve_options, d.store, d.scope, profile, policy(codex), capture_trace=False
    ) == "DRIVER_OPTIONS_UNSUPPORTED"  # fmt: skip


# -- local.json keys (12.2) -----------------------------------------------------------------------
def init(tmp_path: Path) -> tuple[Path, CodexProfileInputs]:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
    run(
        "ops", "local-init", "--repo", str(repo), "--app", "app", "--codex-home", str(codex_home),
        "--container-profile", str(inputs.container_profile),
        "--qualification-report", str(inputs.qualification_report),
        "--egress-profile", str(inputs.egress_profile),
        "--egress-qualification", str(inputs.egress_qualification),
        "--verifier", "check=python3 -c 'import app' | app imports",
        "--home", str(home), "--operator", "pinesky",
    )  # fmt: skip
    return home / "local.json", inputs


def run(*args: str) -> dict[str, Any]:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    parsed: dict[str, Any] = json.loads(result.output)
    return parsed


def fails(*args: str) -> Any:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code != 0, result.output
    return result


def test_a_config_without_the_new_keys_behaves_as_today(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    value = json.loads(config.read_text())
    assert not {"cells", "roles", "meta", "jev"} & set(value)
    cfg = LocalConfig.model_validate(value)
    assert cfg.cells == [] and cfg.roles is None and cfg.meta is None and cfg.jev is None
    assert cfg.legacy_cells() == {CODEX: CODEX_MODEL}
    assert value["schema_version"] == "local-1"


def test_the_new_keys_are_optional_and_validated(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    base = json.loads(config.read_text())
    full = {
        **base,
        "cells": [{"driver": CODEX, "model": CODEX_MODEL, "effort": "high"}],
        "roles": {"planner": [CODEX], "reviewer": ["codex-cli.gpt-5.6-sol.high"], "proposer": None},
        "meta": {"max_parallel_trials": 4, "nightly": {"cells": [CODEX], "stop_at": "06:30"}},
        "jev": {"enabled": False},
    }
    cfg = LocalConfig.model_validate(full)
    assert cfg.cells[0].cell().cell_id == "codex-cli.gpt-5.6-sol.high"
    assert cfg.meta is not None and cfg.meta.evaluator_version == "eval-2"
    assert cfg.meta.nightly is not None and cfg.meta.nightly.shares["search"] == 0.6
    assert cfg.jev is not None and cfg.jev.data_classes_allowed == ["public"]

    def invalid(**override: Any) -> None:
        with pytest.raises((ValidationError, RuntimeFault)):
            LocalConfig.model_validate({**base, **override})

    invalid(cells=[{"driver": CODEX, "model": CODEX_MODEL, "effort": "high", "extra": 1}])
    invalid(cells=[{"driver": "gemini-cli", "model": "m", "effort": "high"}])
    invalid(meta={"max_parallel_trials": 5})  # 1..4
    invalid(meta={"max_parallel_trials": 0})
    invalid(meta={"unknown_key": 1})
    invalid(meta={"nightly": {"stop_at": "25:00"}})
    invalid(
        meta={
            "nightly": {
                "shares": {"drift": 0.5, "screening_design": 0, "search": 0.6, "confirmation": 0.3}
            }
        }
    )
    invalid(jev={"data_classes_allowed": ["secret"]})
    invalid(roles={"planner": []})  # min_length 1
    invalid(roles={"planner": ["no-such-cell"]})
    invalid(meta={"nightly": {"cells": ["no-such-cell"]}})


def test_config_refuses_cells_that_break_ic07(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    base = json.loads(config.read_text())
    high = {"driver": CODEX, "model": CODEX_MODEL, "effort": "high"}
    with pytest.raises(ValidationError, match="already configured"):
        LocalConfig.model_validate({**base, "cells": [high, high]})
    # the legacy cell is the driver entry; a provider-default entry of its model duplicates it
    legacy_again = {"driver": CODEX, "model": CODEX_MODEL, "effort": LEGACY_EFFORT}
    with pytest.raises(ValidationError, match="already configured"):
        LocalConfig.model_validate({**base, "cells": [legacy_again]})
    with pytest.raises(ValidationError, match="configure the claude-cli driver first"):
        LocalConfig.model_validate(
            {**base, "cells": [{"driver": CLAUDE, "model": "claude-sonnet-5", "effort": "high"}]}
        )
    with pytest.raises(ValidationError, match="unknown apps"):
        LocalConfig.model_validate(
            {**base, "cells": [{**high, "qualification_reports": {"nowhere": "/q.json"}}]}
        )


def test_config_refuses_an_effort_the_driver_does_not_document(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    base = json.loads(config.read_text())
    for driver, model, effort in (
        (CODEX, CODEX_MODEL, "turbo"),
        (CLAUDE, "claude-sonnet-5", "ultracode"),
        ("opencode-server", "opencode-go/glm-5.3-flash", "high"),  # no effort axis (OD-12)
    ):
        assert hold_code(
            LocalConfig.model_validate,
            {**base, "cells": [{"driver": driver, "model": model, "effort": effort}]},
        ) == "EFFORT_UNSUPPORTED"  # fmt: skip
    assert hold_code(
        LocalConfig.model_validate,
        {**base, "cells": [{"driver": CODEX, "model": "latest", "effort": "high"}]},
    ) == "MODEL_UNPINNED"  # fmt: skip


# -- amplai ops local-cell add / remove / list ----------------------------------------------------
def test_local_cell_add_list_and_remove(tmp_path: Path) -> None:
    config, inputs = init(tmp_path)
    added = run(
        "ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "high",
        "--config", str(config),
    )  # fmt: skip
    assert added["cell_id"] == "codex-cli.gpt-5.6-sol.high"
    assert "local-cell probe codex-cli.gpt-5.6-sol.high" in added["next"]
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert json.loads(config.read_text())["cells"] == [
        {
            "driver": CODEX,
            "model": CODEX_MODEL,
            "effort": "high",
            "qualification_reports": None,
            "enabled": True,
        }
    ]
    # adding the same cell again changes nothing
    run(
        "ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "high",
        "--config", str(config),
    )  # fmt: skip
    assert len(json.loads(config.read_text())["cells"]) == 1
    run(
        "ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "low",
        "--config", str(config),
    )  # fmt: skip
    listed = run("ops", "local-cell", "list", "--config", str(config))["cells"]
    assert [(c["cell_id"], c["legacy"], c["effort"]) for c in listed] == [
        (CODEX, True, LEGACY_EFFORT),
        ("codex-cli.gpt-5.6-sol.high", False, "high"),
        ("codex-cli.gpt-5.6-sol.low", False, "low"),
    ]
    assert listed[0]["model"] == CODEX_MODEL and listed[0]["enabled"] is True
    removed = run(
        "ops", "local-cell", "remove", "codex-cli.gpt-5.6-sol.high", "--config", str(config)
    )
    assert removed["removed"] == "codex-cli.gpt-5.6-sol.high"
    left = run("ops", "local-cell", "list", "--config", str(config))["cells"]
    assert [c["cell_id"] for c in left] == [CODEX, "codex-cli.gpt-5.6-sol.low"]
    assert inputs  # the rig inputs were only needed to init


def test_local_cell_add_records_the_models_qualification_report_per_app(tmp_path: Path) -> None:
    config, inputs = init(tmp_path)
    run(
        "ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "high",
        "--qualification-report", str(inputs.qualification_report), "--config", str(config),
    )  # fmt: skip
    (entry,) = json.loads(config.read_text())["cells"]
    assert entry["qualification_reports"] == {"app": str(inputs.qualification_report.absolute())}
    # a second effort of the same model reuses the same report (D-097)
    run(
        "ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "low",
        "--qualification-report", str(inputs.qualification_report), "--config", str(config),
    )  # fmt: skip
    reports = {
        c["effort"]: c["qualification_reports"] for c in json.loads(config.read_text())["cells"]
    }
    assert reports["high"] == reports["low"]
    listed = run("ops", "local-cell", "list", "--config", str(config))["cells"]
    assert listed[1]["qualification_reports"] == reports["high"]


def test_local_cell_add_refusals_leave_the_file_unchanged(tmp_path: Path) -> None:
    config, inputs = init(tmp_path)
    before = config.read_bytes()

    def add(*extra: str, driver: str = CODEX, effort: str = "high") -> Any:
        return fails(
            "ops", "local-cell", "add", "--driver", driver, "--model", CODEX_MODEL,
            "--effort", effort, "--config", str(config), *extra,
        )  # fmt: skip

    assert "EFFORT_UNSUPPORTED" in add(effort="turbo").output
    assert "EFFORT_UNSUPPORTED" in add(driver="opencode-server").output
    assert "DRIVER_UNKNOWN" in add(driver="gemini-cli").output
    assert "configure the claude-cli driver first" in add(driver=CLAUDE).output
    # --app names the app of a --qualification-report; alone it is meaningless
    assert add("--app", "app").exit_code == 2
    assert inputs.qualification_report.is_file()
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.tmp"))


def test_local_cell_remove_refuses_the_unknown_and_the_legacy_cell(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    before = config.read_bytes()
    for cell_id in ("codex-cli.gpt-5.6-sol.high", CODEX):  # not configured / legacy (driver entry)
        result = fails("ops", "local-cell", "remove", cell_id, "--config", str(config))
        assert "CELL_UNKNOWN" in result.output
    assert config.read_bytes() == before


# -- amplai ops local-cell probe -------------------------------------------------------------------
def accepting_factory(log: list[Any]) -> Any:
    def factory(c: Cell, inputs: CodexProfileInputs, scratch_root: Path) -> Any:
        log.append((c.cell_id, inputs.model, inputs.qualification_report, scratch_root))
        return FakeTurn()

    return factory


def test_probe_local_cell_records_an_accepted_probe(tmp_path: Path) -> None:
    from amplai_foundry.runtime.storage.store import Scope, Store

    config, _ = init(tmp_path)
    run("ops", "local-cell", "add", "--driver", CODEX, "--model", CODEX_MODEL, "--effort", "high",
        "--config", str(config))  # fmt: skip
    log: list[Any] = []
    out = probe_local_cell(
        config, "codex-cli.gpt-5.6-sol.high", turn_factory=accepting_factory(log)
    )
    assert out["outcome"] == "accepted" and out["app"] == "app" and "local-serve" in out["next"]
    assert log[0][0] == "codex-cli.gpt-5.6-sol.high" and log[0][1] == CODEX_MODEL
    cfg = json.loads(config.read_text())
    store = Store(
        Path(config.parent / cfg["runtime_root"])
        if not Path(cfg["runtime_root"]).is_absolute()
        else Path(cfg["runtime_root"])
    )
    try:
        found = latest_probe(store, Scope.parse(cfg["scope"]), "codex-cli.gpt-5.6-sol.high")
    finally:
        store.close()
    assert found is not None and found[1]["outcome"] == "accepted" and found[1]["image"] == IMAGE


def test_probe_local_cell_refusals(tmp_path: Path) -> None:
    config, _ = init(tmp_path)
    run("ops", "local-cell", "add", "--driver", CODEX, "--model", OTHER_MODEL, "--effort", "high",
        "--config", str(config))  # fmt: skip
    factory = accepting_factory([])
    # the legacy cell has no effort to probe; an unknown id names no cell
    assert hold_code(probe_local_cell, config, CODEX, turn_factory=factory) == "EFFORT_UNSUPPORTED"
    assert hold_code(probe_local_cell, config, "codex-cli.nope.high", turn_factory=factory) == (
        "CELL_UNKNOWN"
    )
    # the rig's report names another model: the cell is not qualified, so no turn is spent
    cell_id = Cell.make_id(CODEX, OTHER_MODEL, "high", legacy=False)
    assert (
        hold_code(probe_local_cell, config, cell_id, turn_factory=factory) == "DRIVER_UNQUALIFIED"
    )
    assert hold_code(probe_local_cell, config, cell_id, app_id="nowhere", turn_factory=factory) == (
        "DRIVER_UNQUALIFIED"
    )
