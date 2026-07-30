"""Fail-closed lifecycle gates for legacy migration backfill and approval review."""

from __future__ import annotations

import sqlite3

from amplai_foundry.governance.models import ProposalRef


def legacy_mutation_block(connection: sqlite3.Connection, ref: ProposalRef) -> str | None:
    """Return the stable error code that blocks a governed Proposal mutation."""

    if not _table_exists(connection, "governance_legacy_event_backfill_pending"):
        return None
    identity = (ref.project_ref.namespace, ref.project_ref.project_id, ref.proposal_id)
    pending = connection.execute(
        """
        SELECT 1 FROM governance_legacy_event_backfill_pending
        WHERE project_namespace = ? AND project_id = ? AND proposal_id = ?
        """,
        identity,
    ).fetchone()
    if pending is not None:
        return "LEGACY_MIGRATION_EVENT_BACKFILL_PENDING"
    if not _table_exists(connection, "governance_legacy_approval_holds"):
        return None
    unresolved = connection.execute(
        """
        SELECT 1
        FROM governance_legacy_approval_holds h
        LEFT JOIN governance_legacy_approval_reviews r
          ON r.migration_id = h.migration_id
         AND r.project_namespace = h.project_namespace
         AND r.project_id = h.project_id
         AND r.proposal_id = h.proposal_id
        WHERE h.project_namespace = ? AND h.project_id = ? AND h.proposal_id = ?
          AND r.review_id IS NULL
        """,
        identity,
    ).fetchone()
    return "LEGACY_APPROVAL_REVIEW_REQUIRED" if unresolved is not None else None


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )
