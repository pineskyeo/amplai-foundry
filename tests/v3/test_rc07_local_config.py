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


def test_an_app_verifier_is_added_replaced_in_place_and_removed(tmp_path: Path) -> None:
    config, _inputs = init(tmp_path)

    def ids() -> list[tuple[str, list[str]]]:
        (app,) = json.loads(config.read_text())["apps"]
        return [(v["id"], v["argv"]) for v in app["verifiers"]]

    # the format gate CI runs (found when a verified agent change failed CI format check)
    run(
        "ops", "local-verifier", "--app", "app",
        "--verifier", "format=ruff format --check . | ruff format is clean",
        "--config", str(config),
    )  # fmt: skip
    assert ids() == [
        ("check", ["python3", "-c", "import app"]),
        ("format", ["ruff", "format", "--check", "."]),
    ]
    run(
        "ops", "local-verifier", "--app", "app",
        "--verifier", "check=python3 -c 'import app, sys' | app imports",
        "--config", str(config),
    )  # fmt: skip
    assert ids()[0] == ("check", ["python3", "-c", "import app, sys"])  # same place
    run("ops", "local-verifier", "--app", "app", "--remove", "format", "--config", str(config))
    assert [i for i, _ in ids()] == ["check"]
    # the last verifier and an unknown app are refused, and the file is left as it was
    before = config.read_bytes()
    for args in (["--app", "app", "--remove", "check"], ["--app", "nope", "--remove", "x"]):
        result = CliRunner().invoke(
            cli.app, ["ops", "local-verifier", *args, "--config", str(config)]
        )
        assert result.exit_code != 0
    assert config.read_bytes() == before
    dep = LocalProductDeployment(config, start_loop=False)
    try:
        assert [v.id for v in dep.service.apps["app"].config.verifiers] == ["check"]
    finally:
        dep.close()


def test_local_update_fast_forwards_a_clean_checkout_and_refuses_a_dirty_one(
    tmp_path: Path,
) -> None:
    import subprocess

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = make_repo(tmp_path / "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main")
    installed = tmp_path / "installed"
    subprocess.run(["git", "clone", "-q", str(remote), str(installed)], check=True)
    (seed / "notes.txt").write_text("merged on main\\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "merged")
    git(seed, "push", "-q", "origin", "main")
    head = git(seed, "rev-parse", "HEAD").strip()
    out = json.loads(run("ops", "local-update", "--root", str(installed), "--no-restart"))
    assert out["after"] == head and out["before"] != head
    assert out["changed_files"] == 1 and out["reinstalled"] is False
    assert out["restarted"] is False
    # an operator's local change is never overwritten
    (installed / "notes.txt").write_text("local edit\\n")
    result = CliRunner().invoke(
        cli.app, ["ops", "local-update", "--root", str(installed), "--no-restart"]
    )
    assert result.exit_code == 3 and "LOCAL_UPDATE_DIRTY" in result.output
    assert (installed / "notes.txt").read_text() == "local edit\\n"
