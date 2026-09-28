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


UPSTREAM_PATCH = 60_000  # characters of an upstream patch shown to a downstream node


def _node_app(plan: dict[str, Any], node: dict[str, Any] | None) -> str:
    """Nodes are ``node-<app>`` (product._compile); older one-node plans name plan["app"]."""
    node_id = (node or {}).get("node_id", "")
    app = node_id[len("node-") :] if node_id.startswith("node-") else ""
    return app if app in (plan.get("bases") or {plan["app"]: None}) else str(plan["app"])


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
            self._end(goal_id, "cancelled", "cancelled by the operator")
            return self._finish(goal_id, "cancelled", reason="cancelled by the operator")
        return {**plan, "status": "cancelling"}

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
        graph = self.store.get(self.scope, "workgraph", plan["graph_ref"])
        deadline = _utc(plan["approved_at"]) + contract["budget"]["max_wall_seconds"]
        attempts: list[dict[str, Any]] = []
        # one node per app (D-081); a one-app goal is one node on plan["base"]
        bases = plan.get("bases") or {plan["app"]: plan["base"]}
        feedback: dict[str, list[dict[str, Any]]] = {}
        repair: dict[str, dict[str, Any]] = {}
        self._update(goal_id, status="running", attempts=attempts)
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
            prompt = self.prompt(
                contract, plan, feedback.get(node["node_id"]), node=node,
                upstream=self._upstream(graph, node),
            )  # fmt: skip
            self.coordinator.max_seconds = max(1, int(remaining))
            try:
                self.coordinator.execute(
                    worker,
                    dispatch,
                    prompt=prompt,
                    base_snapshot=repair.get(node["node_id"]) or bases[app],
                    output_paths={PORT: PATCH_BINDING},
                )
            except Exception as exc:
                attempts.append(
                    {"run_id": dispatch["run_id"], "app": app, "outcome": "driver_failed",
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
                {"run_id": dispatch["run_id"], "app": app, "outcome": finished["outcome"],
                 "change": change,
                 "verdicts": [{"acceptance": o["acceptance_id"], "outcome": o["outcome"],
                               "reason": o["reason"]} for o in observations],
                 "seconds": round(self.clock() - started, 1)}
            )  # fmt: skip
            self._update(goal_id, attempts=attempts)
            self._discard(dispatch["run_id"])
            state = self.store.head(self.scope, "work", node["work_id"])["state"]
            if state == "succeeded":
                feedback.pop(node["node_id"], None)
                if self._all_succeeded(graph):
                    break
                continue  # the next node whose dependencies are now met
            if state != "ready":
                return self._stop_goal(
                    goal_id, "failed", "acceptance failed within the attempt budget", attempts
                )
            feedback[node["node_id"]] = observations
            repair[node["node_id"]] = self._repair_base(bases[app], change)
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
        if self.publisher is not None:
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
    def _all_succeeded(self, graph: dict[str, Any]) -> bool:
        return all(
            self.store.head(self.scope, "work", n["work_id"])["state"] == "succeeded"
            for n in graph["nodes"]
        )

    def _upstream(self, graph: dict[str, Any], node: dict[str, Any]) -> list[tuple[str, str]]:
        """(app, verified patch text) of every node this one consumes (D-081)."""
        by_id = {n["node_id"]: n for n in graph["nodes"]}
        out = []
        for consumed in node.get("consumes") or []:
            producer = by_id.get(consumed.get("from_node") or "")
            if producer is None:
                continue
            work = self.store.head(self.scope, "work", producer["work_id"])
            ref = (work["data"].get("outputs") or {}).get(consumed["output_name"])
            if work["state"] != "succeeded" or not ref:
                continue
            raw = self.service.workspaces.artifacts.read(self.scope, ref)
            _base, patch = self.service.workspaces.read_change(self.scope, raw)
            out.append((producer["node_id"][len("node-") :], patch.decode(errors="replace")))
        return out

    def prompt(
        self,
        contract: dict[str, Any],
        plan: dict[str, Any],
        feedback: list[dict[str, Any]] | None,
        *,
        node: dict[str, Any] | None = None,
        upstream: list[tuple[str, str]] | None = None,
    ) -> str:
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
                "every citation must exist here. Implementation Plan lists the steps for a "
                "later work goal; do not implement them.",
                "",
                "Objective: " + contract["objective"],
            ]
        else:
            lines = [
                "You are the IMPLEMENTER for AMPLAI. The current directory is a copy of the "
                f"{app.app_id} repository at commit {plan['base_commit']} (no .git, network "
                "limited).",
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
        if feedback:
            lines += ["", "The previous attempt did not pass. The directory already contains "
                      "that attempt's changes. Fix what failed:"]  # fmt: skip
            for o in feedback:
                if o["outcome"] == "pass":
                    continue
                tail = o["details"].get("stdout_tail", "") + o["details"].get("stderr_tail", "")
                lines.append(f"- {o['acceptance_id']} {o['outcome']}: {o['reason']}")
                for key in ("outside", "missing_sections", "unresolved_sources"):
                    if o["details"].get(key):  # the design check says exactly what to fix
                        lines.append(f"  {key}: " + ", ".join(map(str, o["details"][key][:20])))
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
            service.subject_id, service.scope, frozenset({"execution.approve"}), "service",
            service.authn_context_ref,
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
        return self._update(goal_id, status=status, finished_at=now(), **fields)
