"""Idempotent bridge from Control Plane intent to the Project Store Work graph."""

from __future__ import annotations

import sqlite3
from typing import Protocol

from amplai_foundry.control_plane.errors import NotFoundError, ValidationError
from amplai_foundry.control_plane.events import append_event
from amplai_foundry.control_plane.models import OrchestrationRequest, OutboxItem
from amplai_foundry.control_plane.service import _orchestration_from_row
from amplai_foundry.control_plane.store import ControlPlaneStore, utc_now


class ProjectStoreResolutionHold(Exception):
    """The host adapter cannot safely resolve an intent into a Work yet."""

    def __init__(self, hold_code: str) -> None:
        self.hold_code = hold_code
        super().__init__(hold_code)


class ProjectStoreGateway(Protocol):
    """Host-edge port; the domain never imports the Loop Kit script directly."""

    def ensure_draft_work(self, request: OrchestrationRequest) -> str: ...


class ActivationCardScheduler(Protocol):
    """Host edge that queues a card only for an already-bridged DRAFT Work."""

    def schedule(self, *, request: OrchestrationRequest, work_ref: str) -> str: ...


class OrchestrationBridge:
    """Bridge a request once logically, even when delivery retries after a crash.

    The external gateway must use ``request_id`` as its own uniqueness key.  We
    intentionally do not hold a Control Plane SQLite transaction across that
    call: the two stores have no distributed transaction.
    """

    def __init__(self, store: ControlPlaneStore, gateway: ProjectStoreGateway) -> None:
        self.store = store
        self.gateway = gateway

    def bridge_request(
        self, *, tenant_id: str, project_id: str, request_id: str
    ) -> OrchestrationRequest:
        request = self._get(tenant_id=tenant_id, project_id=project_id, request_id=request_id)
        if request.status == "RESOLUTION_HOLD" or request.status == "BRIDGED":
            return request
        try:
            work_ref = self.gateway.ensure_draft_work(request)
        except ProjectStoreResolutionHold as error:
            return self._hold(request, error.hold_code)
        with self.store.transaction() as connection:
            current = connection.execute(
                """SELECT * FROM cp_orchestration_requests
                WHERE request_id=? AND tenant_id=? AND project_id=?""",
                (request_id, tenant_id, project_id),
            ).fetchone()
            if current is None:
                raise NotFoundError("ORCHESTRATION_REQUEST_NOT_FOUND")
            existing = _orchestration_from_row(current)
            if existing.status == "BRIDGED":
                return existing
            connection.execute(
                """UPDATE cp_orchestration_requests
                SET status='BRIDGED', work_ref=?, hold_code=NULL, updated_at=?
                WHERE request_id=?""",
                (work_ref, utc_now(), request_id),
            )
            append_event(
                connection,
                tenant_id=tenant_id,
                project_id=project_id,
                event_type="orchestration.bridged",
                aggregate_ref=request_id,
                correlation_id=request.correlation_id,
                payload={"request_id": request_id, "work_ref": work_ref},
            )
            row = connection.execute(
                "SELECT * FROM cp_orchestration_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert row is not None
            return _orchestration_from_row(row)

    def _hold(self, request: OrchestrationRequest, hold_code: str) -> OrchestrationRequest:
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE cp_orchestration_requests
                SET status='RESOLUTION_HOLD', hold_code=?, updated_at=?
                WHERE request_id=? AND tenant_id=? AND project_id=?
                  AND status != 'BRIDGED'""",
                (hold_code, utc_now(), request.request_id, request.tenant_id, request.project_id),
            )
            append_event(
                connection,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                event_type="orchestration.resolution_held",
                aggregate_ref=request.request_id,
                correlation_id=request.correlation_id,
                payload={"request_id": request.request_id, "hold_code": hold_code},
            )
            row = connection.execute(
                "SELECT * FROM cp_orchestration_requests WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
            assert row is not None
            return _orchestration_from_row(row)

    def _get(self, *, tenant_id: str, project_id: str, request_id: str) -> OrchestrationRequest:
        with self.store.connect() as connection:
            row: sqlite3.Row | None = connection.execute(
                """SELECT * FROM cp_orchestration_requests
                WHERE request_id=? AND tenant_id=? AND project_id=?""",
                (request_id, tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("ORCHESTRATION_REQUEST_NOT_FOUND")
        return _orchestration_from_row(row)


class OrchestrationBridgeConnector:
    """Lease-owned outbox connector that turns one intent into a DRAFT Work.

    This is deliberately a connector instead of a request-handler side effect:
    the Control Plane commit and Project Store mutation cannot share one
    transaction.  The generic outbox lease gives retries exactly one active
    bridge worker, while the request id stays the Project Store idempotency key.
    """

    destination = "orchestration-bridge"

    def __init__(
        self, bridge: OrchestrationBridge, *, card_scheduler: ActivationCardScheduler | None = None
    ) -> None:
        self.bridge = bridge
        self.card_scheduler = card_scheduler

    def send(self, item: OutboxItem) -> str:
        if item.destination != self.destination or item.event_type != "orchestration.requested":
            raise ValidationError("ORCHESTRATION_BRIDGE_EVENT_INVALID")
        payload_request_id = item.payload.get("payload", {}).get("request_id")
        if payload_request_id != item.aggregate_ref:
            raise ValidationError("ORCHESTRATION_BRIDGE_EVENT_INVALID")
        result = self.bridge.bridge_request(
            tenant_id=item.tenant_id,
            project_id=item.project_id,
            request_id=item.aggregate_ref,
        )
        if result.status == "BRIDGED" and result.work_ref:
            if self.card_scheduler is not None:
                self.card_scheduler.schedule(request=result, work_ref=result.work_ref)
            return f"work:{result.work_ref}"
        return f"hold:{result.hold_code or 'UNKNOWN'}"
