"""Work 031 — OpenCode is composed into the local product when it is configured and qualified.

Real: `amplai ops local-init`, the local config, LocalProductDeployment composition, the driver
registry and composition selection. No container starts: a server is launched only by a
dispatch (tests/v3/test_031_opencode_port.py covers that with a stand-in launcher).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.agent_drivers.opencode_port import PerDispatchOpenCodePort
from amplai_foundry.runtime import cli
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import IMAGE, codex_inputs, make_repo


def configure(tmp_path: Path, *, qualified: bool = True) -> Path:
    repo = make_repo(tmp_path)
    inputs = codex_inputs(tmp_path)
    container = json.loads(inputs.container_profile.read_text())
    container["tools"]["opencode"] = "1.17.13"
    inputs.container_profile.write_text(json.dumps(container))
    codex_home = tmp_path / "codex-home"
    (codex_home / ".codex").mkdir(parents=True)
    (codex_home / AUTH).write_text('{"tokens": "x"}')
    home = tmp_path / "amplai"
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
            "--home", str(home), "--operator", "pinesky",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    report = {
        "container_image": IMAGE,
        "checked_at": "2026-09-30T00:00:00Z",
        "reports": {
            "opencode-server": {
                "status": "pass" if qualified else "fail",
                "driver_version": "1.17.13",
                "model": "opencode-go/glm-5.3-flash",
                "qualification_id": "qualification-opencode-rig",
                "checks": [{"name": "exact_version", "outcome": "pass"}],
                "tool_use": {"outcome": "pass"},
            }
        },
    }
    (tmp_path / "opencode-qual.json").write_text(json.dumps(report))
    oc_home = tmp_path / "opencode-home"
    for rel, body in (
        (Path(".local/share/opencode/auth.json"), '{"opencode-go": {"type": "api"}}'),
        (Path(".cache/opencode/models.json"), "{}"),
    ):
        (oc_home / rel).parent.mkdir(parents=True, exist_ok=True)
        (oc_home / rel).write_text(body)
    config = home / "local.json"
    added = CliRunner().invoke(
        cli.app,
        [
            "ops", "local-opencode", "--credential-home", str(oc_home),
            "--qualification-report", str(tmp_path / "opencode-qual.json"),
            "--config", str(config),
        ],
    )  # fmt: skip
    assert added.exit_code == 0, added.output
    value = json.loads(config.read_text())
    assert value["opencode"]["model"] == "opencode-go/glm-5.3-flash"
    return config


def test_a_qualified_opencode_becomes_the_third_candidate(tmp_path: Path) -> None:
    dep = LocalProductDeployment(configure(tmp_path), start_loop=False)
    try:
        installed = dep.service.apps["app"]
        assert set(installed.compositions) == {"codex-cli", "opencode-server"}
        chosen = dep.service.select_composition(installed)
        assert chosen["driver_id"] == "codex-cli"  # router order: codex, claude, opencode
        rows = {c["driver_id"]: c for c in chosen["candidates"]}
        assert rows["opencode-server"]["eligible"] is True
        port = dep.coordinator.registry.resolve(
            dep.scope, installed.driver_refs["opencode-server"]["driver"], "direct"
        )
        assert isinstance(port, PerDispatchOpenCodePort)
        assert port._runs == {}  # nothing launched before a dispatch
    finally:
        dep.close()


def test_an_unqualified_opencode_report_is_refused(tmp_path: Path) -> None:
    with pytest.raises(Hold):
        LocalProductDeployment(configure(tmp_path, qualified=False), start_loop=False)


def test_opencode_can_be_left_out_by_the_operator(tmp_path: Path) -> None:
    config = configure(tmp_path)
    value: dict[str, Any] = json.loads(config.read_text())
    value["apps"][0].pop("opencode_qualification_report")
    config.write_text(json.dumps(value))
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        assert set(dep.service.apps["app"].compositions) == {"codex-cli"}
    finally:
        dep.close()
