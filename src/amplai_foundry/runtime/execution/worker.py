"""Bounded worker coordinator: admitted dispatch -> native execution -> evidence.

No success authority lives here. VerificationService owns repair and completion.
Each dispatch is durably bound before I/O. An interrupted launching state is held
for reconciliation; at-least-once dispatch is not at-least-once native spawn.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ...agent_drivers.ports import UNKNOWN_USAGE, DriverRegistry
from ...agent_drivers.sessions import SessionStore
from ..contracts.authority import Actor
from ..contracts.identity import digest
from ..errors import Conflict, Hold, RuntimeFault
from .envelope import assert_execution_live, execution_envelope

USAGE_DETAIL_KIND = "usage-detail"


class Workspaces(Protocol):
    """What the coordinator needs from a workspace manager (content snapshots or git)."""

    def materialize(self, scope: Any, run_id: str, snapshot: dict[str, Any]) -> Path: ...

    def collect(
        self,
        scope: Any,
        workspace: str | Path,
        bindings: dict[str, str],
        node: dict[str, Any],
        *,
        process_stopped: bool,
    ) -> dict[str, dict[str, Any]]: ...

    def snapshot(self, scope: Any, directory: Path) -> dict[str, Any]: ...

    def assert_matches(
        self, scope: Any, directory: str | Path, snapshot: dict[str, Any]
    ) -> bool: ...


if TYPE_CHECKING:
    from .service import Runtime


class WorkCoordinator:
    def __init__(
        self,
        runtime: Runtime,
        registry: DriverRegistry,
        workspaces: Workspaces,
        *,
        poll_seconds: float = 0.05,
        max_seconds: float = 3600,
    ) -> None:
        if not 0 < poll_seconds <= 30 or not 0 < max_seconds <= 86400:
            raise RuntimeFault("WORKER_LIMITS", "Polling and execution must be bounded")
        self.runtime, self.store, self.registry, self.workspaces = (
            runtime,
            runtime.store,
            registry,
            workspaces,
        )
        self.sessions = SessionStore(self.store, runtime.contracts)
        self.poll_seconds, self.max_seconds = poll_seconds, max_seconds

    def _state(self, worker: Actor, did: str) -> dict[str, Any]:
        return self.store.head(worker.scope, "worker-execution", did)

    def _update(self, worker: Actor, did: str, state: str, **changes: Any) -> dict[str, Any] | None:
        with self.store.tx() as db:
            h = self.store.head(worker.scope, "worker-execution", did, db=db)
            if state == "held" and h["state"] in {"paused", "cancelled", "resuming"}:
                return h
            self.store.cas(
                db,
                worker.scope,
                "worker-execution",
                did,
                h["row_version"],
                state,
                {**h["data"], **changes},
            )
            self.store.event(
                db,
                worker.scope,
                "run",
                h["data"]["run_id"],
                "worker." + state,
                {"dispatch_id": did},
            )
        return None

    def _existing(self, worker: Actor, did: str, request_digest: str) -> dict[str, Any] | None:
        try:
            h = self._state(worker, did)
        except RuntimeFault as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise
        if (
            h["data"]["request_digest"] != request_digest
            or h["data"]["worker_id"] != worker.subject_id
        ):
            raise Conflict("EXECUTION_REPLAY", "Dispatch payload or owner changed")
        if h["state"] == "verifying":
            result: dict[str, Any] = h["data"]["result"]
            return result
        raise Hold(
            "EXECUTION_RECONCILE", "Existing dispatch may have started; do not spawn it again"
        )

    def execute(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        *,
        prompt: str,
        base_snapshot: dict[str, Any],
        output_paths: dict[str, str],
        planning_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker.require("worker.execute")
        did = dispatch["dispatch_id"]
        scope = worker.scope
        request = {
            "dispatch": dispatch,
            "prompt_digest": digest(prompt),
            "snapshot_digest": base_snapshot["digest"],
            "output_paths": output_paths,
            "planning_receipt": planning_receipt,
        }
        request_digest = digest(request)
        prior = self._existing(worker, did, request_digest)
        if prior:
            return prior
        envelope = execution_envelope(self.runtime, worker, dispatch)
        if envelope is None:
            raise RuntimeFault("EXECUTION_ENVELOPE", "Execution envelope was not constructed")
        port = self.registry.resolve(
            scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
        )
        if dispatch["node"]["strategy"] == "deliberative":
            # A planner's unsigned text is not an approval. Refer to an immutable
            # server-reviewed artifact, independent of this worker.
            if not planning_receipt:
                raise Hold(
                    "PLANNING_BOUNDARY", "Deliberative work requires a reviewed plan receipt"
                )
            review = self.store.get(scope, "planning-review", planning_receipt)
            if (
                review.get("contract_ref") != envelope["contract_ref"]
                or review.get("graph_ref") != envelope["graph_ref"]
                or review.get("verdict") != "pass"
                or review.get("reviewer_id") == worker.subject_id
            ):
                raise Hold(
                    "PLANNING_BOUNDARY",
                    "Plan review must be independent and bound to current revisions",
                )
        prompt_ref = self.runtime.artifacts.admit(
            scope, prompt.encode(), "text/plain", trust="worker"
        )
        with self.store.tx() as db:
            self.store.cas(
                db,
                scope,
                "worker-execution",
                did,
                0,
                "preparing",
                {
                    "run_id": dispatch["run_id"],
                    "worker_id": worker.subject_id,
                    "request_digest": request_digest,
                    "dispatch": dispatch,
                    "envelope": envelope,
                    "base_snapshot": base_snapshot,
                    "output_paths": output_paths,
                    "driver_handle": None,
                    "workspace": None,
                    "result": None,
                    "prompt_artifact": prompt_ref,
                },
            )
            self.store.event(
                db, scope, "run", dispatch["run_id"], "worker.preparing", {"dispatch_id": did}
            )
        workspace = self.workspaces.materialize(scope, dispatch["run_id"], base_snapshot)
        self._update(worker, did, "preparing", workspace=str(workspace))
        session = self.sessions.prepare(
            worker, did, envelope, dispatch["profile"], workspace_digest=base_snapshot["digest"]
        )
        prepared = port.prepare(dispatch, prompt, workspace)
        # Context/authority may have changed while preparing the sandbox.
        assert_execution_live(self.runtime, worker, dispatch)
        self._update(worker, did, "launching", prepared_digest=digest(prepared))
        handle = None
        bound = False
        sequence = 0
        last_heartbeat: float = 0
        start = time.monotonic()
        timeout = min(self.max_seconds, dispatch["node"]["budget"]["max_wall_seconds"])
        try:
            self.store.assert_outside_tx()
            handle = port.start(prepared)
            self._update(worker, did, "observing", driver_handle=handle)
            while True:
                if time.monotonic() - start >= timeout:
                    raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
                assert_execution_live(
                    self.runtime, worker, dispatch
                )  # Revocation & steering are checked during execution.
                if time.monotonic() - last_heartbeat >= 30:
                    sequence += 1
                    self.runtime.heartbeat(
                        worker,
                        dispatch["run_id"],
                        dispatch["lease"]["lease_id"],
                        dispatch["lease"]["fencing_token"],
                        sequence=sequence,
                    )
                    last_heartbeat = time.monotonic()
                observation = port.poll(handle)
                native = observation.get("session_handle")
                if native and not bound:
                    self.sessions.bind(worker, did, native, expected_version=session["row_version"])
                    self.runtime.start(worker, dispatch, native)
                    bound = True
                if observation["state"] == "completed":
                    break
                if observation["state"] in {
                    "failed",
                    "unknown",
                    "cancelled",
                    "paused",
                    "awaiting_tools",
                }:
                    raise Hold(
                        "DRIVER_BOUNDARY",
                        "Driver is not a successful completed turn",
                        details={"state": observation["state"]},
                    )
                time.sleep(self.poll_seconds)
            if not bound:
                raise Hold("SESSION_UNKNOWN", "Native session not established")
            receipt = port.collect(handle)
            if (
                receipt.get("process_stopped") is not True
                or receipt.get("provider_completed") is not True
            ):
                raise Hold(
                    "COLLECT_BOUNDARY",
                    "Driver receipt is not positive completion/termination evidence",
                )
            # Stop accepting old-generation outputs even if the provider finished.
            assert_execution_live(self.runtime, worker, dispatch)
            outputs = self.workspaces.collect(
                scope, workspace, output_paths, dispatch["node"], process_stopped=True
            )
            usage = self._usage(scope, dispatch["run_id"], receipt)
            self.runtime.contracts.validate(
                "run-record",
                {
                    **self.store.head(scope, "run", dispatch["run_id"])["data"]["record"],
                    "usage": usage,
                },
            )
            result = self.runtime.output_ready(
                worker,
                dispatch["run_id"],
                dispatch["lease"]["lease_id"],
                dispatch["lease"]["fencing_token"],
                outputs,
                usage=usage,
                process_stopped=True,
            )
            self._update(
                worker, did, "verifying", result=result, driver_receipt_digest=digest(receipt)
            )
            # Outputs are in the CAS; remove the stopped run's container (never before collect).
            with contextlib.suppress(Exception):
                port.destroy(handle)
            return result
        except Exception as exc:
            if getattr(exc, "code", None) == "EXECUTION_PAUSED" and handle is not None:
                # Steering: the controller stops this process at a boundary and checkpoints it
                # (SteeringService.quiesce -> stop_and_snapshot); cancelling here would lose the
                # session the operator's message resumes (D-082).
                self._update(worker, did, "pause_requested", hold_code="EXECUTION_PAUSED")
                raise
            stopped = False
            if handle is not None:
                with contextlib.suppress(Exception):
                    stopped = port.cancel(handle).get("process_stopped") is True
                if stopped:
                    with contextlib.suppress(Exception):
                        port.destroy(handle)
            self._update(
                worker,
                did,
                "held",
                hold_code=getattr(exc, "code", type(exc).__name__),
                process_stopped=stopped,
            )
            raise

    def stop_and_snapshot(self, worker: Actor, run_id: str, kind: str) -> dict[str, Any]:
        # Used by SteeringService.quiesce. Side effects must still be reconciled
        # there before a checkpoint is admitted or reservations are released.
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='worker-execution'",
                worker.scope.keys(),
            ).fetchall()
        matches = [
            (r["id"], self._state(worker, r["id"]))
            for r in rows
            if self._state(worker, r["id"])["data"]["run_id"] == run_id
        ]
        if len(matches) != 1:
            raise Hold("WORKER_SESSION_MISSING", "One exact dispatch is required for a run")
        did, h = matches[0]
        data = h["data"]
        if data["worker_id"] != worker.subject_id:
            raise Hold("WORKER_OWNER", "Another worker owns the process")
        port = self.registry.resolve(
            worker.scope,
            data["dispatch"]["profile"]["driver_profile_ref"],
            data["dispatch"]["node"]["strategy"],
        )
        handle = data["driver_handle"]
        if not handle:
            raise Hold("SPAWN_UNKNOWN", "Reconcile provider creation outcome before checkpoint")
        result = port.pause(handle) if kind == "pause" else port.cancel(handle)
        if result.get("process_stopped") is not True:
            return {"process_stopped": False}
        receipt = port.checkpoint(handle)
        snapshot = self.workspaces.snapshot(worker.scope, Path(data["workspace"]))
        self._update(
            worker,
            did,
            "paused" if kind == "pause" else "cancelled",
            snapshot=snapshot,
            driver_checkpoint=receipt,
        )
        return {
            "process_stopped": True,
            "session_handle": receipt["session_handle"],
            "workspace_diff_artifact": snapshot,
            "workspace_base_digest": data["base_snapshot"]["digest"],
        }

    def abort(self, worker: Actor, run_id: str) -> bool:
        """Stop a run's process that no controller pause took over (never leave it running).

        ``execute`` leaves a steering-paused process to the controller; when the controller
        cannot apply that steering (no request, no checkpoint, a failed resume), it calls this.
        True when the process is confirmed stopped.
        """
        did, h = self._run_execution(worker, run_id)
        data = h["data"]
        handle = data.get("driver_handle")
        if not handle:
            return True
        port = self.registry.resolve(
            worker.scope,
            data["dispatch"]["profile"]["driver_profile_ref"],
            data["dispatch"]["node"]["strategy"],
        )
        stopped = False
        with contextlib.suppress(Exception):
            stopped = port.cancel(handle).get("process_stopped") is True
        if stopped:
            with contextlib.suppress(Exception):
                port.destroy(handle)
        self._update(worker, did, "held", hold_code="CONTROLLER_ABORT", process_stopped=stopped)
        return stopped

    def _run_execution(self, worker: Actor, run_id: str) -> tuple[str, dict[str, Any]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='worker-execution'",
                worker.scope.keys(),
            ).fetchall()
        values = [
            (r["id"], self._state(worker, r["id"]))
            for r in rows
            if self._state(worker, r["id"])["data"]["run_id"] == run_id
        ]
        if len(values) != 1:
            raise Hold("SESSION_LOOKUP", "Expected one durable execution for run")
        did, h = values[0]
        if h["data"]["worker_id"] != worker.subject_id:
            raise Hold("SESSION_OWNER", "Wrong process owner")
        return did, h

    def resume_exact(
        self,
        worker: Actor,
        run_id: str,
        checkpoint: dict[str, Any],
        *,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """Callback for SteeringService.resume after authority/resource preflight.

        Returns only exact provider session acceptance. New effects remain blocked
        until SteeringService commits the new fence. Unknown transport outcome is
        held and cannot send the same native resume twice.
        """
        from copy import deepcopy

        did, h = self._run_execution(worker, run_id)
        data = h["data"]
        run = self.store.head(worker.scope, "run", run_id)
        if h["state"] != "paused" or not run["data"].get("resume_pending"):
            raise Hold("RESUME_NOT_ADMITTED", "Controller must prepare a scoped resume first")
        if data["snapshot"] != checkpoint["workspace_diff_artifact"]:
            raise Hold("CHECKPOINT_BINDING", "Driver and controller checkpoints differ")
        self.workspaces.assert_matches(worker.scope, Path(data["workspace"]), data["snapshot"])
        port = self.registry.resolve(
            worker.scope,
            data["dispatch"]["profile"]["driver_profile_ref"],
            data["dispatch"]["node"]["strategy"],
        )
        request = deepcopy(data["dispatch"])
        request["dispatch_id"] = (
            "resume-" + digest({"run_id": run_id, "pending": run["data"]["resume_pending"]})[7:39]
        )
        if prompt is None:  # a plain resume repeats the original turn's prompt
            prompt = self.runtime.artifacts.read(worker.scope, data["prompt_artifact"]).decode()
        self._update(worker, did, "resuming", resume_dispatch_id=request["dispatch_id"])
        self.store.assert_outside_tx()
        handle = port.resume(request, prompt, Path(data["workspace"]), data["driver_checkpoint"])
        self._update(worker, did, "resuming", driver_handle=handle)
        start = time.monotonic()
        while time.monotonic() - start < min(30, self.max_seconds):
            observation = port.poll(handle)
            session = observation.get("session_handle")
            if session:
                if session != checkpoint["driver_session_handle"]:
                    try:
                        port.cancel(handle)
                    finally:
                        raise Hold("RESUME_SESSION", "Provider resumed a different native session")
                return {"resumed": True, "session_handle": session}
            if observation["state"] in {"failed", "unknown", "cancelled"}:
                break
            time.sleep(self.poll_seconds)
        raise Hold(
            "RESUME_UNCONFIRMED",
            "No exact native session receipt; reconcile before another attempt",
        )

    def _usage(self, scope: Any, run_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """The run record's usage (3.0.0 shape); the provider's breakdown is stored beside it.

        D-094: cache and reasoning tokens do not fit the closed usage object, so the breakdown is
        a ``usage-detail`` record and ``usage.source_ref`` points at it. An earlier source (the
        Claude CLI cost event) is kept inside the record as ``cost_source_ref``.
        """
        usage = dict(receipt.get("usage") or UNKNOWN_USAGE.copy())
        detail = receipt.get("usage_detail")
        if not isinstance(detail, dict):
            return usage
        record_id = "usage-detail-" + digest({"run_id": run_id, "detail": detail})[7:39]
        value = {
            "usage_detail_id": record_id,
            "run_id": run_id,
            **detail,
            "cost_source_ref": usage.get("source_ref"),
        }
        with self.store.tx() as db:  # the same detail for the same run is the same object
            ref = self.store.put(db, scope, USAGE_DETAIL_KIND, record_id, 1, value)
        usage["source_ref"] = {k: ref[k] for k in ("id", "revision", "digest")}
        return usage

    def continue_resumed(self, worker: Actor, run_id: str) -> dict[str, Any]:
        """Collect a resumed turn only after the controller commits the new lease."""
        from copy import deepcopy

        did, h = self._run_execution(worker, run_id)
        data = h["data"]
        run = self.store.head(worker.scope, "run", run_id)
        if (
            h["state"] != "resuming"
            or run["state"] != "running"
            or run["data"].get("resume_pending")
        ):
            raise Hold(
                "RESUME_NOT_COMMITTED", "Resume must be committed before effects or collection"
            )
        dispatch = deepcopy(data["dispatch"])
        dispatch["lease"] = run["data"]["lease"]
        port = self.registry.resolve(
            worker.scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
        )
        handle = data["driver_handle"]
        start = time.monotonic()
        last: float = 0
        seq = 0
        try:
            while time.monotonic() - start < min(
                self.max_seconds, dispatch["node"]["budget"]["max_wall_seconds"]
            ):
                assert_execution_live(self.runtime, worker, dispatch)
                if time.monotonic() - last >= 30:
                    seq += 1
                    last = time.monotonic()
                    self.runtime.heartbeat(
                        worker,
                        run_id,
                        dispatch["lease"]["lease_id"],
                        dispatch["lease"]["fencing_token"],
                        sequence=seq,
                    )
                observation = port.poll(handle)
                if observation.get("session_handle") != run["data"]["session_handle"]:
                    raise Hold("SESSION_REBIND", "Resumed stream changed exact session")
                if observation["state"] == "completed":
                    break
                if observation["state"] in {
                    "failed",
                    "unknown",
                    "cancelled",
                    "paused",
                    "awaiting_tools",
                }:
                    raise Hold("RESUMED_BOUNDARY", "Resumed turn needs reconciliation")
                time.sleep(self.poll_seconds)
            else:
                raise Hold("WORKER_DEADLINE", "Resumed turn exceeded its remaining wall budget")
            receipt = port.collect(handle)
            if (
                receipt.get("process_stopped") is not True
                or receipt.get("provider_completed") is not True
            ):
                raise Hold(
                    "COLLECT_BOUNDARY",
                    "Resumed process and completed turn require positive evidence",
                )
            assert_execution_live(self.runtime, worker, dispatch)
            outputs = self.workspaces.collect(
                worker.scope,
                Path(data["workspace"]),
                data["output_paths"],
                dispatch["node"],
                process_stopped=True,
            )
            result = self.runtime.output_ready(
                worker,
                run_id,
                dispatch["lease"]["lease_id"],
                dispatch["lease"]["fencing_token"],
                outputs,
                usage=self._usage(worker.scope, run_id, receipt),
                process_stopped=True,
            )
            self._update(worker, did, "verifying", dispatch=dispatch, result=result)
            return result
        except Exception as exc:
            if getattr(exc, "code", None) == "EXECUTION_PAUSED":
                # steering again during a resumed turn: as in execute, the controller stops
                # this process at a boundary and resumes the same session (D-082)
                self._update(worker, did, "pause_requested", hold_code="EXECUTION_PAUSED")
                raise
            stopped = False
            with contextlib.suppress(Exception):
                stopped = port.cancel(handle).get("process_stopped") is True
            self._update(
                worker,
                did,
                "held",
                hold_code=getattr(exc, "code", type(exc).__name__),
                process_stopped=stopped,
            )
            raise
