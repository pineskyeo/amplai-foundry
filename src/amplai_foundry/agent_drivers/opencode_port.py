"""One ``opencode serve`` per dispatch, bound to that run's workspace (specs/031-opencode-driver).

The registry holds one port per admitted profile, while the worker materializes a new workspace
per run (runtime/execution/worker.py). ``PerDispatchOpenCodePort`` bridges the two: it launches a
server for each dispatch on that dispatch's workspace through an ``OpenCodeServerLauncher``,
builds an ``OpenCodeDriver`` against the server's exec transport with the server password on
(design 11:36 "password/auth explicitly on"; D-091 records that the agent can read it), and stops
the server once the driver has confirmed a stopped process.

The container launcher itself is pending an operator decision on the execution image
(design.md D3); ``PendingDockerLauncher`` holds until then.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from ..runtime.errors import Hold
from .http import OpenCodeDriver
from .protocol import SessionJournal

AUTH = Path(".local") / "share" / "opencode" / "auth.json"
CATALOG = Path(".cache") / "opencode" / "models.json"
PORT = 4096


class OpenCodeServer(Protocol):
    """A started server for exactly one dispatch."""

    base_url: str
    transport: httpx.BaseTransport
    password: str | None  # None: no server password (D-087, on hold; design.md Auth Options)

    def workspace(self) -> str: ...  # the workspace the server actually serves
    def boundary(self, session: str) -> bool: ...  # idle and no tool process left
    def stop(self) -> None: ...


class OpenCodeServerLauncher(Protocol):
    def launch(self, dispatch_id: str, workspace: Path, native_home: Path) -> OpenCodeServer: ...


class PendingDockerLauncher:
    """Stub: which image runs OpenCode is an open decision (design.md D3)."""

    def launch(self, dispatch_id: str, workspace: Path, native_home: Path) -> OpenCodeServer:
        raise Hold(
            "OPENCODE_LAUNCHER_PENDING",
            "The OpenCode execution image is an open decision (specs/031-opencode-driver D3)",
        )


class ScopedOpenCodeHome:
    """The operator's scoped OpenCode data copy, seeded into per-dispatch homes.

    Only ``auth.json`` and the public model catalog are copied. The run copy of ``auth.json`` is
    removed when the run stops. Whether OpenCode rotates the provider credential is unknown, so
    nothing is written back (design.md Credential Handling).
    """

    def __init__(self, credential_home: Path) -> None:
        home = Path(credential_home).absolute()
        if home.resolve() != home or not (home / AUTH).is_file() or not (home / CATALOG).is_file():
            raise Hold(
                "OPENCODE_CREDENTIAL",
                "Scoped copy (sandbox_up.sh --opencode-home) with auth.json and models.json needed",
            )
        real = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        if home == Path.home().resolve() or home.resolve() == real.resolve():
            raise Hold("OPENCODE_CREDENTIAL", "Use a scoped copy, never the real data dir")
        self.home = home
        self._lock = threading.Lock()

    def seed(self, run_home: Path) -> None:
        for rel in (AUTH, CATALOG):
            (run_home / rel).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = run_home / rel
            if target.is_symlink():
                raise Hold("OPENCODE_CREDENTIAL", "Run home entry is a symlink")
            with self._lock:
                data = (self.home / rel).read_bytes()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            # colima maps the bind owner for the container uid; the 0700 parents keep others out
            target.chmod(0o644)

    def release(self, run_home: Path) -> None:
        target = run_home / AUTH
        if target.is_file() and not target.is_symlink():
            target.unlink()


@dataclass
class _Run:
    driver: OpenCodeDriver
    server: OpenCodeServer
    home: Path
    stopped: bool = False


DriverFactory = Callable[[OpenCodeServer], OpenCodeDriver]


class PerDispatchOpenCodePort:
    """AgentDriverPort that owns one OpenCode server per dispatch."""

    strategies = frozenset({"direct", "bounded_loop", "deliberative", "discovery"})
    driver_id = "opencode-server"  # design.md D2 keeps the existing OpenCodePort id

    def __init__(
        self,
        *,
        version: str,
        launcher: OpenCodeServerLauncher,
        credential: ScopedOpenCodeHome,
        journal: SessionJournal,
        native_root: Path,
        provider_id: str,
        model_id: str,
        driver_factory: DriverFactory | None = None,
    ) -> None:
        self.version = version
        self.launcher, self.credential, self.journal = launcher, credential, journal
        self.native_root = Path(native_root).absolute()
        self.provider_id, self.model_id = provider_id, model_id
        self.driver_factory = driver_factory or self._driver
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def _driver(self, server: OpenCodeServer) -> OpenCodeDriver:
        if not server.password:
            raise Hold("OPENCODE_AUTH", "The OpenCode server runs with a password (design 11:36)")
        return OpenCodeDriver(
            server.base_url,
            "opencode",
            server.password,
            self.journal,
            provider_id=self.provider_id,
            model_id=self.model_id,
            qualified=True,
            transport=server.transport,
            allow_local=True,
            expected_version=self.version,
            boundary_probe=server.boundary,
            subscribe=True,  # D1: SSE events, REST reads only as recovery
        )

    def _home(self, dispatch_id: str) -> Path:
        self.journal._path(dispatch_id)  # validate the id before joining a path
        return self.native_root / dispatch_id

    def _run(self, handle: str) -> _Run:
        run = self._runs.get(handle)
        if run is None:
            raise Hold(
                "OPENCODE_SERVER_LOST",
                "No server for this dispatch in this process; reconcile its container",
            )
        return run

    def _launch(self, dispatch_id: str, workspace: Path, home: Path) -> _Run:
        with self._lock:
            if dispatch_id in self._runs:
                return self._runs[dispatch_id]
            self.credential.seed(home)
            try:
                server = self.launcher.launch(dispatch_id, Path(workspace).resolve(), home)
            except Exception:
                self.credential.release(home)
                raise
            if server.workspace() != str(Path(workspace).resolve()):
                server.stop()
                self.credential.release(home)
                raise Hold(
                    "OPENCODE_WORKSPACE", "Server is not bound to this exact isolated run workspace"
                )
            run = _Run(self.driver_factory(server), server, home)
            self._runs[dispatch_id] = run
            return run

    def _stop(self, handle: str) -> None:
        run = self._runs.get(handle)
        if run is None or run.stopped:
            return
        run.driver.close()
        run.server.stop()
        self.credential.release(run.home)
        run.stopped = True

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]:
        did = dispatch["dispatch_id"]
        run = self._launch(did, workspace, self._home(did))
        run.driver.prepare(dispatch, prompt)
        return {"dispatch": dispatch, "prompt": prompt}

    def start(self, prepared: dict[str, Any]) -> str:
        did: str = prepared["dispatch"]["dispatch_id"]
        self._run(did).driver.start(prepared["dispatch"], prepared["prompt"])
        return did

    def poll(self, handle: str) -> dict[str, Any]:
        return self._run(handle).driver.poll(handle)

    def cancel(self, handle: str) -> dict[str, Any]:
        result = self._run(handle).driver.cancel(handle)
        if result.get("process_stopped") is True:
            self._stop(handle)
        return result

    def pause(self, handle: str) -> dict[str, Any]:
        result = self._run(handle).driver.pause(handle)
        if result.get("process_stopped") is True:
            self._stop(handle)  # no server or credential at rest while paused
        return result

    def checkpoint(self, handle: str) -> dict[str, Any]:
        run = self._run(handle)
        return {**run.driver.checkpoint(handle), "native_home": str(run.home)}

    def collect(self, handle: str) -> dict[str, Any]:
        receipt = self._run(handle).driver.collect(handle)
        self._stop(handle)
        return receipt

    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str:
        """Relaunch on the paused session's home. Session restore after a restart is unmeasured
        (design.md R3); the driver's own checks hold if the native session is not there."""
        home = Path(checkpoint["native_home"])
        if home.parent != self.native_root:
            raise Hold("RESUME_HOME", "Checkpoint home is outside this port's native root")
        did = dispatch["dispatch_id"]
        run = self._launch(did, workspace, home)
        native = {k: v for k, v in checkpoint.items() if k != "native_home"}
        try:
            run.driver.resume(dispatch, prompt, native)
        except Exception:
            self._stop(did)
            raise
        return str(did)

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        return self._run(handle).driver.steer(handle, event)

    def destroy(self, handle: str) -> None:
        run = self._run(handle)
        run.driver.destroy(handle)  # holds unless the driver confirmed a stopped process
        self._stop(handle)
        with self._lock:
            self._runs.pop(handle, None)
