"""Web tools off in tests: the driver contract (operator decision 2026-10-08, supersedes IC-35).

Every trial dispatch (calibration, stage experiments, canary trials, nightly) runs its agent with
all internet and web tools disabled, for Claude Code, Codex, OpenCode and any driver added later.
The container egress allowlist (model API only) and answer-lookup detection
(``answer_lookup.py``) stay as additional guards.

The contract: a driver port (or the read-only turn of a cell) declares how its web tools are
turned off as ``offline_tools``, a non-empty mapping whose keys are among

- ``argv``: the arguments the driver adds to a trial dispatch's command line,
- ``env``: the environment entries it adds (never a secret: the values are recorded),
- ``config``: the configuration it applies,
- ``no_tools``: a reason, for a port that runs no model and has no tools to turn off.

A trial goal whose port declares nothing is held ``DRIVER_WEB_UNDECLARED`` before any claim
(``LocalTrialExecutor.__call__``, ``ExecutionLoop.run_goal``) and a trial read-only turn whose
turn declares nothing before it runs (``strategy_runner``, ``product.plan``), so a future driver
cannot run a test until it declares one. The trial flag reaches a port as
``DispatchOptions.offline`` (``ExecutionLoop._options``: every goal with a trial context) and a
read-only turn as ``run(..., offline=True)``. A real (non-trial) goal never sets it; its argv
is unchanged, except Codex hosted web search, which decision (C) turned off for every dispatch.
Judges (``LlmCellJudge.ask``), the proposer ensemble, dreaming and effort probes (``run_probe``)
always require the declaration and run ``offline=True``, and the qualification scripts run the
trial argv: the operator asked for every test turn of every driver to be off the internet.

Per-driver settings (the facts behind them are in ``interfaces.md``, "Operator Decision
2026-10-08: Web Tools Off In Tests"):

- Claude Code 2.1.292: ``--disallowedTools`` with the tools of the stored ``init`` tool list
  that reach a network service (``CLAUDE_WEB_TOOLS``); ``--strict-mcp-config`` (no MCP server
  unless ``--mcp-config`` names one, and none is named) is already in the OAuth argv and is added
  to the API-key (``--bare``) argv.
- Codex 0.155.1: ``-c web_search="disabled"`` (decision (C), every dispatch), plus on a trial
  ``-c features.<name>=false`` for the default-on features whose names are browser, app or remote
  surfaces (``CODEX_NETWORK_FEATURES``) and ``--ignore-user-config`` (``$CODEX_HOME/config.toml``,
  where MCP servers are configured, is never loaded, also not one the agent wrote in its home).
- OpenCode 1.17.13: ``OPENCODE_CONFIG_CONTENT`` (the final config merge) denying the
  ``webfetch`` and ``websearch`` permissions at the top level and for the ``general`` and
  ``explore`` subagents, and ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` (a project ``opencode.json``,
  which the agent could write into the workspace, is not loaded).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..runtime.errors import Hold

UNDECLARED = "DRIVER_WEB_UNDECLARED"
KEYS = frozenset({"argv", "env", "config", "no_tools"})

# Claude Code 2.1.292 (pinned app image): the stored init tool list
# (specs/033-harness-taxonomy/runs/artifacts/claude-*-stream.bin) has 24 tools; these reach a
# network service by the CLI's own schema and strings: WebFetch (url) and WebSearch (query),
# sdk-tools.d.ts:1099-1126; RemoteTrigger (remote trigger runs, an HTTP status in its output),
# :2939-2959, :4027-4031; PushNotification (mobile push, "pushSent"), :3246-3252, :4185-4194;
# DesignSync ("only available with claude.ai authentication", binary strings).
CLAUDE_WEB_TOOLS = ("WebSearch", "WebFetch", "RemoteTrigger", "DesignSync", "PushNotification")
CLAUDE_OFFLINE = ("--disallowedTools", ",".join(CLAUDE_WEB_TOOLS))
CLAUDE_NO_MCP = ("--strict-mcp-config",)

# Codex CLI 0.155.1 (pinned app image), `codex features list`: default-on ("true") features whose
# names are a browser, computer, app (connector) or remote surface. `-c features.<name>=false` is
# what `--disable <FEATURE>` means (`codex exec --help`).
CODEX_NETWORK_FEATURES = (
    "apps", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "computer_use", "in_app_browser", "remote_plugin", "skill_mcp_dependency_install",
)  # fmt: skip
CODEX_FEATURES_OFF = tuple(
    part for name in CODEX_NETWORK_FEATURES for part in ("-c", f"features.{name}=false")
)
# `codex exec --help` and `codex exec resume --help`: "--ignore-user-config  Do not load
# `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`"
CODEX_IGNORE_CONFIG = ("--ignore-user-config",)

# OpenCode 1.17.13: the config skill shipped in the binary names the permission keys ("Known
# permission keys: ... webfetch, websearch ...", flat actions "allow"/"ask"/"deny"), says
# "Per-agent `permission:` overrides top-level `permission:`", and lists the escape hatches
# OPENCODE_CONFIG_CONTENT ("inject inline JSON as a final local-scope merge") and
# OPENCODE_DISABLE_PROJECT_CONFIG=1 ("skip the project's local `opencode.json`"). The built-in
# ``explore`` subagent allows webfetch and websearch in its own ruleset (binary strings).
OPENCODE_WEB_DENY = {"webfetch": "deny", "websearch": "deny"}
OPENCODE_OFFLINE_CONFIG: dict[str, Any] = {
    "permission": dict(OPENCODE_WEB_DENY),
    "agent": {name: {"permission": dict(OPENCODE_WEB_DENY)} for name in ("general", "explore")},
}
OPENCODE_OFFLINE_ENV = {
    "OPENCODE_CONFIG_CONTENT": json.dumps(OPENCODE_OFFLINE_CONFIG, separators=(",", ":")),
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
}


def declaration(subject: Any) -> dict[str, Any] | None:
    """The valid declaration of a port or read-only turn, or None. Only the subject's own
    attribute counts: a wrapper that cannot pass the trial flag to the driver it wraps (a
    ``CliPort`` without ``accepts_options``) declares nothing."""
    value = getattr(subject, "offline_tools", None)
    if not isinstance(value, Mapping) or not value or not set(value) <= KEYS:
        return None
    if "no_tools" in value and len(value) != 1:
        return None
    # every entry says something: an empty argv, env or config turns nothing off
    if not all(isinstance(v, str | list | tuple | Mapping) and v for v in value.values()):
        return None
    return dict(value)


def require(subject: Any) -> dict[str, Any]:
    """The declaration, or Hold DRIVER_WEB_UNDECLARED: a test never runs a driver that has not
    said how its web tools are off."""
    found = declaration(subject)
    if found is None:
        raise Hold(
            UNDECLARED,
            "The driver does not declare how its web tools are turned off; tests never run it",
            details={"driver_id": getattr(subject, "driver_id", None)
                     or getattr(subject, "cell_id", None)},
        )  # fmt: skip
    return found


def toolless(subject: Any) -> bool:
    """A port that declares it runs no model and has no tools (``no_tools``)."""
    found = declaration(subject)
    return found is not None and "no_tools" in found


def require_port(port: Any) -> dict[str, Any]:
    """``require`` for a driver port: the trial flag reaches a port only inside its dispatch
    options, so a port that declares settings to apply must take options (``accepts_options``);
    only a ``no_tools`` port runs a trial without them. Hold DRIVER_WEB_UNDECLARED otherwise."""
    found = require(port)
    if "no_tools" not in found and getattr(port, "accepts_options", False) is not True:
        raise Hold(
            UNDECLARED,
            "The port declares web-off settings but takes no dispatch options to receive them",
            details={"driver_id": getattr(port, "driver_id", None)},
        )  # fmt: skip
    return found
