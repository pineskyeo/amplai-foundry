from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "integrations" / "hermes-amplai"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("hermes_amplai", INTEGRATION / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hermes_plugin_exposes_only_the_five_approved_tools() -> None:
    plugin = load_plugin_module()
    assert set(plugin.TOOLS) == {
        "amplai_submit_request",
        "amplai_get_request",
        "amplai_get_work",
        "amplai_list_pending_actions",
        "amplai_request_knowledge_intake",
    }
    source = (INTEGRATION / "__init__.py").read_text(encoding="utf-8")
    for forbidden in ("subprocess", "os.system", "shell=True", "activation.manage"):
        assert forbidden not in source


def test_restricted_profile_disables_execution_toolsets() -> None:
    profile = json.loads((INTEGRATION / "slack-profile.example.yaml").read_text(encoding="utf-8"))
    assert profile["feature_flags"]["enabled"] is False
    assert profile["toolsets"]["enabled"] == ["amplai"]
    assert set(profile["toolsets"]["disabled"]) >= {
        "terminal",
        "file",
        "code_execution",
        "delegation",
        "mcp",
    }


def test_profile_verifier_accepts_the_shipped_template() -> None:
    verifier = load_plugin_module().verify_profile
    verifier(INTEGRATION / "slack-profile.example.yaml")
