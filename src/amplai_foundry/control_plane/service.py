"""Application services for authenticated Platform 0.4 APIs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from amplai_foundry.control_plane.auth import ApiTokenService
from amplai_foundry.control_plane.errors import ConflictError, NotFoundError, ValidationError
from amplai_foundry.control_plane.events import append_event
from amplai_foundry.control_plane.models import (
    CanonicalReference,
    DurableJob,
    OrchestrationRequest,
    Principal,
)
from amplai_foundry.control_plane.store import ControlPlaneStore, canonical_json, utc_now

IdempotentOperation = Callable[[sqlite3.Connection, Principal, str], tuple[int, dict[str, Any]]]

CONTEXT_LIMIT_RANGE = (1, 500)
CONTEXT_MAX_ATTEMPTS_RANGE = (1, 10)
ORCHESTRATION_CONTROLLERS = frozenset({"status", "design", "work"})
ORCHESTRATION_RUNNERS = frozenset({"claude-code", "codex"})


class OrchestrationWorkReader(Protocol):
    """Read-only Project Store port; the control-plane domain never imports Kit code."""

    def get_work(self, work_id: str) -> dict[str, Any]: ...


def bounded_int(value: object, *, default: int, minimum: int, maximum: int, field: str) -> int:
    """Coerce a request field to an int inside a stated range, or refuse it.

    `int()` on arbitrary JSON raised `TypeError` for a list or object, which the
    WSGI boundary did not classify — the request escaped as an unhandled error
    rather than a 400 (`D-056`).  `bool` is excluded explicitly because
    `isinstance(True, int)` is true.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValidationError(f"{field}_INVALID")
    try:
        parsed = int(value)
    except ValueError:
        raise ValidationError(f"{field}_INVALID") from None
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{field}_OUT_OF_RANGE")
    return parsed


class ControlPlaneService:
    def __init__(
        self,
        store: ControlPlaneStore,
        auth: ApiTokenService | None = None,
        *,
        work_reader: OrchestrationWorkReader | None = None,
    ) -> None:
        self.store = store
        self.auth = auth or ApiTokenService(store)
        self.work_reader = work_reader

    @staticmethod
    def _digest(value: object) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_ref(
        *,
        kind: str,
        tenant_id: str,
        project_id: str,
        origin_store: str,
        origin_ref: str,
    ) -> str:
        """Derive the identifier from the origin tuple, never from the payload.

        `cp_objects` already declares UNIQUE(tenant_id, project_id, kind,
        origin_store, origin_ref), so deriving the primary key from exactly that
        tuple makes a collision impossible by construction.  Deriving it from the
        content digest did not: two origins may legitimately publish identical
        bytes, and the duplicate check runs on the origin, so the insert reached a
        primary-key conflict (`D-056`).  `tenant_id` is part of the input, which is
        what keeps two tenants sharing a `project_id` string apart.
        """
        identity = canonical_json(
            {
                "t": tenant_id,
                "p": project_id,
                "k": kind,
                "s": origin_store,
                "r": origin_ref,
            }
        )
        return f"{kind}:{project_id}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"

    def _idempotent(
        self,
        *,
        raw_token: str,
        project_id: str,
        permission: str,
        route: str,
        idempotency_key: str,
        request: dict[str, Any],
        operation: IdempotentOperation,
    ) -> tuple[int, dict[str, Any]]:
        if not idempotency_key:
            raise ValidationError("IDEMPOTENCY_KEY_REQUIRED")
        principal = self.auth.authenticate(raw_token, project_id=project_id, permission=permission)
        request_digest = self._digest(request)
        correlation_id = request.get("correlation_id") or f"cor-{uuid.uuid4().hex}"
        with self.store.transaction() as connection:
            previous = self.store.idempotency_lookup(
                connection,
                tenant_id=principal.tenant_id,
                project_id=project_id,
                route=route,
                key=idempotency_key,
                request_digest=request_digest,
            )
            if previous is not None:
                return previous
            status_code, response = operation(connection, principal, str(correlation_id))
            self.store.idempotency_store(
                connection,
                tenant_id=principal.tenant_id,
                project_id=project_id,
                route=route,
                key=idempotency_key,
                request_digest=request_digest,
                response=response,
                status_code=status_code,
            )
            return status_code, response

    def publish_reference(
        self,
        *,
        kind: str,
        project_id: str,
        raw_token: str,
        idempotency_key: str,
        envelope: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        if kind not in {"decision", "evidence"}:
            raise ValidationError("REFERENCE_KIND_INVALID")
        permission = f"{kind}:publish"
        route = f"POST:/v1/projects/{project_id}/{kind}"

        def operation(
            connection: sqlite3.Connection, principal: Principal, correlation_id: str
        ) -> tuple[int, dict[str, Any]]:
            origin = envelope.get("origin")
            payload = envelope.get("payload")
            if not isinstance(origin, dict) or not isinstance(payload, dict):
                raise ValidationError("PROMOTION_ENVELOPE_INVALID")
            if envelope.get("kind") != kind:
                raise ValidationError("PROMOTION_KIND_MISMATCH")
            origin_store = str(origin.get("store") or "")
            origin_ref = str(origin.get("object_ref") or "")
            if not origin_store or not origin_ref:
                raise ValidationError("PROMOTION_ORIGIN_REQUIRED")
            content_digest = self._digest(payload)
            tenant_id = principal.tenant_id
            existing = connection.execute(
                """
                SELECT canonical_ref, content_digest FROM cp_objects
                WHERE tenant_id=? AND project_id=? AND kind=?
                  AND origin_store=? AND origin_ref=?
                """,
                (tenant_id, project_id, kind, origin_store, origin_ref),
            ).fetchone()
            if existing is not None:
                if existing["content_digest"] != content_digest:
                    raise ConflictError("ORIGIN_REF_ALREADY_PUBLISHED_WITH_DIFFERENT_CONTENT")
                return 200, {
                    "status": "accepted",
                    "canonical_ref": str(existing["canonical_ref"]),
                    "content_digest": content_digest,
                    "duplicate_origin": True,
                }
            canonical_ref = self._canonical_ref(
                kind=kind,
                tenant_id=tenant_id,
                project_id=project_id,
                origin_store=origin_store,
                origin_ref=origin_ref,
            )
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO cp_objects(
                    canonical_ref, kind, tenant_id, project_id, origin_store,
                    origin_ref, content_digest, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    canonical_ref,
                    kind,
                    tenant_id,
                    project_id,
                    origin_store,
                    origin_ref,
                    content_digest,
                    canonical_json(payload),
                    created_at,
                ),
            )
            event_id = append_event(
                connection,
                tenant_id=tenant_id,
                project_id=project_id,
                event_type=f"{kind}.published",
                aggregate_ref=canonical_ref,
                correlation_id=correlation_id,
                payload={
                    "canonical_ref": canonical_ref,
                    "origin_store": origin_store,
                    "origin_ref": origin_ref,
                    "content_digest": content_digest,
                },
            )
            return 201, {
                "status": "accepted",
                "canonical_ref": canonical_ref,
                "content_digest": content_digest,
                "event_id": event_id,
                "duplicate_origin": False,
            }

        return self._idempotent(
            raw_token=raw_token,
            project_id=project_id,
            permission=permission,
            route=route,
            idempotency_key=idempotency_key,
            request=envelope,
            operation=operation,
        )

    def request_context(
        self,
        *,
        project_id: str,
        raw_token: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        route = f"POST:/v1/projects/{project_id}/context-requests"

        def operation(
            connection: sqlite3.Connection, principal: Principal, correlation_id: str
        ) -> tuple[int, dict[str, Any]]:
            query = request.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValidationError("CONTEXT_QUERY_REQUIRED")
            tenant_id = principal.tenant_id
            job_id = f"job-{uuid.uuid4().hex}"
            now = utc_now()
            limit = bounded_int(
                request.get("limit"),
                default=20,
                minimum=CONTEXT_LIMIT_RANGE[0],
                maximum=CONTEXT_LIMIT_RANGE[1],
                field="LIMIT",
            )
            max_attempts = bounded_int(
                request.get("max_attempts"),
                default=3,
                minimum=CONTEXT_MAX_ATTEMPTS_RANGE[0],
                maximum=CONTEXT_MAX_ATTEMPTS_RANGE[1],
                field="MAX_ATTEMPTS",
            )
            payload = {
                "query": query.strip(),
                "filters": request.get("filters") or {},
                "limit": limit,
                "correlation_id": correlation_id,
            }
            connection.execute(
                """
                INSERT INTO cp_jobs(
                    job_id, tenant_id, project_id, kind, payload_json, status,
                    attempts, max_attempts, lease_token, lease_expires_at,
                    result_json, last_error, not_before, created_at, updated_at
                ) VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,NULL,?,?)
                """,
                (
                    job_id,
                    tenant_id,
                    project_id,
                    "context.build",
                    canonical_json(payload),
                    max_attempts,
                    now,
                    now,
                ),
            )
            event_id = append_event(
                connection,
                tenant_id=tenant_id,
                project_id=project_id,
                event_type="context.requested",
                aggregate_ref=job_id,
                correlation_id=correlation_id,
                payload={"job_id": job_id, "query": query.strip()},
            )
            return 202, {"job_id": job_id, "status": "pending", "event_id": event_id}

        return self._idempotent(
            raw_token=raw_token,
            project_id=project_id,
            permission="context:request",
            route=route,
            idempotency_key=idempotency_key,
            request=request,
            operation=operation,
        )

    def submit_orchestration_request(
        self,
        *,
        project_id: str,
        raw_token: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Persist a DRAFT-only orchestration intent and its bridge outbox event."""
        route = f"POST:/v1/projects/{project_id}/orchestration-requests"

        def operation(
            connection: sqlite3.Connection, principal: Principal, correlation_id: str
        ) -> tuple[int, dict[str, Any]]:
            controller = request.get("controller")
            if controller not in ORCHESTRATION_CONTROLLERS:
                raise ValidationError("ORCHESTRATION_CONTROLLER_INVALID")
            goal = request.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValidationError("ORCHESTRATION_GOAL_REQUIRED")
            project_hint = _nullable_string(request.get("project_hint"), "PROJECT_HINT_INVALID")
            target_app_hint = _nullable_string(
                request.get("target_app_hint"), "TARGET_APP_HINT_INVALID"
            )
            runner_hint = _nullable_string(request.get("runner_hint"), "RUNNER_HINT_INVALID")
            if runner_hint is not None and runner_hint not in ORCHESTRATION_RUNNERS:
                raise ValidationError("RUNNER_HINT_INVALID")
            artifact_refs = _string_list(request.get("artifact_refs", []), "ARTIFACT_REFS_INVALID")
            reply_route = _reply_route(request.get("reply_route"))
            hold_code = None
            if project_hint is not None and project_hint != project_id:
                hold_code = "PROJECT_MISMATCH"
            elif controller == "status":
                hold_code = "STATUS_QUERY_REQUIRES_WORK_REF"
            elif target_app_hint is None:
                hold_code = "TARGET_APP_REQUIRED"
            request_id = request.get("request_id")
            if request_id is None:
                request_id = f"REQ-{uuid.uuid4().hex}"
            if not isinstance(request_id, str) or not request_id.startswith("REQ-"):
                raise ValidationError("REQUEST_ID_INVALID")
            request_digest = self._digest(request)
            existing = connection.execute(
                "SELECT * FROM cp_orchestration_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["tenant_id"] != principal.tenant_id
                    or existing["project_id"] != project_id
                ):
                    raise ConflictError("REQUEST_ID_ALREADY_EXISTS")
                if existing["request_digest"] != request_digest:
                    raise ConflictError("REQUEST_ID_REUSED_WITH_DIFFERENT_REQUEST")
                return 200, _orchestration_response(_orchestration_from_row(existing))
            now = utc_now()
            status = "RESOLUTION_HOLD" if hold_code else "REQUESTED"
            connection.execute(
                """
                INSERT INTO cp_orchestration_requests(
                    request_id, tenant_id, project_id, request_digest, controller, goal,
                    project_hint, target_app_hint, runner_hint, artifact_refs_json,
                    reply_route_json, correlation_id, status, hold_code, work_ref,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
                """,
                (
                    request_id,
                    principal.tenant_id,
                    project_id,
                    request_digest,
                    controller,
                    goal.strip(),
                    project_hint,
                    target_app_hint,
                    runner_hint,
                    canonical_json(artifact_refs),
                    canonical_json(reply_route),
                    correlation_id,
                    status,
                    hold_code,
                    now,
                    now,
                ),
            )
            event_id = append_event(
                connection,
                tenant_id=principal.tenant_id,
                project_id=project_id,
                event_type="orchestration.requested",
                aggregate_ref=request_id,
                correlation_id=correlation_id,
                payload={
                    "request_id": request_id,
                    "controller": controller,
                    "target_app_hint": target_app_hint,
                    "status": status,
                    "hold_code": hold_code,
                },
                destinations=("project-events", "orchestration-bridge"),
            )
            row = connection.execute(
                "SELECT * FROM cp_orchestration_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert row is not None
            response = _orchestration_response(_orchestration_from_row(row))
            response["event_id"] = event_id
            return 202, response

        return self._idempotent(
            raw_token=raw_token,
            project_id=project_id,
            permission="orchestration.request.submit",
            route=route,
            idempotency_key=idempotency_key,
            request=request,
            operation=operation,
        )

    def get_orchestration_request(
        self, *, request_id: str, project_id: str, raw_token: str
    ) -> OrchestrationRequest:
        principal = self.auth.authenticate(
            raw_token, project_id=project_id, permission="orchestration.request.read"
        )
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT * FROM cp_orchestration_requests
                WHERE request_id=? AND tenant_id=? AND project_id=?""",
                (request_id, principal.tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("ORCHESTRATION_REQUEST_NOT_FOUND")
        return _orchestration_from_row(row)

    def get_orchestration_work(
        self, *, work_id: str, project_id: str, raw_token: str
    ) -> dict[str, Any]:
        principal = self.auth.authenticate(
            raw_token, project_id=project_id, permission="orchestration.work.read"
        )
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT * FROM cp_orchestration_requests
                WHERE work_ref=? AND tenant_id=? AND project_id=?""",
                (work_id, principal.tenant_id, project_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("ORCHESTRATION_WORK_NOT_FOUND")
        if self.work_reader is None:
            raise NotFoundError("AUTHORITATIVE_WORK_READER_UNAVAILABLE")
        work = self.work_reader.get_work(work_id)
        if str(work.get("work_id")) != work_id:
            raise NotFoundError("ORCHESTRATION_WORK_NOT_FOUND")
        return work

    def list_pending_orchestration_actions(
        self, *, project_id: str, raw_token: str
    ) -> dict[str, Any]:
        principal = self.auth.authenticate(
            raw_token, project_id=project_id, permission="orchestration.work.read"
        )
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM cp_orchestration_requests
                WHERE tenant_id=? AND project_id=?
                  AND status IN ('REQUESTED', 'RESOLUTION_HOLD')
                ORDER BY created_at, request_id""",
                (principal.tenant_id, project_id),
            ).fetchall()
        pending_actions = [_orchestration_response(_orchestration_from_row(row)) for row in rows]
        return {"pending_actions": pending_actions}

    def request_knowledge_intake(
        self,
        *,
        project_id: str,
        raw_token: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        source_text = request.get("source_text")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValidationError("SOURCE_TEXT_REQUIRED")
        _reply_route(request.get("reply_route"))
        route = f"POST:/v1/projects/{project_id}/knowledge-intake-requests"

        def operation(
            connection: sqlite3.Connection, principal: Principal, correlation_id: str
        ) -> tuple[int, dict[str, Any]]:
            job_id = f"knowledge-intake-{uuid.uuid4().hex}"
            now = utc_now()
            payload = {
                "source_text": source_text.strip(),
                "reply_route": request["reply_route"],
                "correlation_id": correlation_id,
                "workflow": "source-preserve_then_governed_curation",
            }
            connection.execute(
                """
                INSERT INTO cp_jobs(
                    job_id, tenant_id, project_id, kind, payload_json, status,
                    attempts, max_attempts, lease_token, lease_expires_at,
                    result_json, last_error, not_before, created_at, updated_at
                ) VALUES(?,?,?,?,?,'pending',0,1,NULL,NULL,NULL,NULL,NULL,?,?)
                """,
                (
                    job_id,
                    principal.tenant_id,
                    project_id,
                    "knowledge.intake",
                    canonical_json(payload),
                    now,
                    now,
                ),
            )
            event_id = append_event(
                connection,
                tenant_id=principal.tenant_id,
                project_id=project_id,
                event_type="knowledge.intake.requested",
                aggregate_ref=job_id,
                correlation_id=correlation_id,
                payload={"job_id": job_id},
            )
            return 202, {"job_id": job_id, "status": "pending", "event_id": event_id}

        return self._idempotent(
            raw_token=raw_token,
            project_id=project_id,
            permission="orchestration.request.submit",
            route=route,
            idempotency_key=idempotency_key,
            request=request,
            operation=operation,
        )

    def get_reference(
        self, *, kind: str, canonical_ref: str, project_id: str, raw_token: str
    ) -> CanonicalReference:
        principal = self.auth.authenticate(
            raw_token, project_id=project_id, permission="reference:read"
        )
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM cp_objects
                WHERE tenant_id=? AND project_id=? AND kind=? AND canonical_ref=?
                """,
                (principal.tenant_id, project_id, kind, canonical_ref),
            ).fetchone()
        if row is None:
            raise NotFoundError("REFERENCE_NOT_FOUND")
        return CanonicalReference(
            canonical_ref=str(row["canonical_ref"]),
            kind=str(row["kind"]),
            tenant_id=str(row["tenant_id"]),
            project_id=str(row["project_id"]),
            origin_store=str(row["origin_store"]),
            origin_ref=str(row["origin_ref"]),
            content_digest=str(row["content_digest"]),
            payload=json.loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
        )

    def get_job(self, *, job_id: str, project_id: str, raw_token: str) -> DurableJob:
        principal = self.auth.authenticate(
            raw_token, project_id=project_id, permission="context:read"
        )
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cp_jobs WHERE tenant_id=? AND project_id=? AND job_id=?",
                (principal.tenant_id, project_id, job_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("JOB_NOT_FOUND")
        return _job_from_row(row)


def _job_from_row(row: sqlite3.Row) -> DurableJob:
    return DurableJob(
        job_id=str(row["job_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        kind=str(row["kind"]),
        payload=json.loads(str(row["payload_json"])),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        lease_token=str(row["lease_token"]) if row["lease_token"] else None,
        lease_expires_at=str(row["lease_expires_at"]) if row["lease_expires_at"] else None,
        result=json.loads(str(row["result_json"])) if row["result_json"] else None,
        last_error=str(row["last_error"]) if row["last_error"] else None,
        not_before=str(row["not_before"]) if row["not_before"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _nullable_string(value: object, error: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(error)
    return value.strip()


def _string_list(value: object, error: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValidationError(error)
    if len(set(value)) != len(value):
        raise ValidationError(error)
    return tuple(value)


def _reply_route(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("provider") != "slack":
        raise ValidationError("REPLY_ROUTE_INVALID")
    for field in ("workspace_id", "channel_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValidationError("REPLY_ROUTE_INVALID")
    thread_id = value.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        raise ValidationError("REPLY_ROUTE_INVALID")
    return {
        "provider": "slack",
        "workspace_id": value["workspace_id"],
        "channel_id": value["channel_id"],
        "thread_id": thread_id,
    }


def _orchestration_from_row(row: sqlite3.Row) -> OrchestrationRequest:
    return OrchestrationRequest(
        request_id=str(row["request_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        controller=str(row["controller"]),
        goal=str(row["goal"]),
        project_hint=str(row["project_hint"]) if row["project_hint"] else None,
        target_app_hint=str(row["target_app_hint"]) if row["target_app_hint"] else None,
        runner_hint=str(row["runner_hint"]) if row["runner_hint"] else None,
        artifact_refs=tuple(json.loads(str(row["artifact_refs_json"]))),
        reply_route=json.loads(str(row["reply_route_json"])),
        correlation_id=str(row["correlation_id"]),
        status=str(row["status"]),
        hold_code=str(row["hold_code"]) if row["hold_code"] else None,
        work_ref=str(row["work_ref"]) if row["work_ref"] else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _orchestration_response(value: OrchestrationRequest) -> dict[str, Any]:
    return {
        "request_id": value.request_id,
        "status": value.status,
        "hold_code": value.hold_code,
        "controller": value.controller,
        "goal": value.goal,
        "target_app_hint": value.target_app_hint,
        "runner_hint": value.runner_hint,
        "work_ref": value.work_ref,
        "correlation_id": value.correlation_id,
    }
