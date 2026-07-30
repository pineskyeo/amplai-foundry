"""Immutable content-addressed objects for Proposal definitions and Apply inputs."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from contextlib import suppress
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
        project_root: Path = Path("."),
        *,
        filesystem_guard: LocalFilesystemGuard | None = None,
    ) -> None:
        self.project_ref = project_ref
        declared_root = project_root.expanduser().absolute()
        try:
            self.project_root = declared_root.resolve(strict=True)
            project_stat = self.project_root.stat()
        except OSError as error:
            raise DefinitionObjectStoreError("project root를 확인할 수 없습니다.") from error
        if declared_root != self.project_root:
            raise DefinitionObjectStoreError(
                "project root ancestor에 symlink를 사용할 수 없습니다."
            )
        if not stat.S_ISDIR(project_stat.st_mode):
            raise DefinitionObjectStoreError("project root는 directory여야 합니다.")
        self._project_identity = (project_stat.st_dev, project_stat.st_ino)
        self.root = self.project_root / ".amplai/proposals"
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
        directory_fd = -1
        try:
            directory_fd = self._open_object_directory(path, create=False)
            payload = self._read_at(directory_fd, path.name)
            manifest = ProposalDefinitionManifest.model_validate_json(payload)
            canonical = canonicalize_definition(manifest)
        except (OSError, ValidationError, ValueError) as error:
            raise DefinitionObjectIntegrityError(
                "definition object를 읽거나 검증할 수 없습니다."
            ) from error
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
        if (
            manifest.proposal_ref != ref
            or canonical.digest != digest
            or canonical.canonical_bytes != payload
        ):
            raise DefinitionObjectIntegrityError("definition object integrity가 깨졌습니다.")
        self._verify_project_root_path()
        return payload

    def get_input_object(self, ref: ProposalRef, digest: str) -> bytes:
        path = self._path_for(ref, digest, "input")
        directory_fd = -1
        try:
            directory_fd = self._open_object_directory(path, create=False)
            payload = self._read_at(directory_fd, path.name)
        except OSError as error:
            raise DefinitionObjectIntegrityError("input object를 읽을 수 없습니다.") from error
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)
        if sha256_digest(payload) != digest:
            raise DefinitionObjectIntegrityError("input object integrity가 깨졌습니다.")
        self._verify_project_root_path()
        return payload

    def _write_immutable(self, path: Path, payload: bytes) -> None:
        self.filesystem_guard.validate(path)
        expected_storage_digest = sha256_digest(payload)
        directory_fd = self._open_object_directory(path)
        temporary_name = f".{path.stem}.{secrets.token_hex(12)}.tmp"
        descriptor = -1
        published = False
        try:
            try:
                existing = self._read_at(directory_fd, path.name)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._verify_payload(existing, payload, expected_storage_digest)
                os.fsync(directory_fd)
                self._verify_project_root_path()
                return

            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary = path.parent / temporary_name
            self._before_publish(temporary, path)
            try:
                _rename_no_replace(directory_fd, temporary_name, path.name)
            except FileExistsError:
                existing = self._read_at(directory_fd, path.name)
                self._verify_payload(existing, payload, expected_storage_digest)
            else:
                published = True
            self._after_publish(path)
            os.fsync(directory_fd)
            self._after_directory_fsync(path)
            actual = self._read_at(directory_fd, path.name)
            self._verify_payload(actual, payload, expected_storage_digest)
            self._verify_project_root_path()
        except DefinitionObjectStoreError:
            raise
        except OSError as error:
            raise DefinitionObjectStoreError("immutable object를 기록할 수 없습니다.") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not published:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=directory_fd)
            os.close(directory_fd)

    def _before_publish(self, temporary: Path, canonical: Path) -> None:
        """Failure-injection seam invoked after file fsync and before publication."""

    def _after_publish(self, canonical: Path) -> None:
        """Failure-injection seam invoked after rename and before directory fsync."""

    def _after_directory_fsync(self, canonical: Path) -> None:
        """Failure-injection seam invoked before post-write rehash."""

    @staticmethod
    def _verify_payload(actual: bytes, expected: bytes, expected_storage_digest: str) -> None:
        if sha256_digest(actual) != expected_storage_digest or actual != expected:
            raise DefinitionObjectCollisionError(
                "canonical object path에 다른 bytes가 이미 존재합니다."
            )

    def _open_object_directory(self, path: Path, *, create: bool = True) -> int:
        relative = path.parent.relative_to(self.project_root)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = -1
        current_fd = -1
        try:
            root_fd = os.open(self.project_root, flags)
            root_stat = os.fstat(root_fd)
            if (root_stat.st_dev, root_stat.st_ino) != self._project_identity:
                raise DefinitionObjectStoreError("project root identity가 변경됐습니다.")
            current_fd = root_fd
            root_fd = -1
            for component in relative.parts:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                        os.fsync(current_fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            result_fd = current_fd
            current_fd = -1
            return result_fd
        except OSError as error:
            if not create and isinstance(error, FileNotFoundError):
                raise
            raise DefinitionObjectStoreError(
                "project-local object directory를 안전하게 열 수 없습니다."
            ) from error
        finally:
            if root_fd >= 0:
                os.close(root_fd)
            if current_fd >= 0:
                os.close(current_fd)

    def _verify_project_root_path(self) -> None:
        try:
            metadata = self.project_root.stat(follow_symlinks=False)
        except OSError as error:
            raise DefinitionObjectStoreError(
                "project root identity를 재검증할 수 없습니다."
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
            )
            != self._project_identity
        ):
            raise DefinitionObjectStoreError("project root identity가 write 중 변경됐습니다.")

    @staticmethod
    def _read_at(directory_fd: int, name: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise DefinitionObjectIntegrityError(
                    "canonical object는 regular file이어야 합니다."
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _rename_no_replace(directory_fd: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        flags = 0x04 | 0x10 | 0x20
        result = rename(
            directory_fd,
            source_bytes,
            directory_fd,
            target_bytes,
            flags,
        )
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise DefinitionObjectStoreError(
                "renameat2를 지원하지 않는 Linux runtime입니다."
            ) from error
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(directory_fd, source_bytes, directory_fd, target_bytes, 1)
    else:
        raise DefinitionObjectStoreError("atomic no-replace rename을 지원하지 않는 platform입니다.")
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target)
    raise OSError(error_number, os.strerror(error_number), target)
