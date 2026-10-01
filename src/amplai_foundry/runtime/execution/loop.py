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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...sandbox.git_workspace import BASE_MEDIA, PATCH_BINDING
from ..contracts.authority import Actor
from ..contracts.identity import canonical, new_id, now
from ..errors import Hold, RuntimeFault
from . import context_assembly, policies, prompts
from .cells import DispatchOptions, resolve_options
from .product import PORT, LocalExecutionService
from .steering import SteeringService
from .strategy_runner import node_app

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
    def cancel(self, operator: Actor, goal_id: str) -> dict[str, Any]:
        operator.require("execution.approve")
        plan = self.service.plan_record(goal_id)
        if plan["status"] in {"verified", "published", "failed", "cancelled", "timed_out", "held"}:
            return plan
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
        message = text.strip()[:4000]
        if not message:
            raise Hold("STEER_TEXT", "Steering needs a message")
        return self._request(operator, goal_id, "pause", message, kind="steer")

    def replan(self, operator: Actor, goal_id: str, reason: str) -> dict[str, Any]:
        """Change a running goal's plan (D-082): its attempt stops at a process boundary, the
        goal is blocked, the planner drafts the next contract revision with the reason, and
        nothing runs until the operator approves that revision."""
        operator.require("goal.steer")
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
        """
        worker = self.service.actors.worker
        # passed only when set: a coordinator stand-in without hooks keeps working (v1 strategies)
        extra = {"hooks": hooks} if hooks is not None else {}

        def first() -> Any:
            return self.coordinator.execute(
                worker, dispatch, prompt=prompt, base_snapshot=base,
                output_paths={PORT: PATCH_BINDING}, options=options,
                deadline_seconds=deadline_seconds, **extra,
            )  # fmt: skip

        def resumed() -> Any:
            return self.coordinator.continue_resumed(
                worker, dispatch["run_id"], options=options, deadline_seconds=deadline_seconds
            )

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
            return self._run_attempts(goal_id, plan, context, options, runner)
        finally:
            runner.forget(goal_id)

    def _run_attempts(
        self,
        goal_id: str,
        plan: dict[str, Any],
        context: policies.ContextPolicy,
        options: DispatchOptions,
        runner: Any,
    ) -> dict[str, Any]:
        """Claim -> attempt -> protected verification until the graph is done or stops.

        The strategy (``StrategyRunner``, Work 033 S9) gives each attempt its base, feedback,
        hooks and extra prompt sections (M1, M3, M5) and decides after a failed verification
        (retry, escalate, stop); ``finish_work`` stays the only judge of an attempt."""
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
                prompt = self.prompt(
                    contract, plan, spec.feedback, node=node,
                    upstream=self._upstream(graph, node, plan), context=context,
                    extra_sections=spec.extra_sections,
                )  # fmt: skip
            except Exception as exc:
                self._discard(dispatch["run_id"])
                reason = f"prompt: {getattr(exc, 'code', type(exc).__name__)}: {exc}"[:600]
                return self._stop_goal(goal_id, "held", reason, attempts)
            try:
                # the remaining wall budget is this call's deadline (S8): writing the shared
                # coordinator.max_seconds raced once trials of one app run concurrently
                steered = self._execute(
                    goal_id, dispatch, prompt, spec.base, options=options,
                    deadline_seconds=max(1, int(remaining)), hooks=spec.hooks,
                )  # fmt: skip
            except _Replan as replan:
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
                attempts.append(
                    {"run_id": dispatch["run_id"], "app": app, "node_id": node["node_id"],
                     "outcome": "driver_failed",
                     "reason": getattr(exc, "code", type(exc).__name__),
                     "seconds": round(self.clock() - started, 1),
                     **self._turns(spec.hooks)}
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
                     "seconds": round(self.clock() - started, 1), **self._turns(spec.hooks)}
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
                 "seconds": round(self.clock() - started, 1), **self._turns(spec.hooks)}
            )  # fmt: skip
            self._update(goal_id, attempts=attempts)
            self._discard(dispatch["run_id"])
            state = self.store.head(self.scope, "work", node["work_id"])["state"]
            if state == "succeeded":
                if self._all_succeeded(graph):
                    break
                continue  # the next node whose dependencies are now met
            # M1/M6: another attempt of this node (finish_work allowed it within the node's
            # max_attempts), an escalation to the cascade's next cell, or the end
            decision = runner.on_failure(svc.plan_record(goal_id), node, observations)
            if decision == "retry" and state == "ready":
                continue
            if decision == "escalate":
                return self._escalate(goal_id, attempts, runner)
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
    @staticmethod
    def _turns(hooks: Any) -> dict[str, Any]:
        """The follow-up turns and reviewer rounds an attempt's hooks made (M3), for metrics."""
        if hooks is None:
            return {}
        return {
            "followups": int(getattr(hooks, "sent", 0) or 0),
            "reviewer_rounds": int(getattr(hooks, "rounds", 0) or 0),
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
        context = policies.context_policy(self.store, self.scope, composition)
        budget = policies.budget_policy(self.store, self.scope, composition, ceiling=ceiling)
        policies.check_combination(context.feedback_form, budget.attempt_policy, budget.limits)
        context_assembly.check_supported(context)
        # driver_options (L5) are honoured since S8: resolve_options carries them to the port;
        # the execution strategies since S9, except a strategy the plan refused or one held
        # (vote, §14 Q16); fast checks (L7, M3) are not built yet
        refused = self.service.strategy_runner().refusal(plan, budget)
        unsupported = [
            name
            for name, on in (
                (refused or "", refused is not None),
                ("fast_checks (S9)", bool(budget.fast_checks and budget.fast_checks["enabled"])),
                ("deciders L5-L8 (S10)", any(budget.deciders.values())),
            )
            if on
        ]  # fmt: skip
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
        profile = self.store.head(self.scope, "goal", goal_id)["data"]["profile"]
        trial = plan.get("trial") or {}
        return resolve_options(
            self.store, self.scope, profile, budget,
            capture_trace=trial.get("capture_trace") is True,
        )  # fmt: skip

    def _router_unsupported(self, plan: dict[str, Any], composition: dict[str, Any]) -> list[str]:
        """Router-carrier parts of the goal composition that S3 does not honour (D-096, §2.3).

        S3 plans with the v1 interpretation and selects with the installed router, which the plan
        records as ``composition.policy_ref`` (``product.py`` select_composition); reading the
        router from the composition, roles and interpretation are S4 (§4.1 rows
        ``product.py:443-494``, ``planner_codex.py:195-408``), deciders L1-L3 are S10. A legacy
        task_class_baseline router reads as route_policy v1 (``policies.router_policy``).
        """
        router = policies.router_policy(self.store, self.scope, composition["router_policy_ref"])
        return [
            name
            for name, on in (
                ("interpretation (S4)", router.interpretation != policies.V1["interpretation"]),
                ("route_policy order of the composition (S4)",
                 self._own_route_order(plan, composition, router)),
                ("route_policy roles (S4)", bool(router.roles)),
                ("deciders L1-L3 (S10)", any(router.deciders.values())),
            )
            if on
        ]  # fmt: skip

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
