"""Construct the normative ExecutionEnvelope from server-owned live records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..contracts.authority import intersect_capabilities
from ..contracts.identity import digest, new_id
from ..errors import Conflict, Hold

if TYPE_CHECKING:
    from ..contracts.authority import Actor
    from .service import Runtime


def execution_envelope(
    runtime: Runtime, worker: Actor, dispatch: dict[str, Any], *, _validate_only: bool = False
) -> dict[str, Any] | None:
    worker.require("worker.execute")
    scope, store = worker.scope, runtime.store
    lease = dispatch["lease"]
    runtime.lease(worker, dispatch["run_id"], lease["lease_id"], lease["fencing_token"])
    run = store.head(scope, "run", dispatch["run_id"])
    work = store.head(scope, "work", run["data"]["record"]["work_id"])
    goal = store.head(scope, "goal", work["data"]["goal_id"])
    data = goal["data"]
    if goal["state"] != "active" or data.get("admission_paused"):
        raise Hold("EXECUTION_PAUSED", "Goal no longer admits a new driver operation")
    if (
        dispatch["node"] != work["data"]["node"]
        or dispatch["profile"] != data["profile"]
        or dispatch["contract_ref"] != data["active_contract_ref"]
        or dispatch["graph_ref"] != data["active_graph_ref"]
    ):
        raise Conflict("DISPATCH_TAMPERED", "Dispatch differs from authoritative run definition")
    contract = store.get(scope, "goal-contract", data["active_contract_ref"])
    intent = store.get(scope, "intent-envelope", contract["intent_ref"])
    profile = runtime._profile(
        scope, data["profile"], contract["requested_capabilities"], intent["data_classification"]
    )
    grant = runtime.authority.preflight(
        scope,
        data["grant_ref"],
        subject_id=data["authority_subject_id"],
        contract_ref=data["active_contract_ref"],
        graph_ref=data["active_graph_ref"],
        capabilities=dispatch["node"]["capabilities"],
    )
    policy = store.get(scope, "policy", contract["policy_ref"])
    ceilings = [
        contract["requested_capabilities"],
        grant["capabilities"],
        profile["environment"]["capabilities"],
    ]
    if "requested_ceiling" in policy:
        ceilings.append(policy["requested_ceiling"])
    caps = intersect_capabilities(
        dispatch["node"]["capabilities"], *ceilings, denied=policy.get("denied_capabilities", [])
    )
    if caps != dispatch["node"]["capabilities"]:
        raise Hold(
            "CAPABILITY_INTERSECTION",
            "Required actions exceed the current effective capability intersection",
        )
    with store.tx() as db:
        current = runtime._check_lease(
            db,
            scope,
            dispatch["run_id"],
            worker.subject_id,
            lease["lease_id"],
            lease["fencing_token"],
        )
        head = store.head(scope, "goal", work["data"]["goal_id"], db=db)
        if head["row_version"] != goal["row_version"]:
            raise Conflict("ENVELOPE_RACE", "Goal changed while checking live authority")
        row = db.execute(
            "SELECT * FROM reservations WHERE tenant=? AND project=? AND run_id=?",
            (*scope.keys(), dispatch["run_id"]),
        ).fetchone()
        if row is None or row["status"] != "active":
            raise Hold("RESERVATION_INACTIVE", "Dispatch needs an active root budget reservation")
        if (
            store.clock() - data["started_at_epoch"] >= contract["budget"]["max_wall_seconds"]
            or store.clock() - row["started"] >= row["seconds"]
        ):
            raise Hold(
                "WALL_BUDGET",
                "Pause or transport wait never resets root or run elapsed-time ceilings",
            )
        if _validate_only:
            return None
        reservation = {
            "reservation_id": new_id("execution-reservation"),
            "scope": scope.wire(),
            "run_id": dispatch["run_id"],
            "reserved_tokens": row["tokens"],
            "reserved_cost_microunits": row["cost"],
            "status": row["status"],
        }
        ref = store.put(
            db, scope, "reservation-snapshot", reservation["reservation_id"], 1, reservation
        )
        result = {
            "schema_version": "3.0.0",
            "scope": scope.wire(),
            "run_id": dispatch["run_id"],
            "work_id": current.work_id,
            "node_id": dispatch["node"]["node_id"],
            "attempt": run["data"]["record"]["attempt"],
            "contract_ref": data["active_contract_ref"],
            "graph_ref": data["active_graph_ref"],
            "context_bundle_ref": contract["context_bundle_ref"],
            "composition_ref": data["profile"]["composition_ref"],
            "sandbox_ref": data["profile"]["environment_ref"],
            "grant_ref": data["grant_ref"],
            "lease_id": current.lease_id,
            "fencing_token": current.fencing_token,
            "policy_generation": grant["generation"],
            "expires_at": datetime.fromtimestamp(current.expires_at_epoch, UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "budget_reservation_ref": ref,
            "effective_capabilities": caps,
        }
        runtime.contracts.validate("execution-envelope", result)
        store.event(
            db, scope, "run", dispatch["run_id"], "execution.envelope", {"digest": digest(result)}
        )
    return result


def assert_execution_live(runtime: Runtime, worker: Actor, dispatch: dict[str, Any]) -> None:
    """Recheck authority/fencing without creating an envelope for every poll."""
    execution_envelope(runtime, worker, dispatch, _validate_only=True)
