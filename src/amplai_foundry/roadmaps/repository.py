"""YAML repository for authoritative roadmap state."""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import yaml
from pydantic import ValidationError

from amplai_foundry.roadmaps.models import RoadmapDefinition


class RoadmapRepositoryError(RuntimeError):
    pass


class RoadmapRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RoadmapDefinition:
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            return RoadmapDefinition.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise RoadmapRepositoryError(f"{self.path}: {error}") from error

    def save(self, roadmap: RoadmapDefinition) -> Path:
        payload = yaml.safe_dump(
            roadmap.model_dump(mode="json", exclude_none=True),
            allow_unicode=True,
            sort_keys=False,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = -1
        temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise RoadmapRepositoryError(f"roadmap을 저장할 수 없습니다: {error}") from error
        return self.path

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Serialize revision checks and replacement for this roadmap."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            with lock_path.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    if handle.read(1) == b"":
                        handle.write(b"\0")
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
            raise RoadmapRepositoryError(
                f"roadmap apply lock을 사용할 수 없습니다: {error}"
            ) from error
