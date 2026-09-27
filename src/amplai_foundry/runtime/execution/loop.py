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
from ..contracts.identity import canonical, now
from ..errors import Hold, RuntimeFault
from .product import PORT, LocalExecutionService

FEEDBACK_TAIL = 3000


def _utc(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp()


class ExecutionLoop:
    def __init__(
        self,
        service: LocalExecutionService,
        coordinator: Any,
        *,
        publisher: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        idle_seconds: float = 2.0,
    ) -> None:
        self.service, self.coordinator, self.publisher = service, coordinator, publisher
        self.clock, self.idle = clock, idle_seconds
        self.store, self.scope = service.store, service.scope
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: set[str] = set()
        self._lock = threading.Lock()

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
        if plan["status"] in {"awaiting_approval", "needs_answers"}:
            return self._finish(goal_id, "cancelled", reason="cancelled by the operator")
        return {**plan, "status": "cancelling"}

    # -- the loop --------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
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
                self._stop.wait(self.idle)
                continue
            try:
                self.run_goal(goal)
            except Exception as exc:  # recorded, never kills the loop
                self._finish(goal, "held", reason=f"{type(exc).__name__}: {exc}")

    def next_goal(self) -> str | None:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id FROM heads WHERE tenant=? AND project=? AND kind='execution-plan' "
                "AND state='approved' ORDER BY rowid",
                self.scope.keys(),
            ).fetchall()
        return rows[0]["id"] if rows else None

    def run_goal(self, goal_id: str) -> dict[str, Any]:
        svc, worker, verifier = (
            self.service,
            self.service.actors.worker,
            self.service.actors.verifier,
        )
        plan = svc.plan_record(goal_id)
        if plan["status"] != "approved":
            raise Hold("PLAN_NOT_APPROVED", "Only an approved goal runs")
        contract = self.store.get(self.scope, "goal-contract", plan["contract_ref"])
        deadline = _utc(plan["approved_at"]) + contract["budget"]["max_wall_seconds"]
        attempts: list[dict[str, Any]] = []
        base, feedback = plan["base"], None
        self._update(goal_id, status="running", attempts=attempts)
        while True:
            if goal_id in self._cancel:
                return self._stop_goal(goal_id, "cancelled", "cancelled by the operator", attempts)
            remaining = deadline - self.clock()
            if remaining <= 0:
                return self._stop_goal(goal_id, "timed_out", "wall-clock budget spent", attempts)
            dispatch = self.service.runtime.claim(worker, goal_id=goal_id)
            if dispatch is None:
                raise Hold("NO_WORK", "Approved goal has no claimable work")
            started = self.clock()
            prompt = self.prompt(contract, plan, feedback)
            self.coordinator.max_seconds = max(1, int(remaining))
            try:
                self.coordinator.execute(
                    worker,
                    dispatch,
                    prompt=prompt,
                    base_snapshot=base,
                    output_paths={PORT: PATCH_BINDING},
                )
            except Exception as exc:
                attempts.append(
                    {"run_id": dispatch["run_id"], "outcome": "driver_failed",
                     "reason": getattr(exc, "code", type(exc).__name__),
                     "seconds": round(self.clock() - started, 1)}
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
            verdicts = [
                self.service.verification.verify(verifier, dispatch["run_id"], ac, change)
                for ac in dispatch["node"]["acceptance_ids"]
            ]
            finished = self.service.verification.finish_work(verifier, dispatch["run_id"])
            observations = [self._observation(v) for v in verdicts]
            attempts.append(
                {"run_id": dispatch["run_id"], "outcome": finished["outcome"], "change": change,
                 "verdicts": [{"acceptance": o["acceptance_id"], "outcome": o["outcome"],
                               "reason": o["reason"]} for o in observations],
                 "seconds": round(self.clock() - started, 1)}
            )  # fmt: skip
            self._update(goal_id, attempts=attempts)
            self._discard(dispatch["run_id"])
            state = self.store.head(self.scope, "work", dispatch["node"]["work_id"])["state"]
            if state == "succeeded":
                break
            if state != "ready":
                return self._stop_goal(
                    goal_id, "failed", "acceptance failed within the attempt budget", attempts
                )
            feedback = observations
            base = self._repair_base(plan["base"], change)
        result = self.service.verification.finish_goal(verifier, goal_id)
        record = self._finish(goal_id, "verified", attempts=attempts, verification=result)
        if self.publisher is not None:
            try:
                published = self.publisher(goal_id)
                record = self._update(goal_id, status="published", publication=published)
            except Exception as exc:
                record = self._update(
                    goal_id, publication={"error": getattr(exc, "code", type(exc).__name__),
                                          "message": str(exc)[:400]}
                )  # fmt: skip
        return record

    # -- helpers ---------------------------------------------------------------------------------
    def prompt(
        self, contract: dict[str, Any], plan: dict[str, Any], feedback: list[dict[str, Any]] | None
    ) -> str:
        app = self.service.apps[plan["app"]].config
        commands = {v.id: " ".join(v.argv) for v in app.verifiers}
        lines = [
            "You are the IMPLEMENTER for AMPLAI. The current directory is a copy of the "
            f"{app.app_id} repository at commit {plan['base_commit']} (no .git, network limited).",
            "Make the change below. Do not commit. When you are done, run the acceptance "
            "commands yourself; the result is judged by running them on a clean copy with "
            "exactly your file changes applied.",
            "",
            "Objective: " + contract["objective"],
        ]
        draft = plan["draft"]
        if draft.get("in_scope"):
            lines.append("In scope: " + "; ".join(draft["in_scope"]))
        if contract["non_goals"]:
            lines.append("Do not: " + "; ".join(contract["non_goals"]))
        lines += ["Constraints: " + "; ".join(c["statement"] for c in contract["constraints"])]
        lines.append("Acceptance (each must pass):")
        for a, d in zip(contract["acceptance"], draft["acceptance"], strict=False):
            lines.append(f"- {a['id']}: {a['statement']}  command: `{commands[d['verifier']]}`")
        if feedback:
            lines += ["", "The previous attempt did not pass. The directory already contains "
                      "that attempt's changes. Fix what failed:"]  # fmt: skip
            for o in feedback:
                if o["outcome"] == "pass":
                    continue
                tail = o["details"].get("stdout_tail", "") + o["details"].get("stderr_tail", "")
                lines.append(f"- {o['acceptance_id']} {o['outcome']}: {o['reason']}")
                if tail:
                    lines.append("```\n" + tail[-FEEDBACK_TAIL:] + "\n```")
        return "\n".join(lines)

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
        return self._finish(goal_id, status, reason=reason, attempts=attempts)

    def _controller(self) -> Actor:
        # The loop stops its own goals with the controller's authority, never a human's.
        service = self.service.actors.service
        return Actor(
            service.subject_id, service.scope, frozenset({"execution.approve"}), "service",
            service.authn_context_ref,
        )  # fmt: skip

    def _update(self, goal_id: str, **fields: Any) -> dict[str, Any]:
        plan = {**self.service.plan_record(goal_id), **fields, "updated_at": now()}
        self.service._save_plan(goal_id, plan)
        return plan

    def _finish(self, goal_id: str, status: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._cancel.discard(goal_id)
        return self._update(goal_id, status=status, finished_at=now(), **fields)
