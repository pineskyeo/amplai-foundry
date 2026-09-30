"""Work 030 S6 (D-089) — ``amplai meta``: each gate is one explicit operator command.

Real: `amplai ops local-init` output, the deployment each command opens, the MetaHarness gates and
the durable approvals. The gates that run trials are covered by the lifecycle rehearsal
(tests/e2e/test_rc12_meta_rehearsal, stand-in agent) and by the recorded real runs; these tests
cover the commands that need no driver.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from amplai_foundry.runtime import cli
from amplai_foundry.runtime.execution.codex import AUTH
from rc06_rig import codex_inputs, make_repo

GOOD = (
    "You are the IMPLEMENTER for {app_id} at commit {base_commit}.\n"
    "Run the checks. Do not commit.\n"
)


def mini_corpus(root: Path) -> Path:
    (root / "base").mkdir(parents=True)
    (root / "base" / "app.py").write_text("def value():\n    return 1\n")
    ids = ["t00", "t01"]
    for task_id in ids:
        folder = root / "tasks" / task_id
        (folder / "hidden").mkdir(parents=True)
        (folder / "reference").mkdir(parents=True)
        (folder / "hidden" / "test_t.py").write_text(
            "from app import value\n\n\ndef test_two() -> None:\n    assert value() == 2\n"
        )
        (folder / "reference" / "app.py").write_text("def value():\n    return 2\n")
        (folder / "task.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "difficulty": "small",
                    "objective": f"Make value() return 2 ({task_id}).",
                    "acceptance": [f"value() returns 2 ({task_id})"],
                }
            )
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": "cli",
                "app_id": "app",
                "base_commit": "0" * 40,
                "task_ids": ids,
            }
        )
    )
    return root


@pytest.fixture
def home(tmp_path: Path) -> dict[str, Any]:
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
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(GOOD)
    return {
        "config": str(where / "local.json"),
        "corpus": str(mini_corpus(tmp_path / "corpus")),
        "prompt": str(prompt),
    }


def meta(home: dict[str, Any], *args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(
        cli.app,
        ["meta", *args, "--config", home["config"], "--corpus", home["corpus"]],
    )
    text = result.output.strip()
    return result.exit_code, json.loads(text[text.index("{") :]) if "{" in text else {}


def propose(home: dict[str, Any], suffix: str = "shorter") -> str:
    code, out = meta(
        home, "propose", "--suffix", suffix, "--prompt-file", home["prompt"],
        "--hypothesis", "A shorter prompt keeps the pass rate.",
        "--benefit", "Less prompt text", "--observation", "the prompt is long",
        "--risk", "a shorter prompt may drop an instruction",
    )  # fmt: skip
    assert code == 0, out
    return str(out["proposal_id"])


def test_every_gate_is_a_command_and_none_chains_them() -> None:
    result = CliRunner().invoke(cli.app, ["meta", "--help"])
    assert result.exit_code == 0
    for name in (
        "propose", "screen", "approve-experiment", "run-experiment", "approve-canary",
        "run-canary", "promote", "rollback", "reject", "abort", "status",
    ):  # fmt: skip
        assert name in result.output
    assert "evolve" not in result.output and "auto" not in result.output


def test_propose_screen_status_reject(home: dict[str, Any]) -> None:
    pid = propose(home)
    assert meta(home, "status", pid)[1]["state"] == "draft"
    code, screened = meta(home, "screen", pid)
    assert code == 0 and screened["state"] == "screened"
    code, rejected = meta(home, "reject", pid, "--reason", "not worth an experiment")
    assert code == 0 and rejected["state"] == "rejected"
    status = meta(home, "status", pid)[1]
    assert status["state"] == "rejected"
    assert status["rejection"]["reason"] == "not worth an experiment"
    assert status["active_release"].startswith("local-baseline-")


def test_a_gate_out_of_order_is_refused_with_the_state_it_needs(home: dict[str, Any]) -> None:
    pid = propose(home)
    for gate in (
        ("approve-experiment", pid, "--max-tokens", "1000", "--max-wall-seconds", "60"),
        ("run-experiment", pid, "--per-trial-tokens", "10", "--basis", "x", "--evidence", "y"),
        ("approve-canary", pid, "--tasks", "t00", "--max-trial-tokens", "10"),
        ("promote", pid),
        ("rollback", pid),
    ):
        code, out = meta(home, *gate)
        assert code == 3 and out["code"] == "META_STATE", (gate[0], out)
    assert meta(home, "status", pid)[1]["state"] == "draft"


def test_a_prompt_with_an_unknown_placeholder_is_refused(
    home: dict[str, Any], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_text("Work on {secret_value} now.\n")
    code, out = meta(
        home, "propose", "--suffix", "bad", "--prompt-file", str(bad),
        "--hypothesis", "h", "--benefit", "b", "--observation", "o", "--risk", "r",
    )  # fmt: skip
    assert code != 0 and out.get("code")
    # nothing was proposed: a fresh proposal still works and is the only one
    assert propose(home, "fine")


def test_reject_needs_a_reason(home: dict[str, Any]) -> None:
    pid = propose(home)
    result = CliRunner().invoke(
        cli.app, ["meta", "reject", pid, "--config", home["config"], "--corpus", home["corpus"]]
    )
    assert result.exit_code != 0  # --reason is required by the command itself
    code, out = meta(home, "reject", pid, "--reason", "  ")
    assert code == 2 and out["code"] == "REJECT_REASON"
