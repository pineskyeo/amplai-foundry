"""The in-process execution loop (D-066): approved goal → attempts → verified → publish.

One goal at a time (single operator, one write scope). For each attempt the loop claims the
goal's work, runs the registered driver port through ``WorkCoordinator`` on a git workspace
(the base commit, or base + the previous attempt's patch on a repair), verifies every
acceptance with the installed runners, and lets ``finish_work`` decide: succeeded, ready for
another attempt (the design's repair loop, first attempt included in
``max_verifier_repair_attempts_including_initial``), or failed. A driver failure is not
retried; the goal stops with the reason. The wall-clock budget is the contract's
``max_wall_seconds`` from approval.

Stopping (cancel, timeout, driver failure) revokes the approval first, so a running attempt
fails its next liveness check and its container is stopped by the coordinator, then records
the terminal status. ``main`` and the operator's checkout are never written here.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from ...sandbox.git_workspace import BASE_MEDIA, PATCH_BINDING
from ..contracts.authority import Actor
from ..contracts.identity import canonical, digest, new_id, now
from ..contracts.intake import check_goal_access, is_intake, stop_requested
from ..errors import Hold, RuntimeFault
from . import context_assembly, policies, prompts
from .cells import DispatchOptions, resolve_options
from .product import PORT, LocalExecutionService
from .steering import SteeringService
from .strategy_runner import StrategyChoice, node_app

# the v1 feedback form's tail; the form of a goal comes from its composition (D-096)
FEEDBACK_TAIL = policies.V1["feedback_form"]["tail_chars"]


def _utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()


UPSTREAM_PATCH = 60_000  # characters of an upstream patch shown to a downstream node


class _Replan(Exception):
    """The operator replanned the running goal; its attempt stopped at a boundary."""

    def __init__(self, reason: str, steering_id: str) -> None:
        super().__init__(reason)
        self.reason, self.steering_id = reason, steering_id


def _node_app(plan: dict[str, Any], node: dict[str, Any] | None) -> str:
    """``plan["node_apps"]`` first (Work 033 S9: ``node-<app>.s<k>`` / ``.p<k>`` / ``.int``), else
    ``node-<app>`` (product._compile); older one-node plans name plan["app"]."""
    return node_app(plan, node)


class ExecutionLoop:
    def __init__(
        self,
        service: LocalExecutionService,
        coordinator: Any,
        *,
        publisher: Callable[[str], dict[str, Any]] | None = None,
        tracker: Any = None,
        clock: Callable[[], float] = time.time,
        idle_seconds: float = 2.0,
    ) -> None:
        self.service, self.coordinator, self.publisher = service, coordinator, publisher
        self.tracker = tracker  # PullRequestTracker: PR outcomes while idle (D-078)
        self.clock, self.idle = clock, idle_seconds
        self.store, self.scope = service.store, service.scope
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: set[str] = set()
        self._lock = threading.Lock()
        self.steering = SteeringService(service.runtime)
        self._steering: dict[str, dict[str, Any]] = {}  # goal -> pending operator steering
        self._in_attempt: set[str] = set()  # goals whose attempt process is running

    # -- operator controls --------------------------------------------------------------------
    def cancel(self, operator: Actor, goal_id: str, *, reason: str = "") -> dict[str, Any]:
        """Stop the goal. ``goal.cancel`` (Work 034 D-112) or, as before, ``execution.approve``.
        A front agent (intake actor) cancels only its linked operator's goals and states why; that
        stop is recorded for the operator (``intake.stop_requested``)."""
        operator.require_any("goal.cancel", "execution.approve")
        check_goal_access(self.store, operator, goal_id)
        why = reason.strip()[:4096]
        if is_intake(operator) and not why:
            raise Hold("CANCEL_REASON", "A cancel through the front agent states why")
        plan = self.service.plan_record(goal_id)
        if plan["status"] in {"verified", "published", "failed", "cancelled", "timed_out", "held"}:
            return plan
        if is_intake(operator):
            with self.store.tx() as db:
                stop_requested(self.store, db, operator, goal_id, "cancel", why)
        with self._lock:
            self._cancel.add(goal_id)
        if plan.get("decision_ref"):
            self.service.revoke(operator, goal_id)
        if plan["status"] in {
            "awaiting_approval",
            "needs_answers",
            "replan_failed",
            "escalation_pending",
        }:
            self._end(goal_id, "cancelled", "cancelled by the operator")
            return self._finish(goal_id, "cancelled", reason="cancelled by the operator")
        return {**plan, "status": "cancelling"}

    def steer(self, operator: Actor, goal_id: str, text: str) -> dict[str, Any]:
        """Operator guidance for the running attempt (D-082): the agent is stopped at a process
        boundary, checkpointed, and its exact session resumes with the message. The contract and
        acceptance do not change; to change them, replan."""
        operator.require("goal.steer")
        check_goal_access(self.store, operator, goal_id)
        message = text.strip()[:4000]
        if not message:
            raise Hold("STEER_TEXT", "Steering needs a message")
        return self._request(operator, goal_id, "pause", message, kind="steer")

    def replan(self, operator: Actor, goal_id: str, reason: str) -> dict[str, Any]:
        """Change a running goal's plan (D-082): its attempt stops at a process boundary, the
        goal is blocked, the planner drafts the next contract revision with the reason, and
        nothing runs until the operator approves that revision."""
        operator.require("goal.steer")
        check_goal_access(self.store, operator, goal_id)
        why = reason.strip()[:4000]
        if not why:
            raise Hold("REPLAN_REASON", "Replanning needs a reason")
        return self._request(operator, goal_id, "acceptance_change", why, kind="replan")

    def _request(
        self, operator: Actor, goal_id: str, steering_kind: str, text: str, *, kind: str
    ) -> dict[str, Any]:
        plan = self.service.plan_record(goal_id)
        code = "STEER_NOT_RUNNING" if kind == "steer" else "REPLAN_NOT_RUNNING"
        with self._lock:
            # only while an attempt's process runs: between attempts a pause would block the
            # next claim, and before approval there is no active contract to revise
            if plan["status"] != "running" or goal_id not in self._in_attempt:
                raise Hold(
                    code,
                    "Only while an attempt runs; before approval, cancel and submit again",
                    details=plan["status"],
                )
            if goal_id in self._steering:
                raise Hold("STEER_PENDING", "A steering request is already being applied")
            goal = self.store.head(self.scope, "goal", goal_id)
            queued = self.steering.receive(
                operator, goal_id, steering_kind, text,
                expected_contract_ref=goal["data"]["active_contract_ref"], key=new_id(kind),
            )  # fmt: skip
            self._steering[goal_id] = {
                "operator": operator, "pause_id": queued["steering_id"], "text": text,
                "kind": kind,
            }  # fmt: skip
        status = "steering" if kind == "steer" else "replanning"
        return {"goal_id": goal_id, "status": status, "steering_id": queued["steering_id"]}

    def _execute(
        self,
        goal_id: str,
        dispatch: dict[str, Any],
        prompt: str,
        base: dict[str, Any],
        *,
        options: DispatchOptions | None = None,
        deadline_seconds: float | None = None,
        hooks: Any = None,
        aux_traces: Any = None,
    ) -> bool:
        """Run one attempt; take over whenever operator steering paused it. True if steered.

        The first turn and every resumed turn are the same attempt: each accepts steering and
        replanning while its process runs, so an operator can steer again (or replan) during a
        steered turn. A paused process is never left behind: when no steering request stands
        behind the pause, or the steering cannot be applied, the process is stopped (abort)
        before the attempt fails. A request that arrives as a turn ends is withdrawn.

        ``options`` are the goal's resolved dispatch options and ``deadline_seconds`` the wall
        budget left at the attempt's start; both go to every turn of the attempt as call
        arguments (Work 033 S8), never into the shared coordinator. ``hooks`` are the strategy's
        turn hooks (Work 033 S9, M3); a resumed turn after steering runs without them.
        ``aux_traces`` (Work 033 S13, §9.1) is the node's ``StrategyRunner.trace_source``: the
        worker calls it once, only for a capturing dispatch, at the run's single trace admission
        (in ``execute``, or in ``continue_resumed`` for a steered run), so the run's reviewer and
        investigator turns join its trace.
        """
        worker = self.service.actors.worker
        # passed only when set: a coordinator stand-in without hooks or aux_traces keeps working
        traced = {"aux_traces": aux_traces} if aux_traces is not None else {}
        extra = {**({"hooks": hooks} if hooks is not None else {}), **traced}

        def first() -> Any:
            return self.coordinator.execute(
                worker, dispatch, prompt=prompt, base_snapshot=base,
                output_paths={PORT: PATCH_BINDING}, options=options,
                deadline_seconds=deadline_seconds, **extra,
            )  # fmt: skip

        def resumed() -> Any:
            return self.coordinator.continue_resumed(
                worker, dispatch["run_id"], options=options, deadline_seconds=deadline_seconds,
                **traced,
            )  # fmt: skip

        turn, steered = first, False
        while True:
            steer: dict[str, Any] | None = None
            with self._lock:
                self._in_attempt.add(goal_id)
            try:
                try:
                    turn()
                finally:
                    with self._lock:
                        self._in_attempt.discard(goal_id)
                        steer = self._steering.pop(goal_id, None)
            except Exception as exc:
                paused = getattr(exc, "code", None) == "EXECUTION_PAUSED"
                if paused and steer is not None:
                    self._take_over(goal_id, dispatch, steer)
                    turn, steered = resumed, True
                    continue
                if paused:  # a pause nobody here will quiesce (e.g. another client's steering)
                    self._abort(dispatch)
                elif steer is not None:
                    self._withdraw(goal_id, steer, "the turn ended before the request reached it")
                raise
            if steer is not None:
                self._withdraw(goal_id, steer, "the turn finished before the request reached it")
            return steered

    def _take_over(self, goal_id: str, dispatch: dict[str, Any], steer: dict[str, Any]) -> None:
        """Replan: stop the turn and raise _Replan. Steer: stop at a checkpoint and resume the
        exact session with the message (the caller then continues the resumed turn)."""
        try:
            if steer["kind"] == "replan":
                self._quiesce(steer, "cancelled")
                raise _Replan(steer["text"], steer["pause_id"])
            self._apply_steering(goal_id, dispatch, steer)
        except _Replan:
            raise
        except Exception as exc:
            self._abort(dispatch)  # what the steering could not finish must not keep running
            code = str(getattr(exc, "code", type(exc).__name__))
            self._withdraw(goal_id, steer, "not applied: " + code)
            raise

    def _quiesce(self, steer: dict[str, Any], expected: str) -> None:
        worker = self.service.actors.worker
        quiesced = self.steering.quiesce(
            self._controller(), steer["pause_id"],
            lambda run_id, kind: self.coordinator.stop_and_snapshot(worker, run_id, kind),
        )  # fmt: skip
        # every stopped run must reach the boundary the request asked for (paused for steering,
        # cancelled for a revision); an unconfirmed process or effect leaves the run as it was
        outcomes = quiesced.get("outcomes") or []
        if not outcomes or any(o["status"] != expected for o in outcomes):
            raise Hold(
                "STEER_BOUNDARY", "The agent did not stop at a confirmed boundary",
                details=quiesced,
            )  # fmt: skip

    def _abort(self, dispatch: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.coordinator.abort(self.service.actors.worker, dispatch["run_id"])

    def _withdraw(self, goal_id: str, steer: dict[str, Any], reason: str) -> None:
        with contextlib.suppress(Exception):
            self.steering.withdraw(self._controller(), steer["pause_id"], reason)
        plan = self.service.plan_record(goal_id)
        entry = {"text": steer["text"], "kind": steer["kind"], "at": now(), "applied": False,
                 "reason": reason}  # fmt: skip
        self._update(goal_id, steering=[*(plan.get("steering") or []), entry])

    def _apply_steering(
        self, goal_id: str, dispatch: dict[str, Any], steer: dict[str, Any]
    ) -> None:
        worker, controller = self.service.actors.worker, self._controller()
        self._quiesce(steer, "paused")
        goal = self.store.head(self.scope, "goal", goal_id)
        resume = self.steering.receive(
            steer["operator"], goal_id, "resume", steer["text"],
            expected_contract_ref=goal["data"]["active_contract_ref"], key=new_id("steer"),
        )  # fmt: skip
        message = (
            "Operator steering from the human operator (it does not change the objective or "
            "the acceptance commands):\n" + steer["text"] + "\nContinue the same task with this "
            "guidance, then run the acceptance commands again."
        )
        self.steering.resume(
            controller, resume["steering_id"], worker,
            lambda run_id, cp: self.coordinator.resume_exact(worker, run_id, cp, prompt=message),
        )  # fmt: skip
        # applied: the exact session now has the message; its turn runs as part of the attempt
        plan = self.service.plan_record(goal_id)
        entry = {"text": steer["text"], "kind": "steer", "run_id": dispatch["run_id"],
                 "at": now(), "applied": True}  # fmt: skip
        self._update(goal_id, steering=[*(plan.get("steering") or []), entry])

    # -- the loop --------------------------------------------------------------------------------
    def reconcile(self) -> list[dict[str, Any]]:
        """Bring runtime state in line with plan records (startup; after a crash or old bugs).

        - a stopped plan whose runtime goal is still open: end it (releases its claims)
        - a plan left ``running`` by a dead process: the attempt is lost; end it failed
        - a plan ``held`` before any attempt with a valid approval: back to ``approved``
        """
        actions: list[dict[str, Any]] = []
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id, state FROM heads WHERE tenant=? AND project=? AND kind=?",
                (*self.scope.keys(), "execution-plan"),
            ).fetchall()
        for row in rows:
            goal_id, status = row["id"], row["state"]
            try:
                goal_state = self.store.head(self.scope, "goal", goal_id)["state"]
            except RuntimeFault:
                continue
            plan = self.service.plan_record(goal_id)
            if status == "running":
                self._stop_goal(
                    goal_id, "failed", "interrupted by a server restart", plan.get("attempts") or []
                )
                actions.append({"goal_id": goal_id, "action": "ended_interrupted"})
            elif status == "held" and not plan.get("attempts") and self._approval_valid(plan):
                self._update(goal_id, status="approved", reason=None)
                actions.append({"goal_id": goal_id, "action": "requeued"})
            elif status == "escalation_pending":
                # stopped between ending the revision and compiling the next (M6): the goal
                # failed on its cell; the next revision is not compiled after a restart
                self._end(goal_id, "failed", "escalation interrupted by a server restart")
                self._finish(goal_id, "failed", reason="escalation interrupted by a restart")
                actions.append({"goal_id": goal_id, "action": "ended_escalation_pending"})
            elif status in {"failed", "held", "timed_out", "cancelled"} and goal_state not in {
                "verified",
                "failed",
                "cancelled",
            }:
                self._end(goal_id, "cancelled" if status == "cancelled" else "failed", status)
                actions.append({"goal_id": goal_id, "action": "ended_" + status})
        return actions

    def _approval_valid(self, plan: dict[str, Any]) -> bool:
        ref = plan.get("decision_ref")
        if not ref:
            return False
        try:
            decision = self.service.authority.resolver(self.scope, ref)
        except (Hold, RuntimeFault):
            return False
        return decision.get("status") == "approved" and not decision.get("revoked")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.reconcile()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="amplai-execution", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            goal = self.next_goal()
            if goal is None:
                self.idle_tick()
                self._stop.wait(self.idle)
                continue
            try:
                self.run_goal(goal)
            except Exception as exc:  # recorded, never kills the loop
                self._finish(goal, "held", reason=f"{type(exc).__name__}: {exc}")

    def idle_tick(self) -> None:
        """Work done only when no goal is waiting: read draft PR outcomes when due."""
        if self.tracker is not None and self.tracker.due():
            with contextlib.suppress(Exception):  # an outage of gh never stops the loop
                self.tracker.sync()

    def next_goal(self) -> str | None:
        """The oldest approved goal that is not a trial. Trial goals are run only by the trial
        executor (IC-03); one left approved by a crash is closed there, never run here."""
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='execution-plan' "
                "AND state='approved' ORDER BY rowid",
                self.scope.keys(),
            ).fetchall()
        for row in rows:
            if not self.service.plan_record(row["id"]).get("trial"):
                return str(row["id"])
        return None

    def run_goal(self, goal_id: str) -> dict[str, Any]:
        svc = self.service
        plan = svc.plan_record(goal_id)
        if plan["status"] != "approved":
            raise Hold("PLAN_NOT_APPROVED", "Only an approved goal runs")
        try:  # read once, before any claim: a composition the loop cannot honour never runs
            context, budget = self._policies(plan)
        except (Hold, RuntimeFault) as exc:
            parts = exc.details if isinstance(exc.details, list) else []
            named = f" ({'; '.join(str(p) for p in parts)})" if parts else ""
            reason = f"harness components: {exc.code}: {exc.message}{named}"[:600]
            return self._stop_goal(goal_id, "held", reason, [])
        try:  # the activated profile's model and effort, the L5 driver options (S8)
            options = self._options(goal_id, plan, budget)
        except (Hold, RuntimeFault) as exc:
            reason = f"dispatch options: {exc.code}: {exc.message}"[:600]
            return self._stop_goal(goal_id, "held", reason, [])
        runner = self.service.strategy_runner()  # Work 033 S9: the goal's strategy (§5)
        try:
            return self._run_attempts(goal_id, plan, context, options, runner, budget)
        finally:
            runner.forget(goal_id)

    def _run_attempts(
        self,
        goal_id: str,
        plan: dict[str, Any],
        context: policies.ContextPolicy,
        options: DispatchOptions,
        runner: Any,
        budget: policies.BudgetPolicy | None = None,
    ) -> dict[str, Any]:
        """Claim -> attempt -> protected verification until the graph is done or stops.

        The strategy (``StrategyRunner``, Work 033 S9) gives each attempt its base, feedback,
        hooks and extra prompt sections (M1, M3, M5) and decides after a failed verification
        (retry, escalate, stop); ``finish_work`` stays the only judge of an attempt.

        Work 033 S10 (§6.1): per attempt the L4 decision (context parts) before the prompt, the
        L7 decision before the final verification (fast checks as M3 follow-ups of the attempt,
        never a verdict) and, after a failed verification, the L6 decision (retry, escalate,
        stop). Each writes a harness-decision named in ``plan["decisions"]``."""
        budget = budget or policies.v1_budget(self.service.budget.wire())
        svc, worker, verifier = (
            self.service,
            self.service.actors.worker,
            self.service.actors.verifier,
        )
        contract = self.store.get(self.scope, "goal-contract", plan["contract_ref"])
        graph = self.store.get(self.scope, "workgraph", plan["graph_ref"])
        deadline = _utc(plan["approved_at"]) + contract["budget"]["max_wall_seconds"]
        attempts: list[dict[str, Any]] = []
        self._update(goal_id, status="running", attempts=attempts)
        try:  # M2 before the first claim (parallel read-only investigators): no lease waits
            runner.prepare(svc.plan_record(goal_id))
        except (Hold, RuntimeFault) as exc:  # a failed turn is recorded; this is the copy etc.
            reason = f"strategy: {exc.code}: {exc.message}"[:600]
            return self._stop_goal(goal_id, "held", reason, attempts)
        while True:
            if goal_id in self._cancel:
                return self._stop_goal(goal_id, "cancelled", "cancelled by the operator", attempts)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return self._stop_goal(goal_id, "timed_out", "wall-clock budget spent", attempts)
            try:
                dispatch = self.service.runtime.claim(worker, goal_id=goal_id)
            except Hold as exc:
                dispatch, why = None, f"{exc.code}: {exc.details}"
            else:
                why = "no claimable work (resource held elsewhere?)"
            if dispatch is None:
                if attempts:
                    return self._stop_goal(goal_id, "held", "claim failed: " + why, attempts)
                # nothing ran: keep the approval, park the goal for reconcile to requeue
                return self._finish(goal_id, "held", reason="claim failed: " + why, attempts=[])
            started = self.clock()
            node = dispatch["node"]
            app = _node_app(plan, node)
            number = 1 + sum(1 for a in attempts if a.get("node_id") == node["node_id"])
            try:  # after the claim: a fault here must end the goal, which frees its claims
                # M1/M3/M5: the strategy's base, feedback, hooks and sections for this attempt
                spec = runner.before_attempt(
                    svc.plan_record(goal_id), node, number, options=options
                )
                # L4 (S10): the context parts this attempt shows, within the manifest
                shown = self._context_for(goal_id, plan, node, context, attempts)
                prompt = self.prompt(
                    contract, plan, spec.feedback, node=node,
                    upstream=self._upstream(graph, node, plan), context=shown,
                    extra_sections=spec.extra_sections,
                )  # fmt: skip
                # L7 (S10, M3): fast checks before the final verification, as follow-up turns
                hooks = spec.hooks
                fast = (
                    self._fast_hooks(goal_id, plan, node, budget, context)
                    if hooks is None
                    else None
                )
                hooks = fast or hooks
            except Exception as exc:
                self._discard(dispatch["run_id"])
                reason = f"prompt: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600]
                return self._stop_goal(goal_id, "held", reason, attempts)
            try:
                # the remaining wall budget is this call's deadline (S8): writing the shared
                # coordinator.max_seconds raced once trials of one app run concurrently
                steered = self._execute(
                    goal_id, dispatch, prompt, spec.base, options=options,
                    deadline_seconds=max(1, int(remaining)), hooks=hooks,
                    aux_traces=runner.trace_source(goal_id, node["node_id"]),
                )  # fmt: skip
            except _Replan as replan:
                self._fast_decided(goal_id, fast)
                attempts.append(
                    {"run_id": dispatch["run_id"], "app": app, "node_id": node["node_id"],
                     "outcome": "replanned", "reason": replan.reason[:200],
                     "seconds": round(self.clock() - started, 1)}
                )  # fmt: skip
                self._discard(dispatch["run_id"])
                self._update(goal_id, status="replanning", attempts=attempts)
                try:
                    return self.service.replan(goal_id, replan.reason, replan.steering_id)
                except Exception as exc:
                    # the goal stays blocked; the operator cancels it (amplai cancel)
                    return self._finish(
                        goal_id, "replan_failed", attempts=attempts,
                        reason=f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600],
                    )  # fmt: skip
            except Exception as exc:
                self._fast_decided(goal_id, fast)
                attempts.append(
                    {"run_id": dispatch["run_id"], "app": app, "node_id": node["node_id"],
                     "outcome": "driver_failed",
                     "reason": getattr(exc, "code", type(exc).__name__),
                     "seconds": round(self.clock() - started, 1),
                     **self._turns(hooks, dispatch)}
                )  # fmt: skip
                self._discard(dispatch["run_id"])
                status = "cancelled" if goal_id in self._cancel else "held"
                reason = (
                    "cancelled by the operator"
                    if status == "cancelled"
                    else "driver failed: " + str(getattr(exc, "code", exc))
                )
                return self._stop_goal(goal_id, status, reason, attempts)
            work = self.store.head(self.scope, "work", dispatch["node"]["work_id"])
            change = work["data"]["outputs"][PORT]
            try:  # L7 (S10): recorded by the fast-check hook, or here before the verification
                self._fast_decided(goal_id, fast)
                if fast is None:
                    self._decide_checks(goal_id, plan, node, budget, change, hooks)
            except Exception as exc:
                self._discard(dispatch["run_id"])
                reason = f"decision: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600]
                return self._stop_goal(goal_id, "held", reason, attempts)
            try:
                verdicts = [
                    self.service.verification.verify(verifier, dispatch["run_id"], ac, change)
                    for ac in dispatch["node"]["acceptance_ids"]
                ]
                finished = self.service.verification.finish_work(verifier, dispatch["run_id"])
            except Exception as exc:  # never leave the goal open on a verification fault
                code = getattr(exc, "code", type(exc).__name__)
                attempts.append(
                    {"run_id": dispatch["run_id"], "app": app, "node_id": node["node_id"],
                     "outcome": "verify_failed", "reason": code,
                     "seconds": round(self.clock() - started, 1),
                     **self._turns(hooks, dispatch)}
                )  # fmt: skip
                self._discard(dispatch["run_id"])
                return self._stop_goal(goal_id, "held", f"verification: {code}", attempts)
            observations = [self._observation(v) for v in verdicts]
            attempts.append(
                {"run_id": dispatch["run_id"], "app": app, "node_id": node["node_id"],
                 "outcome": finished["outcome"],
                 "change": change, **({"steered": True} if steered else {}),
                 "verdicts": [{"acceptance": o["acceptance_id"], "outcome": o["outcome"],
                               "reason": o["reason"]} for o in observations],
                 "seconds": round(self.clock() - started, 1),
                 **self._turns(hooks, dispatch)}
            )  # fmt: skip
            self._update(goal_id, attempts=attempts)
            self._discard(dispatch["run_id"])
            state = self.store.head(self.scope, "work", node["work_id"])["state"]
            if state == "succeeded":
                if self._all_succeeded(graph):
                    break
                continue  # the next node whose dependencies are now met
            # M1/M6: another attempt of this node (finish_work allowed it within the node's
            # max_attempts), an escalation to the cascade's next cell, or the end; the L6
            # decider (S10) may choose among what is possible, its prior is exactly this rule
            decision = runner.on_failure(svc.plan_record(goal_id), node, observations)
            try:
                action = self._on_failure(
                    goal_id, node, observations, attempts, budget, runner, decision, state,
                    deadline=deadline, wall=float(contract["budget"]["max_wall_seconds"]),
                )  # fmt: skip
            except Exception as exc:  # the work is ready or failed: end the goal, free claims
                reason = f"decision: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600]
                return self._stop_goal(goal_id, "held", reason, attempts)
            if action == "retry" and state == "ready":
                continue
            if action == "escalate":
                return self._escalate(goal_id, attempts, runner)
            if decision == "retry":  # the L6 decider stopped what the attempt policy allowed
                return self._stop_goal(goal_id, "failed", "stopped by the L6 decider", attempts)
            return self._stop_goal(
                goal_id, "failed", "acceptance failed within the attempt budget", attempts
            )
        try:
            result = self.service.verification.finish_goal(verifier, goal_id)
        except Hold as exc:
            # the goal-level check (e.g. a cross-app integration command) failed: every node
            # passed its own suite, but the goal is not verified (D-081); replan from here
            detail = f"{exc.code}: {exc.message}"
            if isinstance(exc.details, dict) and exc.details.get("reason"):
                detail += f" ({exc.details['reason']})"
            return self._stop_goal(goal_id, "failed", "goal verification: " + detail, attempts)
        record = self._finish(goal_id, "verified", attempts=attempts, verification=result)
        # A trial goal never publishes, whoever runs it (IC-03): the loop checks the plan itself
        # instead of trusting that only the trial executor, built without a publisher, runs it.
        if self.publisher is not None and not plan.get("trial"):
            try:
                published = self.publisher(goal_id)
                opened = (
                    ("publication.opened", {"pr_url": published["pr_url"]})
                    if published.get("pr_url")
                    else None
                )
                record = self._update(
                    goal_id, event=opened, status="published", publication=published
                )
            except Exception as exc:
                record = self._update(
                    goal_id, publication={"error": getattr(exc, "code", type(exc).__name__),
                                          "message": str(exc)[:400]}
                )  # fmt: skip
        return record

    # -- helpers ---------------------------------------------------------------------------------
    def _turns(self, hooks: Any, dispatch: dict[str, Any] | None = None) -> dict[str, Any]:
        """The follow-up turns and reviewer rounds an attempt's hooks made (M3), and for a vote
        (M4, S9b) its candidates as the worker recorded them, for metrics."""
        if hooks is None:
            return {}
        out: dict[str, Any] = {
            "followups": int(getattr(hooks, "sent", 0) or 0),
            "reviewer_rounds": int(getattr(hooks, "rounds", 0) or 0),
        }
        if isinstance(hooks, FastCheckHooks):  # what the L7 fast checks found (S10)
            out["fast_checks"] = list(hooks.results)
        if getattr(hooks, "candidates", 1) != 1 and dispatch is not None:
            out.update(self._candidates(dispatch["dispatch_id"]))
        return out

    def _candidates(self, dispatch_id: str) -> dict[str, Any]:
        """A vote attempt's candidates from its worker-execution head (``worker.py``, M4): the
        candidate turns that ran (candidate 0 and every candidate whose process was launched:
        a recorded handle, or ``launched`` / state ``starting`` recorded before ``port.start``
        when the handle never came back, so ``agent_calls`` never undercounts a spawned turn),
        the selected index, each candidate's state, diff size and fast-check results, and why no
        further candidate started (``candidates_stopped``: 80 % of the node budget)."""
        try:
            data = self.store.head(self.scope, "worker-execution", dispatch_id)["data"]
        except RuntimeFault:
            return {"candidates": 0, "selected": None, "candidate_results": []}
        entries = [e for e in data.get("candidates") or [] if isinstance(e, dict)]
        ran = [
            e for e in entries
            if e.get("index") == 0 or e.get("handle") or e.get("launched") is True
            or e.get("state") == "starting"
        ]  # fmt: skip
        return {
            "candidates": len(ran),
            "selected": data.get("selected"),
            "candidate_results": [
                {k: e.get(k) for k in ("index", "state", "patch_lines", "fast_checks", "error")}
                for e in entries
            ],
            "candidates_stopped": data.get("candidates_stopped"),
        }

    def _escalate(
        self, goal_id: str, attempts: list[dict[str, Any]], runner: Any
    ) -> dict[str, Any]:
        """M6 (IC-05): the goal ends failed on its cell (approval revoked, runtime goal ended,
        status ``escalation_pending``), then the next revision is compiled on the cascade's next
        cell and waits for approval (``LocalExecutionService.escalate``)."""
        plan = self.service.plan_record(goal_id)
        to_cell = runner.next_cell(plan)
        cell = (plan.get("composition") or {}).get("cell_id")
        reason = f"acceptance failed within the attempt budget on {cell}; escalating to {to_cell}"
        self._stop_goal(goal_id, "escalation_pending", reason, attempts)
        try:
            record: dict[str, Any] = self.service.escalate(goal_id, to_cell=to_cell, reason=reason)
        except (Hold, RuntimeFault) as exc:
            return self._finish(
                goal_id, "failed", reason=f"escalation: {exc.code}: {exc.message}"[:600]
            )
        return record

    def _all_succeeded(self, graph: dict[str, Any]) -> bool:
        return all(
            self.store.head(self.scope, "work", n["work_id"])["state"] == "succeeded"
            for n in graph["nodes"]
        )

    def _upstream(
        self, graph: dict[str, Any], node: dict[str, Any], plan: dict[str, Any] | None = None
    ) -> list[tuple[str, str]]:
        """(app, verified patch text) of every node of another app this one consumes (D-081).

        A producer of the same app (Work 033 S9, M5) is not text: its verified change is the
        node's base (``StrategyRunner.before_attempt``)."""
        by_id = {n["node_id"]: n for n in graph["nodes"]}
        out = []
        for consumed in node.get("consumes") or []:
            producer = by_id.get(consumed.get("from_node") or "")
            if producer is None:
                continue
            if plan is not None and node_app(plan, producer) == node_app(plan, node):
                continue
            work = self.store.head(self.scope, "work", producer["work_id"])
            ref = (work["data"].get("outputs") or {}).get(consumed["output_name"])
            if work["state"] != "succeeded" or not ref:
                continue
            raw = self.service.workspaces.artifacts.read(self.scope, ref)
            _base, patch = self.service.workspaces.read_change(self.scope, raw)
            name = (
                node_app(plan, producer)
                if plan is not None
                else producer["node_id"][len("node-") :]
            )
            out.append((name, patch.decode(errors="replace")))
        return out

    def prompt(
        self,
        contract: dict[str, Any],
        plan: dict[str, Any],
        feedback: list[dict[str, Any]] | None,
        *,
        node: dict[str, Any] | None = None,
        upstream: list[tuple[str, str]] | None = None,
        context: policies.ContextPolicy | None = None,
        extra_sections: tuple[tuple[str, tuple[str, ...]], ...] = (),
    ) -> str:
        """The execution prompt. ``context`` is the goal composition's context policy (read from
        the plan's composition when not given); the v1 policy renders today's text (G1).

        ``extra_sections`` are the strategy's (Work 033 S9: plan steps, investigation notes,
        earlier parts, merged and conflicting parts), after the context sections and before the
        feedback; none for the v1 strategies, so their text is unchanged."""
        context = context or self._context_policy(plan)
        app = self.service.apps[_node_app(plan, node) if node else plan["app"]].config
        commands = {v.id: " ".join(v.argv) for v in app.verifiers}
        if plan.get("mode") == "design":
            commands = {"design": "the design document check (sections, sources, paths)"}
            lines = [
                "You are the DESIGNER for AMPLAI. The current directory is a copy of the "
                f"{app.app_id} repository at commit {plan['base_commit']} (no .git, network "
                "limited).",
                f"Write one design document: {plan['design_dir']}design.md. Change nothing "
                "else: no source, test or configuration file. Do not commit.",
                "Use exactly these '## ' sections: Goal, Current State, Options, Decision, Risks, "
                "Implementation Plan, Sources.",
                "Ground every statement about the current system in this repository and cite it "
                "as path:line (for example src/pkg/module.py:42); at least 3 citations, and "
                "every citation must name a committed regular file here exactly (not a symlink, "
                "not the design directory). Implementation Plan lists the steps for a later "
                "work goal; do not implement them.",
                "",
                "Objective: " + contract["objective"],
            ]
        else:
            lines = [
                *prompts.render(
                    self._implementer_role(plan),
                    app_id=app.app_id,
                    base_commit=plan["base_commit"],
                ),
                "",
                "Objective: " + contract["objective"],
            ]
        draft = plan["draft"]
        if draft.get("in_scope"):
            lines.append("In scope: " + "; ".join(draft["in_scope"]))
        if contract["non_goals"]:
            lines.append("Do not: " + "; ".join(contract["non_goals"]))
        lines += ["Constraints: " + "; ".join(c["statement"] for c in contract["constraints"])]
        mapping = plan.get("acceptance_map")
        if node is not None and len(plan.get("work_items") or []) > 1:
            lines.append(f"This part of the goal, in {app.app_id}: {node['objective']}")
        lines.append("Acceptance (each must pass):")
        if mapping and node is not None:
            for ac in node["acceptance_ids"]:
                entry = mapping[ac]
                lines.append(
                    f"- {ac}: {entry['statement']}  command: `{commands[entry['verifier']]}`"
                )
        else:  # plans recorded before work items (Work 018)
            for a, d in zip(contract["acceptance"], draft["acceptance"], strict=False):
                lines.append(f"- {a['id']}: {a['statement']}  command: `{commands[d['verifier']]}`")
        for upstream_app, patch in upstream or []:
            lines += [
                "",
                f"The {upstream_app} change this builds on is already verified (do not redo it; "
                "it is applied in its own repository, not in this directory):",
                "```diff\n" + patch[-UPSTREAM_PATCH:] + "\n```",
            ]
        # context sections after acceptance/upstream, before feedback, only when enabled
        lines += context_assembly.sections(context, plan=plan, app=app, commands=commands)
        for title, body in extra_sections:
            lines += ["", title, *body]
        if feedback:
            lines += context_assembly.feedback(context.feedback_form, feedback)
        return "\n".join(lines)

    def _composition(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        ref = (plan.get("composition") or {}).get("ref")
        if not ref:
            return None  # planned before composition selection existed (Work 018)
        value: dict[str, Any] = self.store.get(self.scope, "harness-composition", ref)
        return value

    def _context_policy(self, plan: dict[str, Any]) -> policies.ContextPolicy:
        composition = self._composition(plan)
        if composition is None:
            return policies.v1_context()
        return policies.context_policy(self.store, self.scope, composition)

    def _policies(
        self, plan: dict[str, Any]
    ) -> tuple[policies.ContextPolicy, policies.BudgetPolicy]:
        """The goal composition's context and budget policies, checked for the §2.3 combination
        rules and for parts this loop honours (S3: env bootstrap, memory notes, the tail
        feedback form, the attempt policy, limits; a router equal to the one selection used).
        A part that switches on a mechanism of a later slice is refused, never ignored."""
        ceiling = self.service.budget.wire()
        composition = self._composition(plan)
        if composition is None:
            return policies.v1_context(), policies.v1_budget(ceiling)
        from ...meta_harness import deciders

        context = policies.context_policy(self.store, self.scope, composition)
        budget = policies.budget_policy(self.store, self.scope, composition, ceiling=ceiling)
        policies.check_combination(context.feedback_form, budget.attempt_policy, budget.limits)
        # the L4 decider is honoured since S10 (``_context_for``); its parts are checked below
        context_assembly.check_supported(replace(context, decider_l4=None))
        # driver_options (L5) are honoured since S8: resolve_options carries them to the port;
        # the execution strategies since S9 (vote since S9b, after §14 Q16), except a strategy
        # the plan refused; fast checks (L7, M3) and the deciders L4-L8 since S10
        refused = self.service.strategy_runner().refusal(plan, budget)
        unsupported = [refused] if refused is not None else []
        unsupported += self._fast_check_refusals(plan, budget)
        unsupported += deciders.check(self.store, self.scope, "L4", context.decider_l4)
        for layer in policies.BUDGET_DECIDERS:
            unsupported += deciders.check(self.store, self.scope, layer, budget.deciders[layer])
        unsupported += self._router_unsupported(plan, composition)
        if unsupported:
            raise RuntimeFault(
                "COMPONENT_CONTENT", "The execution loop does not honour these components yet",
                details=unsupported,
            )  # fmt: skip
        return context, budget

    def _options(
        self, goal_id: str, plan: dict[str, Any], budget: policies.BudgetPolicy
    ) -> DispatchOptions:
        """The goal's dispatch options (Work 033 S8, interfaces.md §3.4): model and effort of the
        activated profile, the budget policy's driver options, and the trace flag of a trial goal
        (``plan["trial"]["capture_trace"]``; a real goal never captures, D-100). Resolved once
        before any claim: an effort profile then runs with its effort instead of holding
        DISPATCH_OPTIONS_BINDING, and the worker still checks the binding before preparing."""
        from ...meta_harness import deciders

        profile = self.store.head(self.scope, "goal", goal_id)["data"]["profile"]
        trial = plan.get("trial") or {}
        capture = trial.get("capture_trace") is True
        # operator decision 2026-10-08 (agent_drivers/offline.py): a trial goal runs with every
        # web tool off; its driver must declare how, checked here, before any claim
        offline = bool(trial)
        if offline:
            self._require_offline(profile)
        # L5 (S10, §6.1): the driver options among the decider's driver_options versions, once
        # before the first dispatch (its features, cell and strategy, do not change in a goal);
        # the prior is the composition's own driver_options
        content, ref = budget.deciders.get("L5"), budget.refs.get("L5")
        manifest = budget.refs.get("driver_options")
        prior = deciders.option_id(manifest) if manifest else "driver_defaults"
        candidates: dict[str, dict[str, Any]] = {}
        why: dict[str, str] = {}
        for option_ref in (content or {}).get("options") or []:
            label = deciders.option_id(option_ref)
            parts = policies.component_content(
                self.store, self.scope, option_ref, "driver_options", "driver_options"
            )
            candidates[label] = parts
            try:
                resolve_options(
                    self.store, self.scope, profile, replace(budget, driver_options=parts),
                    capture_trace=capture, offline=offline,
                )  # fmt: skip
            except (Hold, RuntimeFault) as exc:
                why[label] = f"{exc.code}: {exc.message}"
        decision = self._decide(
            goal_id, plan, "L5", content, ref,
            features={"cell": self._cell(plan), "strategy": StrategyChoice.of(plan).strategy},
            options=tuple(candidates) or (prior,), prior=prior, ineligible=why,
        )  # fmt: skip
        if content is not None and decision.option in candidates and decision.option != prior:
            budget = replace(budget, driver_options=candidates[decision.option])
        return resolve_options(
            self.store, self.scope, profile, budget, capture_trace=capture, offline=offline
        )

    def _require_offline(self, profile: dict[str, Any]) -> None:
        """Hold DRIVER_WEB_UNDECLARED when the goal's registered port does not declare how its
        web tools are off (``offline.require_port``). A profile without a registered port is left to
        the worker, which holds DRIVER_NOT_INSTALLED at dispatch as before."""
        from ...agent_drivers import offline

        registry = getattr(self.coordinator, "registry", None)
        installed = getattr(registry, "installed", None)
        port = installed(self.scope, profile["driver_profile_ref"]) if callable(installed) else None
        if port is not None:
            offline.require_port(port)

    # -- per-layer decisions at dispatch, on failure, before verification (S10, §6.1) ---------
    @staticmethod
    def _cell(plan: dict[str, Any]) -> str:
        return str((plan.get("composition") or {}).get("cell_id") or "")

    def _decide(
        self,
        goal_id: str,
        plan: dict[str, Any],
        layer: str,
        content: dict[str, Any] | None,
        ref: dict[str, Any] | None,
        *,
        features: dict[str, Any],
        options: tuple[str, ...],
        prior: str,
        ineligible: dict[str, str] | None = None,
        record: bool = True,
    ) -> Any:
        """One decision of the goal (a trial's subject and arm cell, else the goal and its
        composition's cell); its ref is appended to ``plan["decisions"]`` unless ``record`` is
        False (the caller appends it, e.g. from a hook thread)."""
        from ...meta_harness import deciders

        trial = plan.get("trial") or {}
        subject = dict(trial["subject"]) if trial.get("subject") else {"goal_id": goal_id}
        cell = str(trial.get("cell_id") or self._cell(plan))
        decision = deciders.Decider.of(self.store, self.scope, content, ref=ref).decide(
            deciders.DecisionContext(
                layer=layer, cell_id=cell, features=features, options=options, prior=prior,
                subject=subject, production=not trial, ineligible=dict(ineligible or {}),
            )
        )  # fmt: skip
        if record:
            self._decided(goal_id, [decision.record_ref])
        return decision

    def _decided(self, goal_id: str, refs: list[dict[str, Any]]) -> None:
        if refs:
            plan = self.service.plan_record(goal_id)
            self._update(goal_id, decisions=[*(plan.get("decisions") or []), *refs])

    def _context_for(
        self,
        goal_id: str,
        plan: dict[str, Any],
        node: dict[str, Any],
        context: policies.ContextPolicy,
        attempts: list[dict[str, Any]],
    ) -> policies.ContextPolicy:
        """L4: which of the manifest's enabled context parts this attempt shows. An option is
        the "+"-joined set of parts kept on ("none" for all off); the prior is the manifest as
        written (v1: every part off, so the only option is "none")."""
        from ...meta_harness import deciders

        parts = ("env_bootstrap", "memory_notes")  # retrieval is refused while unspecified
        enabled = [p for p in parts if (getattr(context, p) or {}).get("enabled")]
        subsets = [s for k in range(len(enabled) + 1) for s in combinations(enabled, k)]
        options = tuple("+".join(s) or "none" for s in subsets)
        prior = "+".join(enabled) or "none"
        app = _node_app(plan, node)
        facts = (plan.get("repo_facts") or {}).get(app) or {}
        node_id = node["node_id"]
        failures = sum(
            1 for a in attempts if a.get("node_id") == node_id and a.get("outcome") == "fail"
        )
        content = context.decider_l4
        ref = context.refs.get("L4")
        decision = self._decide(
            goal_id, plan, "L4", content, ref,
            features={
                "strategy": StrategyChoice.of(plan).strategy,
                "repo_files": deciders.repo_files_bucket(
                    len(facts.get("tree") or []), bool(facts.get("tree_truncated"))
                ),
                "prior_failures": failures,
            },
            options=options, prior=prior,
        )  # fmt: skip
        if content is None or decision.option == prior:
            return context
        kept = set() if decision.option == "none" else set(decision.option.split("+"))
        changes = {
            p: {**(getattr(context, p) or {}), "enabled": False} for p in enabled if p not in kept
        }
        return replace(context, **changes)

    def _fast_hooks(
        self,
        goal_id: str,
        plan: dict[str, Any],
        node: dict[str, Any],
        budget: policies.BudgetPolicy,
        context: policies.ContextPolicy,
    ) -> FastCheckHooks | None:
        """The L7 hook of this attempt when the manifest enables fast checks (``_policies``
        refused what cannot run); None otherwise."""
        fast = budget.fast_checks
        if not fast or not fast.get("enabled") or plan.get("mode") == "design":
            return None
        app = self.service.apps[_node_app(plan, node)].config
        return FastCheckHooks(
            self, goal_id, plan, node, app, fast, context.feedback_form,
            budget.deciders.get("L7"), budget.refs.get("L7"),
        )  # fmt: skip

    def _fast_decided(self, goal_id: str, hooks: FastCheckHooks | None) -> None:
        """The L7 decision a fast-check hook made in the worker's hook thread, appended to the
        plan record here, in the loop's thread."""
        if hooks is not None and hooks.decision_refs:
            refs, hooks.decision_refs = list(hooks.decision_refs), []
            self._decided(goal_id, refs)

    def _decide_checks(
        self,
        goal_id: str,
        plan: dict[str, Any],
        node: dict[str, Any],
        budget: policies.BudgetPolicy,
        change: dict[str, Any],
        hooks: Any,
    ) -> None:
        """L7 without a fast-check hook: recorded before the final verification; no quick check
        can run (the manifest has fast checks off, a design goal, or the strategy's own
        follow-up turns hold the hook), so the decision is ``none``."""
        from ...meta_harness import deciders

        app = self.service.apps[_node_app(plan, node)].config
        fast = budget.fast_checks or {}
        if plan.get("mode") == "design":
            why = "a design goal verifies its document only"
        elif hooks is not None:
            why = "the strategy's follow-up turns hold the attempt's hook"
        else:
            why = "fast checks are off for this goal"
        self._decide(
            goal_id, plan, "L7", budget.deciders.get("L7"), budget.refs.get("L7"),
            features={
                "changed_files": deciders.count_bucket(self._changed_files(change)),
                "quick_available": bool(app.quick_verifiers) and bool(fast.get("enabled")),
            },
            options=deciders.OPTIONS["L7"], prior="none", ineligible={"quick_checks": why},
        )  # fmt: skip

    def _changed_files(self, change: dict[str, Any]) -> int:
        raw = self.service.workspaces.artifacts.read(self.scope, change)
        _base, patch = self.service.workspaces.read_change(self.scope, raw)
        return count_files(patch)

    def _on_failure(
        self,
        goal_id: str,
        node: dict[str, Any],
        observations: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        budget: policies.BudgetPolicy,
        runner: Any,
        decision: str,
        state: str,
        *,
        deadline: float,
        wall: float,
    ) -> str:
        """L6: retry (in the form the attempt policy gives the next attempt), escalate to the
        cascade's next cell, or stop. The prior is the strategy's own rule (``decision``)."""
        from ...meta_harness import deciders

        plan = self.service.plan_record(goal_id)
        strategy = StrategyChoice.of(plan).strategy
        attempt_policy = budget.attempt_policy
        feedback = bool(attempt_policy["feedback"]) and strategy not in {"single", "best_of_n"}
        retry = "retry_feedback" if feedback else "retry_fresh"
        other = "retry_fresh" if feedback else "retry_feedback"
        ineligible = {other: f"the attempt policy's next attempt is {retry}"}
        if state != "ready":
            ineligible[retry] = "finish_work admitted no further attempt"
        if runner.next_cell(plan) is None:
            ineligible["escalate"] = "no next cascade cell"
        prior = {"retry": retry, "escalate": "escalate"}.get(decision, "stop")
        failing = [o for o in observations if o["outcome"] != "pass"]
        mine = [a for a in attempts if a.get("node_id") == node["node_id"] and a.get("verdicts")]
        same = len(mine) >= 2 and _signature(mine[-1]) == _signature(mine[-2])
        remaining = max(0.0, deadline - self.clock()) / wall if wall > 0 else 0.0
        made = self._decide(
            goal_id, plan, "L6", budget.deciders.get("L6"), budget.refs.get("L6"),
            features={
                "attempt": len(mine),
                "failing_acceptance": deciders.count_bucket(len(failing)),
                "same_signature": same,
                "tokens_so_far": deciders.tokens_bucket(self._tokens_so_far(attempts)),
                "remaining_fraction": deciders.fraction_bucket(remaining),
            },
            options=deciders.OPTIONS["L6"], prior=prior, ineligible=ineligible,
        )  # fmt: skip
        option = str(made.option)
        return "retry" if option in {"retry_feedback", "retry_fresh"} else option

    def _tokens_so_far(self, attempts: list[dict[str, Any]]) -> int | None:
        """Input + output tokens of the goal's runs so far; None when one is not known."""
        total = 0
        for attempt in attempts:
            try:
                run = self.store.head(self.scope, "run", attempt["run_id"])["data"]["record"]
            except (Hold, RuntimeFault, KeyError):
                return None
            usage = run.get("usage") or {}
            tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
            if type(tokens_in) is not int or type(tokens_out) is not int:
                return None
            total += tokens_in + tokens_out
        return total

    def _router_unsupported(self, plan: dict[str, Any], composition: dict[str, Any]) -> list[str]:
        """Router-carrier parts of the goal composition nobody honours (D-096, §2.3).

        Since S10 a plan refuses them before the planner turn (``refuse_router_parts``); this
        check keeps holding a goal planned before that. The plan was made with the v1
        interpretation and selected with the app's router, which it records as
        ``composition.policy_ref``; route-policy roles are not read (L3 decides the executor
        cell); the deciders L1-L3 run at plan time and are refused only when their parts cannot
        run. A legacy task_class_baseline router reads as route_policy v1.

        The interpretation (§2.2) is honoured at plan time: a plan whose recorded interpretation
        (``plan["interpretation"]``, absent = v1) differs from its composition router's was
        drafted under another planner text. ``approve`` holds such a plan (COMPOSITION_CHANGED);
        this check holds one approved before that check existed, before any claim."""
        router = policies.router_policy(self.store, self.scope, composition["router_policy_ref"])
        parts = self.service.router_refusals(router)
        if self._own_route_order(plan, composition, router):
            parts.append("route_policy order of the composition (selection reads the app router)")
        drafted_with = plan.get("interpretation") or policies.V1["interpretation"]
        if router.interpretation != drafted_with:
            parts.append(
                "interpretation of the plan differs from the composition router's (planned "
                f"{json.dumps(drafted_with, sort_keys=True)}, router "
                f"{json.dumps(router.interpretation, sort_keys=True)})"
            )
        return parts

    def _fast_check_refusals(
        self, plan: dict[str, Any], budget: policies.BudgetPolicy
    ) -> list[str]:
        """L7 fast checks this loop cannot run (S10): checks that are not quick verifiers of
        every app of the goal, none at all, no follow-up turn to report a failure in, or a
        strategy whose own follow-up turns hold the attempt's M3 hook (generator_reviewer)."""
        fast = budget.fast_checks
        if not fast or not fast.get("enabled"):
            return []
        problems = []
        if not fast["checks"]:
            problems.append("fast_checks: enabled without checks")
        if int(fast["max_followups"]) < 1:
            problems.append("fast_checks: max_followups 0 leaves no follow-up turn")
        for app in plan.get("apps") or [plan["app"]]:
            quick = set(self.service.apps[app].config.quick_verifiers)
            missing = [c for c in fast["checks"] if c not in quick]
            if missing:
                problems.append(f"fast_checks: {', '.join(missing)} not quick verifiers of {app}")
        strategy = StrategyChoice.of(plan).strategy
        if strategy == "generator_reviewer":
            problems.append("fast_checks with generator_reviewer (one M3 hook per attempt)")
        if strategy == "vote":  # S9b: the vote's own hook (M4) holds the attempt
            problems.append("fast_checks with vote (one hook per attempt)")
        return problems

    def _own_route_order(
        self, plan: dict[str, Any], composition: dict[str, Any], router: policies.RouterPolicy
    ) -> bool:
        """True when the composition's route order is not the order selection used."""
        selected = (plan.get("composition") or {}).get("policy_ref")
        if selected == composition["router_policy_ref"]:
            return False
        if selected is None:  # nothing to compare with: the order cannot have been honoured
            return True
        return router.order != policies.router_policy(self.store, self.scope, selected).order

    def _implementer_role(self, plan: dict[str, Any]) -> list[str]:
        """The IMPLEMENTER role lines from the goal's fixed composition (D-089 S1).

        A composition whose prompt bundle ref is not a prompt bundle (planned before bundles
        existed) gets the built-in baseline, which is the same text.
        """
        ref = (plan.get("composition") or {}).get("ref")
        if not ref:
            return list(prompts.IMPLEMENTER_BASELINE)
        composition = self.store.get(self.scope, "harness-composition", ref)
        try:
            value = self.store.get(self.scope, prompts.KIND, composition["prompt_bundle_ref"])
        except RuntimeFault:
            return list(prompts.IMPLEMENTER_BASELINE)
        return prompts.validate(value)

    def _observation(self, verdict_ref: dict[str, Any]) -> dict[str, Any]:
        verdict = self.store.get(self.scope, "verdict", verdict_ref)
        evidence = self.store.get(self.scope, "evidence", verdict["evidence_refs"][0])
        observed = json.loads(
            self.service.workspaces.artifacts.read(self.scope, evidence["observations_artifact"])
        )
        return {
            "acceptance_id": verdict["acceptance_id"],
            "outcome": verdict["outcome"],
            "reason": verdict["reason"],
            "details": observed.get("details") or {},
        }

    def _repair_base(self, base: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
        workspaces = self.service.workspaces
        raw = workspaces.artifacts.read(self.scope, change)
        change_value = json.loads(raw)
        value = json.loads(workspaces.artifacts.read(self.scope, base))
        repaired = {**value, "patch": change_value["patch"]}
        ref: dict[str, Any] = workspaces.artifacts.admit(
            self.scope, canonical(repaired), BASE_MEDIA
        )
        return ref

    def _discard(self, run_id: str) -> None:
        path = self.service.workspaces.root / run_id
        if path.exists():
            self.service.workspaces.discard(Path(path))

    def _stop_goal(
        self, goal_id: str, status: str, reason: str, attempts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        plan = self.service.plan_record(goal_id)
        if plan.get("decision_ref"):
            with contextlib.suppress(Hold, RuntimeFault):
                self.service.revoke(self._controller(), goal_id)
        self._end(goal_id, "cancelled" if status == "cancelled" else "failed", reason)
        return self._finish(goal_id, status, reason=reason, attempts=attempts)

    def _end(self, goal_id: str, outcome: str, reason: str) -> None:
        """End the runtime goal too, so metrics count it (not only this plan record)."""
        try:
            self.service.runtime.end_goal(
                self.service.actors.service, goal_id, outcome=outcome, reason=reason
            )
        except (Hold, RuntimeFault) as exc:
            self._update(goal_id, end_error=getattr(exc, "code", type(exc).__name__))

    def _controller(self) -> Actor:
        # The loop stops its own goals with the controller's authority, never a human's.
        service = self.service.actors.service
        return Actor(
            service.subject_id, service.scope, frozenset({"execution.approve", "goal.steer"}),
            "service", service.authn_context_ref,
        )  # fmt: skip

    def _update(
        self, goal_id: str, *, event: tuple[str, dict[str, Any]] | None = None, **fields: Any
    ) -> dict[str, Any]:
        plan = {**self.service.plan_record(goal_id), **fields, "updated_at": now()}
        self.service._save_plan(goal_id, plan, event)
        return plan

    def _finish(self, goal_id: str, status: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._cancel.discard(goal_id)
        # what the strategy did (plan.md §8.1), over every revision so far (Work 033 S9)
        with contextlib.suppress(Exception):  # metrics never stop a goal from finishing
            plan = {**self.service.plan_record(goal_id), **fields}
            fields["strategy_metrics"] = self.service.strategy_runner().metrics(plan)
        return self._update(goal_id, status=status, finished_at=now(), **fields)


def count_files(patch: bytes) -> int:
    """Files a git patch touches (its ``diff --git`` headers)."""
    return sum(1 for line in patch.splitlines() if line.startswith(b"diff --git "))


def _signature(attempt: dict[str, Any]) -> frozenset[tuple[Any, Any, Any]]:
    """The failing verdicts of an attempt (acceptance, outcome, reason): L6 ``same_signature``."""
    return frozenset(
        (v.get("acceptance"), v.get("outcome"), v.get("reason"))
        for v in attempt.get("verdicts") or []
        if v.get("outcome") != "pass"
    )


FAST_HEADER = (
    "Fast checks failed on the current change (quick commands only; the acceptance commands "
    "still decide after this turn):"
)


class FastCheckHooks:
    """L7 fast checks (Work 033 S10, interfaces.md §5.2 "L7 fast checks", §6.1 L7, M3).

    After a turn the app's quick verifiers named by the ``fast_checks`` component run on a fresh
    copy of the run workspace's change (base + patch) in the app's verify sandbox with network
    none (the deployment's ``SuiteVerifier``, ``verification/runtime/patch_commands.py``). A
    failure becomes one follow-up message with the failing command and the tail of its output
    (the goal's feedback-form tail length); the worker resumes the same session with it, at most
    ``max_followups`` times. Fast checks never produce a verdict: the protected suite after
    ``output_ready`` stays the only verification. The L7 decision is made at the first turn,
    when the changed files are known; its record ref is handed to the loop's thread
    (``decision_refs``), which appends it to the plan."""

    candidates = 1

    def __init__(
        self,
        loop: ExecutionLoop,
        goal_id: str,
        plan: dict[str, Any],
        node: dict[str, Any],
        app: Any,
        fast: dict[str, Any],
        form: dict[str, Any],
        content: dict[str, Any] | None,
        ref: dict[str, Any] | None,
    ) -> None:
        self.loop, self.goal_id, self.plan, self.node, self.app = loop, goal_id, plan, node, app
        self.checks = tuple(str(c) for c in fast["checks"])
        self.max_followups = int(fast["max_followups"])
        self.tail_chars = int(form["tail_chars"])
        self.content, self.ref = content, ref
        self.decision_refs: list[dict[str, Any]] = []
        self.run: bool | None = None  # the L7 decision, made at the first turn
        self.sent = 0
        self.results: list[dict[str, Any]] = []

    def spec_digest(self) -> str:
        return digest(
            {"mechanism": "fast_checks", "checks": list(self.checks),
             "max_followups": self.max_followups, "decider": self.ref}
        )  # fmt: skip

    def select(self, candidates: list[Any]) -> int:
        return 0

    def _change(self, workspace: Path) -> tuple[bytes, bytes]:
        """(the change artifact bytes, the patch) of the run workspace as it is now."""
        service = self.loop.service
        workspaces, scope = service.workspaces, service.scope
        snapshot = workspaces.snapshot(scope, Path(workspace))
        value = json.loads(workspaces.artifacts.read(scope, snapshot))
        patch = workspaces.artifacts.read(scope, value["patch"])
        change = {
            "format": "amplai.change.v1",
            "base": {k: value[k] for k in ("repo", "commit", "tree")},
            "patch": value["patch"],
            "patch_bytes": len(patch),
        }
        return canonical(change), patch

    def after_turn(self, *, workspace: Path, turn: int, receipt: dict[str, Any]) -> str | None:
        from ...meta_harness import deciders

        raw, patch = self._change(workspace)
        if self.run is None:
            decision = self.loop._decide(
                self.goal_id, self.plan, "L7", self.content, self.ref,
                features={"changed_files": deciders.count_bucket(count_files(patch)),
                          "quick_available": True},
                options=deciders.OPTIONS["L7"], prior="quick_checks", record=False,
            )  # fmt: skip
            self.decision_refs.append(decision.record_ref)
            self.run = decision.option == "quick_checks"
        if not self.run:
            return None
        quick = tuple(v for v in self.app.verifiers if v.id in self.checks)
        suite = self.loop.service.verifier_factory(replace(self.app, verifiers=quick))
        observation = suite(raw)
        commands = observation.details.get("commands") or []
        self.results.append({
            "turn": turn, "outcome": observation.outcome, "reason": observation.reason,
            "commands": [{k: c.get(k) for k in ("command_id", "exit_code", "seconds")}
                         for c in commands],
        })  # fmt: skip
        if observation.outcome == "pass":
            return None
        self.sent += 1
        lines = [FAST_HEADER]
        failing = [c for c in commands if c.get("exit_code") != 0]
        for c in failing or [{"command_id": "-", "argv": [], "exit_code": None}]:
            argv = " ".join(str(a) for a in c.get("argv") or [])
            what = (
                f"exited {c['exit_code']}" if c.get("exit_code") is not None
                else observation.reason
            )  # fmt: skip
            lines.append(f"- {c.get('command_id')}: `{argv}` {what}".replace(" `` ", " "))
        tail = str(observation.details.get("stdout_tail") or "") + str(
            observation.details.get("stderr_tail") or ""
        )
        if tail and self.tail_chars:  # tail[-0:] would be the whole text
            lines.append("```\n" + tail[-self.tail_chars :] + "\n```")
        lines.append("Fix this in the same directory, then run the acceptance commands again.")
        return "\n".join(lines)
