"""Server-owned session bindings and checkpoint compatibility.

Persist only public facts, versions and digests; never private reasoning or auth.
Every mutation is scoped, revision checked, audited and idempotently bound to the
same dispatch. External transport I/O happens outside these transactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..runtime.contracts.identity import digest
from ..runtime.errors import Conflict, Hold, RuntimeFault

if TYPE_CHECKING:
    from ..runtime.contracts.authority import Actor
    from ..runtime.contracts.registry import Contracts
    from ..runtime.storage.store import Store

BINDINGS = ("contract_ref", "graph_ref", "context_bundle_ref", "composition_ref", "sandbox_ref")
PROFILE_BINDINGS = ("driver_profile_ref", "model_profile_ref", "environment_ref")


class SessionStore:
    def __init__(self, store: Store, contracts: Contracts) -> None:
        self.store, self.contracts = store, contracts

    def prepare(
        self,
        actor: Actor,
        dispatch_id: str,
        envelope: dict[str, Any],
        profile: dict[str, Any],
        *,
        workspace_digest: str,
    ) -> dict[str, Any]:
        actor.require("worker.execute")
        self.contracts.validate("execution-envelope", envelope)
        if envelope["scope"] != actor.scope.wire():
            raise RuntimeFault("SCOPE_MISMATCH", "Session belongs to another project")
        if not isinstance(workspace_digest, str) or not workspace_digest.startswith("sha256:"):
            raise Hold("SESSION_WORKSPACE", "A content-addressed workspace snapshot is required")
        binding = {
            "run_id": envelope["run_id"],
            "work_id": envelope["work_id"],
            "worker_id": actor.subject_id,
            **{k: envelope[k] for k in BINDINGS},
            **{k: profile[k] for k in PROFILE_BINDINGS},
            "workspace_digest": workspace_digest,
        }
        with self.store.tx() as db:
            try:
                h = self.store.head(actor.scope, "driver-session", dispatch_id, db=db)
            except RuntimeFault as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                if h["data"]["binding"] != binding:
                    raise Conflict(
                        "SESSION_BINDING",
                        "An existing session cannot be rebound to different execution facts",
                    )
                return h
            data = {
                "binding": binding,
                "provider_session": None,
                "native_turn": None,
                "process_stopped": True,
                "event_cursor": 0,
                "checkpoint": None,
            }
            self.store.cas(db, actor.scope, "driver-session", dispatch_id, 0, "prepared", data)
            self.store.event(
                db,
                actor.scope,
                "run",
                envelope["run_id"],
                "session.prepared",
                {"dispatch_id": dispatch_id},
            )
            return self.store.head(actor.scope, "driver-session", dispatch_id, db=db)

    def bind(
        self, actor: Actor, dispatch_id: str, provider_session: str, *, expected_version: int
    ) -> dict[str, Any]:
        actor.require("worker.execute")
        if (
            not isinstance(provider_session, str)
            or not provider_session
            or provider_session.startswith("-")
            or provider_session in {"latest", "continue"}
        ):
            raise Hold("SESSION_EXACT", "Only an explicit native session is accepted")
        with self.store.tx() as db:
            h = self.store.head(actor.scope, "driver-session", dispatch_id, db=db)
            if h["data"]["binding"]["worker_id"] != actor.subject_id:
                raise Hold("SESSION_OWNER", "Session is assigned to another worker")
            if h["data"]["provider_session"]:
                if h["data"]["provider_session"] != provider_session:
                    raise Conflict("SESSION_REBIND", "Provider session cannot silently change")
                return h
            if h["state"] != "prepared":
                raise Hold("SESSION_STATE", "Session is not prepared")
            self.store.cas(
                db,
                actor.scope,
                "driver-session",
                dispatch_id,
                expected_version,
                "running",
                {**h["data"], "provider_session": provider_session, "process_stopped": False},
            )
            self.store.event(
                db,
                actor.scope,
                "run",
                h["data"]["binding"]["run_id"],
                "session.bound",
                {"dispatch_id": dispatch_id, "session": provider_session},
            )
            return self.store.head(actor.scope, "driver-session", dispatch_id, db=db)

    def checkpoint(
        self,
        actor: Actor,
        dispatch_id: str,
        *,
        expected_version: int,
        process_stopped: bool,
        snapshot_digest: str,
        pending_effects: list[str],
        driver_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        actor.require("worker.execute")
        if process_stopped is not True or pending_effects:
            raise Hold(
                "CHECKPOINT_PENDING",
                "Confirm stopped process and reconcile effects before checkpoint",
            )
        with self.store.tx() as db:
            h = self.store.head(actor.scope, "driver-session", dispatch_id, db=db)
            if h["data"]["binding"]["worker_id"] != actor.subject_id:
                raise Hold("SESSION_OWNER", "Wrong worker")
            if (
                h["state"] not in {"running", "paused"}
                or driver_receipt.get("session_handle") != h["data"]["provider_session"]
            ):
                raise Hold("CHECKPOINT_SESSION", "Exact bound native session required")
            cp = {
                "binding": h["data"]["binding"],
                "provider_session": h["data"]["provider_session"],
                "workspace_digest": snapshot_digest,
                "driver_receipt_digest": digest(driver_receipt),
            }
            self.store.cas(
                db,
                actor.scope,
                "driver-session",
                dispatch_id,
                expected_version,
                "paused",
                {**h["data"], "checkpoint": cp, "process_stopped": True},
            )
            self.store.event(
                db,
                actor.scope,
                "run",
                h["data"]["binding"]["run_id"],
                "session.checkpoint",
                {"digest": digest(cp)},
            )
            return cp

    def resume_check(
        self,
        actor: Actor,
        dispatch_id: str,
        envelope: dict[str, Any],
        profile: dict[str, Any],
        *,
        workspace_digest: str,
        pending_effects: list[str],
    ) -> dict[str, Any]:
        actor.require("worker.execute")
        self.contracts.validate("execution-envelope", envelope)
        if envelope["scope"] != actor.scope.wire():
            raise Hold("SESSION_SCOPE", "Cross-project session resume")
        h = self.store.head(actor.scope, "driver-session", dispatch_id)
        cp = h["data"]["checkpoint"]
        if h["data"]["binding"]["worker_id"] != actor.subject_id:
            raise Hold("SESSION_OWNER", "Wrong resume worker")
        if h["state"] != "paused" or not cp or pending_effects:
            raise Hold(
                "SESSION_NOT_RESUMABLE", "Session lacks a safe checkpoint or has pending effects"
            )
        old = cp["binding"]
        if old["run_id"] != envelope["run_id"] or any(old[k] != envelope[k] for k in BINDINGS):
            raise Hold(
                "SESSION_REVISION_STALE",
                "Changed execution requires new admission, not a stale native resume",
            )
        if cp["workspace_digest"] != workspace_digest:
            raise Hold("SESSION_SNAPSHOT_STALE", "Workspace differs from checkpoint")
        if any(old[k] != profile[k] for k in PROFILE_BINDINGS):
            return {
                "status": "new_session_required",
                "reason": "driver/model/environment changed",
                "private_reasoning_portable": False,
            }
        return {
            "status": "resumable",
            "session_handle": cp["provider_session"],
            "binding_digest": digest(cp),
        }
