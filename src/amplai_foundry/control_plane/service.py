"""Application services for authenticated Platform 0.4 APIs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Callable

from amplai_foundry.control_plane.auth import ApiTokenService
from amplai_foundry.control_plane.errors import ConflictError, NotFoundError, ValidationError
from amplai_foundry.control_plane.events import append_event
from amplai_foundry.control_plane.models import CanonicalReference, DurableJob
from amplai_foundry.control_plane.store import ControlPlaneStore, canonical_json, utc_now

IdempotentOperation = Callable[[sqlite3.Connection, str], tuple[int, dict[str, Any]]]


class ControlPlaneService:
    def __init__(self, store: ControlPlaneStore, auth: ApiTokenService | None = None) -> None:
        self.store = store
        self.auth = auth or ApiTokenService(store)

    @staticmethod
    def _digest(value: object) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

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
            status_code, response = operation(connection, str(correlation_id))
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

        def operation(connection: sqlite3.Connection, correlation_id: str) -> tuple[int, dict[str, Any]]:
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
            existing = connection.execute(
                """
                SELECT canonical_ref, content_digest FROM cp_objects
                WHERE tenant_id=(SELECT tenant_id FROM cp_api_tokens WHERE token_digest=?)
                  AND project_id=? AND kind=? AND origin_store=? AND origin_ref=?
                """,
                (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), project_id, kind, origin_store, origin_ref),
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
            tenant_row = connection.execute(
                "SELECT tenant_id FROM cp_api_tokens WHERE token_digest=?",
                (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),),
            ).fetchone()
            if tenant_row is None:
                raise ValidationError("AUTH_CONTEXT_LOST")
            tenant_id = str(tenant_row["tenant_id"])
            canonical_ref = f"{kind}:{project_id}:{content_digest.split(':', 1)[1][:24]}"
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

        def operation(connection: sqlite3.Connection, correlation_id: str) -> tuple[int, dict[str, Any]]:
            query = request.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValidationError("CONTEXT_QUERY_REQUIRED")
            tenant_row = connection.execute(
                "SELECT tenant_id FROM cp_api_tokens WHERE token_digest=?",
                (hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),),
            ).fetchone()
            if tenant_row is None:
                raise ValidationError("AUTH_CONTEXT_LOST")
            tenant_id = str(tenant_row["tenant_id"])
            job_id = f"job-{uuid.uuid4().hex}"
            now = utc_now()
            payload = {
                "query": query.strip(),
                "filters": request.get("filters") or {},
                "limit": int(request.get("limit") or 20),
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
                    int(request.get("max_attempts") or 3),
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

    def get_reference(
        self, *, kind: str, canonical_ref: str, project_id: str, raw_token: str
    ) -> CanonicalReference:
        principal = self.auth.authenticate(raw_token, project_id=project_id, permission="reference:read")
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
        principal = self.auth.authenticate(raw_token, project_id=project_id, permission="context:read")
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
