"""Work 018 S4 — the production Codex profile is registered from measured inputs only (EX-001)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.agent_drivers.ports import DriverRegistry
from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.codex import (
    AUTH,
    CodexProfileInputs,
    SeededCodexPort,
    build_codex_port,
    install_codex_profile,
    measured_qualification,
)

REPO = Path(__file__).resolve().parents[2]
IMAGE = "localhost:5000/amplai-worker-app-x@sha256:" + "a" * 64
CAPS = [{"action": "workspace.write", "resource": "sandbox:app", "effect_class": "sandbox_write"}]


def inputs(tmp_path: Path, **report: Any) -> CodexProfileInputs:
    container = {
        "image": IMAGE,
        "uid": 65534,
        "gid": 65534,
        "memory": "2g",
        "cpus": 2.0,
        "pids": 256,
        "tools": {"codex": "codex-cli 0.155.1"},
    }
    (tmp_path / "container.json").write_text(json.dumps(container))
    doc = {
        "container_image": report.pop("image", IMAGE),
        "checked_at": "2026-09-28T00:00:00Z",
        "reports": {
            "codex-cli": {
                "status": "pass",
                "driver_version": "0.155.1",
                "model": "gpt-5.6-sol",
                "qualification_id": "qualification-codex-test",
                "checks": [{"name": "exact_version", "outcome": "pass"}],
                **report,
            }
        },
    }
    (tmp_path / "qual.json").write_text(json.dumps(doc))
    return CodexProfileInputs(
        container_profile=tmp_path / "container.json",
        egress_profile=REPO / "deployment" / "local-egress.json",
        egress_qualification=REPO / "deployment" / "local-egress-qualification.json",
        qualification_report=tmp_path / "qual.json",
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "fail"},
        {"image": "localhost:5000/other@sha256:" + "b" * 64},
        {"driver_version": "0.154.0"},
        {"model": "some-other-model"},
    ],
)
def test_registration_refuses_anything_but_a_passing_report_for_this_image(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    with pytest.raises(Hold) as exc:
        measured_qualification(inputs(tmp_path, **bad))
    assert exc.value.code == "DRIVER_UNQUALIFIED"


def test_installed_records_register_the_codex_port_and_reinstall_is_idempotent(
    deployment: Any, tmp_path: Path
) -> None:
    d = deployment
    i = inputs(tmp_path)
    refs = install_codex_profile(d.store, d.scope, i, CAPS)
    again = install_codex_profile(d.store, d.scope, i, CAPS)
    assert refs == again
    driver = d.store.get(d.scope, "driver-capabilities", refs["driver"])
    qualification = d.store.get(d.scope, "qualification", refs["qualification"])
    assert driver["maturity"] == "qualified" and driver["driver_version"] == "0.155.1"
    assert qualification["environment_ref"] == refs["environment"] == driver["environment_ref"]
    home = tmp_path / "codex-home"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "x"}')
    port = build_codex_port(i, tmp_path / "journal", home)
    registry = DriverRegistry(d.store)
    admin = replace(d.actor, permissions=d.actor.permissions | {"runtime.admin"})
    registry.register(admin, refs["driver"], port)
    assert registry.resolve(d.scope, refs["driver"], "bounded_loop") is port


class _Driver:
    def __init__(self, root: Path) -> None:
        from amplai_foundry.agent_drivers.protocol import SessionJournal

        self.provider, self.version = "codex", "0.155.1"
        self.journal = SessionJournal(root / "journal")
        self.native_root = root / "journal" / "native"
        self.prepared: dict[str, Any] = {}

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path, **kw: Any) -> Any:
        self.prepared = kw
        return {"dispatch_id": dispatch["dispatch_id"]}

    def collect(self, handle: str) -> dict[str, Any]:
        return {"process_stopped": True, "provider_completed": True}


def test_credential_is_seeded_per_dispatch_written_back_and_removed(tmp_path: Path) -> None:
    home = tmp_path / "scoped"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "old"}')
    driver = _Driver(tmp_path)
    port = SeededCodexPort(driver, home)  # type: ignore[arg-type]
    port.prepare({"dispatch_id": "dispatch-1"}, "task", tmp_path)
    seeded = driver.native_root / "dispatch-1" / AUTH
    assert driver.prepared["native_home"] == driver.native_root / "dispatch-1"
    assert seeded.read_text() == '{"tokens": "old"}'
    # the agent's CLI refreshed the token during the run
    seeded.write_text('{"tokens": "new"}')
    port.collect("dispatch-1")
    assert (home / AUTH).read_text() == '{"tokens": "new"}' and not seeded.exists()
    # a corrupted token file is never written back
    port.prepare({"dispatch_id": "dispatch-2"}, "task", tmp_path)
    (driver.native_root / "dispatch-2" / AUTH).write_text("not json")
    port.collect("dispatch-2")
    assert (home / AUTH).read_text() == '{"tokens": "new"}'


def test_the_real_codex_home_is_never_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "user"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / AUTH).write_text("{}")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    with pytest.raises(Hold) as exc:
        SeededCodexPort(_Driver(tmp_path), fake_home)  # type: ignore[arg-type]
    assert exc.value.code == "CODEX_CREDENTIAL"
