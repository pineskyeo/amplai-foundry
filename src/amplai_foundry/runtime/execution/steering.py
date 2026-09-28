"""Durable steering, safe quiescence, and exact-session checkpoint resume.

Provider acknowledgment never changes a contract. Only a frozen, admitted revision
or a confirmed process boundary makes a steering event effective.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..contracts.authority import Actor
from ..contracts.gates import Observation
from ..contracts.identity import new_id, now
from ..contracts.semantics import check_context, check_refs
from ..errors import Conflict, Hold, RuntimeFault
from ..storage.store import Scope

if TYPE_CHECKING:
    from .service import Runtime


class SteeringService:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.contracts = runtime.contracts

    def _runs(self, scope: Scope, goal_id: str) -> list[tuple[str, dict[str, Any]]]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id,state,row_version,data FROM heads "
                "WHERE tenant=? AND project=? AND kind='run'",
                scope.keys(),
            ).fetchall()
        return [
            (
                x["id"],
                {
                    "state": x["state"],
                    "row_version": x["row_version"],
                    "data": json.loads(x["data"]),
                },
            )
            for x in rows
            if json.loads(x["data"])["record"]["root_goal_id"] == goal_id
        ]

    def _unknown(self, scope: Scope, run_id: str) -> list[str]:
        with self.store._lock:
            rows = self.store.conn.execute(
                "SELECT id,data FROM heads WHERE tenant=? AND project=? AND kind='effect' "
                "AND state IN ('prepared','dispatched','unknown')",
                scope.keys(),
            ).fetchall()
        return [x["id"] for x in rows if json.loads(x["data"])["request"]["run_id"] == run_id]

    def _move(
        self,
        db: sqlite3.Connection,
        scope: Scope,
        steering_id: str,
        command: str,
        observations: dict[str, Observation],
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        h = self.store.head(scope, "steering", steering_id, db=db)
        state, gates = self.runtime.machines.transition(
            "steering", h["state"], command, observations
        )
        value = {**h["data"]["event"], **(updates or {}), "status": state}
        self.contracts.validate("steering-event", value)
        ref = self.store.put(db, scope, "steering-event", steering_id, h["row_version"] + 1, value)
        self.store.cas(
            db,
            scope,
            "steering",
            steering_id,
            h["row_version"],
            state,
            {"event": value, "ref": ref, "gate_results": gates},
        )
        self.store.event(
            db, scope, "goal", value["goal_id"], "steering." + state, {"steering_ref": ref}
        )
        return ref

    def receive(
        self,
        actor: Actor,
        goal_id: str,
        kind: str,
        text: str,
        *,
        expected_contract_ref: dict[str, Any],
        key: str,
        evidence_refs: list[dict[str, Any]] | None = None,
        priority: int | None = None,
    ) -> dict[str, Any]:
        actor.require("goal.steer")
        scope = actor.scope
        goal = self.store.head(scope, "goal", goal_id)
        if goal["data"].get("active_contract_ref") != expected_contract_ref:
            raise Conflict("STALE_CONTRACT", "Steering must target the exact active revision")
        if kind == "priority_change" and (type(priority) is not int or not -100 <= priority <= 100):
            raise RuntimeFault("PRIORITY", "Priority must be an integer between -100 and 100")
        event: dict[str, Any] = {
            "schema_version": "3.0.0",
            "steering_id": new_id("steer"),
            "scope": scope.wire(),
            "goal_id": goal_id,
            "actor": actor.wire(),
            "expected_contract_ref": expected_contract_ref,
            "kind": kind,
            "text": text,
            "evidence_refs": evidence_refs or [],
            "status": "received",
            "native_turn_id": None,
            "effective_contract_ref": None,
            "received_at": now(),
        }
        self.contracts.validate("steering-event", event)
        check_refs(self.store, scope, event)
        # Stop/hold remains available to an authenticated controller after a grant is revoked.
        if kind not in {"pause", "cancel"}:
            contract = self.store.get(scope, "goal-contract", expected_contract_ref)
            self.runtime.authority.preflight(
                scope,
                goal["data"]["grant_ref"],
                subject_id=goal["data"]["authority_subject_id"],
                contract_ref=expected_contract_ref,
                graph_ref=goal["data"]["active_graph_ref"],
                capabilities=contract["requested_capabilities"],
            )
        request = {k: v for k, v in event.items() if k not in {"steering_id", "received_at"}}
        request["priority"] = priority

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            h = self.store.head(scope, "goal", goal_id, db=db)
            if h["data"]["active_contract_ref"] != expected_contract_ref:
                raise Conflict("STALE_CONTRACT", "Concurrent revision activation")
            sid = event["steering_id"]
            ref = self.store.put(db, scope, "steering-event", sid, 1, event)
            self.store.cas(db, scope, "steering", sid, 0, "received", {"event": event, "ref": ref})
            obs = {
                g: Observation.check(True, "Authenticated current scoped steering")
                for g in ["G-01", "G-03", "G-06", "G-09"]
            }
            self._move(db, scope, sid, "validate", obs)
            ref = self._move(db, scope, sid, "queue", obs)
            data = {**h["data"], "steering_cursor": h["data"].get("steering_cursor", 0) + 1}
            if kind != "priority_change":
                data["admission_paused"] = True
            else:
                data["priority"] = priority
            self.store.cas(db, scope, "goal", goal_id, h["row_version"], h["state"], data)
            return {"steering_id": sid, "steering_ref": ref, "status": "queued"}

        return self.store.command(scope, actor.subject_id, key, request, apply)

    def native_ack(self, actor: Actor, steering_id: str, native_turn_id: str) -> dict[str, Any]:
        actor.require("worker.execute")
        if not native_turn_id:
            raise RuntimeFault("NATIVE_ACK", "Exact provider turn identifier required")
        h = self.store.head(actor.scope, "steering", steering_id)
        if h["state"] != "queued":
            raise Hold("STEERING_STATE", "Only queued steering can record delivery")
        # Delivery is an observation, not a normative effective-contract transition.
        with self.store.tx() as db:
            self.store.event(
                db,
                actor.scope,
                "goal",
                h["data"]["event"]["goal_id"],
                "steering.native_ack",
                {"steering_id": steering_id, "native_turn_id": native_turn_id, "effective": False},
            )
        return {"status": "queued", "native_acknowledged": True, "applied": False}

    def _checkpoint(
        self, scope: Scope, run_id: str, run: dict[str, Any], driver_result: dict[str, Any]
    ) -> dict[str, Any]:
        record = run["data"]["record"]
        goal = self.store.head(scope, "goal", record["root_goal_id"])
        diff = driver_result.get("workspace_diff_artifact")
        if not diff:
            raise Hold("CHECKPOINT_DIFF", "Collector must snapshot the actual workspace diff")
        self.runtime.artifacts.read(scope, diff)
        if driver_result.get("session_handle") != run["data"].get("session_handle"):
            raise Hold(
                "CHECKPOINT_SESSION", "Checkpoint does not name the exact current driver session"
            )
        with self.store._lock:
            row = self.store.conn.execute(
                "SELECT * FROM reservations WHERE tenant=? AND project=? AND run_id=?",
                (*scope.keys(), run_id),
            ).fetchone()
        reservation = {
            "reservation_id": new_id("reservation-snapshot"),
            "run_id": run_id,
            "scope": scope.wire(),
            "reserved_tokens": row["tokens"],
            "reserved_cost_microunits": row["cost"],
            "status": row["status"],
        }
        base = {
            "base_id": new_id("workspace-base"),
            "scope": scope.wire(),
            "contract_ref": record["contract_ref"],
            "graph_ref": record["graph_ref"],
            "workspace_base_digest": driver_result.get("workspace_base_digest", diff["digest"]),
        }
        with self.store.tx() as db:
            br = self.store.put(
                db, scope, "reservation-snapshot", reservation["reservation_id"], 1, reservation
            )
            wr = self.store.put(db, scope, "workspace-base", base["base_id"], 1, base)
        value = {
            "schema_version": "3.0.0",
            "checkpoint_id": new_id("checkpoint"),
            "scope": scope.wire(),
            "run_id": run_id,
            "contract_ref": record["contract_ref"],
            "graph_ref": record["graph_ref"],
            "context_bundle_ref": record["context_bundle_ref"],
            "workspace_base_ref": wr,
            "workspace_diff_artifact": diff,
            "last_accepted_seq": run["data"]["worker_seq"],
            "pending_calls": [],
            "steering_cursor": goal["data"].get("steering_cursor", 0),
            "budget_reservation_ref": br,
            "driver_session_handle": driver_result["session_handle"],
            "driver_profile_ref": record["driver_profile_ref"],
            "sandbox_recipe_ref": record["environment_ref"],
            "created_at": now(),
        }
        self.contracts.validate("checkpoint", value)
        check_refs(self.store, scope, value)
        with self.store.tx() as db:
            return self.store.put(db, scope, "checkpoint", value["checkpoint_id"], 1, value)

    def quiesce(
        self,
        actor: Actor,
        steering_id: str,
        stop_and_snapshot: Callable[[str, str], dict[str, Any]],
    ) -> dict[str, Any]:
        actor.require("goal.steer")
        scope = actor.scope
        h = self.store.head(scope, "steering", steering_id)
        event = h["data"]["event"]
        if h["state"] != "queued" or event["kind"] in {"resume", "priority_change"}:
            raise Hold(
                "STEERING_STATE", "This operation requires a queued stop or revision-changing event"
            )
        outcomes: list[dict[str, Any]] = []
        for run_id, run in self._runs(scope, event["goal_id"]):
            if run["state"] in {"succeeded", "failed", "cancelled", "lost"}:
                continue
            if run["state"] == "running":
                with self.store.tx() as db:
                    current = self.store.head(scope, "run", run_id, db=db)
                    state, _ = self.runtime.machines.transition(
                        "run",
                        current["state"],
                        "pause_request",
                        {
                            g: Observation.check(
                                True, "Authenticated pause at current run boundary"
                            )
                            for g in ["G-01", "G-09"]
                        },
                    )
                    self.runtime._run_state(db, scope, run_id, current, state, current["data"])
            self.store.assert_outside_tx()
            result = stop_and_snapshot(run_id, event["kind"])
            if result.get("process_stopped") is not True:
                outcomes.append({"run_id": run_id, "status": "process_unconfirmed"})
                continue
            unknown = self._unknown(scope, run_id)
            current = self.store.head(scope, "run", run_id)
            if unknown:
                with self.store.tx() as db:
                    current = self.store.head(scope, "run", run_id, db=db)
                    self.runtime._run_state(
                        db,
                        scope,
                        run_id,
                        current,
                        "unknown_effect",
                        {
                            **current["data"],
                            "process_stopped": True,
                            "recovery_hold": "unknown_effect",
                        },
                    )
                outcomes.append(
                    {"run_id": run_id, "status": "unknown_effect", "effect_ids": unknown}
                )
                continue
            paused = event["kind"] == "pause" and current["state"] in {"pausing", "paused"}
            checkpoint_ref = self._checkpoint(scope, run_id, current, result) if paused else None
            with self.store.tx() as db:
                current = self.store.head(scope, "run", run_id, db=db)
                command = (
                    "checkpoint_confirmed"
                    if paused and current["state"] == "pausing"
                    else "cancel_complete"
                )
                if paused and current["state"] == "paused":
                    state = "paused"
                else:
                    if current["state"] == "unknown_effect":
                        command = "reconciled_cancel"
                    state, _ = self.runtime.machines.transition(
                        "run",
                        current["state"],
                        command,
                        {
                            g: Observation.check(
                                True, "Process stopped and all broker effects classified"
                            )
                            for g in ["G-09", "G-10", "G-21"]
                        },
                    )
                self.runtime._run_state(
                    db,
                    scope,
                    run_id,
                    current,
                    state,
                    {**current["data"], "process_stopped": True, "checkpoint_ref": checkpoint_ref},
                )
                work = self.store.head(scope, "work", current["data"]["record"]["work_id"], db=db)
                if work["state"] not in {"succeeded", "failed", "cancelled", "superseded"}:
                    self.store.cas(
                        db,
                        scope,
                        "work",
                        current["data"]["record"]["work_id"],
                        work["row_version"],
                        "blocked" if paused else "cancelled",
                        {
                            **work["data"],
                            "hold_reason": "paused" if paused else "steering_replaced",
                        },
                    )
                if paused:
                    db.execute(
                        "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), run_id),
                    )
                    self.runtime.budgets.suspend(db, scope, run_id)
                if not paused:
                    db.execute(
                        "DELETE FROM resources WHERE tenant=? AND project=? AND run_id=?",
                        (*scope.keys(), run_id),
                    )
                    self.runtime.budgets.settle(
                        db, scope, run_id, current["data"]["record"]["usage"]
                    )
                self.store.event(
                    db,
                    scope,
                    "run",
                    run_id,
                    "steering.process_boundary",
                    {"state": state, "checkpoint_ref": checkpoint_ref},
                )
            outcomes.append({"run_id": run_id, "status": state})
        if any(x["status"] in {"process_unconfirmed", "unknown_effect"} for x in outcomes):
            return {"status": "queued", "outcomes": outcomes}
        with self.store.tx() as db:
            goal = self.store.head(scope, "goal", event["goal_id"], db=db)
            if event["kind"] == "cancel":
                state, _ = self.runtime.machines.transition(
                    "goal",
                    goal["state"],
                    "cancel_complete",
                    {
                        "G-21": Observation.check(
                            True, "All process and external-effect boundaries confirmed"
                        )
                    },
                )
                for node in self.store.get(
                    scope, "workgraph", goal["data"]["active_graph_ref"], db=db
                )["nodes"]:
                    work = self.store.head(scope, "work", node["work_id"], db=db)
                    if work["state"] not in {"succeeded", "failed", "cancelled", "superseded"}:
                        self.store.cas(
                            db,
                            scope,
                            "work",
                            node["work_id"],
                            work["row_version"],
                            "cancelled",
                            work["data"],
                        )
                self.store.cas(
                    db, scope, "goal", event["goal_id"], goal["row_version"], state, goal["data"]
                )
            elif event["kind"] != "pause" and goal["state"] != "blocked":
                state, _ = self.runtime.machines.transition(
                    "goal",
                    goal["state"],
                    "block",
                    {
                        "G-01": Observation.check(
                            True, "Explicit revision steering requires new admission"
                        )
                    },
                )
                self.store.cas(
                    db,
                    scope,
                    "goal",
                    event["goal_id"],
                    goal["row_version"],
                    state,
                    {**goal["data"], "replan_reason": event["text"]},
                )
            if event["kind"] in {"pause", "cancel"}:
                self._move(
                    db,
                    scope,
                    steering_id,
                    "apply",
                    {
                        g: Observation.check(
                            True, "Confirmed scoped process boundary, unchanged contract"
                        )
                        for g in ["G-03", "G-05", "G-06", "G-09"]
                    },
                    {"effective_contract_ref": event["expected_contract_ref"]},
                )
        return {
            "status": "applied" if event["kind"] in {"pause", "cancel"} else "queued",
            "outcomes": outcomes,
        }

    def withdraw(self, actor: Actor, steering_id: str, reason: str) -> dict[str, Any]:
        """A queued steering that can no longer take effect (its attempt already finished) is
        superseded; admission resumes once no other steering of the goal is queued. The caller
        records the reason where the operator reads it."""
        actor.require("goal.steer")
        scope = actor.scope
        h = self.store.head(scope, "steering", steering_id)
        if h["state"] != "queued":
            return {"status": h["state"]}
        goal_id = h["data"]["event"]["goal_id"]
        with self.store.tx() as db:
            ref = self._move(
                db,
                scope,
                steering_id,
                "supersede",
                {g: Observation.check(True, reason[:200]) for g in ["G-01", "G-03"]},
            )
            others = [
                r
                for r in db.execute(
                    "SELECT id, data FROM heads WHERE tenant=? AND project=? AND kind='steering' "
                    "AND state='queued'",
                    scope.keys(),
                ).fetchall()
                if r["id"] != steering_id and json.loads(r["data"])["event"]["goal_id"] == goal_id
            ]
            goal = self.store.head(scope, "goal", goal_id, db=db)
            if not others and goal["data"].get("admission_paused"):
                self.store.cas(
                    db, scope, "goal", goal_id, goal["row_version"], goal["state"],
                    {**goal["data"], "admission_paused": False},
                )  # fmt: skip
        return {"status": "superseded", "steering_ref": ref}

    def apply_revision(self, actor: Actor, steering_id: str) -> dict[str, Any]:
        actor.require("goal.steer")
        scope = actor.scope
        h = self.store.head(scope, "steering", steering_id)
        event = h["data"]["event"]
        goal = self.store.head(scope, "goal", event["goal_id"])
        current = goal["data"]["active_contract_ref"]
        if event["kind"] not in {
            "constraint_add",
            "acceptance_change",
            "new_evidence",
            "priority_change",
        }:
            raise Hold("STEERING_KIND", "Use stop/resume handling for this steering kind")
        if event["kind"] != "priority_change" and (
            current == event["expected_contract_ref"]
            or current["revision"] <= event["expected_contract_ref"]["revision"]
        ):
            raise Hold(
                "REVISION_NOT_EFFECTIVE", "Provider delivery does not activate a contract revision"
            )
        if goal["state"] not in {"ready", "active"}:
            raise Hold("REVISION_NOT_ADMITTED", "New revision must pass normal activation first")
        with self.store.tx() as db:
            ref = self._move(
                db,
                scope,
                steering_id,
                "apply",
                {
                    g: Observation.check(True, "Normally admitted immutable revision is now active")
                    for g in ["G-03", "G-05", "G-06", "G-09"]
                },
                {"effective_contract_ref": current},
            )
            goal = self.store.head(scope, "goal", event["goal_id"], db=db)
            self.store.cas(
                db,
                scope,
                "goal",
                event["goal_id"],
                goal["row_version"],
                goal["state"],
                {**goal["data"], "admission_paused": False},
            )
        return {"status": "applied", "steering_ref": ref}

    def resume(
        self,
        actor: Actor,
        steering_id: str,
        worker: Actor,
        resume_exact: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        actor.require("goal.steer")
        worker.require("worker.execute")
        scope = actor.scope
        if worker.scope != scope:
            raise RuntimeFault("SCOPE_MISMATCH", "Resume worker differs from goal scope")
        h = self.store.head(scope, "steering", steering_id)
        event = h["data"]["event"]
        if h["state"] != "queued" or event["kind"] != "resume":
            raise Hold("STEERING_STATE", "Queued resume required")
        goal = self.store.head(scope, "goal", event["goal_id"])
        contract = self.store.get(scope, "goal-contract", goal["data"]["active_contract_ref"])
        self.runtime.authority.preflight(
            scope,
            goal["data"]["grant_ref"],
            subject_id=goal["data"]["authority_subject_id"],
            contract_ref=goal["data"]["active_contract_ref"],
            graph_ref=goal["data"]["active_graph_ref"],
            capabilities=contract["requested_capabilities"],
        )
        context = self.store.get(scope, "context-bundle", contract["context_bundle_ref"])
        check_context(context, context["core_refs"])
        resumed = []
        for run_id, run in self._runs(scope, event["goal_id"]):
            if run["state"] != "paused":
                continue
            cp = self.store.get(scope, "checkpoint", run["data"]["checkpoint_ref"])
            self.contracts.validate("checkpoint", cp)
            if (
                cp["contract_ref"] != goal["data"]["active_contract_ref"]
                or cp["graph_ref"] != goal["data"]["active_graph_ref"]
            ):
                raise Hold(
                    "CHECKPOINT_STALE",
                    "Changed contracts require replacement runs, not native resume",
                )
            if self._unknown(scope, run_id):
                raise Hold("UNKNOWN_EFFECT", "Reconcile before resume")
            if (
                cp["driver_profile_ref"] != goal["data"]["profile"]["driver_profile_ref"]
                or cp["sandbox_recipe_ref"] != goal["data"]["profile"]["environment_ref"]
            ):
                raise Hold(
                    "RESUME_PROFILE_STALE", "Checkpoint was created with another driver or sandbox"
                )
            intent = self.store.get(scope, "intent-envelope", contract["intent_ref"])
            self.runtime._profile(
                scope,
                goal["data"]["profile"],
                contract["requested_capabilities"],
                intent["data_classification"],
            )
            if (
                self.store.clock() - goal["data"]["started_at_epoch"]
                >= contract["budget"]["max_wall_seconds"]
            ):
                raise Hold("WALL_BUDGET", "Pause does not reset root wall-clock budget")
            self.runtime.artifacts.read(scope, cp["workspace_diff_artifact"])
            # Journal the resume intent BEFORE external I/O. A lost response is
            # uncertain and cannot dispatch a second native resume automatically.
            with self.store.tx() as db:
                current = self.store.head(scope, "run", run_id, db=db)
                row = db.execute(
                    "SELECT * FROM leases WHERE tenant=? AND project=? AND run_id=?",
                    (*scope.keys(), run_id),
                ).fetchone()
                if row is None or row["worker_id"] != worker.subject_id:
                    raise Hold("RESUME_WORKER", "Resume is assigned to another worker")
                if current["row_version"] != run["row_version"] or current["state"] != "paused":
                    raise Conflict("RESUME_RACE", "Checkpoint changed during resume preflight")
                if current["data"].get("resume_pending"):
                    raise Hold(
                        "RESUME_OUTCOME_UNKNOWN",
                        "Reconcile the previous native resume before retrying",
                    )
                current_goal = self.store.head(scope, "goal", event["goal_id"], db=db)
                if (
                    current_goal["data"]["active_contract_ref"] != cp["contract_ref"]
                    or current_goal["data"]["active_graph_ref"] != cp["graph_ref"]
                ):
                    raise Hold("RESUME_REVISION", "Revision changed during resume preflight")
                work = self.store.head(scope, "work", current["data"]["record"]["work_id"], db=db)
                for claim in work["data"]["node"]["resource_claims"]:
                    rows = db.execute(
                        "SELECT mode FROM resources WHERE tenant=? AND project=? AND resource=?",
                        (*scope.keys(), claim["resource"]),
                    ).fetchall()
                    if any(
                        claim["mode"] == "exclusive_write" or r["mode"] == "exclusive_write"
                        for r in rows
                    ) or (claim["mode"] == "shared_read" and len(rows) >= 4):
                        raise Hold(
                            "RESUME_RESOURCE",
                            "Resource was acquired by another run while this run was paused",
                        )
                self.runtime.budgets.resume(
                    db,
                    scope,
                    run_id,
                    contract["budget"],
                    goal_started=goal["data"]["started_at_epoch"],
                )
                for claim in work["data"]["node"]["resource_claims"]:
                    db.execute(
                        "INSERT INTO resources VALUES(?,?,?,?,?)",
                        (*scope.keys(), claim["resource"], run_id, claim["mode"]),
                    )
                self.store.cas(
                    db,
                    scope,
                    "run",
                    run_id,
                    current["row_version"],
                    "paused",
                    {
                        **current["data"],
                        "resume_pending": {
                            "steering_id": steering_id,
                            "checkpoint_ref": run["data"]["checkpoint_ref"],
                        },
                    },
                )
                self.store.event(
                    db,
                    scope,
                    "run",
                    run_id,
                    "resume.dispatching",
                    {"checkpoint_ref": run["data"]["checkpoint_ref"]},
                )
            self.store.assert_outside_tx()
            result = resume_exact(run_id, cp)
            if (
                result.get("session_handle") != cp["driver_session_handle"]
                or result.get("resumed") is not True
            ):
                raise Hold(
                    "RESUME_UNCONFIRMED", "Native resume must confirm the exact frozen session"
                )
            with self.store.tx() as db:
                current = self.store.head(scope, "run", run_id, db=db)
                state, _ = self.runtime.machines.transition(
                    "run",
                    current["state"],
                    "resume",
                    {
                        g: Observation.check(
                            True,
                            "Exact checkpoint, current grant, resources and root budget confirmed",
                        )
                        for g in ["G-04", "G-06", "G-07", "G-08", "G-09"]
                    },
                )
                row = db.execute(
                    "SELECT * FROM leases WHERE tenant=? AND project=? AND run_id=?",
                    (*scope.keys(), run_id),
                ).fetchone()
                if row["worker_id"] != worker.subject_id:
                    raise Hold("RESUME_WORKER", "Resume is assigned to another worker")
                lease = {
                    **current["data"]["lease"],
                    "lease_id": new_id("lease"),
                    "fencing_token": row["fence"] + 1,
                    "owner_epoch": self.store.epoch,
                    "expires_at_epoch": self.store.clock() + 120,
                }
                db.execute(
                    "UPDATE leases SET lease_id=?,fence=?,epoch=?,expires=?,heartbeat_seq=0 "
                    "WHERE tenant=? AND project=? AND run_id=?",
                    (
                        lease["lease_id"],
                        lease["fencing_token"],
                        lease["owner_epoch"],
                        lease["expires_at_epoch"],
                        *scope.keys(),
                        run_id,
                    ),
                )
                self.runtime._run_state(
                    db,
                    scope,
                    run_id,
                    current,
                    state,
                    {
                        **current["data"],
                        "lease": lease,
                        "process_stopped": False,
                        "resume_pending": None,
                    },
                )
                work = self.store.head(scope, "work", current["data"]["record"]["work_id"], db=db)
                self.store.cas(
                    db,
                    scope,
                    "work",
                    current["data"]["record"]["work_id"],
                    work["row_version"],
                    "running",
                    {**work["data"], "hold_reason": None},
                )
                self.store.event(
                    db,
                    scope,
                    "run",
                    run_id,
                    "run.resumed",
                    {"checkpoint_ref": run["data"]["checkpoint_ref"], "lease": lease},
                )
            resumed.append({"run_id": run_id, "lease": lease})
        if not resumed:
            raise Hold(
                "NO_PAUSED_RUN", "No exact checkpoint can be resumed; submit a replacement revision"
            )
        with self.store.tx() as db:
            goal = self.store.head(scope, "goal", event["goal_id"], db=db)
            self.store.cas(
                db,
                scope,
                "goal",
                event["goal_id"],
                goal["row_version"],
                goal["state"],
                {**goal["data"], "admission_paused": False},
            )
            self._move(
                db,
                scope,
                steering_id,
                "apply",
                {
                    g: Observation.check(
                        True, "All required exact-session resume receipts confirmed"
                    )
                    for g in ["G-03", "G-05", "G-06", "G-09"]
                },
                {"effective_contract_ref": goal["data"]["active_contract_ref"]},
            )
        return {"status": "applied", "resumed": resumed}
