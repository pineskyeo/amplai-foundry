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
                "tool_use": {"outcome": "pass"},
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
        {"tool_use": {"outcome": "fail"}},
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


class _JournalDriver(_Driver):
    """A driver that keeps the session journal as ``CliDriver`` does: ``prepare`` and ``resume``
    write the dispatch's ``native_home`` before anything could spawn; a resume reuses the
    checkpoint's home (``agent_drivers/cli.py`` ``resume``)."""

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path, **kw: Any) -> Any:
        did = dispatch["dispatch_id"]
        self.journal.create(did, {"prompt": prompt})
        self.journal.update(did, native_home=str(kw["native_home"]))
        return {"dispatch_id": did}

    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: Any, **kw: Any
    ) -> str:
        home = Path(checkpoint["native_home"])
        return str(self.prepare(dispatch, prompt, workspace, native_home=home)["dispatch_id"])

    def cancel(self, handle: str) -> dict[str, Any]:
        self.journal.read(handle)
        return {"process_stopped": True}

    def destroy(self, handle: str) -> None:
        self.journal.read(handle)


def _scoped(tmp_path: Path) -> Path:
    home = tmp_path / "scoped"
    (home / ".codex").mkdir(parents=True)
    (home / AUTH).write_text('{"tokens": "old"}')
    return home


def test_a_restarted_port_releases_a_follow_up_and_a_steering_resume_by_the_journal_home(
    tmp_path: Path,
) -> None:
    """Work 033 S9b, §5.1 M3 rule 2: a follow-up ``<did>-f<k>`` and a steering ``resume-<digest>``
    run in the bound turn's home. A port built after a worker restart did not seed them, so it
    releases the home their driver journal names (refreshed token written back), never a
    guessed ``native_root/<handle>``."""
    home = _scoped(tmp_path)
    first = SeededCodexPort(_JournalDriver(tmp_path), home)  # type: ignore[arg-type]
    first.prepare({"dispatch_id": "d-1"}, "task", tmp_path)
    native = first.driver.native_root
    bound = native / "d-1"
    first.collect("d-1")
    assert not (bound / AUTH).exists()  # released between turns
    checkpoint = {"native_home": str(bound)}
    steering = "resume-" + "a" * 32
    for handle, stop in (("d-1-f1", "cancel"), (steering, "destroy")):
        first.resume({"dispatch_id": handle}, "more", tmp_path, checkpoint)
        assert (bound / AUTH).is_file()  # leased for the resumed turn
        (bound / AUTH).write_text('{"tokens": "' + handle + '"}')  # the CLI refreshed it
        # the worker restarts: a new driver and port over the same journal and scoped copy
        restarted = SeededCodexPort(_JournalDriver(tmp_path), home)  # type: ignore[arg-type]
        assert restarted._resumed == {}
        getattr(restarted, stop)(handle)
        assert not (bound / AUTH).exists(), handle  # nothing at rest in the bound home
        assert (home / AUTH).read_text() == '{"tokens": "' + handle + '"}'  # written back
        assert not (native / handle).exists()  # no guessed home was touched or created


def test_a_port_never_releases_a_home_its_journal_does_not_name(tmp_path: Path) -> None:
    home = _scoped(tmp_path)
    driver = _JournalDriver(tmp_path)
    port = SeededCodexPort(driver, home)  # type: ignore[arg-type]
    # a credential file in native_root/<handle> for a handle that has no journal stays put
    guessed = driver.native_root / "no-journal" / AUTH
    guessed.parent.mkdir(parents=True)
    guessed.write_text('{"tokens": "elsewhere"}')
    port._release("no-journal")
    assert guessed.read_text() == '{"tokens": "elsewhere"}'
    # a journal whose native_home lies outside the driver's native root is not followed
    outside = tmp_path / "outside"
    (outside / ".codex").mkdir(parents=True)
    (outside / AUTH).write_text('{"tokens": "outside"}')
    driver.journal.create("d-2", {"prompt": "x"})
    driver.journal.update("d-2", native_home=str(outside))
    SeededCodexPort(_JournalDriver(tmp_path), home)._release("d-2")  # type: ignore[arg-type]
    assert (outside / AUTH).is_file() and (home / AUTH).read_text() == '{"tokens": "old"}'


def test_a_failed_prepare_leaves_no_credential_at_rest(tmp_path: Path) -> None:
    home = _scoped(tmp_path)

    class Refusing(_JournalDriver):
        def prepare(self, dispatch: dict[str, Any], *a: Any, **kw: Any) -> Any:
            raise Hold("DRIVER_UNQUALIFIED", "refused before the journal")

    driver = Refusing(tmp_path)
    port = SeededCodexPort(driver, home)  # type: ignore[arg-type]
    with pytest.raises(Hold):
        port.prepare({"dispatch_id": "d-3"}, "task", tmp_path)
    assert not (driver.native_root / "d-3" / AUTH).exists()


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
