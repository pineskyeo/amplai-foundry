"""Connector isolation and file-drop destination for Platform events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol

from amplai_foundry.control_plane.errors import ValidationError
from amplai_foundry.control_plane.models import OutboxItem


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> bytes: ...


class EnvironmentSecretResolver:
    """Resolve operator-owned secrets without persisting their raw value in SQLite."""

    def __init__(self, prefix: str = "AMPLAI_SECRET_") -> None:
        self.prefix = prefix

    def resolve(self, secret_ref: str) -> bytes:
        if not secret_ref or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-."
            for char in secret_ref
        ):
            raise ValidationError("SECRET_REF_INVALID")
        name = self.prefix + secret_ref.upper().replace("-", "_").replace(".", "_")
        value = os.environ.get(name)
        if value is None:
            raise ValidationError(f"SECRET_NOT_AVAILABLE:{secret_ref}")
        return value.encode("utf-8")


class Connector(Protocol):
    def send(self, item: OutboxItem) -> str: ...


class FileDropConnector:
    """Atomic JSON event delivery under one operator-selected directory.

    Optional HMAC signing demonstrates secret isolation: the outbox stores only
    a secret reference in connector configuration; raw material is resolved at
    send time and never appears in event payloads or receipts.
    """

    def __init__(
        self,
        root: Path,
        *,
        secret_ref: str | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.secret_ref = secret_ref
        self.secret_resolver = secret_resolver

    def send(self, item: OutboxItem) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        body: dict[str, object] = {
            "outbox_id": item.outbox_id,
            "destination": item.destination,
            "event_type": item.event_type,
            "aggregate_ref": item.aggregate_ref,
            "payload": item.payload,
        }
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if self.secret_ref is not None:
            if self.secret_resolver is None:
                raise ValidationError("SECRET_RESOLVER_REQUIRED")
            secret = self.secret_resolver.resolve(self.secret_ref)
            body["signature"] = (
                "hmac-sha256:" + hmac.new(secret, canonical, hashlib.sha256).hexdigest()
            )
        final = self.root / f"{item.outbox_id}.json"
        if final.exists():
            return f"file:{final.name}"
        descriptor, name = tempfile.mkstemp(
            prefix=f".{item.outbox_id}.", suffix=".tmp", dir=self.root
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, final)
        finally:
            temporary.unlink(missing_ok=True)
        return f"file:{final.name}"


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, destination: str, connector: Connector) -> None:
        if not destination:
            raise ValueError("destination is required")
        self._connectors[destination] = connector

    def get(self, destination: str) -> Connector:
        connector = self._connectors.get(destination)
        if connector is None:
            raise ValidationError(f"CONNECTOR_NOT_REGISTERED:{destination}")
        return connector
