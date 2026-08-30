"""Replayable read projection for Platform 0.4 observability."""

from __future__ import annotations

import json
from typing import Any

from amplai_foundry.control_plane.store import ControlPlaneStore, canonical_json, utc_now


class ProjectionService:
    NAME = "project-summary-v1"

    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store

    def rebuild(self, *, tenant_id: str, project_id: str) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "last_sequence": 0,
            "published": {"decision": 0, "evidence": 0},
            "jobs": {"requested": 0, "completed": 0, "dead_lettered": 0},
            "last_event_type": None,
        }
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM cp_events
                WHERE tenant_id=? AND project_id=? ORDER BY sequence
                """,
                (tenant_id, project_id),
            ).fetchall()
            for row in rows:
                event_type = str(row["event_type"])
                state["last_sequence"] = int(row["sequence"])
                state["last_event_type"] = event_type
                if event_type == "decision.published":
                    state["published"]["decision"] += 1
                elif event_type == "evidence.published":
                    state["published"]["evidence"] += 1
                elif event_type == "context.requested":
                    state["jobs"]["requested"] += 1
                elif event_type == "job.completed":
                    state["jobs"]["completed"] += 1
                elif event_type == "job.dead_lettered":
                    state["jobs"]["dead_lettered"] += 1
            connection.execute(
                """
                INSERT INTO cp_projections(
                    tenant_id, project_id, name, last_sequence, state_json, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(tenant_id,project_id,name) DO UPDATE SET
                    last_sequence=excluded.last_sequence,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (
                    tenant_id,
                    project_id,
                    self.NAME,
                    int(state["last_sequence"]),
                    canonical_json(state),
                    utc_now(),
                ),
            )
        return state

    def snapshot(self, *, tenant_id: str, project_id: str) -> dict[str, Any] | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT state_json FROM cp_projections
                WHERE tenant_id=? AND project_id=? AND name=?
                """,
                (tenant_id, project_id, self.NAME),
            ).fetchone()
        return json.loads(str(row["state_json"])) if row is not None else None
