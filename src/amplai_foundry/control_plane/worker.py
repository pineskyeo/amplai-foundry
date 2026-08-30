"""Lease-based durable worker for Control Plane jobs."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from amplai_foundry.control_plane.errors import LeaseError, NotFoundError
from amplai_foundry.control_plane.events import append_event
from amplai_foundry.control_plane.models import DurableJob
from amplai_foundry.control_plane.service import _job_from_row
from amplai_foundry.control_plane.store import ControlPlaneStore, canonical_json, utc_after, utc_now

JobHandler = Callable[[DurableJob], dict[str, Any]]


def _expired(value: str | None) -> bool:
    if not value:
        return True
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed <= datetime.now(UTC)


class JobQueue:
    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store

    def _recover_expired(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM cp_jobs WHERE status='running' AND lease_expires_at IS NOT NULL"
        ).fetchall()
        now = utc_now()
        for row in rows:
            if not _expired(str(row["lease_expires_at"])):
                continue
            attempts = int(row["attempts"])
            maximum = int(row["max_attempts"])
            status = "pending" if attempts < maximum else "dead"
            connection.execute(
                """
                UPDATE cp_jobs SET status=?, lease_token=NULL, lease_expires_at=NULL,
                    last_error='lease expired', not_before=?, updated_at=?
                WHERE job_id=? AND status='running'
                """,
                (
                    status,
                    utc_after(min(60, 2**attempts)) if status == "pending" else None,
                    now,
                    row["job_id"],
                ),
            )

    def claim_next(
        self, *, worker_id: str, lease_seconds: int = 60, project_id: str | None = None
    ) -> DurableJob | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self.store.transaction() as connection:
            self._recover_expired(connection)
            params: list[object] = [utc_now()]
            project_clause = ""
            if project_id is not None:
                project_clause = " AND project_id=?"
                params.append(project_id)
            row = connection.execute(
                """
                SELECT * FROM cp_jobs
                WHERE status='pending'
                  AND (not_before IS NULL OR not_before<=?)
                """
                + project_clause
                + " ORDER BY created_at, job_id LIMIT 1",
                tuple(params),
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(24)
            now = utc_now()
            cursor = connection.execute(
                """
                UPDATE cp_jobs SET status='running', attempts=attempts+1,
                    lease_token=?, lease_expires_at=?, last_error=NULL, updated_at=?
                WHERE job_id=? AND status='pending'
                """,
                (token, utc_after(lease_seconds), now, row["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            current = connection.execute(
                "SELECT * FROM cp_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            assert current is not None
            payload = json.loads(str(current["payload_json"]))
            append_event(
                connection,
                tenant_id=str(current["tenant_id"]),
                project_id=str(current["project_id"]),
                event_type="job.claimed",
                aggregate_ref=str(current["job_id"]),
                correlation_id=str(payload.get("correlation_id") or current["job_id"]),
                payload={
                    "job_id": current["job_id"],
                    "worker_id": worker_id,
                    "attempt": current["attempts"],
                },
            )
            return _job_from_row(current)

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: int = 60) -> DurableJob:
        with self.store.transaction() as connection:
            self._require_lease(connection, job_id, lease_token)
            connection.execute(
                "UPDATE cp_jobs SET lease_expires_at=?, updated_at=? WHERE job_id=?",
                (utc_after(lease_seconds), utc_now(), job_id),
            )
            current = connection.execute(
                "SELECT * FROM cp_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            assert current is not None
            return _job_from_row(current)

    def complete(self, job_id: str, lease_token: str, result: dict[str, Any]) -> DurableJob:
        with self.store.transaction() as connection:
            row = self._require_lease(connection, job_id, lease_token)
            now = utc_now()
            connection.execute(
                """
                UPDATE cp_jobs SET status='done', result_json=?, lease_token=NULL,
                    lease_expires_at=NULL, not_before=NULL, updated_at=? WHERE job_id=?
                """,
                (canonical_json(result), now, job_id),
            )
            payload = json.loads(str(row["payload_json"]))
            append_event(
                connection,
                tenant_id=str(row["tenant_id"]),
                project_id=str(row["project_id"]),
                event_type="job.completed",
                aggregate_ref=job_id,
                correlation_id=str(payload.get("correlation_id") or job_id),
                payload={"job_id": job_id, "result": result},
            )
            current = connection.execute(
                "SELECT * FROM cp_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            assert current is not None
            return _job_from_row(current)

    def fail(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        retryable: bool = True,
        retry_delay_seconds: int = 1,
    ) -> DurableJob:
        with self.store.transaction() as connection:
            row = self._require_lease(connection, job_id, lease_token)
            attempts = int(row["attempts"])
            maximum = int(row["max_attempts"])
            retry = retryable and attempts < maximum
            status = "pending" if retry else "dead"
            not_before = utc_after(max(0, retry_delay_seconds)) if retry else None
            now = utc_now()
            connection.execute(
                """
                UPDATE cp_jobs SET status=?, lease_token=NULL, lease_expires_at=NULL,
                    last_error=?, not_before=?, updated_at=? WHERE job_id=?
                """,
                (status, error, not_before, now, job_id),
            )
            payload = json.loads(str(row["payload_json"]))
            append_event(
                connection,
                tenant_id=str(row["tenant_id"]),
                project_id=str(row["project_id"]),
                event_type="job.retry_scheduled" if retry else "job.dead_lettered",
                aggregate_ref=job_id,
                correlation_id=str(payload.get("correlation_id") or job_id),
                payload={"job_id": job_id, "attempts": attempts, "error": error},
            )
            current = connection.execute(
                "SELECT * FROM cp_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            assert current is not None
            return _job_from_row(current)

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, job_id: str, lease_token: str
    ) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "SELECT * FROM cp_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("JOB_NOT_FOUND")
        if row["status"] != "running" or row["lease_token"] != lease_token:
            raise LeaseError("JOB_LEASE_MISMATCH")
        expires = str(row["lease_expires_at"]) if row["lease_expires_at"] else None
        if _expired(expires):
            raise LeaseError("JOB_LEASE_EXPIRED")
        return row


class ContextWorker:
    def __init__(
        self, queue: JobQueue, handler: JobHandler, *, worker_id: str = "context-worker"
    ) -> None:
        self.queue = queue
        self.handler = handler
        self.worker_id = worker_id

    def run_once(self, *, project_id: str | None = None) -> DurableJob | None:
        job = self.queue.claim_next(worker_id=self.worker_id, project_id=project_id)
        if job is None:
            return None
        assert job.lease_token is not None
        try:
            result = self.handler(job)
        except Exception as error:
            return self.queue.fail(job.job_id, job.lease_token, str(error), retryable=True)
        return self.queue.complete(job.job_id, job.lease_token, result)
