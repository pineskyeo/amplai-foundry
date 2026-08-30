"""Immutable views exposed by the Platform 0.4 Control Plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    token_id: str
    tenant_id: str
    project_id: str
    permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token_id: str
    raw_token: str
    tenant_id: str
    project_id: str
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalReference:
    canonical_ref: str
    kind: str
    tenant_id: str
    project_id: str
    origin_store: str
    origin_ref: str
    content_digest: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class DurableJob:
    job_id: str
    tenant_id: str
    project_id: str
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    lease_token: str | None
    lease_expires_at: str | None
    result: dict[str, Any] | None
    last_error: str | None
    not_before: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxItem:
    outbox_id: str
    tenant_id: str
    project_id: str
    destination: str
    event_type: str
    aggregate_ref: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    lease_token: str | None
    lease_expires_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
