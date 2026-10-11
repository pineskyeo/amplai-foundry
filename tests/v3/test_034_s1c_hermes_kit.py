"""Work 034 S1c — the Hermes front-agent kit is consistent with AMPLAI (spec F-1, F-4, H-9).

The kit is configuration (``integrations/front_agents/hermes/``): it must name exactly the tool
contract's tools, keep the H-9 switches, carry no secret, and keep the approval and code-edit
prohibitions in the agent's instructions. Hermes itself is not run here (AC-H8 is the operator's
VM check, ``vm-checklist.ko.md``).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

from amplai_foundry.integrations.intake_mcp.contract import CONTRACT_ID, CONTRACT_VERSION, load

KIT = Path(str(resources.files("amplai_foundry.integrations").joinpath("front_agents/hermes")))


def text(name: str) -> str:
    return (KIT / name).read_text(encoding="utf-8")


def block(config: str, header: str) -> list[str]:
    """The ``- item`` lines under a ``header:`` line of the YAML template."""
    lines = config.splitlines()
    start = next(i for i, line in enumerate(lines) if line.split("#")[0].strip() == header + ":")
    indent = len(lines[start]) - len(lines[start].lstrip())
    items = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].split("#")[0].strip())
    return items


def test_the_manifest_lists_the_kit_files_and_the_contract_version() -> None:
    manifest = json.loads(text("kit.json"))
    assert manifest["tool_contract"] == {"contract": CONTRACT_ID, "version": CONTRACT_VERSION}
    assert manifest["front_agent"]["version"] == "v0.21.6"
    for name in manifest["files"]:
        assert (KIT / name).is_file(), name
    on_disk = {p.name for p in KIT.iterdir() if p.is_file()} - {"kit.json"}
    assert on_disk == set(manifest["files"])


def test_the_mcp_connection_includes_exactly_the_contract_tools() -> None:
    config = text("config.yaml.template")
    assert block(config, "include") == list(load())
    assert 'args: ["-m", "amplai_foundry.integrations.intake_mcp"]' in config
    assert "AMPLAI_INTAKE_ADAPTER: hermes" in config
    assert "AMPLAI_INTAKE_TOKEN: ${AMPLAI_INTAKE_TOKEN}" in config


def test_the_h9_switches_are_set() -> None:
    config = text("config.yaml.template")
    assert set(block(config, "disabled_toolsets")) >= {
        "delegation", "code_execution", "cronjob", "browser", "catalog", "web", "search",
    }  # fmt: skip
    assert re.search(r"^skills:\n  write_approval: true", config, re.M)
    assert re.search(r"^memory:\n  write_approval: true", config, re.M)
    assert re.search(r"^  backend: docker", config, re.M)
    assert "docker_volumes: []" in config and "docker_network: false" in config


def test_the_kit_carries_no_secret() -> None:
    for path in KIT.iterdir():
        content = path.read_text(encoding="utf-8")
        assert not re.search(r"xox[bpa]-[A-Za-z0-9]", content), path.name
        assert not re.search(r"xapp-[A-Za-z0-9]", content), path.name
        assert not re.search(r"hooks\.slack\.com/services/[A-Z0-9]", content), path.name
    env = text("env.template")
    assert "webhook" not in env.split("# The intake")[1].lower()  # the alert URL is not on the VM


def test_the_instructions_keep_the_prohibitions() -> None:
    soul = text("SOUL.md")
    assert "never approve" in soul and "never edit code" in soul
    for tool in load():
        assert "approve" not in tool
