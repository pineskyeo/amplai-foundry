"""Sequence-fenced YAML projection for authoritative outbox events."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from amplai_foundry.governance.events import (
    GovernanceEventError,
    OutboxEventView,
    OutboxReconcileError,
)
from amplai_foundry.governance.models import Digest, ProposalRef


class YamlProjectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    proposal_ref: ProposalRef
    source_state_revision: int = Field(ge=1)
    aggregate_sequence: int = Field(ge=1)
    payload_digest: Digest
    payload: dict[str, object]


class YamlProjectionDestination:
    def __init__(self, path: Path, *, destination_ref: str) -> None:
        self.path = path
        self.destination_ref = destination_ref

    def reconcile(self, event: OutboxEventView) -> str | None:
        with self._locked():
            current = self._read()
            if current is None:
                return None
            if (
                current.aggregate_sequence == event.aggregate_sequence
                and current.payload_digest == event.payload_digest
            ):
                return self._receipt(current)
            if current.aggregate_sequence >= event.aggregate_sequence:
                raise OutboxReconcileError("YAML_PROJECTION_DIVERGED")
            return None

    def send(self, event: OutboxEventView) -> str:
        if event.destination_ref != self.destination_ref:
            raise GovernanceEventError("OUTBOX_DESTINATION_MISMATCH")
        actual_digest = self._payload_digest(event.payload)
        if actual_digest != event.payload_digest:
            raise GovernanceEventError("OUTBOX_PAYLOAD_INTEGRITY_FAILURE")
        with self._locked():
            current = self._read()
            if current is not None and (
                current.aggregate_sequence == event.aggregate_sequence
                and current.payload_digest == event.payload_digest
            ):
                return self._receipt(current)
            if current is None:
                if event.aggregate_sequence != 1:
                    raise GovernanceEventError("YAML_SEQUENCE_CAS_CONFLICT")
            elif (
                event.aggregate_sequence != current.aggregate_sequence + 1
                or event.source_state_revision <= current.source_state_revision
            ):
                raise GovernanceEventError("YAML_SEQUENCE_CAS_CONFLICT")
            record = YamlProjectionRecord(
                proposal_ref=event.proposal_ref,
                source_state_revision=event.source_state_revision,
                aggregate_sequence=event.aggregate_sequence,
                payload_digest=event.payload_digest,
                payload=event.payload,
            )
            self._write(record)
            return self._receipt(record)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    locking = vars(msvcrt)["locking"]
                    lock_mode = vars(msvcrt)["LK_LOCK"]
                    unlock_mode = vars(msvcrt)["LK_UNLCK"]
                    locking(handle.fileno(), lock_mode, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        locking(handle.fileno(), unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise GovernanceEventError("YAML_PROJECTION_LOCK_FAILED") from error

    def _read(self) -> YamlProjectionRecord | None:
        if not self.path.exists():
            return None
        try:
            return YamlProjectionRecord.model_validate(
                yaml.safe_load(self.path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise OutboxReconcileError("YAML_PROJECTION_INVALID") from error

    def _write(self, record: YamlProjectionRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = yaml.safe_dump(
            record.model_dump(mode="json"),
            allow_unicode=True,
            sort_keys=False,
        )
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise GovernanceEventError("YAML_PROJECTION_WRITE_FAILED") from error

    @staticmethod
    def _payload_digest(payload: dict[str, object]) -> str:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    @staticmethod
    def _receipt(record: YamlProjectionRecord) -> str:
        return f"yaml:{record.aggregate_sequence}:{record.payload_digest}"
