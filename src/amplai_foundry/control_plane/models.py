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


@dataclass(frozen=True, slots=True)
class OrchestrationRequest:
    """Durable, untrusted intent submitted by a client partner such as Hermes.

    This is deliberately not a Work.  The Project Store bridge is the only
    component allowed to turn a REQUESTED row into a DRAFT Work graph.
    """

    request_id: str
    tenant_id: str
    project_id: str
    controller: str
    goal: str
    project_hint: str | None
    target_app_hint: str | None
    runner_hint: str | None
    artifact_refs: tuple[str, ...]
    reply_route: dict[str, Any]
    correlation_id: str
    status: str
    hold_code: str | None
    work_ref: str | None
    created_at: str
    updated_at: str
