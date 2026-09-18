"""Durable effect outbox. Unknown is a first-class outcome, never an automatic retry."""

from __future__ import annotations

from ..contracts.authority import Actor, capability_contains
from ..contracts.identity import digest, new_id, now
from ..contracts.registry import strict_json_loads
from ..errors import Conflict, Hold, RuntimeFault
from ..storage.store import Scope


class Effects:
    def __init__(self, runtime, tools):
        self.runtime, self.store, self.authority, self.artifacts, self.tools = (
            runtime,
            runtime.store,
            runtime.authority,
            runtime.artifacts,
            tools,
        )

    def _receipt(
        self,
        db,
        scope,
        head,
        state,
        *,
        result_artifact=None,
        operation_id=None,
        reconciliation_ref=None,
        resolved_outcome=None,
    ):
        receipt = {
            **head["data"]["receipt"],
            "state": state,
            "last_observed_at": now(),
            "result_artifact": result_artifact,
            "external_operation_id": operation_id,
            "reconciliation_ref": reconciliation_ref,
            "resolved_outcome": resolved_outcome,
        }
        self.runtime.contracts.validate("effect-receipt", receipt)
        ref = self.store.put(
            db, scope, "effect-receipt", receipt["effect_id"], head["row_version"] + 1, receipt
        )
        self.store.cas(
            db,
            scope,
            "effect",
            receipt["effect_id"],
            head["row_version"],
            state,
            {**head["data"], "receipt": receipt},
        )
        self.store.event(
            db, scope, "effect", receipt["effect_id"], "effect." + state, {"receipt_ref": ref}
        )
        return ref

    def prepare(self, worker: Actor, request: dict) -> dict:
        worker.require("worker.execute")
        scope = worker.scope
        self.runtime.contracts.validate("effect-request", request)
        if request["scope"] != scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Effect belongs to another project")
        lease = self.runtime.lease(
            worker, request["run_id"], request["lease_id"], request["fencing_token"]
        )
        run = self.store.head(scope, "run", request["run_id"])
        if run["state"] != "running":
            raise Hold(
                "EFFECT_RUN_STATE", "Paused, stopped, or verifying work cannot start effects"
            )
        record = run["data"]["record"]
        for field in ("contract_ref", "graph_ref"):
            if request[field] != record[field]:
                raise Hold("EFFECT_REVISION", "Effect is not bound to the current run revision")
        goal = self.store.head(scope, "goal", record["root_goal_id"])
        if goal["state"] != "active" or goal["data"].get("admission_paused"):
            raise Hold("EFFECT_GOAL_STATE", "Goal is not admitting effects")
        tool = self.tools.get(request["tool_ref"])
        if tool.effect_class != request["effect_class"]:
            raise RuntimeFault("EFFECT_CLASS", "Worker cannot downgrade a tool effect class")
        if tool.effect_class == "production_control":
            raise Hold(
                "PRODUCTION_DISABLED", "Default deployment does not permit production control"
            )
        args = strict_json_loads(self.artifacts.read(scope, request["args_artifact"]))
        self.tools.validate_input(tool, args)
        cap = {"action": tool.action, "resource": tool.resource, "effect_class": tool.effect_class}
        work = self.store.head(scope, "work", record["work_id"])
        if not capability_contains(work["data"]["node"]["capabilities"], cap):
            raise Hold(
                "NODE_CAPABILITY_DENIED", "A run cannot borrow a sibling or root grant capability"
            )
        grant = self.authority.preflight(
            scope,
            request["grant_ref"],
            subject_id=goal["data"]["authority_subject_id"],
            contract_ref=request["contract_ref"],
            graph_ref=request["graph_ref"],
            capabilities=[cap],
            effect_key=request["effect_key"],
            artifact_digest=request["args_artifact"]["digest"],
        )
        # Transport IDs and timestamps are not semantic effect payload.
        identity = {
            k: request[k]
            for k in (
                "scope",
                "contract_ref",
                "graph_ref",
                "tool_ref",
                "effect_class",
                "args_artifact",
                "effect_key",
                "grant_ref",
            )
        }
        request_digest = digest(identity)
        with self.store.tx() as db:
            self.runtime._check_lease(
                db,
                scope,
                request["run_id"],
                worker.subject_id,
                request["lease_id"],
                request["fencing_token"],
            )
            current_goal = self.store.head(scope, "goal", record["root_goal_id"], db=db)
            if current_goal["state"] != "active" or current_goal["data"].get("admission_paused"):
                raise Hold(
                    "EFFECT_ADMISSION_PAUSED", "Goal stopped admitting effects during preparation"
                )
            prior = db.execute(
                "SELECT request_digest,effect_id FROM effect_keys WHERE tenant=? AND project=? AND key=?",
                (*scope.keys(), request["effect_key"]),
            ).fetchone()
            if prior:
                if prior[0] != request_digest:
                    raise Conflict(
                        "EFFECT_KEY_CONFLICT", "Effect key already binds a different request"
                    )
                return self.store.head(scope, "effect", prior[1], db=db)["data"]["receipt"]
            self.authority.consume(db, scope, grant, request["effect_key"], request_digest)
            receipt = {
                "schema_version": "3.0.0",
                "effect_id": request["effect_id"],
                "scope": scope.wire(),
                "request_digest": request_digest,
                "state": "prepared",
                "resolved_outcome": None,
                "external_operation_id": None,
                "result_artifact": None,
                "grant_ref": request["grant_ref"],
                "policy_generation": grant["generation"],
                "last_observed_at": now(),
                "reconciliation_ref": None,
            }
            self.runtime.contracts.validate("effect-receipt", receipt)
            self.store.put(db, scope, "effect-request", request["effect_id"], 1, request)
            self.store.put(db, scope, "effect-receipt", request["effect_id"], 1, receipt)
            self.store.cas(
                db,
                scope,
                "effect",
                request["effect_id"],
                0,
                "prepared",
                {"request": request, "receipt": receipt, "worker_id": worker.subject_id},
            )
            db.execute(
                "INSERT INTO effect_keys VALUES(?,?,?,?,?)",
                (*scope.keys(), request["effect_key"], request_digest, request["effect_id"]),
            )
            self.store.event(
                db,
                scope,
                "effect",
                request["effect_id"],
                "effect.prepared",
                {"effect_key": request["effect_key"]},
            )
        return receipt

    def dispatch(self, scope: Scope, effect_id: str) -> dict:
        self.store.assert_outside_tx()
        head = self.store.head(scope, "effect", effect_id)
        request = head["data"]["request"]
        if head["state"] != "prepared":
            if head["state"] in {"unknown", "dispatched"}:
                raise Hold("EFFECT_UNKNOWN", "Reconcile before any retry; dispatch is not repeated")
            return head["data"]["receipt"]
        tool = self.tools.get(request["tool_ref"])
        args = strict_json_loads(self.artifacts.read(scope, request["args_artifact"]))
        run = self.store.head(scope, "run", request["run_id"])
        goal = self.store.head(scope, "goal", run["data"]["record"]["root_goal_id"])
        if (
            run["state"] != "running"
            or goal["state"] != "active"
            or goal["data"].get("admission_paused")
        ):
            raise Hold("EFFECT_ADMISSION_PAUSED", "Effect admission was paused after preparation")
        self.authority.preflight(
            scope,
            request["grant_ref"],
            subject_id=goal["data"]["authority_subject_id"],
            contract_ref=request["contract_ref"],
            graph_ref=request["graph_ref"],
            capabilities=[
                {
                    "action": tool.action,
                    "resource": tool.resource,
                    "effect_class": tool.effect_class,
                }
            ],
            effect_key=request["effect_key"],
            artifact_digest=request["args_artifact"]["digest"],
        )
        with self.store.tx() as db:
            self.runtime._check_lease(
                db,
                scope,
                request["run_id"],
                head["data"]["worker_id"],
                request["lease_id"],
                request["fencing_token"],
            )
            current_goal = self.store.head(
                scope, "goal", run["data"]["record"]["root_goal_id"], db=db
            )
            if current_goal["state"] != "active" or current_goal["data"].get("admission_paused"):
                raise Hold(
                    "EFFECT_ADMISSION_PAUSED", "Goal stopped admitting effects during dispatch"
                )
            current = self.store.head(scope, "effect", effect_id, db=db)
            if current["row_version"] != head["row_version"]:
                raise Conflict("EFFECT_VERSION", "Effect dispatch raced another command")
            self._receipt(db, scope, current, "dispatched")
        try:
            from amplai_foundry.tool_broker.native import invoke_bounded

            result = invoke_bounded(tool, args, request["effect_key"])
            self.tools.validate_result(tool, result)
            artifact = self.artifacts.admit(
                scope,
                __import__("json").dumps(result.value, ensure_ascii=False).encode(),
                "application/json",
                trust="verifier",
            )
            state = result.outcome
        except Exception as exc:
            # The adapter may have committed the external write before failing.
            # Do not expose exception details that could contain credentials.
            with self.store.tx() as db:
                current = self.store.head(scope, "effect", effect_id, db=db)
                self._receipt(db, scope, current, "unknown")
            raise Hold(
                "EFFECT_UNKNOWN",
                "Tool dispatch outcome is unknown; a trusted reconciliation is required",
                details={"exception_type": type(exc).__name__},
            ) from exc
        with self.store.tx() as db:
            current = self.store.head(scope, "effect", effect_id, db=db)
            self._receipt(
                db,
                scope,
                current,
                state,
                result_artifact=artifact,
                operation_id=result.operation_id,
            )
        return self.store.head(scope, "effect", effect_id)["data"]["receipt"]

    def reconcile(self, actor: Actor, effect_id: str) -> dict:
        actor.require("effect.reconcile")
        scope = actor.scope
        head = self.store.head(scope, "effect", effect_id)
        if head["state"] not in {"unknown", "dispatched"}:
            raise Conflict(
                "EFFECT_RECONCILE_STATE", "Only uncertain dispatched effects need reconciliation"
            )
        request = head["data"]["request"]
        tool = self.tools.get(request["tool_ref"])
        if tool.reconcile is None:
            raise Hold("NO_RECONCILER", "No trusted read-only reconciliation adapter is installed")
        # Reconciliation is read-only and does not resurrect an expired write grant.
        result = tool.reconcile(
            request["effect_key"],
            head["data"]["receipt"]["external_operation_id"],
            tool.timeout_seconds,
        )
        self.tools.validate_result(tool, result)
        artifact = self.artifacts.admit(
            scope,
            __import__("json").dumps(result.value, ensure_ascii=False).encode(),
            "application/json",
            trust="verifier",
        )
        observation = {
            "reconciliation_id": new_id("reconcile"),
            "scope": scope.wire(),
            "effect_id": effect_id,
            "actor": actor.wire(),
            "outcome": result.outcome,
            "artifact": artifact,
            "checked_at": now(),
        }
        with self.store.tx() as db:
            current = self.store.head(scope, "effect", effect_id, db=db)
            if current["row_version"] != head["row_version"]:
                raise Conflict("RECONCILE_RACE", "Effect changed during reconciliation")
            ref = self.store.put(
                db, scope, "effect-reconciliation", observation["reconciliation_id"], 1, observation
            )
            self._receipt(
                db,
                scope,
                current,
                "reconciled",
                result_artifact=artifact,
                operation_id=result.operation_id,
                reconciliation_ref=ref,
                resolved_outcome=result.outcome,
            )
        return self.store.head(scope, "effect", effect_id)["data"]["receipt"]
