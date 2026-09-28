"""Work 019 — operator configuration commands for Claude, extra apps and integrations.

Real local-init + the new `amplai ops local-*` commands on a temp home; the resulting config is
loaded by the real LocalProductDeployment (drivers, apps, integration factory).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.execution.codex import AUTH
from amplai_foundry.runtime.local_deployment import LocalProductDeployment
from rc06_rig import codex_inputs, git, make_repo


def init(tmp_path: Path) -> tuple[Path, Any]:
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
    return home / "local.json", inputs


def run(*args: str) -> str:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_claude_second_app_and_integration_are_configured_and_loaded(tmp_path: Path) -> None:
    config, inputs = init(tmp_path)
    token = tmp_path / "claude.env"
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("CLAUDE_CODE_OAUTH_TOKEN=test-token-not-a-secret\n")
    run(
        "ops", "local-claude", "--token-file", str(token),
        "--qualification-report", str(inputs.qualification_report), "--config", str(config),
    )  # fmt: skip
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    git(consumer, "init", "-q", "-b", "main")
    (consumer / "report.py").write_text('LABEL = "old"\n')
    git(consumer, "add", "-A")
    git(consumer, "commit", "-q", "-m", "base")
    run(
        "ops", "local-add-app", "--app", "consumer", "--repo", str(consumer),
        "--container-profile", str(inputs.container_profile),
        "--qualification-report", str(inputs.qualification_report),
        "--claude-qualification-report", str(inputs.qualification_report),
        "--verifier", "ccheck=python3 -c 'import report' | report imports",
        "--config", str(config),
    )  # fmt: skip
    run(
        "ops", "local-integration", "--id", "both", "--app", "app", "--app", "consumer",
        "--command", "python3 -c 'import sys'", "--description", "imports",
        "--config", str(config),
    )  # fmt: skip
    run("ops", "local-driver", "codex", "--disable", "--config", str(config))
    value = json.loads(config.read_text())
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert value["claude"]["enabled"] and value["codex"]["enabled"] is False
    assert [a["app_id"] for a in value["apps"]] == ["app", "consumer"]
    assert value["integrations"][0]["argv"] == ["python3", "-c", "import sys"]
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        assert set(dep.service.apps) == {"app", "consumer"}
        for installed in dep.service.apps.values():
            assert set(installed.compositions) == {"codex-cli", "claude-cli"}
            assert set(installed.planners) == {"codex-cli", "claude-cli"}
        chosen = dep.service.select_composition(dep.service.apps["consumer"])
        assert chosen["driver_id"] == "claude-cli"  # Codex disabled by the operator
        # both apps share one image: its environment covers both, in config order, each once
        composition = dep.store.get(dep.scope, "harness-composition", chosen["ref"])
        environment = dep.store.get(dep.scope, "environment", composition["sandbox_profile_ref"])
        assert [(c["action"], c["resource"]) for c in environment["capabilities"]] == [
            ("workspace.write", "sandbox:app"), ("workspace.design_write", "sandbox:app"),
            ("workspace.write", "sandbox:consumer"),
            ("workspace.design_write", "sandbox:consumer"),
        ]  # fmt: skip
        assert dep.service.integration_factory is not None
    finally:
        dep.close()


def test_a_config_change_that_does_not_validate_is_not_written(tmp_path: Path) -> None:
    config, _inputs = init(tmp_path)
    before = config.read_bytes()
    result = CliRunner().invoke(
        cli.app,
        ["ops", "local-integration", "--id", "x", "--app", "app", "--app", "nowhere",
         "--command", "true", "--description", "d", "--config", str(config)],
    )  # fmt: skip
    assert result.exit_code != 0 and "INTEGRATION_APPS" in result.output
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.tmp"))
