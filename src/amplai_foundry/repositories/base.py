"""Repository ports for storage-independent application services."""

from __future__ import annotations

import builtins
from typing import Protocol

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.models import MemoryObject


class MemoryRepository(Protocol):
    """Read-only access to canonical memory objects."""

    def get(self, memory_id: str) -> MemoryObject | None: ...

    def list(self, namespace: str | None = None) -> builtins.list[MemoryObject]: ...

    def find_by_kind(self, kind: MemoryKind) -> builtins.list[MemoryObject]: ...

    def find_referencing(self, target_id: str) -> builtins.list[MemoryObject]: ...

    def exists(self, memory_id: str) -> bool: ...
