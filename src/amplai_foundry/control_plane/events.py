"""Event envelope and transactional outbox primitives."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from amplai_foundry.control_plane.store import canonical_json, utc_now


def append_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    project_id: str,
    event_type: str,
    aggregate_ref: str,
    payload: dict[str, Any],
    correlation_id: str,
    causation_id: str | None = None,
    destinations: tuple[str, ...] = ("project-events",),
) -> str:
    event_id = f"evt-{uuid.uuid4().hex}"
    created_at = utc_now()
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "event_id": event_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "event_type": event_type,
        "aggregate_ref": aggregate_ref,
        "correlation_id": correlation_id,
        "causation_id": causation_id,
        "payload": payload,
        "created_at": created_at,
    }
    connection.execute(
        """
        INSERT INTO cp_events(
            event_id, schema_version, tenant_id, project_id, event_type,
            aggregate_ref, correlation_id, causation_id, payload_json, created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            1,
            tenant_id,
            project_id,
            event_type,
            aggregate_ref,
            correlation_id,
            causation_id,
            canonical_json(payload),
            created_at,
        ),
    )
    for destination in destinations:
        outbox_id = f"out-{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO cp_outbox(
                outbox_id, event_id, tenant_id, project_id, destination,
                event_type, aggregate_ref, payload_json, status, attempts,
                max_attempts, lease_token, lease_expires_at, last_error,
                receipt, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?, 'pending',0,5,NULL,NULL,NULL,NULL,?,?)
            """,
            (
                outbox_id,
                event_id,
                tenant_id,
                project_id,
                destination,
                event_type,
                aggregate_ref,
                canonical_json(envelope),
                created_at,
                created_at,
            ),
        )
    return event_id


def decode_event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]),
        "event_id": str(row["event_id"]),
        "schema_version": int(row["schema_version"]),
        "tenant_id": str(row["tenant_id"]),
        "project_id": str(row["project_id"]),
        "event_type": str(row["event_type"]),
        "aggregate_ref": str(row["aggregate_ref"]),
        "correlation_id": str(row["correlation_id"]),
        "causation_id": str(row["causation_id"]) if row["causation_id"] else None,
        "payload": json.loads(str(row["payload_json"])),
        "created_at": str(row["created_at"]),
    }
