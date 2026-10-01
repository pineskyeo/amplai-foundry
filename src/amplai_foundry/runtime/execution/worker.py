"""Bounded worker coordinator: admitted dispatch -> native execution -> evidence.

No success authority lives here. VerificationService owns repair and completion.
Each dispatch is durably bound before I/O. An interrupted launching state is held
for reconciliation; at-least-once dispatch is not at-least-once native spawn.

Follow-up turns (Work 033 S9, interfaces.md §5.1 M3, IC-04): after a completed turn and before
``output_ready``, turn hooks may resume the same native session with a message as dispatch
``<dispatch_id>-f<k>`` (k = 1, 2) on the same run, lease and bound session, inside ``execute``
(never through the controller's ``resume_pending`` steering path). Each follow-up runs
collect -> checkpoint -> resume -> poll -> collect, so a seeded credential is released at every
collect and seeded again at every resume; ``destroy`` runs only after the last turn. An execution
interrupted inside a follow-up is held, never re-sent (``_existing``: EXECUTION_RECONCILE); its
recovery is §14 Q16 (a). Vote candidates (M4) are not built (§14 Q16 (b)).
"""

from __future__ import annotations

import contextlib
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from ...agent_drivers.ports import UNKNOWN_USAGE, DriverRegistry
from ...agent_drivers.sessions import SessionStore
from ..contracts.authority import Actor
from ..contracts.identity import digest
from ..errors import Conflict, Hold, RuntimeFault
from .cells import DispatchOptions, check_binding, profile_effort
from .envelope import assert_execution_live, execution_envelope

USAGE_DETAIL_KIND = "usage-detail"
MAX_FOLLOWUPS = 2  # §5.1 M3: at most 2 follow-up turns per attempt
STOPPED_STATES = frozenset({"failed", "unknown", "cancelled", "paused", "awaiting_tools"})


def _hooks_followups(hooks: Any) -> int:
    """The follow-up cap of ``hooks`` (0 without hooks). Candidates (M4, vote) are held until
    §14 Q16 is answered: hooks asking for more than one candidate are refused."""
    if hooks is None:
        return 0
    followups, candidates = getattr(hooks, "max_followups", 0), getattr(hooks, "candidates", 1)
    if type(followups) is not int or not 0 <= followups <= MAX_FOLLOWUPS:
        raise RuntimeFault("WORKER_HOOKS", "Turn hooks allow 0..2 follow-up turns")
    if candidates != 1:
        raise RuntimeFault(
            "COMPONENT_CONTENT", "Vote candidates (M4) are held until §14 Q16 is answered"
        )
    return followups


def sum_usage(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """The usage of several turns of one run: token counts and costs summed per turn; unknown
    when a turn did not report both counts; ``estimated`` when a turn was estimated. The detail
    breakdown (D-094) is summed per field when every turn reports the same provider's."""
    usages = [r.get("usage") or {} for r in receipts]
    counted = all(
        type(u.get("input_tokens")) is int and type(u.get("output_tokens")) is int for u in usages
    )
    if not counted:
        return dict(UNKNOWN_USAGE)
    statuses = {u.get("status") for u in usages}
    status = "measured" if statuses == {"measured"} else "estimated"
    costs = [c for u in usages if type(c := u.get("cost_microunits")) is int]
    total: dict[str, Any] = {
        **UNKNOWN_USAGE,
        "input_tokens": sum(int(u["input_tokens"]) for u in usages),
        "output_tokens": sum(int(u["output_tokens"]) for u in usages),
        "cost_microunits": sum(costs) if len(costs) == len(usages) else None,
        "status": status,
    }  # fmt: skip
    details = [r.get("usage_detail") for r in receipts]
    out: dict[str, Any] = {"usage": total}
    if all(isinstance(d, dict) for d in details):
        first = cast(dict[str, Any], details[0])
        same = all(
            isinstance(d, dict)
            and d.get("provider") == first.get("provider")
            and d.get("input_includes_cache") == first.get("input_includes_cache")
            for d in details
        )
        if same:
            keys = set.intersection(*(set((d or {}).get("fields") or {}) for d in details))
            out["usage_detail"] = {
                **first,
                "fields": {k: sum(int((d or {})["fields"][k]) for d in details) for k in keys},
            }
    return out


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

    def _check_options(
        self, worker: Actor, dispatch: dict[str, Any], port: Any, options: DispatchOptions | None
    ) -> DispatchOptions | None:
        """Work 033 S4 (interfaces.md §3.4): the options a port receives, checked against the
        activated model profile before anything is prepared.

        A port without ``accepts_options`` receives none, and non-default options for it hold
        DRIVER_OPTIONS_UNSUPPORTED. A port that takes options but got none runs the profile's
        model at the provider default, so an effort profile holds DISPATCH_OPTIONS_BINDING
        rather than run without its effort (never substituted, spec.md Constraints).
        """
        profile = dispatch["profile"]
        accepts = getattr(port, "accepts_options", False) is True
        if options is not None:
            if not accepts:
                if options.is_default():
                    return None
                raise Hold(
                    "DRIVER_OPTIONS_UNSUPPORTED", "This driver port takes no dispatch options",
                    details={"driver_id": getattr(port, "driver_id", None)},
                )  # fmt: skip
            check_binding(self.store, worker.scope, profile, options)
            return options
        if accepts:
            model = self.store.get(worker.scope, "model-profile", profile["model_profile_ref"])
            if profile_effort(model) is not None:
                raise Hold(
                    "DISPATCH_OPTIONS_BINDING", "An effort profile needs its dispatch options",
                    details={"effort": model.get("reasoning_profile")},
                )  # fmt: skip
        return None

    def _limit(self, deadline_seconds: float | None) -> float:
        """The wall limit of one call: its deadline when given, else ``max_seconds``."""
        if deadline_seconds is None:
            return self.max_seconds
        if isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, int | float):
            raise RuntimeFault("WORKER_LIMITS", "A call deadline is a number of seconds")
        if not deadline_seconds > 0:
            raise RuntimeFault("WORKER_LIMITS", "A call deadline must be positive")
        return float(deadline_seconds)

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

    # -- turns of one attempt (Work 033 S9, M3) ---------------------------------------------------
    def _beat(self, worker: Actor, dispatch: dict[str, Any], beat: dict[str, Any]) -> None:
        """A lease heartbeat every 30 s across all turns and hooks of the attempt."""
        if time.monotonic() - beat["last"] >= 30:
            beat["sequence"] += 1
            self.runtime.heartbeat(
                worker,
                dispatch["run_id"],
                dispatch["lease"]["lease_id"],
                dispatch["lease"]["fencing_token"],
                sequence=beat["sequence"],
            )
            beat["last"] = time.monotonic()

    def _after_turn(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        hooks: Any,
        workspace: Path,
        turn: int,
        receipt: dict[str, Any],
        *,
        start: float,
        timeout: float,
        beat: dict[str, Any],
    ) -> str | None:
        """``hooks.after_turn`` (a host-side read-only turn may take minutes) while this thread
        keeps the lease alive and checks revocation and steering, as during a native turn.

        When the attempt stops while the hook still runs (deadline, revocation, steering), the
        hook is told so (``hooks.abandon()`` when it has one) before the exception leaves: its
        late result is then dropped instead of being written after the loop moved on."""
        box: dict[str, Any] = {}

        def call() -> None:
            try:
                box["message"] = hooks.after_turn(workspace=workspace, turn=turn, receipt=receipt)
            except BaseException as exc:  # handed to the worker thread below
                box["error"] = exc

        thread = threading.Thread(target=call, name="amplai-turn-hook", daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                if time.monotonic() - start >= timeout:
                    raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
                assert_execution_live(self.runtime, worker, dispatch)
                self._beat(worker, dispatch, beat)
                thread.join(self.poll_seconds)
        except BaseException:
            abandon = getattr(hooks, "abandon", None)
            if callable(abandon):
                abandon()
            raise
        if "error" in box:
            raise box["error"]
        message = box.get("message")
        if message is None:
            return None
        if not isinstance(message, str):
            raise RuntimeFault("WORKER_HOOKS", "A follow-up message is text")
        return message.strip() or None

    def _followup(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        port: Any,
        handles: list[str],
        k: int,
        message: str,
        workspace: Path,
        *,
        session: str,
        options: DispatchOptions | None,
        start: float,
        timeout: float,
        beat: dict[str, Any],
        followups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Follow-up ``k``: checkpoint the finished turn, resume the bound session as dispatch
        ``<dispatch_id>-f<k>`` (same run id, lease and fencing token), observe it to a completed
        turn and collect it. Every observed session must be the bound one (SESSION_REBIND).

        The new handle joins ``handles`` as soon as the resume returns, so a failure while it
        runs stops that process (``execute`` cancels the last handle), not the finished one."""
        did = dispatch["dispatch_id"]
        follow = deepcopy(dispatch)
        follow["dispatch_id"] = f"{did}-f{k}"
        assert_execution_live(self.runtime, worker, dispatch)
        self._beat(worker, dispatch, beat)
        checkpoint = port.checkpoint(handles[-1])
        if checkpoint.get("session_handle") != session:
            raise Hold("SESSION_REBIND", "A follow-up resumes only the bound session")
        entry: dict[str, Any] = {
            "dispatch_id": follow["dispatch_id"], "prompt_digest": digest(message),
            "receipt_digest": None, "usage": None,
        }  # fmt: skip
        followups.append(entry)
        # recorded before the resume: an execution interrupted from here on is held (Q16 (a))
        self._update(
            worker, did, "observing", followups=deepcopy(followups),
            followup_dispatch_id=follow["dispatch_id"],
        )  # fmt: skip
        self.store.assert_outside_tx()
        new = (
            cast(Any, port).resume(follow, message, workspace, checkpoint, options=options)
            if options is not None
            else port.resume(follow, message, workspace, checkpoint)
        )
        handles.append(new)
        self._update(worker, did, "observing", driver_handle=new)
        while True:
            if time.monotonic() - start >= timeout:
                raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
            assert_execution_live(self.runtime, worker, dispatch)
            self._beat(worker, dispatch, beat)
            observation = port.poll(new)
            native = observation.get("session_handle")
            if native and native != session:
                raise Hold("SESSION_REBIND", "A follow-up stream changed the exact session")
            if observation["state"] == "completed":
                break
            if observation["state"] in STOPPED_STATES:
                raise Hold(
                    "DRIVER_BOUNDARY", "Driver is not a successful completed turn",
                    details={"state": observation["state"], "followup": k},
                )  # fmt: skip
            time.sleep(self.poll_seconds)
        receipt: dict[str, Any] = port.collect(new)
        if (
            receipt.get("process_stopped") is not True
            or receipt.get("provider_completed") is not True
        ):
            raise Hold(
                "COLLECT_BOUNDARY", "Driver receipt is not positive completion/termination evidence"
            )
        if receipt.get("session_handle") not in (None, session):
            raise Hold("SESSION_REBIND", "A follow-up receipt names another session")
        entry.update(receipt_digest=digest(receipt), usage=receipt.get("usage"))
        self._update(worker, did, "observing", followups=deepcopy(followups))
        assert_execution_live(self.runtime, worker, dispatch)
        return receipt

    def _turns_usage(
        self, scope: Any, run_id: str, port: Any, receipts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The run's usage over its turns (interfaces.md §5.3, M3 rule 5).

        One turn: its receipt, as before S9. With follow-ups, a Codex session keeps the rule of a
        resumed Codex turn (the last receipt only, ``continue_resumed``) until §14 Q4 says whether
        its usage is cumulative; other drivers sum the turns."""
        if len(receipts) == 1:
            return self._usage(scope, run_id, receipts[0])
        if str(getattr(port, "driver_id", "") or "").startswith("codex"):
            return self._usage(scope, run_id, receipts[-1])
        return self._usage(scope, run_id, sum_usage(receipts))

    def execute(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        *,
        prompt: str,
        base_snapshot: dict[str, Any],
        output_paths: dict[str, str],
        planning_receipt: dict[str, Any] | None = None,
        options: DispatchOptions | None = None,
        hooks: Any = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run one admitted dispatch to evidence.

        ``deadline_seconds`` bounds this call's native run in place of ``max_seconds`` (Work 033
        S8, interfaces.md §3.4): the loop passes its remaining wall budget per call instead of
        writing the shared ``max_seconds``, which raced once trials ran concurrently. The node
        budget's ``max_wall_seconds`` still caps it, over every turn of the attempt.

        ``hooks`` (``strategy_runner.TurnHooks``, Work 033 S9): after each completed turn, while
        follow-ups remain, ``hooks.after_turn`` may return a message; the bound session is then
        resumed with it as dispatch ``<dispatch_id>-f<k>`` (module docstring, M3).
        """
        worker.require("worker.execute")
        limit = self._limit(deadline_seconds)
        followups_cap = _hooks_followups(hooks)
        did = dispatch["dispatch_id"]
        scope = worker.scope
        request = {
            "dispatch": dispatch,
            "prompt_digest": digest(prompt),
            "snapshot_digest": base_snapshot["digest"],
            "output_paths": output_paths,
            "planning_receipt": planning_receipt,
        }
        if options is not None and not options.is_default():
            # only non-default options: an existing dispatch replays with its digest (§3.4)
            request["options_digest"] = options.digest()
        if followups_cap:
            # only hooks that can resume: an existing dispatch replays with its digest (§3.4)
            request["hooks_digest"] = hooks.spec_digest()
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
        options = self._check_options(worker, dispatch, port, options)
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
                    # a resumed turn runs under the same options (checkpoint/resume, §3.4)
                    **({"options": options.wire()} if options is not None else {}),
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
        prepared = (
            cast(Any, port).prepare(dispatch, prompt, workspace, options=options)
            if options is not None
            else port.prepare(dispatch, prompt, workspace)
        )
        # Context/authority may have changed while preparing the sandbox.
        assert_execution_live(self.runtime, worker, dispatch)
        self._update(worker, did, "launching", prepared_digest=digest(prepared))
        handle = None
        bound: str | None = None  # the native session this run is bound to
        beat = {"sequence": 0, "last": 0.0}
        start = time.monotonic()
        timeout = min(limit, dispatch["node"]["budget"]["max_wall_seconds"])
        handles: list[str] = []
        try:
            self.store.assert_outside_tx()
            handle = port.start(prepared)
            handles.append(handle)
            self._update(worker, did, "observing", driver_handle=handle)
            while True:
                if time.monotonic() - start >= timeout:
                    raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
                assert_execution_live(
                    self.runtime, worker, dispatch
                )  # Revocation & steering are checked during execution.
                self._beat(worker, dispatch, beat)
                observation = port.poll(handle)
                native = observation.get("session_handle")
                if native and not bound:
                    self.sessions.bind(worker, did, native, expected_version=session["row_version"])
                    self.runtime.start(worker, dispatch, native)
                    bound = native
                if observation["state"] == "completed":
                    break
                if observation["state"] in STOPPED_STATES:
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
            receipts = [receipt]
            followups: list[dict[str, Any]] = []
            for k in range(1, followups_cap + 1):
                # M3: the hook reads the finished turn; a message resumes the bound session
                message = self._after_turn(
                    worker, dispatch, hooks, Path(workspace), k - 1, receipt,
                    start=start, timeout=timeout, beat=beat,
                )  # fmt: skip
                if not message:
                    break
                receipt = self._followup(
                    worker, dispatch, port, handles, k, message, Path(workspace),
                    session=bound, options=options, start=start, timeout=timeout, beat=beat,
                    followups=followups,
                )  # fmt: skip
                handle = handles[-1]
                receipts.append(receipt)
            outputs = self.workspaces.collect(
                scope, workspace, output_paths, dispatch["node"], process_stopped=True
            )
            usage = self._turns_usage(scope, dispatch["run_id"], port, receipts)
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
            # Outputs are in the CAS; remove the stopped run's containers (never before collect),
            # every turn's only after the last turn (M3)
            for each in reversed(handles):
                with contextlib.suppress(Exception):
                    port.destroy(each)
            return result
        except Exception as exc:
            # the running turn's process: a follow-up's from its resume on (M3)
            handle = handles[-1] if handles else handle
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
        options = DispatchOptions.from_wire(data["options"]) if data.get("options") else None
        if options is not None and getattr(port, "accepts_options", False) is not True:
            raise Hold("DRIVER_OPTIONS_UNSUPPORTED", "This driver port takes no dispatch options")
        self._update(worker, did, "resuming", resume_dispatch_id=request["dispatch_id"])
        self.store.assert_outside_tx()
        workspace, checkpoint_receipt = Path(data["workspace"]), data["driver_checkpoint"]
        handle = (
            cast(Any, port).resume(request, prompt, workspace, checkpoint_receipt, options=options)
            if options is not None
            else port.resume(request, prompt, workspace, checkpoint_receipt)
        )
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

    def continue_resumed(
        self,
        worker: Actor,
        run_id: str,
        *,
        options: DispatchOptions | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Collect a resumed turn only after the controller commits the new lease.

        ``options``, when given, must be the options the run was dispatched with (the resumed
        turn already runs under them, ``resume_exact``); Hold DISPATCH_OPTIONS_BINDING otherwise.
        ``deadline_seconds`` bounds the resumed turn as in ``execute`` (Work 033 S8).
        """
        from copy import deepcopy

        limit = self._limit(deadline_seconds)
        did, h = self._run_execution(worker, run_id)
        data = h["data"]
        stored = DispatchOptions.from_wire(data["options"]) if data.get("options") else None
        if options is not None and (None if options.is_default() else options.digest()) != (
            None if stored is None or stored.is_default() else stored.digest()
        ):
            raise Hold(
                "DISPATCH_OPTIONS_BINDING",
                "A resumed turn keeps the options it was dispatched with",
            )
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
                limit, dispatch["node"]["budget"]["max_wall_seconds"]
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
