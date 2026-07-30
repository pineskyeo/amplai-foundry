"""Immutable content-addressed objects for Proposal definitions and Apply inputs."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from amplai_foundry.domain.identity import ProjectRef
from amplai_foundry.governance.definitions import (
    ProposalDefinitionManifest,
    canonicalize_definition,
)
from amplai_foundry.governance.filesystem import LocalFilesystemGuard
from amplai_foundry.governance.models import ProposalRef


class DefinitionObjectStoreError(RuntimeError):
    """An immutable object cannot be stored or verified safely."""


class DefinitionObjectIntegrityError(DefinitionObjectStoreError):
    """Stored object bytes do not match their content identity."""


class DefinitionObjectCollisionError(DefinitionObjectStoreError):
    """A canonical path already contains different or corrupt bytes."""


@dataclass(frozen=True, slots=True)
class DefinitionObjectRef:
    proposal_ref: ProposalRef
    object_kind: Literal["definition", "input"]
    digest: str
    path: Path


def sha256_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validate_digest(value: str) -> str:
    prefix, separator, hexadecimal = value.partition(":")
    if prefix != "sha256" or separator != ":" or len(hexadecimal) != 64:
        raise DefinitionObjectStoreError("digest는 sha256:<64 lowercase hex> 형식이어야 합니다.")
    if any(character not in "0123456789abcdef" for character in hexadecimal):
        raise DefinitionObjectStoreError("digest는 sha256:<64 lowercase hex> 형식이어야 합니다.")
    return value


class ImmutableDefinitionObjectStore:
    """Project-scoped immutable object repository with no active-pointer behavior."""

    def __init__(
        self,
        project_ref: ProjectRef,
        root: Path = Path(".amplai/proposals"),
        *,
        filesystem_guard: LocalFilesystemGuard | None = None,
    ) -> None:
        self.project_ref = project_ref
        self.root = root.expanduser().absolute()
        self.filesystem_guard = filesystem_guard or LocalFilesystemGuard()

    def _require_ref(self, ref: ProposalRef) -> None:
        if ref.project_ref != self.project_ref:
            raise DefinitionObjectStoreError("PROPOSAL_NOT_FOUND")

    def _path_for(
        self,
        ref: ProposalRef,
        digest: str,
        object_kind: Literal["definition", "input"],
    ) -> Path:
        self._require_ref(ref)
        validated_digest = _validate_digest(digest)
        directory = "definitions" if object_kind == "definition" else "inputs"
        suffix = ".yaml" if object_kind == "definition" else ".md"
        return self.root / ref.proposal_id / directory / f"{validated_digest}{suffix}"

    def put_definition_object(
        self,
        ref: ProposalRef,
        canonical_bytes: bytes,
        expected_digest: str,
    ) -> DefinitionObjectRef:
        path = self._path_for(ref, expected_digest, "definition")
        try:
            manifest = ProposalDefinitionManifest.model_validate_json(canonical_bytes)
            canonical = canonicalize_definition(manifest)
        except (ValidationError, ValueError) as error:
            raise DefinitionObjectIntegrityError(
                "definition bytes를 검증할 수 없습니다."
            ) from error
        if manifest.proposal_ref != ref:
            raise DefinitionObjectStoreError("PROPOSAL_NOT_FOUND")
        if canonical.digest != expected_digest or canonical.canonical_bytes != canonical_bytes:
            raise DefinitionObjectIntegrityError(
                "definition canonical bytes 또는 digest가 expected identity와 다릅니다."
            )
        self._write_immutable(path, canonical_bytes)
        return DefinitionObjectRef(ref, "definition", expected_digest, path)

    def put_input_object(
        self,
        ref: ProposalRef,
        object_bytes: bytes,
        expected_digest: str,
    ) -> DefinitionObjectRef:
        path = self._path_for(ref, expected_digest, "input")
        if sha256_digest(object_bytes) != expected_digest:
            raise DefinitionObjectIntegrityError("input bytes가 expected digest와 다릅니다.")
        self._write_immutable(path, object_bytes)
        return DefinitionObjectRef(ref, "input", expected_digest, path)

    def get_definition_object(self, ref: ProposalRef, digest: str) -> bytes:
        path = self._path_for(ref, digest, "definition")
        try:
            payload = path.read_bytes()
            manifest = ProposalDefinitionManifest.model_validate_json(payload)
            canonical = canonicalize_definition(manifest)
        except (OSError, ValidationError, ValueError) as error:
            raise DefinitionObjectIntegrityError(
                "definition object를 읽거나 검증할 수 없습니다."
            ) from error
        if (
            manifest.proposal_ref != ref
            or canonical.digest != digest
            or canonical.canonical_bytes != payload
        ):
            raise DefinitionObjectIntegrityError("definition object integrity가 깨졌습니다.")
        return payload

    def get_input_object(self, ref: ProposalRef, digest: str) -> bytes:
        path = self._path_for(ref, digest, "input")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise DefinitionObjectIntegrityError("input object를 읽을 수 없습니다.") from error
        if sha256_digest(payload) != digest:
            raise DefinitionObjectIntegrityError("input object integrity가 깨졌습니다.")
        return payload

    def _write_immutable(self, path: Path, payload: bytes) -> None:
        self.filesystem_guard.validate(path)
        self._reject_symlink_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink_path(path)
        if path.exists():
            self._verify_existing(path, payload)
            self._fsync_directory(path.parent)
            return

        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._before_publish(temporary, path)
            try:
                os.link(temporary, path)
            except FileExistsError:
                self._verify_existing(path, payload)
            self._fsync_directory(path.parent)
            self._verify_existing(path, payload)
        except DefinitionObjectStoreError:
            raise
        except OSError as error:
            raise DefinitionObjectStoreError("immutable object를 기록할 수 없습니다.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _before_publish(self, temporary: Path, canonical: Path) -> None:
        """Failure-injection seam invoked after file fsync and before publication."""

    @staticmethod
    def _verify_existing(path: Path, expected: bytes) -> None:
        if path.is_symlink():
            raise DefinitionObjectIntegrityError("canonical object는 symlink일 수 없습니다.")
        try:
            actual = path.read_bytes()
        except OSError as error:
            raise DefinitionObjectIntegrityError("existing object를 읽을 수 없습니다.") from error
        if actual != expected:
            raise DefinitionObjectCollisionError(
                "canonical object path에 다른 bytes가 이미 존재합니다."
            )

    def _reject_symlink_path(self, path: Path) -> None:
        if path.is_symlink():
            raise DefinitionObjectStoreError("immutable object는 symlink일 수 없습니다.")
        current = path.parent
        while current != self.root.parent:
            if current.is_symlink():
                raise DefinitionObjectStoreError(
                    "immutable object path에 symlink를 사용할 수 없습니다."
                )
            if current == self.root:
                return
            current = current.parent
        raise DefinitionObjectStoreError("immutable object path가 configured root 밖입니다.")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
