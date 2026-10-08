"""AgentDriver ports and exact profile registration, independent of model names.

A model (including Astra) is a ModelProfile, not a transport. Ports own native
protocol details; WorkCoordinator owns the execution envelope and verification
handoff. Registration is an administrator action, never a worker self-approval.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from ..runtime.contracts.identity import digest
from ..runtime.contracts.registry import strict_json_loads
from ..runtime.errors import Conflict, Hold, RuntimeFault
from ..sandbox.local import DataSandbox
from .protocol import SessionJournal

if TYPE_CHECKING:
    from ..runtime.contracts.authority import Actor
    from ..runtime.storage.store import Scope, Store
    from .cli import CliDriver
    from .http import OpenCodeDriver

UNKNOWN_USAGE = {
    "input_tokens": None,
    "output_tokens": None,
    "cost_microunits": None,
    "currency": "USD",
    "status": "unknown",
    "source_ref": None,
}
ZERO_USAGE = {
    **UNKNOWN_USAGE,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_microunits": 0,
    "status": "measured",
}


@runtime_checkable
class AgentDriverPort(Protocol):
    driver_id: str
    version: str
    strategies: frozenset[str]

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]: ...
    def start(self, prepared: dict[str, Any]) -> str: ...
    def poll(self, handle: str) -> dict[str, Any]: ...
    def cancel(self, handle: str) -> dict[str, Any]: ...
    def pause(self, handle: str) -> dict[str, Any]: ...
    def checkpoint(self, handle: str) -> dict[str, Any]: ...
    def collect(self, handle: str) -> dict[str, Any]: ...
    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str: ...
    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]: ...
    def destroy(self, handle: str) -> None: ...


class DriverRegistry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.entries: dict[tuple[str, ...], tuple[dict[str, Any], AgentDriverPort]] = {}

    def register(
        self, administrator: Actor, profile_ref: dict[str, Any], port: AgentDriverPort
    ) -> None:
        administrator.require("runtime.admin")
        if not isinstance(port, AgentDriverPort):
            raise RuntimeFault("DRIVER_PORT", "Driver does not implement the required lifecycle")
        profile = self.store.get(administrator.scope, "driver-capabilities", profile_ref)
        if profile["driver_id"] != port.driver_id or profile["driver_version"] != port.version:
            raise Hold(
                "DRIVER_BINARY_BINDING", "Port identity/version differs from the admitted profile"
            )
        if profile["maturity"] != "qualified":
            raise Hold(
                "DRIVER_UNQUALIFIED",
                "Experimental/disabled ports cannot be registered for unattended work",
            )
        key = (*administrator.scope.keys(), digest(profile_ref))
        if key in self.entries:
            raise Conflict(
                "DRIVER_REGISTERED", "Registry entries are immutable in an owner process"
            )
        self.entries[key] = (profile_ref, port)
        with self.store.tx() as db:
            self.store.event(
                db,
                administrator.scope,
                "driver",
                profile["driver_id"],
                "driver.registered",
                {
                    "profile_ref": profile_ref,
                    "port_version": port.version,
                    "actor": administrator.subject_id,
                },
            )

    def installed(self, scope: Scope, profile_ref: dict[str, Any]) -> AgentDriverPort | None:
        """The port registered for the exact scoped profile, or None (no strategy check; for a
        check that runs before any claim, such as the web-tools declaration of a trial)."""
        value = self.entries.get((*scope.keys(), digest(profile_ref)))
        return None if value is None else value[1]

    def resolve(self, scope: Scope, profile_ref: dict[str, Any], strategy: str) -> AgentDriverPort:
        value = self.entries.get((*scope.keys(), digest(profile_ref)))
        if value is None:
            raise Hold("DRIVER_NOT_INSTALLED", "No port for the exact scoped driver profile")
        port = value[1]
        if strategy not in port.strategies:
            raise Hold("DRIVER_STRATEGY", "Do not downgrade an unsupported execution strategy")
        return port


class RecipePort:
    """Actual deterministic declarative execution; explicitly NOT a model agent."""

    driver_id = "local-recipe"
    version = "3.0.0"
    strategies = frozenset({"direct", "bounded_loop"})
    # operator decision 2026-10-08 (agent_drivers/offline.py): no model, no tools to turn off
    offline_tools: ClassVar[dict[str, str]] = {"no_tools": "deterministic recipe; no model agent"}

    def __init__(self, journal: SessionJournal) -> None:
        self.journal = journal

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]:
        recipe = strict_json_loads(prompt)
        if not isinstance(recipe, dict) or set(recipe) != {"operations"}:
            raise Hold("RECIPE_SHAPE", "Declarative worker accepts operations only")
        if len(recipe["operations"]) > 1024:
            raise Hold("RECIPE_LIMIT", "Too many bounded operations")
        prepared = {
            "dispatch_id": dispatch["dispatch_id"],
            "run_id": dispatch["run_id"],
            "workspace": str(Path(workspace).resolve()),
            "recipe": recipe,
        }
        self.journal.create(dispatch["dispatch_id"], prepared)
        return prepared

    def start(self, prepared: dict[str, Any]) -> str:
        did: str = prepared["dispatch_id"]
        record = self.journal.read(did)
        if record["request_digest"] != digest(prepared):
            raise Conflict("RECIPE_REPLAY", "Recipe changed after preparation")
        if record["state"] != "prepared":
            return did
        self.journal.transition(
            did, {"prepared"}, "starting", expected_version=record["row_version"]
        )
        session = "recipe:" + prepared["run_id"]
        try:
            self.journal.transition(
                did, {"starting"}, "running", session_handle=session, process_stopped=False
            )
            DataSandbox(Path(prepared["workspace"])).execute(prepared["recipe"]["operations"])
            self.journal.append(
                did,
                "recipe-completed",
                {
                    "session_handle": session,
                    "operation_count": len(prepared["recipe"]["operations"]),
                },
            )
            self.journal.transition(
                did, {"running"}, "completed", usage=ZERO_USAGE, process_stopped=True
            )
        except Exception as exc:
            self.journal.update(
                did,
                state="failed",
                failure=getattr(exc, "code", type(exc).__name__),
                process_stopped=True,
            )
            raise
        return did

    def poll(self, handle: str) -> dict[str, Any]:
        return self.journal.read(handle)

    def cancel(self, handle: str) -> dict[str, Any]:
        record = self.journal.read(handle)
        if record["state"] in {"running", "starting"}:
            # No asynchronous interrupt claims for an in-progress synchronous operation.
            raise Hold("RECIPE_BUSY", "Wait for the current bounded data operation boundary")
        return {
            "process_stopped": True,
            "session_handle": record["session_handle"],
            "state": record["state"],
        }

    def pause(self, handle: str) -> dict[str, Any]:
        return self.cancel(handle)

    def checkpoint(self, handle: str) -> dict[str, Any]:
        record = self.journal.read(handle)
        if record.get("process_stopped") is not True:
            raise Hold("CHECKPOINT_UNCONFIRMED", "Recipe is not at a boundary")
        return {
            "session_handle": record["session_handle"],
            "journal_digest": digest(record),
            "driver_version": self.version,
        }

    def collect(self, handle: str) -> dict[str, Any]:
        record = self.journal.read(handle)
        if record["state"] != "completed":
            raise Hold("DRIVER_NOT_COMPLETE", "Recipe did not complete")
        return {
            "process_stopped": True,
            "session_handle": record["session_handle"],
            "usage": record["usage"],
            "provider_completed": True,
            "goal_verified": False,
        }

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        self.journal.read(handle)
        return {"status": "checkpoint_required", "native_applied": False}

    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str:
        raise Hold(
            "NEW_SESSION_REQUIRED",
            "Declarative operations use bounded new attempts, not private native resume",
        )

    def destroy(self, handle: str) -> None:
        self.cancel(handle)


class CliPort:
    strategies = frozenset({"direct", "bounded_loop", "deliberative", "discovery"})

    def __init__(self, driver: CliDriver) -> None:
        self.driver = driver
        self.driver_id = driver.provider + "-cli"
        self.version = driver.version

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]:
        return self.driver.prepare(dispatch, prompt, workspace)

    def start(self, prepared: dict[str, Any]) -> str:
        return self.driver.start(prepared)

    def poll(self, handle: str) -> dict[str, Any]:
        return self.driver.poll(handle)

    def cancel(self, handle: str) -> dict[str, Any]:
        return self.driver.cancel(handle)

    def pause(self, handle: str) -> dict[str, Any]:
        return self.driver.pause(handle)

    def checkpoint(self, handle: str) -> dict[str, Any]:
        return self.driver.checkpoint(handle)

    def collect(self, handle: str) -> dict[str, Any]:
        return self.driver.collect(handle)

    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str:
        return self.driver.resume(dispatch, prompt, workspace, checkpoint)

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.driver.steer(handle, event)

    def destroy(self, handle: str) -> None:
        return self.driver.destroy(handle)


class OpenCodePort:
    """An isolated server must own the exact workspace, not a shared user's session."""

    strategies = frozenset({"direct", "bounded_loop", "deliberative", "discovery"})
    driver_id = "opencode-server"

    def __init__(
        self, driver: OpenCodeDriver, *, workspace: Path, workspace_probe: Callable[[], str]
    ) -> None:
        self.driver = driver
        self.version = driver.expected_version
        self.workspace = Path(workspace).absolute()
        self.workspace_probe = workspace_probe

    def prepare(self, dispatch: dict[str, Any], prompt: str, workspace: Path) -> dict[str, Any]:
        if Path(workspace).resolve() != self.workspace or self.workspace_probe() != str(
            self.workspace
        ):
            raise Hold(
                "OPENCODE_WORKSPACE", "Server is not bound to this exact isolated run workspace"
            )
        self.driver.prepare(dispatch, prompt)
        return {"dispatch": dispatch, "prompt": prompt}

    def start(self, prepared: dict[str, Any]) -> str:
        self.driver.start(prepared["dispatch"], prepared["prompt"])
        dispatch_id: str = prepared["dispatch"]["dispatch_id"]
        return dispatch_id

    def poll(self, handle: str) -> dict[str, Any]:
        return self.driver.poll(handle)

    def cancel(self, handle: str) -> dict[str, Any]:
        return self.driver.cancel(handle)

    def pause(self, handle: str) -> dict[str, Any]:
        return self.driver.pause(handle)

    def checkpoint(self, handle: str) -> dict[str, Any]:
        return self.driver.checkpoint(handle)

    def collect(self, handle: str) -> dict[str, Any]:
        return self.driver.collect(handle)

    def resume(
        self, dispatch: dict[str, Any], prompt: str, workspace: Path, checkpoint: dict[str, Any]
    ) -> str:
        if Path(workspace).resolve() != self.workspace or self.workspace_probe() != str(
            self.workspace
        ):
            raise Hold("OPENCODE_WORKSPACE", "Resume workspace changed")
        self.driver.resume(dispatch, prompt, checkpoint)
        dispatch_id: str = dispatch["dispatch_id"]
        return dispatch_id

    def steer(self, handle: str, event: dict[str, Any]) -> dict[str, Any]:
        return self.driver.steer(handle, event)

    def destroy(self, handle: str) -> None:
        return self.driver.destroy(handle)
