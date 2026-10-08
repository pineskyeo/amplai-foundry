"""Work 018 S1 — seams the real execution path depends on.

- The runtime reads verifier profiles as kind ``verifier-profile``; the registry routes used a
  different name, so a verifier could not be installed through the API or deployment config.
- The production Codex argv must equal the argv Work 016 qualified in the container.
- The coordinator removes a run's driver resources once the process is confirmed stopped.
"""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from amplai_foundry.agent_drivers.cli import CliDriver
from amplai_foundry.agent_drivers.ports import DriverRegistry, RecipePort
from amplai_foundry.agent_drivers.protocol import SessionJournal
from amplai_foundry.control_plane.api_v3.server import ApiServices, BearerAuthenticator, create_app
from amplai_foundry.runtime.execution.worker import WorkCoordinator
from amplai_foundry.sandbox.workspace import WorkspaceManager

TOKEN = "test-only-api-token-bbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def api(deployment: Any) -> Any:
    d = deployment
    admin = replace(d.actor, permissions=d.actor.permissions | {"runtime.read", "runtime.admin"})
    bindings = {hashlib.sha256(TOKEN.encode()).hexdigest(): "admin"}
    services = ApiServices(
        d.runtime, d.goals, BearerAuthenticator(bindings, lambda _b: admin), verification=None
    )
    with TestClient(create_app(services)) as c:
        c.headers["Authorization"] = "Bearer " + TOKEN
        yield d, c


def test_registry_installs_verifier_profile_under_the_runtime_kind(api: Any) -> None:
    # given: an installed verifier profile the runtime reads as kind verifier-profile
    d, c = api
    p = d.prepare()
    value = dict(d.store.get(d.scope, "verifier-profile", p["verifier_ref"]))
    value["profile_id"] = "second-verifier"
    body = {"value": value, "object_id": "second-verifier", "revision": 1}
    # when: an operator registers it through the API under that kind
    ok = c.post("/api/v3/registry/verifier-profile", headers={"Idempotency-Key": "v1"}, json=body)
    wrong = c.post(
        "/api/v3/registry/verification-profile", headers={"Idempotency-Key": "v2"}, json=body
    )
    # expected: the runtime kind is accepted and the unknown name is not
    assert ok.status_code < 300, ok.text
    assert wrong.status_code == 400 and wrong.json()["code"] == "PROTECTED_REGISTRY"


class _Sandbox:
    def command(self, argv: list[str], *_a: Any, **_k: Any) -> list[str]:
        return argv


def _codex(tmp_path: Path) -> CliDriver:
    return CliDriver(
        "codex",
        "codex",
        "0.155.1",
        _Sandbox(),  # type: ignore[arg-type]
        SessionJournal(tmp_path / "journal"),
        model="gpt-5.6-sol",
        qualified=True,
    )


def test_codex_argv_equals_the_qualified_container_argv(tmp_path: Path) -> None:
    # given: the argv scripts/container_qualify.py qualifies. Codex's own bwrap sandbox cannot
    # create namespaces in the unprivileged container (measured 2026-09-28: shell and file
    # writes fail), so the container is the sandbox and Codex's is bypassed (D-073).
    # Operator decision (C), 2026-10-08: plus `-c web_search="disabled"` on every dispatch, in
    # the qualification argv as in production
    qualified = [
        "codex", "--ask-for-approval", "never", "exec", "--json", "--model", "gpt-5.6-sol",
        "-c", 'web_search="disabled"',
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "do it",
    ]  # fmt: skip
    resumed = [
        "codex", "--ask-for-approval", "never", "exec", "resume", "sess_1", "--json", "--model",
        "gpt-5.6-sol", "-c", 'web_search="disabled"',
        "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "do it",
    ]  # fmt: skip
    driver = _codex(tmp_path)
    # expected: production builds exactly the same vector (workspaces carry no .git)
    assert driver.argv("do it") == qualified
    assert driver.argv("do it", session="sess_1") == resumed
    # and so does the qualification script itself (its argv is built without a sandbox)
    script = _qualify_script()
    turns = SimpleNamespace(driver="codex", model="gpt-5.6-sol")
    assert script.ContainerTurns.argv(turns, "do it") == qualified
    assert script.ContainerTurns.argv(turns, "do it", session="sess_1") == resumed


def _qualify_script() -> Any:
    """``scripts/container_qualify.py`` as a module (it runs nothing on import)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "container_qualify.py"
    spec = importlib.util.spec_from_file_location("container_qualify_rc06", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SpyPort(RecipePort):
    def __init__(self, journal: SessionJournal) -> None:
        super().__init__(journal)
        self.destroyed: list[str] = []

    def destroy(self, handle: str) -> None:
        self.destroyed.append(handle)


def test_coordinator_destroys_driver_resources_after_a_stopped_run(deployment: Any) -> None:
    import json

    # given: a prepared goal and a port that records destroy calls
    d = deployment
    p = d.prepare()
    registry = DriverRegistry(d.store)
    port = _SpyPort(SessionJournal(d.root / "native-journal"))
    registry.register(d.actor, p["execution_profile"]["driver_profile_ref"], port)
    workspaces = WorkspaceManager(d.root / "isolated", d.artifacts)
    coordinator = WorkCoordinator(d.runtime, registry, workspaces)
    dispatch = d.runtime.claim(d.worker, goal_id=p["goal_id"])
    recipe = p["recipes"][dispatch["node"]["work_id"]]
    # when: the run completes and its outputs are collected
    coordinator.execute(
        d.worker,
        dispatch,
        prompt=json.dumps({"operations": recipe["operations"]}),
        base_snapshot=workspaces.empty_snapshot(d.scope),
        output_paths={k: v["path"] for k, v in recipe["outputs"].items()},
    )
    # expected: the stopped run's driver resources (container) are removed exactly once
    assert len(port.destroyed) == 1


def test_the_sandbox_can_mount_the_workspace_read_only(tmp_path: Path) -> None:
    from amplai_foundry.sandbox.container import ContainerProfile, ContainerSandbox

    sandbox = ContainerSandbox(ContainerProfile("localhost:5000/x@sha256:" + "d" * 64))
    rw = sandbox.command(["true"], tmp_path, "rw")
    ro = sandbox.command(["true"], tmp_path, "ro", workspace_readonly=True)
    assert f"type=bind,src={tmp_path},dst=/workspace" in rw
    assert f"type=bind,src={tmp_path},dst=/workspace,readonly" in ro
