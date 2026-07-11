"""Read-only Markdown implementation of the memory repository port."""

from __future__ import annotations

import builtins
from pathlib import Path

from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file


class MarkdownRepositoryError(RuntimeError):
    """A vault cannot be loaded as a coherent repository."""


class MarkdownMemoryRepository:
    """Load canonical memory objects from a Markdown vault."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._objects: dict[str, MemoryObject] | None = None

    def _load(self) -> dict[str, MemoryObject]:
        if self._objects is not None:
            return self._objects
        objects: dict[str, MemoryObject] = {}
        for path in sorted(self.root.rglob("*.md")):
            try:
                document = parse_markdown_file(path)
                memory = MemoryObject.model_validate(
                    {**document.metadata, "content": document.content}
                )
            except (MarkdownParseError, ValidationError) as error:
                raise MarkdownRepositoryError(f"{path}: {error}") from error
            if memory.id in objects:
                raise MarkdownRepositoryError(f"duplicate memory id: {memory.id}")
            objects[memory.id] = memory
        self._objects = objects
        return objects

    def get(self, memory_id: str) -> MemoryObject | None:
        """Return one object by ID."""
        return self._load().get(memory_id)

    def list(self, namespace: str | None = None) -> builtins.list[MemoryObject]:
        """List all objects, optionally restricted to a namespace."""
        objects = self._load().values()
        return sorted(
            (item for item in objects if namespace is None or item.namespace == namespace),
            key=lambda item: item.id,
        )

    def find_by_kind(self, kind: MemoryKind) -> builtins.list[MemoryObject]:
        """List objects of one kind."""
        return [item for item in self.list() if item.kind is kind]

    def find_referencing(self, target_id: str) -> builtins.list[MemoryObject]:
        """List objects with any structured reference to the target."""
        return [item for item in self.list() if target_id in item.referenced_ids()]

    def exists(self, memory_id: str) -> bool:
        """Return whether an ID is present."""
        return memory_id in self._load()
