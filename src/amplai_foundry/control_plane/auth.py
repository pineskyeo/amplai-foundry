"""Project-scoped API tokens for the Platform 0.4 Control Plane."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid

from amplai_foundry.control_plane.errors import AuthenticationError, AuthorizationError
from amplai_foundry.control_plane.models import IssuedToken, Principal
from amplai_foundry.control_plane.store import ControlPlaneStore, utc_now


class ApiTokenService:
    def __init__(self, store: ControlPlaneStore) -> None:
        self.store = store

    @staticmethod
    def _digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        tenant_id: str,
        project_id: str,
        permissions: set[str] | frozenset[str],
    ) -> IssuedToken:
        if not tenant_id or not project_id or not permissions:
            raise ValueError("tenant_id, project_id, and permissions are required")
        token_id = f"tok-{uuid.uuid4().hex}"
        raw_token = f"amp_{secrets.token_urlsafe(32)}"
        normalized = tuple(sorted(set(permissions)))
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO cp_api_tokens(
                    token_id, token_digest, tenant_id, project_id,
                    permissions_json, status, created_at, revoked_at
                ) VALUES(?,?,?,?,?,'active',?,NULL)
                """,
                (
                    token_id,
                    self._digest(raw_token),
                    tenant_id,
                    project_id,
                    json.dumps(normalized),
                    utc_now(),
                ),
            )
        return IssuedToken(token_id, raw_token, tenant_id, project_id, normalized)

    def authenticate(
        self,
        raw_token: str,
        *,
        project_id: str,
        permission: str | None = None,
    ) -> Principal:
        if not raw_token:
            raise AuthenticationError("BEARER_TOKEN_REQUIRED")
        with self.store.connect() as connection:
            row = connection.execute(
                """
                SELECT token_id, tenant_id, project_id, permissions_json, status
                FROM cp_api_tokens WHERE token_digest=?
                """,
                (self._digest(raw_token),),
            ).fetchone()
        if row is None or row["status"] != "active":
            raise AuthenticationError("BEARER_TOKEN_INVALID")
        if row["project_id"] != project_id:
            raise AuthenticationError("TOKEN_PROJECT_SCOPE_MISMATCH")
        permissions = frozenset(json.loads(str(row["permissions_json"])))
        principal = Principal(
            token_id=str(row["token_id"]),
            tenant_id=str(row["tenant_id"]),
            project_id=str(row["project_id"]),
            permissions=permissions,
        )
        if permission is not None and permission not in permissions and "*" not in permissions:
            raise AuthorizationError(f"PERMISSION_REQUIRED:{permission}")
        return principal

    def revoke(self, token_id: str) -> None:
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE cp_api_tokens SET status='revoked', revoked_at=?
                WHERE token_id=? AND status='active'
                """,
                (utc_now(), token_id),
            )
            if cursor.rowcount != 1:
                raise AuthenticationError("TOKEN_NOT_ACTIVE")
