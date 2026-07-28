"""Read-only Markdown implementation of the memory repository port."""

from __future__ import annotations

import builtins
from pathlib import Path

from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.identity import MemoryRef
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown_file


class MarkdownRepositoryError(RuntimeError):
    """A vault cannot be loaded as a coherent repository."""


class MarkdownMemoryRepository:
    """Load canonical memory objects from a Markdown vault."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._objects: dict[MemoryRef, MemoryObject] | None = None
        self._paths: dict[MemoryRef, Path] = {}

    def _load(self) -> dict[MemoryRef, MemoryObject]:
        if self._objects is not None:
            return self._objects
        objects: dict[MemoryRef, MemoryObject] = {}
        for path in sorted(self.root.rglob("*.md")):
            try:
                path.resolve().relative_to(self.root.resolve())
            except ValueError as error:
                raise MarkdownRepositoryError(
                    f"memory path escapes repository root: {path}"
                ) from error
            try:
                document = parse_markdown_file(path)
                memory = MemoryObject.model_validate(
                    {**document.metadata, "content": document.content}
                )
            except (MarkdownParseError, ValidationError) as error:
                raise MarkdownRepositoryError(f"{path}: {error}") from error
            if memory.ref in objects:
                raise MarkdownRepositoryError(f"duplicate memory ref: {memory.ref}")
            objects[memory.ref] = memory
            self._paths[memory.ref] = path
        self._objects = objects
        return objects

    def get(
        self,
        memory_ref: MemoryRef | str,
        *,
        namespace: str | None = None,
    ) -> MemoryObject | None:
        """Return one object by qualified ref or an unambiguous legacy local ID."""
        resolved = self._resolve_ref(memory_ref, namespace=namespace)
        return self._load().get(resolved) if resolved is not None else None

    def list(self, namespace: str | None = None) -> builtins.list[MemoryObject]:
        """List all objects, optionally restricted to a namespace."""
        objects = self._load().values()
        return sorted(
            (item for item in objects if namespace is None or item.namespace == namespace),
            key=lambda item: (item.namespace, item.id),
        )

    def find_by_kind(
        self,
        kind: MemoryKind,
        *,
        namespace: str | None = None,
    ) -> builtins.list[MemoryObject]:
        """List objects of one kind."""
        return [item for item in self.list(namespace) if item.kind is kind]

    def find_referencing(
        self,
        target_ref: MemoryRef | str,
        *,
        namespace: str | None = None,
    ) -> builtins.list[MemoryObject]:
        """List objects with any structured reference to the target."""
        resolved = self._resolve_ref(target_ref, namespace=namespace)
        if resolved is None:
            return []
        return [item for item in self.list() if resolved in item.referenced_refs()]

    def exists(
        self,
        memory_ref: MemoryRef | str,
        *,
        namespace: str | None = None,
    ) -> bool:
        """Return whether an ID is present."""
        return self.get(memory_ref, namespace=namespace) is not None

    def path_for(
        self,
        memory_ref: MemoryRef | str,
        *,
        namespace: str | None = None,
    ) -> str:
        """Return the adapter path for display and diffs."""
        resolved = self._resolve_ref(memory_ref, namespace=namespace)
        path = self._paths.get(resolved) if resolved is not None else None
        return str(path) if path else ""

    def _resolve_ref(
        self,
        value: MemoryRef | str,
        *,
        namespace: str | None,
    ) -> MemoryRef | None:
        objects = self._load()
        if isinstance(value, MemoryRef):
            return value
        if "#" in value:
            try:
                return MemoryRef.parse(value)
            except ValueError as error:
                raise MarkdownRepositoryError(str(error)) from error
        if namespace is not None:
            try:
                return MemoryRef(namespace=namespace, local_id=value)
            except ValueError as error:
                raise MarkdownRepositoryError(str(error)) from error
        matches = [reference for reference in objects if reference.local_id == value]
        if len(matches) > 1:
            qualified = ", ".join(sorted(reference.qualified for reference in matches))
            raise MarkdownRepositoryError(
                f"ambiguous local memory ID {value}; qualified ref를 사용하세요: {qualified}"
            )
        return matches[0] if matches else None
