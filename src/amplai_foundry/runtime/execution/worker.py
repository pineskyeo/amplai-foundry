"""Bounded worker coordinator: admitted dispatch -> native execution -> evidence.

No success authority lives here. VerificationService owns repair and completion.
Each dispatch is durably bound before I/O. An interrupted launching state is held
for reconciliation; at-least-once dispatch is not at-least-once native spawn.

Follow-up turns (Work 033 S9, interfaces.md §5.1 M3, IC-04): after a completed turn and before
``output_ready``, turn hooks may resume the same native session with a message as dispatch
``<dispatch_id>-f<k>`` (k = 1, 2) on the same run, lease and bound session, inside ``execute``
(never through the controller's ``resume_pending`` steering path). Each follow-up runs
collect -> checkpoint -> resume -> poll -> collect, so a seeded credential is released at every
collect and seeded again at every resume; ``destroy`` runs only after the last turn.

Recovery of a follow-up (Work 033 S9b, §14 Q16 (a)): a replay of ``execute`` with the same request
digest (``_existing``) whose execution stopped inside follow-up k (head ``observing``, the
follow-up recorded before its resume) is recovered, never sent twice. The driver journal of
``<dispatch_id>-f<k>`` decides: none (the resume never prepared it, ``SESSION_NOT_FOUND``) or
``prepared`` (never started: ``CliDriver.start`` persists ``starting`` before it spawns) -> the
kept message is re-sent once under the same id (``prepare`` is idempotent for the same bytes);
``completed`` -> collected (resumed, no second turn); still running in a process this port owns ->
observed to its end; a process this port does not own (``ORPHAN_SESSION``), a turn that ended
without completing, a message that was not kept, or a run no longer running -> held with that
reason (``FOLLOWUP_RECOVERY`` or the driver's code). A re-send happens only when no native turn
ran, so no effect of it can exist; effects stay keyed by run (``output_ready`` holds
EFFECT_PENDING). Any other interrupted execution is held as before (EXECUTION_RECONCILE). A
follow-up runs in the bound turn's native home; a seeding port releases a handle it did not seed
itself (a port built after a restart) by the home its driver journal records
(``SeededCodexPort._release``), so every confirmed stop of a follow-up leaves no credential at rest
(M3 rule 2).

A process ``port.start`` spawned before the worker recorded its handle (the worker died between
the two; the head keeps ``driver_handle`` null after ``launching``) is stopped by ``abort`` and
``stop_and_snapshot`` by the dispatch id, which every port returns as its handle, when the
dispatch's driver journal exists (``_stop_spawned``): cancelled to a confirmed stop and destroyed.

Vote candidates (Work 033 S9b, §5.1 M4, §14 Q16 (b)): candidate 0 is the run's own turn (bound
session, ``runtime.start`` once). Candidates 1..k-1 run one after another as dispatches
``<dispatch_id>-c<i>`` in scratch workspaces materialized from the same base, each with its own
native session that is never bound (``runtime.start`` refuses a dispatch id without a claim row,
DISPATCH_BINDING) and never resumed: it is collected, its trace kept, and destroyed (credential
released) before the next one starts. No candidate starts once the attempt's tokens reach 80 % of
the node budget (or are unknown). ``hooks.evaluate`` runs the host fast checks on each candidate,
``hooks.select`` picks one, and for a winner other than 0 the run workspace is reset to the base
with the winner's patch applied (the host materializes the winner's snapshot; checked by the
snapshot, VOTE_APPLY) before ``workspaces.collect`` and ``output_ready``. A candidate session has
no ``driver-session`` row and can raise no effect (the native broker accepts only the run's bound
session, ``tool_broker/native.py``); effects stay keyed by run. The run's ``driver_handle`` stays
candidate 0's, so a steering pause during candidate i stops candidate i here and is then taken
over on the bound session with the run workspace as candidate 0 left it (the vote is dropped);
every stop path (pause, abort, a failed attempt) also stops a candidate whose stop was not
confirmed; after a selection other than 0 no pause is taken over (``stop_and_snapshot`` holds
VOTE_SELECTED).

Trace admission (Work 033 S13, interfaces.md §3.12, §9.1, D-100): when the dispatch's options
capture (``DispatchOptions.capture_trace``, set only for trial goals) and a ``trace_sink`` is
configured, the sanitized trace of every turn of the attempt (the first turn and its follow-ups,
``port.trace(handle)``) goes to ``trace_sink(run_id, sanitized)`` after the last collect. Trace
capture never changes the run: a port without traces is skipped and a sink failure is ignored
(``TraceService.admit`` records its own refusals as ``trace-drop``). A port without
``accepts_options`` has no capture point (OpenCode capture is deferred, §9.1): options that differ
from the default only by ``capture_trace`` reach it as no options, so the trial runs uncaptured
instead of holding DRIVER_OPTIONS_UNSUPPORTED (``capture_only``); a resumed turn of such a run keeps
that binding (``continue_resumed``).

The run's auxiliary read-only turns (investigators, reviewers, §9.1; plan-time turns go to the
goal-level planner trace instead, ``product.plan``) reach the same single admission through
``aux_traces``, an argument of ``execute`` and ``continue_resumed`` that
``ExecutionLoop._execute`` passes: a zero-argument callable (``StrategyRunner.trace_source``)
called once, only when the dispatch captures, after the last collect; each ``(name, snapshot)``
whose name ``traces.AUX_TURN`` accepts joins ``traces.combine`` after the executor turns. It is
not part of the request digest (capture never changes the run). A run resumed after steering
admits its trace in ``continue_resumed``: a capturing ``execute`` that pauses records the
attempt's handles (``trace_handles``), ``resume_exact`` appends each resumed handle, and the
resumed run's trace holds every turn.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from ...agent_drivers.ports import UNKNOWN_USAGE, DriverRegistry
from ...agent_drivers.sessions import SessionStore
from ..contracts.authority import Actor
from ..contracts.identity import digest, now
from ..errors import Conflict, Hold, RuntimeFault
from .cells import DispatchOptions, check_binding, profile_effort
from .envelope import assert_execution_live, execution_envelope

USAGE_DETAIL_KIND = "usage-detail"
MAX_FOLLOWUPS = 2  # §5.1 M3: at most 2 follow-up turns per attempt
MAX_CANDIDATES = 3  # §5.1 M4: k <= 3 candidates per attempt (IC-06)
CANDIDATE_BUDGET = 0.8  # §5.1 M4: no candidate starts at 80 % of the node budget's tokens
STOPPED_STATES = frozenset({"failed", "unknown", "cancelled", "paused", "awaiting_tools"})
# a candidate (i >= 1) that ends with one of these, its process confirmed stopped, is left out of
# the vote instead of ending the attempt (candidate 0, the run's own turn, still holds)
CANDIDATE_LOCAL = frozenset({"DRIVER_BOUNDARY", "COLLECT_BOUNDARY", "DRIVER_NOT_COMPLETE"})
# a candidate entry in one of these states may hold a prepared journal, a seeded credential or a
# running process: every stop path (pause, abort, a failed attempt) stops it (§14 Q16 (b))
LIVE_CANDIDATE = frozenset({"preparing", "starting", "running"})


def _hooks_shape(hooks: Any) -> tuple[int, int]:
    """(follow-up cap, candidates) of ``hooks``: (0, 1) without hooks. Vote hooks (M4) ask for
    2..3 candidates, take no follow-up turn and must ``evaluate`` and ``select`` candidates."""
    if hooks is None:
        return 0, 1
    followups, candidates = getattr(hooks, "max_followups", 0), getattr(hooks, "candidates", 1)
    if type(followups) is not int or not 0 <= followups <= MAX_FOLLOWUPS:
        raise RuntimeFault("WORKER_HOOKS", "Turn hooks allow 0..2 follow-up turns")
    if type(candidates) is not int or not 1 <= candidates <= MAX_CANDIDATES:
        raise RuntimeFault("WORKER_HOOKS", "Turn hooks ask for 1..3 candidates")
    if candidates > 1:
        if followups:
            raise RuntimeFault("WORKER_HOOKS", "Vote candidates (M4) take no follow-up turns")
        if not callable(getattr(hooks, "evaluate", None)) or not callable(
            getattr(hooks, "select", None)
        ):
            raise RuntimeFault("WORKER_HOOKS", "Vote hooks evaluate and select candidates")
    return followups, candidates


def _tokens(receipts: list[dict[str, Any]]) -> int | None:
    """Input + output tokens of these turns; None when one did not report both counts."""
    total = 0
    for receipt in receipts:
        usage = receipt.get("usage") or {}
        counts = (usage.get("input_tokens"), usage.get("output_tokens"))
        if any(type(c) is not int or c < 0 for c in counts):
            return None
        total += cast(int, counts[0]) + cast(int, counts[1])
    return total


def _without_ids(value: Any) -> Any:
    """``value`` with every ``id`` key dropped (artifact refs then compare by digest)."""
    if isinstance(value, dict):
        return {k: _without_ids(v) for k, v in value.items() if k != "id"}
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    return value


def _candidate_result(result: Any, index: int) -> dict[str, Any]:
    """The checked fields of ``hooks.evaluate``'s ``CandidateResult`` for the head."""
    patch_lines, checks = getattr(result, "patch_lines", None), getattr(result, "fast_checks", None)
    if (
        getattr(result, "index", None) != index
        or type(patch_lines) is not int
        or patch_lines < 0
        or not isinstance(checks, dict)
        or not all(isinstance(k, str) and type(v) is bool for k, v in checks.items())
    ):
        raise RuntimeFault("WORKER_HOOKS", "A candidate evaluation is a CandidateResult")
    return {"patch_lines": patch_lines, "fast_checks": dict(checks)}


def port_trace(port: Any, handle: str) -> dict[str, Any] | None:
    """The sanitized trace a port keeps for ``handle`` (S13): ``port.trace``, else the trace of
    the CLI driver a ``CliPort`` wraps (``agent_drivers/ports.py`` forwards no ``trace``); None
    for a port that captures nothing (OpenCode is deferred, §9.1)."""
    fn = getattr(port, "trace", None)
    if not callable(fn):
        fn = getattr(getattr(port, "driver", None), "trace", None)
    if not callable(fn):
        return None
    found = fn(handle)
    return found if isinstance(found, dict) else None


AuxTraces = Callable[[], list[tuple[str, dict[str, Any]]]]


def named_turns(aux_traces: AuxTraces | None) -> list[tuple[int | str, dict[str, Any]]]:
    """The auxiliary turns ``aux_traces`` hands over (S13, §9.1): each ``(name, snapshot)`` whose
    name ``traces.AUX_TURN`` accepts and whose snapshot is a dict; anything else is left out, and
    a source that fails gives none (trace capture never changes the run)."""
    if aux_traces is None:
        return []
    from ...meta_harness.traces import AUX_TURN

    try:
        given = list(aux_traces() or [])
    except Exception:
        return []
    out: list[tuple[int | str, dict[str, Any]]] = []
    for entry in given:
        if not isinstance(entry, tuple | list) or len(entry) != 2:
            continue
        name, snapshot = entry
        if isinstance(name, str) and AUX_TURN.fullmatch(name) and isinstance(snapshot, dict):
            out.append((name, snapshot))
    return out


def capture_only(options: DispatchOptions) -> bool:
    """Options that differ from the default only by the trace flag (S13): what a trial goal
    resolves for a cell at the provider default without driver options."""
    return options.capture_trace and replace(options, capture_trace=False).is_default()


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
        trace_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """``trace_sink(run_id, sanitized)`` runs after collect when ``options.capture_trace``
        is true (Work 033 S13, §3.12); None captures nothing."""
        if not 0 < poll_seconds <= 30 or not 0 < max_seconds <= 86400:
            raise RuntimeFault("WORKER_LIMITS", "Polling and execution must be bounded")
        if trace_sink is not None and not callable(trace_sink):
            raise RuntimeFault("WORKER_TRACE_SINK", "A trace sink is a callable")
        self.trace_sink = trace_sink
        self.runtime, self.store, self.registry, self.workspaces = (
            runtime,
            runtime.store,
            registry,
            workspaces,
        )
        self.sessions = SessionStore(self.store, runtime.contracts)
        self.poll_seconds, self.max_seconds = poll_seconds, max_seconds
        # dispatches an ``execute`` or a recovery of this coordinator is driving (S9b)
        self._active: set[str] = set()
        self._active_lock = threading.Lock()

    def _state(self, worker: Actor, did: str) -> dict[str, Any]:
        return self.store.head(worker.scope, "worker-execution", did)

    def _update(self, worker: Actor, did: str, state: str, **changes: Any) -> dict[str, Any] | None:
        with self.store.tx() as db:
            h = self.store.head(worker.scope, "worker-execution", did, db=db)
            if state == "held" and h["state"] in {"paused", "cancelled", "resuming"}:
                return h
            if state == "pause_requested" and h["state"] in {"paused", "cancelled", "resuming"}:
                # The controller's stop_and_snapshot applied the pause before this thread
                # recorded its request (a race a loaded runner shows): keep the controller's
                # state, the one resume_exact admits, and add this thread's data (trace handles)
                self.store.cas(
                    db, worker.scope, "worker-execution", did, h["row_version"], h["state"],
                    {**h["data"], **changes},
                )  # fmt: skip
                return None
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

        S13 (§9.1): such a port has no capture point, so options that differ from the default
        only by ``capture_trace`` (``capture_only``) reach it as none and the trial runs
        uncaptured; the trace flag changes no argv and no run behaviour.
        """
        profile = dispatch["profile"]
        accepts = getattr(port, "accepts_options", False) is True
        if options is not None:
            if not accepts:
                if options.is_default() or capture_only(options):
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

    def _takes_no_options(self, scope: Any, data: dict[str, Any]) -> bool:
        """The stored execution's port has no ``accepts_options`` (S13, ``continue_resumed``);
        False when the port is not resolvable (the binding check then holds)."""
        dispatch = data["dispatch"]
        try:
            port = self.registry.resolve(
                scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
            )
        except (Hold, RuntimeFault):
            return False
        return getattr(port, "accepts_options", False) is not True

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
        """The stored execution of ``did`` for this request (EXECUTION_REPLAY when the payload or
        the owner differs): None when there is none; its head when it is ``verifying`` (the
        caller returns its result) or stopped inside a follow-up turn (``_in_followup``: the
        caller recovers it, §14 Q16 (a)); any other execution is held (EXECUTION_RECONCILE)."""
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
        if h["state"] == "verifying" or self._in_followup(h):
            return h
        raise Hold(
            "EXECUTION_RECONCILE", "Existing dispatch may have started; do not spawn it again"
        )

    @staticmethod
    def _in_followup(h: dict[str, Any]) -> bool:
        """The execution stopped inside a follow-up turn (§14 Q16 (a)): still ``observing``, the
        last recorded follow-up is the one in flight, and it is not a vote (no candidates)."""
        data = h["data"]
        entries = data.get("followups")
        return (
            h["state"] == "observing"
            and isinstance(entries, list)
            and bool(entries)
            and isinstance(entries[-1], dict)
            and data.get("followup_dispatch_id") == entries[-1].get("dispatch_id")
            and not data.get("candidates")
        )

    def _claim_local(self, did: str) -> None:
        """One ``execute`` or recovery of a dispatch at a time in this coordinator: a recovery
        must never observe a follow-up another thread of this process still drives."""
        with self._active_lock:
            if did in self._active:
                raise Hold("EXECUTION_RECONCILE", "The dispatch is still executing in this worker")
            self._active.add(did)

    def _release_local(self, did: str) -> None:
        with self._active_lock:
            self._active.discard(did)

    def _lease_sequence(self, scope: Any, run_id: str) -> int:
        """The lease's last heartbeat sequence: a recovered attempt beats on from it (the runtime
        refuses a sequence that does not increase, HEARTBEAT_SEQUENCE)."""
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT heartbeat_seq FROM leases WHERE tenant=? AND project=? AND run_id=?",
                (*scope.keys(), run_id),
            ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    # -- turns of one attempt (Work 033 S9, M3; S9b, M4) ------------------------------------------
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

    def _in_hook(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        hooks: Any,
        call: Callable[[], Any],
        *,
        start: float,
        timeout: float,
        beat: dict[str, Any],
    ) -> Any:
        """``call`` (a hook: a host-side read-only turn or host fast checks may take minutes) in
        its own thread while this thread keeps the lease alive and checks revocation and steering,
        as during a native turn.

        When the attempt stops while the hook still runs (deadline, revocation, steering), the
        hook is told so (``hooks.abandon()`` when it has one) before the exception leaves: its
        late result is then dropped instead of being written after the loop moved on."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["value"] = call()
            except BaseException as exc:  # handed to the worker thread below
                box["error"] = exc

        thread = threading.Thread(target=run, name="amplai-turn-hook", daemon=True)
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
        return box.get("value")

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
        """``hooks.after_turn`` in a hook thread (``_in_hook``): the follow-up message or None."""
        message = self._in_hook(
            worker, dispatch, hooks,
            lambda: hooks.after_turn(workspace=workspace, turn=turn, receipt=receipt),
            start=start, timeout=timeout, beat=beat,
        )  # fmt: skip
        if message is None:
            return None
        if not isinstance(message, str):
            raise RuntimeFault("WORKER_HOOKS", "A follow-up message is text")
        return message.strip() or None

    def _keep_message(self, scope: Any, message: str) -> dict[str, Any] | None:
        """The follow-up message as an artifact, so a recovery can send it again (§14 Q16 (a));
        None when the CAS refuses it (e.g. SECRET_DETECTED): the follow-up still runs, but an
        interruption before its turn started is then held instead of re-sent."""
        try:
            ref: dict[str, Any] = self.runtime.artifacts.admit(
                scope, message.encode(), "text/plain", trust="worker"
            )
        except (Hold, RuntimeFault):
            return None
        return ref

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
        entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Follow-up ``k``: checkpoint the finished turn, resume the bound session as dispatch
        ``<dispatch_id>-f<k>`` (same run id, lease and fencing token), observe it to a completed
        turn and collect it. Every observed session must be the bound one (SESSION_REBIND).

        ``entry``: the recorded entry of a follow-up a recovery sends again (same id, message and
        digest, §14 Q16 (a)); None records a new entry before the resume. The new handle joins
        ``handles`` as soon as the resume returns, so a failure while it runs stops that process
        (the caller cancels the last handle), not the finished one."""
        did = dispatch["dispatch_id"]
        follow = deepcopy(dispatch)
        follow["dispatch_id"] = f"{did}-f{k}"
        assert_execution_live(self.runtime, worker, dispatch)
        self._beat(worker, dispatch, beat)
        checkpoint = port.checkpoint(handles[-1])
        if checkpoint.get("session_handle") != session:
            raise Hold("SESSION_REBIND", "A follow-up resumes only the bound session")
        if entry is None:
            entry = {
                "dispatch_id": follow["dispatch_id"], "prompt_digest": digest(message),
                "receipt_digest": None, "usage": None,
                "message_artifact": self._keep_message(worker.scope, message),
            }  # fmt: skip
            followups.append(entry)
            # recorded before the resume: an execution interrupted from here on is recovered from
            # this entry and the follow-up's driver journal (§14 Q16 (a), ``_recover``)
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
        return self._observe_followup(
            worker, dispatch, port, new, k, session=session, start=start, timeout=timeout,
            beat=beat, followups=followups, entry=entry,
        )  # fmt: skip

    def _observe_followup(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        port: Any,
        handle: str,
        k: int,
        *,
        session: str,
        start: float,
        timeout: float,
        beat: dict[str, Any],
        followups: list[dict[str, Any]],
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        """Observe follow-up ``k`` to a completed turn and collect it into ``entry``. A receipt
        digest the entry already holds (an execution stopped after that collect) must equal the
        receipt collected again (FOLLOWUP_RECOVERY)."""
        did = dispatch["dispatch_id"]
        while True:
            if time.monotonic() - start >= timeout:
                raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
            assert_execution_live(self.runtime, worker, dispatch)
            self._beat(worker, dispatch, beat)
            observation = port.poll(handle)
            native = observation.get("session_handle")
            if native and native != session:
                raise Hold("SESSION_REBIND", "A follow-up stream changed the exact session")
            if observation["state"] == "completed":
                break
            if observation["state"] in STOPPED_STATES:
                raise Hold(
                    "DRIVER_BOUNDARY", "Driver is not a successful completed turn",
                    details={
                        "state": observation["state"], "followup": k,
                        "failure": observation.get("failure"),
                    },
                )  # fmt: skip
            time.sleep(self.poll_seconds)
        receipt: dict[str, Any] = port.collect(handle)
        if (
            receipt.get("process_stopped") is not True
            or receipt.get("provider_completed") is not True
        ):
            raise Hold(
                "COLLECT_BOUNDARY", "Driver receipt is not positive completion/termination evidence"
            )
        if receipt.get("session_handle") not in (None, session):
            raise Hold("SESSION_REBIND", "A follow-up receipt names another session")
        if entry.get("receipt_digest") not in (None, digest(receipt)):
            raise Hold(
                "FOLLOWUP_RECOVERY", "The follow-up's receipt differs from the recorded one",
                details={"dispatch_id": entry.get("dispatch_id")},
            )  # fmt: skip
        entry.update(receipt_digest=digest(receipt), usage=receipt.get("usage"))
        self._update(worker, did, "observing", followups=deepcopy(followups))
        assert_execution_live(self.runtime, worker, dispatch)
        return receipt

    def _admit_trace(
        self, dispatch: dict[str, Any], port: Any, handles: list[str],
        options: DispatchOptions | None, aux_traces: AuxTraces | None = None,
        extra: list[tuple[int, dict[str, Any]]] | None = None,
    ) -> None:  # fmt: skip
        """Hand the attempt's sanitized turns to the trace sink (S13, §9.1): turn 0 is the
        first dispatch, turn k its follow-up ``<dispatch_id>-f<k>`` (or, after steering, the next
        resumed turn), then ``extra`` (vote candidate i as turn i, S9b: their snapshots are kept
        before each candidate is destroyed), then the named auxiliary turns of ``aux_traces``.
        Never raises: trace capture does not change the run. A non-capturing dispatch never calls
        ``aux_traces``."""
        if self.trace_sink is None or options is None or not options.capture_trace:
            return
        turns: list[tuple[int | str, dict[str, Any]]] = []
        for k, handle in enumerate(handles):
            with contextlib.suppress(Exception):
                found = port_trace(port, handle)
                if found is not None:
                    turns.append((k, found))
        turns.extend((k, snapshot) for k, snapshot in extra or [] if isinstance(snapshot, dict))
        if not turns:
            return
        turns.extend(named_turns(aux_traces))
        with contextlib.suppress(Exception):
            from ...meta_harness.traces import combine

            driver_id = str(getattr(port, "driver_id", "") or "")
            self.trace_sink(dispatch["run_id"], combine(driver_id, turns))

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

    def _complete(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        port: Any,
        hooks: Any,
        workspace: Path,
        *,
        session: str,
        options: DispatchOptions | None,
        handles: list[str],
        receipts: list[dict[str, Any]],
        followups: list[dict[str, Any]],
        first: int,
        cap: int,
        start: float,
        timeout: float,
        beat: dict[str, Any],
        output_paths: dict[str, str],
        aux_traces: AuxTraces | None,
        usage: dict[str, Any] | None = None,
        extra_traces: list[tuple[int, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """From the last collected turn to ``output_ready``: follow-ups ``first``..``cap`` (M3),
        the trace, the outputs and the run's usage (``usage`` when the caller summed it: a vote),
        then every handle of the bound session is destroyed (never before the last collect)."""
        did, scope = dispatch["dispatch_id"], worker.scope
        receipt = receipts[-1]
        for k in range(first, cap + 1):
            # M3: the hook reads the finished turn; a message resumes the bound session
            message = self._after_turn(
                worker, dispatch, hooks, workspace, k - 1, receipt,
                start=start, timeout=timeout, beat=beat,
            )  # fmt: skip
            if not message:
                break
            receipt = self._followup(
                worker, dispatch, port, handles, k, message, workspace,
                session=session, options=options, start=start, timeout=timeout, beat=beat,
                followups=followups,
            )  # fmt: skip
            receipts.append(receipt)
        # S13: after the last collect, before the outputs are admitted (never raises)
        self._admit_trace(dispatch, port, handles, options, aux_traces, extra=extra_traces)
        outputs = self.workspaces.collect(
            scope, workspace, output_paths, dispatch["node"], process_stopped=True
        )
        if usage is None:
            usage = self._turns_usage(scope, dispatch["run_id"], port, receipts)
        self.runtime.contracts.validate(
            "run-record",
            {**self.store.head(scope, "run", dispatch["run_id"])["data"]["record"], "usage": usage},
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
            worker, did, "verifying", result=result, driver_receipt_digest=digest(receipt),
            **driver_lookups(port, handles, self._state(worker, did)["data"]),
        )  # fmt: skip
        # Outputs are in the CAS; remove the stopped run's containers (never before collect),
        # every turn's only after the last turn (M3)
        for each in reversed(handles):
            with contextlib.suppress(Exception):
                port.destroy(each)
        return result

    def _fail(
        self,
        worker: Actor,
        did: str,
        port: Any,
        handles: list[str],
        options: DispatchOptions | None,
        exc: BaseException,
    ) -> None:
        """What an attempt that raised leaves behind (``execute``, ``_recover``): a steering pause
        keeps the running turn's process for the controller; anything else cancels it (confirmed
        stop, then destroy) and holds the execution with the code."""
        handle = handles[-1] if handles else None  # the running turn's (a follow-up's once resumed)
        if getattr(exc, "code", None) == "EXECUTION_PAUSED" and handle is not None:
            # Steering: the controller stops this process at a boundary and checkpoints it
            # (SteeringService.quiesce -> stop_and_snapshot); cancelling here would lose the
            # session the operator's message resumes (D-082).
            # S13: a capturing run keeps its turns' handles for the resumed admission
            traced = (
                {"trace_handles": list(handles)}
                if self.trace_sink is not None and options is not None and options.capture_trace
                else {}
            )
            self._update(worker, did, "pause_requested", hold_code="EXECUTION_PAUSED", **traced)
            return
        stopped = False
        if handle is not None:
            with contextlib.suppress(Exception):
                stopped = port.cancel(handle).get("process_stopped") is True
            if stopped:
                with contextlib.suppress(Exception):
                    port.destroy(handle)
        # §14 Q16 (b): a vote candidate whose stop was not confirmed is stopped again here; the
        # execution counts as stopped only when every candidate is
        candidates: dict[str, Any] = {}
        if handle is None:
            # ``port.start`` raised after ``launching`` (``prepared_digest``) was recorded: it may
            # have spawned the process before raising, and no handle was returned, so it is
            # stopped by the dispatch id (``_stop_spawned``; None: nothing can have been spawned)
            with contextlib.suppress(Exception):
                spawned = self._stop_spawned(port, did, self._state(worker, did)["data"])
                if spawned is not None:
                    stopped = spawned
        with contextlib.suppress(Exception):
            data = self._state(worker, did)["data"]
            if data.get("candidates"):
                ok, entries = self._stop_candidates(port, data)
                stopped = stopped and ok
                candidates = {"candidates": entries}
        # the hold's scalar details (DRIVER_BOUNDARY: the driver journal's state and failure) stay
        # with the code, so a held execution says why its turn ended; nothing else is copied
        details = getattr(exc, "details", None)
        scalars = {
            str(k): v for k, v in details.items()
            if v is None or (isinstance(v, (str, bool, int)) and len(str(v)) <= 200)
        } if isinstance(details, dict) else {}  # fmt: skip
        kept = {"hold_details": scalars} if scalars else {}
        with contextlib.suppress(Exception):
            kept.update(driver_lookups(port, handles or [did], self._state(worker, did)["data"]))
        self._update(
            worker, did, "held", hold_code=getattr(exc, "code", type(exc).__name__),
            process_stopped=stopped, **candidates, **kept,
        )  # fmt: skip

    # -- M3 recovery (Work 033 S9b, §14 Q16 (a)) --------------------------------------------------
    def _kept_message(self, scope: Any, entry: dict[str, Any]) -> str:
        """The recorded follow-up's message, checked against its digest (FOLLOWUP_RECOVERY when
        it was not kept or differs: it is then never sent again)."""
        ref = entry.get("message_artifact")
        if not isinstance(ref, dict):
            raise Hold(
                "FOLLOWUP_RECOVERY", "The follow-up message was not kept; it is not sent again",
                details={"dispatch_id": entry.get("dispatch_id")},
            )  # fmt: skip
        message = self.runtime.artifacts.read(scope, ref).decode()
        if digest(message) != entry.get("prompt_digest"):
            raise Hold(
                "FOLLOWUP_RECOVERY", "The kept follow-up message differs from its digest",
                details={"dispatch_id": entry.get("dispatch_id")},
            )  # fmt: skip
        return message

    def _recollect(
        self, port: Any, handle: str, session: str, recorded: str | None
    ) -> dict[str, Any]:
        """A finished earlier turn's receipt, collected again from its driver journal: positive
        completion of the bound session and, for a follow-up, the digest it was recorded with."""
        receipt: dict[str, Any] = port.collect(handle)
        if (
            receipt.get("process_stopped") is not True
            or receipt.get("provider_completed") is not True
            or receipt.get("session_handle") not in (None, session)
            or (recorded is not None and digest(receipt) != recorded)
        ):
            raise Hold(
                "FOLLOWUP_RECOVERY", "An earlier turn's receipt differs from what was recorded",
                details={"handle": handle},
            )  # fmt: skip
        return receipt

    def _recover(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        head: dict[str, Any],
        *,
        hooks: Any,
        cap: int,
        options: DispatchOptions | None,
        limit: float,
        output_paths: dict[str, str],
        aux_traces: AuxTraces | None,
    ) -> dict[str, Any]:
        """§14 Q16 (a): continue an execution that stopped inside its follow-up k (module
        docstring). The follow-up is sent at most once more, and only when its driver journal
        shows that no turn started; then the attempt goes on as ``execute`` would (the remaining
        follow-ups, the outputs, ``output_ready``). Unsafe cases hold the execution with the
        reason; each recovery is recorded under ``recoveries`` before it acts."""
        did, scope, run_id = dispatch["dispatch_id"], worker.scope, dispatch["run_id"]
        data = head["data"]
        followups: list[dict[str, Any]] = deepcopy(data["followups"])
        entry, k = followups[-1], len(followups)
        follow_id = f"{did}-f{k}"
        # every port returns the dispatch id as its handle (``CliDriver.start``, ``RecipePort``,
        # ``OpenCodePort``); the recorded handle is checked against that below
        handles = [did, *(f"{did}-f{j}" for j in range(1, k))]
        self._claim_local(did)
        try:
            port = self.registry.resolve(
                scope, dispatch["profile"]["driver_profile_ref"], dispatch["node"]["strategy"]
            )
            options = self._check_options(worker, dispatch, port, options)
        except BaseException:
            self._release_local(did)
            raise
        try:
            if (
                entry.get("dispatch_id") != follow_id
                or not 1 <= k <= cap
                or data.get("driver_handle") not in {handles[-1], follow_id}
            ):
                raise Hold(
                    "FOLLOWUP_RECOVERY", "The recorded follow-up is not this execution's next turn",
                    details={"dispatch_id": entry.get("dispatch_id"), "cap": cap},
                )  # fmt: skip
            run = self.store.head(scope, "run", run_id)
            session = run["data"].get("session_handle")
            if run["state"] != "running" or not isinstance(session, str) or not session:
                raise Hold(
                    "FOLLOWUP_RECOVERY", "The run is no longer running; its follow-up is not sent",
                    details={"run_state": run["state"]},
                )  # fmt: skip
            assert_execution_live(self.runtime, worker, dispatch)
            beat = {"sequence": self._lease_sequence(scope, run_id), "last": 0.0}
            start = time.monotonic()
            timeout = min(limit, dispatch["node"]["budget"]["max_wall_seconds"])
            self._beat(worker, dispatch, beat)
            workspace = Path(data["workspace"])
            earlier = list(handles)
            try:
                journal: str | None = str(port.poll(follow_id)["state"])
            except RuntimeFault as exc:  # a Hold is a RuntimeFault
                if exc.code != "SESSION_NOT_FOUND" or isinstance(exc, Hold):
                    # its journal exists but this port does not own the process
                    # (ORPHAN_SESSION), or its state cannot be read: never sent again, and the
                    # stop of the failed attempt (and a later abort) targets the follow-up, not
                    # the finished turn before it
                    handles.append(follow_id)
                    self._update(worker, did, "observing", driver_handle=follow_id)
                    raise
                journal = None  # the resume never prepared the follow-up
            record = {"dispatch_id": follow_id, "journal": journal, "at": now()}
            if journal in (None, "prepared"):
                # no native turn ran (``CliDriver.start`` persists ``starting`` before it spawns):
                # the kept message is sent once under the same id
                if entry.get("receipt_digest") is not None:
                    raise Hold(
                        "FOLLOWUP_RECOVERY", "A collected follow-up has no driver journal",
                        details={"dispatch_id": follow_id},
                    )  # fmt: skip
                message = self._kept_message(scope, entry)
                self._update(
                    worker, did, "observing",
                    recoveries=[*(data.get("recoveries") or []), {**record, "action": "resent"}],
                )  # fmt: skip
                receipt = self._followup(
                    worker, dispatch, port, handles, k, message, workspace, session=session,
                    options=options, start=start, timeout=timeout, beat=beat,
                    followups=followups, entry=entry,
                )  # fmt: skip
            else:
                handles.append(follow_id)
                self._update(worker, did, "observing", driver_handle=follow_id)
                if journal in STOPPED_STATES:
                    raise Hold(
                        "FOLLOWUP_RECOVERY",
                        "The interrupted follow-up ended without completing; it is not sent again",
                        details={"dispatch_id": follow_id, "state": journal},
                    )  # fmt: skip
                action = "collected" if journal == "completed" else "observed"
                self._update(
                    worker, did, "observing",
                    recoveries=[*(data.get("recoveries") or []), {**record, "action": action}],
                )  # fmt: skip
                receipt = self._observe_followup(
                    worker, dispatch, port, follow_id, k, session=session, start=start,
                    timeout=timeout, beat=beat, followups=followups, entry=entry,
                )  # fmt: skip
            # the earlier turns' receipts, collected again only now: no turn of the session runs
            # any more (a port's collect may release the session home's credential, which a
            # follow-up still running there needs)
            receipts = [
                self._recollect(
                    port, h, session, None if j == 0 else followups[j - 1].get("receipt_digest")
                )
                for j, h in enumerate(earlier)
            ]
            receipts.append(receipt)
            return self._complete(
                worker, dispatch, port, hooks, workspace, session=session, options=options,
                handles=handles, receipts=receipts, followups=followups, first=k + 1, cap=cap,
                start=start, timeout=timeout, beat=beat, output_paths=output_paths,
                aux_traces=aux_traces,
            )  # fmt: skip
        except Exception as exc:
            self._fail(worker, did, port, handles, options, exc)
            raise
        finally:
            self._release_local(did)

    # -- M4 vote candidates (Work 033 S9b, §5.1 M4, §14 Q16 (b)) ----------------------------------
    def _discard(self, path: Path) -> None:
        """Remove a scratch candidate workspace (never raises)."""
        discard = getattr(self.workspaces, "discard", None)
        with contextlib.suppress(Exception):
            if callable(discard):
                discard(path)
            else:
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _stop_candidate(port: Any, handle: str) -> bool:
        """Stop a candidate's (or an unrecorded follow-up's) process and release it (cancel, then
        destroy). True when it is confirmed stopped, or when its journal does not exist."""
        try:
            stopped = port.cancel(handle).get("process_stopped") is True
        except RuntimeFault as exc:
            return exc.code == "SESSION_NOT_FOUND"
        except Exception:
            return False
        if stopped:
            with contextlib.suppress(Exception):
                port.destroy(handle)
        return stopped

    def _stop_candidates(
        self, port: Any, data: dict[str, Any]
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Stop every candidate (i >= 1) the head lists as possibly holding a process: one that
        is preparing, starting or running, or whose stop was not confirmed (§14 Q16 (b): a pause,
        an abort or a failed attempt stops every process of the run, not only
        ``driver_handle``). Returns (True when none is left running, the entries as now)."""
        stopped = True
        entries = deepcopy([e for e in data.get("candidates") or [] if isinstance(e, dict)])
        for entry in entries[1:]:
            if entry.get("state") in LIVE_CANDIDATE or entry.get("process_stopped") is False:
                handle = str(entry.get("handle") or entry.get("dispatch_id"))
                ok = self._stop_candidate(port, handle)
                if entry.get("state") in LIVE_CANDIDATE:
                    entry["state"] = "stopped"
                entry["process_stopped"] = ok
                stopped = stopped and ok
        return stopped, entries

    def _evaluate(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        hooks: Any,
        workspace: Path,
        index: int,
        receipt: dict[str, Any],
        entry: dict[str, Any],
        *,
        start: float,
        timeout: float,
        beat: dict[str, Any],
    ) -> Any:
        """``hooks.evaluate`` (host fast checks, never a verdict) of candidate ``index`` in a hook
        thread; its patch size and check results join the candidate's entry."""
        result = self._in_hook(
            worker, dispatch, hooks,
            lambda: hooks.evaluate(workspace=workspace, index=index, receipt=receipt),
            start=start, timeout=timeout, beat=beat,
        )  # fmt: skip
        entry.update(_candidate_result(result, index))
        return result

    def _candidate(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        port: Any,
        hooks: Any,
        i: int,
        *,
        prompt: str,
        base_snapshot: dict[str, Any],
        session: str,
        options: DispatchOptions | None,
        start: float,
        timeout: float,
        beat: dict[str, Any],
        entries: list[dict[str, Any]],
        scratch: dict[int, Path],
        capture: bool,
    ) -> tuple[dict[str, Any], Any, dict[str, Any] | None] | None:
        """Candidate ``i`` (§5.1 M4): dispatch ``<dispatch_id>-c<i>`` (the run's dispatch with
        only the id replaced) on a scratch workspace from the same base, with its own native
        session and home: no ``SessionStore`` row and no ``runtime.start`` (the run stays bound to
        candidate 0's session). It is collected, its trace kept, destroyed (its credential
        released) and then evaluated: (receipt, evaluation, trace).

        A candidate-local failure (``CANDIDATE_LOCAL``, its process confirmed stopped) returns
        None: the candidate is left out. Anything else (steering, the deadline, revocation, a
        candidate naming the bound session) stops its process first and raises."""
        did, scope = dispatch["dispatch_id"], worker.scope
        cid = f"{did}-c{i}"
        candidate = deepcopy(dispatch)
        candidate["dispatch_id"] = cid
        entry: dict[str, Any] = {
            "index": i, "dispatch_id": cid, "handle": None, "workspace": None,
            "native_home": None, "session": None, "state": "preparing",
            "receipt_digest": None, "usage": None,
        }  # fmt: skip
        entries.append(entry)
        assert_execution_live(self.runtime, worker, dispatch)
        self._beat(worker, dispatch, beat)
        # listed before anything is prepared: a pause or an abort finds it (``_stop_candidates``)
        self._update(worker, did, "observing", candidates=deepcopy(entries))
        handle: str | None = None
        try:
            ws = self.workspaces.materialize(scope, cid, base_snapshot)
            scratch[i] = ws
            prepared = (
                cast(Any, port).prepare(candidate, prompt, ws, options=options)
                if options is not None
                else port.prepare(candidate, prompt, ws)
            )
            home = prepared.get("native_home") if isinstance(prepared, dict) else None
            entry.update(workspace=str(ws), native_home=home if isinstance(home, str) else None)
            assert_execution_live(self.runtime, worker, dispatch)
            # ``launched``: recorded before ``port.start`` (like ``launching`` of the run), and
            # kept when a failure later rewrites ``state``: the process may have been spawned
            # although its handle never came back (``loop._candidates`` counts it as a turn)
            entry.update(state="starting", launched=True)
            self._update(worker, did, "observing", candidates=deepcopy(entries))
            self.store.assert_outside_tx()
            handle = port.start(prepared)
            entry.update(handle=handle, state="running")
            self._update(worker, did, "observing", candidates=deepcopy(entries))
            while True:
                if time.monotonic() - start >= timeout:
                    raise Hold("WORKER_DEADLINE", "Native run exceeded its wall budget")
                assert_execution_live(self.runtime, worker, dispatch)
                self._beat(worker, dispatch, beat)
                observation = port.poll(handle)
                if observation.get("session_handle") == session:
                    raise Hold("SESSION_REBIND", "A vote candidate never uses the bound session")
                if observation["state"] == "completed":
                    break
                if observation["state"] in STOPPED_STATES:
                    raise Hold(
                        "DRIVER_BOUNDARY", "Driver is not a successful completed turn",
                        details={"state": observation["state"], "candidate": i},
                    )  # fmt: skip
                time.sleep(self.poll_seconds)
            receipt: dict[str, Any] = port.collect(handle)
            if (
                receipt.get("process_stopped") is not True
                or receipt.get("provider_completed") is not True
            ):
                raise Hold(
                    "COLLECT_BOUNDARY",
                    "Driver receipt is not positive completion/termination evidence",
                )
            if receipt.get("session_handle") == session:
                raise Hold("SESSION_REBIND", "A vote candidate never uses the bound session")
            entry.update(
                state="collected", session=receipt.get("session_handle"),
                receipt_digest=digest(receipt), usage=receipt.get("usage"),
            )  # fmt: skip
            trace = None
            if capture:
                with contextlib.suppress(Exception):
                    trace = port_trace(port, handle)
            # never resumed (only candidate 0's session is the run's): released right away
            with contextlib.suppress(Exception):
                port.destroy(handle)
            entry["state"] = "released"
            self._update(worker, did, "observing", candidates=deepcopy(entries))
            result = self._evaluate(
                worker, dispatch, hooks, ws, i, receipt, entry,
                start=start, timeout=timeout, beat=beat,
            )  # fmt: skip
            self._update(worker, did, "observing", candidates=deepcopy(entries))
            return receipt, result, trace
        except Exception as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            stopped = True
            if entry["state"] in LIVE_CANDIDATE:
                # a prepared journal (and a seeded credential) as well as a running process:
                # cancel releases both; a journal never created counts as stopped
                stopped = self._stop_candidate(port, handle or cid)
                entry["state"] = "stopped" if code == "EXECUTION_PAUSED" else "failed"
            entry.update(error=code, process_stopped=stopped)
            with contextlib.suppress(Exception):
                self._update(worker, did, "observing", candidates=deepcopy(entries))
            if code in CANDIDATE_LOCAL and stopped:
                path = scratch.pop(i, None)
                if path is not None:
                    self._discard(path)
                return None
            raise

    def _content(self, scope: Any, ref: dict[str, Any]) -> Any:
        """A workspace snapshot's content (base + patch, or its files) without artifact ids: the
        refs inside are compared by content digest, and every admission names a new id."""
        artifacts = getattr(self.workspaces, "artifacts", None) or self.runtime.artifacts
        return _without_ids(json.loads(artifacts.read(scope, ref)))

    def _apply_candidate(
        self, scope: Any, did: str, index: int, workspace: Path, winner: Path
    ) -> None:
        """§5.1 M4: the run workspace is reset to the base and the selected candidate's patch is
        applied, before ``workspaces.collect``. The host materializes the winner's snapshot (the
        same base + its patch, ``git apply`` for a git workspace) as a fresh copy and the run
        workspace takes that copy's tree in place (its path stays the run's: the workspace
        manager knows its base by path). Afterwards the run workspace's snapshot must equal the
        winner's (VOTE_APPLY); the copy is always discarded. Nothing of the candidate's own
        scratch tree beyond its change reaches the run workspace."""
        snapshot = self.workspaces.snapshot(scope, winner)
        expected = self._content(scope, snapshot)
        fresh = self.workspaces.materialize(scope, f"{did}-v{index}", snapshot)
        try:
            for child in list(workspace.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in fresh.iterdir():
                target = workspace / child.name
                if child.is_dir() and not child.is_symlink():
                    shutil.copytree(child, target, symlinks=True)
                else:
                    shutil.copy2(child, target, follow_symlinks=False)
        finally:
            self._discard(fresh)
        if self._content(scope, self.workspaces.snapshot(scope, workspace)) != expected:
            raise Hold(
                "VOTE_APPLY", "The run workspace does not hold the selected candidate's change"
            )

    def _vote(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        port: Any,
        hooks: Any,
        k: int,
        *,
        prompt: str,
        base_snapshot: dict[str, Any],
        workspace: Path,
        handle: str,
        session: str,
        options: DispatchOptions | None,
        receipt: dict[str, Any],
        start: float,
        timeout: float,
        beat: dict[str, Any],
    ) -> tuple[dict[str, Any], list[tuple[int, dict[str, Any]]]]:
        """M4 (§5.1): after candidate 0 (the run's own finished turn) candidates 1..k-1 run one
        after another; each is evaluated by the host fast checks; ``hooks.select`` picks one and
        a winner other than 0 becomes the run workspace's tree. Returns the run's usage over
        every candidate turn (§5.3: separate sessions, so summed; a candidate whose usage is not
        known makes it unknown) and the candidates' kept traces. Scratch workspaces are always
        discarded."""
        did, scope = dispatch["dispatch_id"], worker.scope
        capture = self.trace_sink is not None and options is not None and options.capture_trace
        entries: list[dict[str, Any]] = [{
            "index": 0, "dispatch_id": did, "handle": handle, "workspace": str(workspace),
            "native_home": None, "session": session, "state": "collected",
            "receipt_digest": digest(receipt), "usage": receipt.get("usage"),
        }]  # fmt: skip
        receipts = [receipt]  # every candidate turn that ran (the run's usage)
        results: list[Any] = []
        scratch: dict[int, Path] = {}
        traces: list[tuple[int, dict[str, Any]]] = []
        stopped: dict[str, Any] | None = None
        try:
            results.append(self._evaluate(
                worker, dispatch, hooks, workspace, 0, receipt, entries[0],
                start=start, timeout=timeout, beat=beat,
            ))  # fmt: skip
            self._update(worker, did, "observing", candidates=deepcopy(entries))
            budget = dispatch["node"]["budget"].get("max_tokens")
            for i in range(1, k):
                used = _tokens(receipts)
                if used is None or type(budget) is not int or used >= CANDIDATE_BUDGET * budget:
                    # §5.1 M4: no further candidate at 80 % of the node budget (or unknown use)
                    stopped = {"before": i, "tokens": used, "node_max_tokens": budget}
                    break
                ran = self._candidate(
                    worker, dispatch, port, hooks, i, prompt=prompt, base_snapshot=base_snapshot,
                    session=session, options=options, start=start, timeout=timeout, beat=beat,
                    entries=entries, scratch=scratch, capture=capture,
                )  # fmt: skip
                if ran is None:  # left out; the turn ran, its usage is not known
                    receipts.append({"usage": None})
                    continue
                candidate_receipt, result, trace = ran
                receipts.append(candidate_receipt)
                results.append(result)
                if trace is not None:
                    traces.append((i, trace))
            # the last point a steering pause is taken over: on the bound session (candidate 0's)
            # with the run workspace as candidate 0 left it
            assert_execution_live(self.runtime, worker, dispatch)
            selected = hooks.select(list(results))
            evaluated = {e["index"] for e in entries if "fast_checks" in e}
            if type(selected) is not int or selected not in evaluated:
                raise RuntimeFault("WORKER_HOOKS", "A vote selects one evaluated candidate")
            # recorded before the run workspace changes: from here on no pause is taken over
            # (``stop_and_snapshot`` holds VOTE_SELECTED for a winner other than 0)
            self._update(
                worker, did, "observing", candidates=deepcopy(entries), selected=selected,
                candidates_stopped=stopped,
            )  # fmt: skip
            if selected:
                self._apply_candidate(scope, did, selected, workspace, scratch[selected])
        finally:
            for path in scratch.values():
                self._discard(path)
        return self._usage(scope, dispatch["run_id"], sum_usage(receipts)), traces

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
        aux_traces: AuxTraces | None = None,
    ) -> dict[str, Any]:
        """Run one admitted dispatch to evidence.

        ``aux_traces`` (Work 033 S13, §9.1): the run's captured auxiliary read-only turns
        (``StrategyRunner.trace_source``), joined to its trace at the single admission when the
        options capture; never part of the request digest (module docstring).

        ``deadline_seconds`` bounds this call's native run in place of ``max_seconds`` (Work 033
        S8, interfaces.md §3.4): the loop passes its remaining wall budget per call instead of
        writing the shared ``max_seconds``, which raced once trials ran concurrently. The node
        budget's ``max_wall_seconds`` still caps it, over every turn of the attempt.

        ``hooks`` (``strategy_runner.TurnHooks``, Work 033 S9): after each completed turn, while
        follow-ups remain, ``hooks.after_turn`` may return a message; the bound session is then
        resumed with it as dispatch ``<dispatch_id>-f<k>`` (module docstring, M3). Hooks with
        ``candidates`` 2..3 (S9b, vote) run the M4 candidates after the first turn instead. A
        replay of an execution stopped inside a follow-up recovers it (§14 Q16 (a), ``_recover``).
        """
        worker.require("worker.execute")
        limit = self._limit(deadline_seconds)
        followups_cap, candidates = _hooks_shape(hooks)
        did = dispatch["dispatch_id"]
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
        if followups_cap or candidates > 1:
            # only hooks that can resume or vote: an existing dispatch replays with its digest
            request["hooks_digest"] = hooks.spec_digest()
        request_digest = digest(request)
        prior = self._existing(worker, did, request_digest)
        if prior is not None:
            if prior["state"] == "verifying":
                done: dict[str, Any] = prior["data"]["result"]
                return done
            return self._recover(
                worker, dispatch, prior, hooks=hooks, cap=followups_cap, options=options,
                limit=limit, output_paths=output_paths, aux_traces=aux_traces,
            )  # fmt: skip
        self._claim_local(did)
        try:
            return self._execute(
                worker, dispatch, prompt=prompt, base_snapshot=base_snapshot,
                output_paths=output_paths, planning_receipt=planning_receipt, options=options,
                hooks=hooks, limit=limit, followups_cap=followups_cap, candidates=candidates,
                request_digest=request_digest, aux_traces=aux_traces,
            )  # fmt: skip
        finally:
            self._release_local(did)

    def _execute(
        self,
        worker: Actor,
        dispatch: dict[str, Any],
        *,
        prompt: str,
        base_snapshot: dict[str, Any],
        output_paths: dict[str, str],
        planning_receipt: dict[str, Any] | None,
        options: DispatchOptions | None,
        hooks: Any,
        limit: float,
        followups_cap: int,
        candidates: int,
        request_digest: str,
        aux_traces: AuxTraces | None,
    ) -> dict[str, Any]:
        did = dispatch["dispatch_id"]
        scope = worker.scope
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
                        details={
                            "state": observation["state"],
                            "failure": observation.get("failure"),
                        },
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
            usage: dict[str, Any] | None = None
            traces: list[tuple[int, dict[str, Any]]] | None = None
            if candidates > 1:  # M4: the vote picks the change ``output_ready`` reports
                usage, traces = self._vote(
                    worker, dispatch, port, hooks, candidates, prompt=prompt,
                    base_snapshot=base_snapshot, workspace=Path(workspace), handle=handle,
                    session=bound, options=options, receipt=receipt, start=start,
                    timeout=timeout, beat=beat,
                )  # fmt: skip
            return self._complete(
                worker, dispatch, port, hooks, Path(workspace), session=bound, options=options,
                handles=handles, receipts=[receipt], followups=[], first=1, cap=followups_cap,
                start=start, timeout=timeout, beat=beat, output_paths=output_paths,
                aux_traces=aux_traces, usage=usage, extra_traces=traces,
            )  # fmt: skip
        except Exception as exc:
            self._fail(worker, did, port, handles, options, exc)
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
            # no session to checkpoint; a process ``port.start`` spawned before the handle was
            # recorded is still stopped (``_stop_spawned``), so none is left running
            stopped = self._stop_spawned(port, did, data)
            if stopped is not None:
                self._update(
                    worker, did, "held" if stopped else h["state"], hold_code="SPAWN_UNKNOWN",
                    process_stopped=stopped,
                )  # fmt: skip
            raise Hold(
                "SPAWN_UNKNOWN", "Reconcile provider creation outcome before checkpoint",
                details={"process_stopped": stopped},
            )  # fmt: skip
        if data.get("selected") not in (None, 0):
            # §5.1 M4: the run workspace holds another candidate's tree than the bound session
            raise Hold(
                "VOTE_SELECTED", "A vote run is not resumable after another candidate was selected"
            )
        # §14 Q16 (b): a vote candidate still holding a process is stopped first; the bound
        # session (candidate 0's, ``driver_handle``) is the one checkpointed, with the run
        # workspace as candidate 0 left it (candidates ran in scratch workspaces)
        candidates: dict[str, Any] = {}
        if data.get("candidates"):
            ok, entries = self._stop_candidates(port, data)
            candidates = {"candidates": entries}
            if not ok:
                self._update(worker, did, h["state"], **candidates)
                return {"process_stopped": False}
        # §14 Q16 (a): a follow-up the worker resumed but never recorded as ``driver_handle`` is
        # stopped first; the bound session is checkpointed from ``driver_handle``, the turn before
        follow = self._unrecorded_followup(data)
        if follow is not None and not self._halt_followup(port, follow, kind):
            return {"process_stopped": False}
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
            **candidates,
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
        if not handle and not data.get("prepared_digest"):
            return True  # ``port.start`` was never reached: nothing was spawned
        port = self.registry.resolve(
            worker.scope,
            data["dispatch"]["profile"]["driver_profile_ref"],
            data["dispatch"]["node"]["strategy"],
        )
        if not handle:
            # started but never recorded (the worker died in between): stop it by dispatch id
            spawned = self._stop_spawned(port, did, data)
            if spawned is None:
                return True  # no driver journal: nothing was spawned
            self._update(worker, did, "held", hold_code="CONTROLLER_ABORT", process_stopped=spawned)
            return spawned
        # vote candidates too (§14 Q16 (b)): every process of the run, not only driver_handle
        candidates: dict[str, Any] = {}
        all_stopped = True
        if data.get("candidates"):
            all_stopped, entries = self._stop_candidates(port, data)
            candidates = {"candidates": entries}
        # §14 Q16 (a): and a follow-up resumed but never recorded as ``driver_handle``
        follow = self._unrecorded_followup(data)
        if follow is not None:
            all_stopped = self._stop_candidate(port, follow) and all_stopped
        stopped = False
        with contextlib.suppress(Exception):
            stopped = port.cancel(handle).get("process_stopped") is True
        if stopped:
            with contextlib.suppress(Exception):
                port.destroy(handle)
        stopped = stopped and all_stopped
        self._update(
            worker, did, "held", hold_code="CONTROLLER_ABORT", process_stopped=stopped,
            **candidates,
        )  # fmt: skip
        return stopped

    @staticmethod
    def _stop_spawned(port: Any, did: str, data: dict[str, Any]) -> bool | None:
        """Stop the process ``port.start`` may have spawned for ``did`` although ``driver_handle``
        was never recorded: ``_execute`` records ``launching`` (with ``prepared_digest``) before
        ``port.start`` and the returned handle only after it, so a worker that died in between
        leaves ``driver_handle`` null (the head ``launching``, or ``held`` when ``start`` raised).
        Every port returns the dispatch id as its handle (``CliDriver.start``, ``RecipePort``,
        ``OpenCodePort``), and the dispatch's driver journal exists once ``port.prepare`` ran:
        that handle is cancelled (a ``prepared`` journal is closed, so it can never start; a
        spawned process is stopped by name) and, once the stop is confirmed, destroyed.

        None when nothing can have been spawned (a handle is recorded, ``port.start`` was never
        reached, or the dispatch has no driver journal: SESSION_NOT_FOUND); True when the stop
        is confirmed; False otherwise."""
        if data.get("driver_handle") or not data.get("prepared_digest"):
            return None
        try:
            stopped = port.cancel(did).get("process_stopped") is True
        except RuntimeFault as exc:
            if exc.code == "SESSION_NOT_FOUND" and not isinstance(exc, Hold):
                return None
            return False
        except Exception:
            return False
        if stopped:
            with contextlib.suppress(Exception):
                port.destroy(did)
        return stopped

    @staticmethod
    def _unrecorded_followup(data: dict[str, Any]) -> str | None:
        """The follow-up whose process may run while ``driver_handle`` does not name it.

        ``_followup`` records ``followup_dispatch_id`` before ``port.resume`` and writes the
        returned handle to ``driver_handle`` only after it; a worker that died in between leaves
        ``driver_handle`` on the finished turn before (§14 Q16 (a)). Returns that follow-up's id
        (``<dispatch_id>-f<k>``, the last recorded entry), else None."""
        follow, entries = data.get("followup_dispatch_id"), data.get("followups")
        if not isinstance(follow, str) or not isinstance(entries, list) or not entries:
            return None
        did, k = data["dispatch"]["dispatch_id"], len(entries)
        last = entries[-1]
        before = did if k == 1 else f"{did}-f{k - 1}"
        if (
            follow != f"{did}-f{k}"
            or not isinstance(last, dict)
            or last.get("dispatch_id") != follow
            or data.get("driver_handle") != before
        ):
            return None
        return follow

    @staticmethod
    def _halt_followup(port: Any, handle: str, kind: str) -> bool:
        """Pause (or cancel) an unrecorded follow-up's process, never destroying it. True when it
        is confirmed stopped, or when its journal does not exist (the resume never prepared it)."""
        try:
            result = port.pause(handle) if kind == "pause" else port.cancel(handle)
        except RuntimeFault as exc:
            return exc.code == "SESSION_NOT_FOUND"
        except Exception:
            return False
        return result.get("process_stopped") is True

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
        traced = data.get("trace_handles")  # S13: only a capturing run recorded them
        extra = {"trace_handles": [*traced, handle]} if isinstance(traced, list) else {}
        self._update(worker, did, "resuming", driver_handle=handle, **extra)
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
        aux_traces: AuxTraces | None = None,
    ) -> dict[str, Any]:
        """Collect a resumed turn only after the controller commits the new lease.

        ``options``, when given, must be the options the run was dispatched with (the resumed
        turn already runs under them, ``resume_exact``); Hold DISPATCH_OPTIONS_BINDING otherwise.
        ``deadline_seconds`` bounds the resumed turn as in ``execute`` (Work 033 S8).

        S13 (§9.1): when the run was dispatched with capturing options (the stored ones), its
        trace is admitted here after the last collect, as ``execute`` does for a run that was not
        steered: every turn of the attempt (``trace_handles``) and the ``aux_traces`` turns.
        """
        from copy import deepcopy

        limit = self._limit(deadline_seconds)
        did, h = self._run_execution(worker, run_id)
        data = h["data"]
        stored = DispatchOptions.from_wire(data["options"]) if data.get("options") else None
        if (
            options is not None
            and (None if options.is_default() else options.digest())
            != (None if stored is None or stored.is_default() else stored.digest())
            and not (
                stored is None
                and capture_only(options)
                and self._takes_no_options(worker.scope, data)
            )
        ):
            # S13: a port without dispatch options ran a capturing trial with none
            # (``_check_options``), so its resumed turn keeps none as well
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
            # S13: after the last collect, before the outputs are admitted (never raises); the
            # stored options decide, as they are what the run ran with (``_check_options``)
            traced = data.get("trace_handles")
            handles = [h for h in traced if isinstance(h, str)] if isinstance(traced, list) else []
            self._admit_trace(dispatch, port, handles or [handle], stored, aux_traces)
            outputs = self.workspaces.collect(
                worker.scope,
                Path(data["workspace"]),
                data["output_paths"],
                dispatch["node"],
                process_stopped=True,
            )
            # S9b (§5.3): vote candidates that ran before the pause are separate sessions, so
            # their turns are added to the resumed turn's usage (unknown when one is unknown)
            ran = [
                c for c in (data.get("candidates") or [])[1:]
                if isinstance(c, dict) and c.get("handle")
            ]  # fmt: skip
            used = (
                sum_usage([receipt, *({"usage": c.get("usage")} for c in ran)]) if ran else receipt
            )
            result = self.runtime.output_ready(
                worker,
                run_id,
                dispatch["lease"]["lease_id"],
                dispatch["lease"]["fencing_token"],
                outputs,
                usage=self._usage(worker.scope, run_id, used),
                process_stopped=True,
            )
            self._update(
                worker, did, "verifying", dispatch=dispatch, result=result,
                **driver_lookups(port, [*handles, handle], data),
            )  # fmt: skip
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
            traced = data.get("trace_handles")
            turns = [t for t in traced if isinstance(t, str)] if isinstance(traced, list) else []
            self._update(
                worker,
                did,
                "held",
                hold_code=getattr(exc, "code", type(exc).__name__),
                process_stopped=stopped,
                **driver_lookups(port, [*turns, handle], data),
            )
            raise


def driver_lookups(
    port: Any, handles: list[str], data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Operator decision (C), 2026-10-08: ``{"answer_lookup": [...]}`` with the answer-lookup
    evidence the driver keeps for ``handles`` and for the vote candidates in ``data``
    (``CliDriver.answer_lookup``, read through the port as ``port_trace`` reads a trace;
    ``agent_drivers/answer_lookup.py``), each entry with its dispatch id; ``{}`` when there is
    none. A port without the reader adds nothing; it never raises and never calls the port's
    lifecycle methods (``poll``, ``collect``). The trial executor reads it from the execution head
    (``meta_harness/local_executor.py``)."""
    fn = getattr(port, "answer_lookup", None)
    if not callable(fn):
        fn = getattr(getattr(port, "driver", None), "answer_lookup", None)
    if not callable(fn):
        return {}
    wanted = list(dict.fromkeys(h for h in handles if isinstance(h, str)))
    for entry in (data or {}).get("candidates") or []:
        if isinstance(entry, dict) and isinstance(entry.get("handle"), str):
            wanted.append(entry["handle"])
    found: list[dict[str, Any]] = []
    for handle in dict.fromkeys(wanted):
        try:
            entries = fn(handle)
        except Exception:
            continue
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                found.append({**entry, "dispatch_id": handle})
    return {"answer_lookup": found} if found else {}
