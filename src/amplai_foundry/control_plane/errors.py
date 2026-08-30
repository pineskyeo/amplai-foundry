"""Control Plane domain errors."""

from __future__ import annotations


class ControlPlaneError(RuntimeError):
    """Base error for the Platform 0.4 control plane."""


class AuthenticationError(ControlPlaneError):
    """A bearer token is absent, invalid, revoked, or scoped elsewhere."""


class AuthorizationError(ControlPlaneError):
    """The authenticated principal lacks a required permission."""


class ConflictError(ControlPlaneError):
    """A fail-closed uniqueness/state/idempotency conflict."""


class NotFoundError(ControlPlaneError):
    """A requested durable object does not exist."""


class LeaseError(ControlPlaneError):
    """A durable job/outbox lease is invalid or expired."""


class ValidationError(ControlPlaneError):
    """A request violates a Control Plane contract."""
