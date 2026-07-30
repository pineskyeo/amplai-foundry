"""Fail-closed local filesystem validation for the Governance Store."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class GovernanceFilesystemError(RuntimeError):
    """The Governance Store path is not on a verified local filesystem."""


@dataclass(frozen=True, slots=True)
class FilesystemStatus:
    kind: str
    mount_point: Path
    local: bool


class FilesystemProbe(Protocol):
    def inspect(self, path: Path) -> FilesystemStatus: ...


_LOCAL_LINUX_FILESYSTEMS = frozenset(
    {
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "overlay",
        "ufs",
        "xfs",
        "zfs",
    }
)
_REMOTE_FILESYSTEMS = frozenset(
    {
        "9p",
        "afpfs",
        "cifs",
        "davfs",
        "fuse.sshfs",
        "nfs",
        "nfs4",
        "smbfs",
        "sshfs",
        "webdav",
    }
)
_DARWIN_MOUNT_PATTERN = re.compile(r"^.+ on (?P<mount>.+) \((?P<details>.+)\)$")


def _existing_ancestor(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise GovernanceFilesystemError(f"filesystem root를 확인할 수 없습니다: {path}")
        candidate = parent
    return candidate


def _is_under(path: Path, mount_point: Path) -> bool:
    return path == mount_point or mount_point in path.parents


def _decode_mount_path(value: str) -> Path:
    decoded = re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )
    return Path(decoded).resolve(strict=False)


class PlatformFilesystemProbe:
    """Inspect Linux and macOS mount metadata without trusting caller input."""

    def inspect(self, path: Path) -> FilesystemStatus:
        existing = _existing_ancestor(path)
        if sys.platform.startswith("linux"):
            return self._inspect_linux(existing)
        if sys.platform == "darwin":
            return self._inspect_darwin(existing)
        raise GovernanceFilesystemError(
            f"지원하지 않는 platform에서는 Governance Store를 활성화하지 않습니다: {sys.platform}"
        )

    @staticmethod
    def _inspect_linux(path: Path) -> FilesystemStatus:
        mount_info = Path("/proc/self/mountinfo")
        try:
            payload = mount_info.read_text(encoding="utf-8")
        except OSError as error:
            raise GovernanceFilesystemError("Linux mount 정보를 읽을 수 없습니다.") from error
        return _parse_linux_mountinfo(payload, path)

    @staticmethod
    def _inspect_darwin(path: Path) -> FilesystemStatus:
        try:
            completed = subprocess.run(
                ("/sbin/mount",),
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            raise GovernanceFilesystemError("macOS mount 정보를 읽을 수 없습니다.") from error
        return _parse_darwin_mounts(completed.stdout, path)


def _parse_linux_mountinfo(payload: str, path: Path) -> FilesystemStatus:
    matches: list[tuple[Path, str]] = []
    for line in payload.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = _decode_mount_path(fields[4])
            filesystem_kind = fields[separator + 1].lower()
        except (IndexError, ValueError):
            continue
        if _is_under(path, mount_point):
            matches.append((mount_point, filesystem_kind))
    if not matches:
        raise GovernanceFilesystemError(f"mount 정보를 확인할 수 없습니다: {path}")
    mount_point, filesystem_kind = max(matches, key=lambda item: len(item[0].parts))
    return FilesystemStatus(
        kind=filesystem_kind,
        mount_point=mount_point,
        local=filesystem_kind in _LOCAL_LINUX_FILESYSTEMS,
    )


def _parse_darwin_mounts(payload: str, path: Path) -> FilesystemStatus:
    matches: list[tuple[Path, str, frozenset[str]]] = []
    for line in payload.splitlines():
        match = _DARWIN_MOUNT_PATTERN.match(line)
        if match is None:
            continue
        mount_point = _decode_mount_path(match.group("mount"))
        details = tuple(part.strip().lower() for part in match.group("details").split(","))
        if not details or not _is_under(path, mount_point):
            continue
        matches.append((mount_point, details[0], frozenset(details[1:])))
    if not matches:
        raise GovernanceFilesystemError(f"mount 정보를 확인할 수 없습니다: {path}")
    mount_point, filesystem_kind, flags = max(matches, key=lambda item: len(item[0].parts))
    return FilesystemStatus(
        kind=filesystem_kind,
        mount_point=mount_point,
        local="local" in flags and filesystem_kind not in _REMOTE_FILESYSTEMS,
    )


class LocalFilesystemGuard:
    def __init__(self, probe: FilesystemProbe | None = None) -> None:
        self.probe = probe or PlatformFilesystemProbe()

    def validate(self, path: Path) -> FilesystemStatus:
        status = self.probe.inspect(path)
        if not status.local:
            raise GovernanceFilesystemError(
                "Governance Store는 verified local filesystem에서만 사용할 수 있습니다: "
                f"kind={status.kind} mount={status.mount_point}"
            )
        return status
