"""Project-scoped semantic anchor repository."""

from __future__ import annotations

import builtins
import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.semantic import (
    ClaimModality,
    SemanticClaim,
    SemanticDescriptor,
    SemanticScope,
    semantic_signature,
)
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository
from amplai_foundry.semantics.models import SemanticAnchor


class SemanticAnchorSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    anchors: list[SemanticAnchor] = Field(default_factory=list)


class SemanticRepositoryError(RuntimeError):
    pass


class SemanticAnchorRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list(self, project: ProjectRef | None = None) -> builtins.list[SemanticAnchor]:
        if not self.path.exists():
            return []
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
            anchor_set = SemanticAnchorSet.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise SemanticRepositoryError(f"{self.path}: {error}") from error
        anchors = anchor_set.anchors
        if project is not None:
            anchors = [anchor for anchor in anchors if anchor.project == project]
        return sorted(anchors, key=lambda item: item.semantic_id)

    def save(self, anchors: builtins.list[SemanticAnchor]) -> Path:
        identifiers = [anchor.semantic_id for anchor in anchors]
        if len(identifiers) != len(set(identifiers)):
            raise SemanticRepositoryError("semantic_id는 repository에서 고유해야 합니다.")
        payload = SemanticAnchorSet(anchors=sorted(anchors, key=lambda item: item.semantic_id))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            yaml.safe_dump(
                payload.model_dump(mode="json"),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return self.path

    @staticmethod
    def from_memory(
        memory_repository: MarkdownMemoryRepository,
        project: ProjectRef,
    ) -> builtins.list[SemanticAnchor]:
        anchors: dict[str, SemanticAnchor] = {}
        for memory in memory_repository.list(namespace=project.namespace):
            if memory.status is not MemoryStatus.ACTIVE or memory.kind in {
                MemoryKind.SOURCE,
                MemoryKind.MAP,
            }:
                continue
            descriptor = memory.semantic
            if descriptor is None:
                scope = SemanticScope(
                    domain="legacy-memory",
                    target=memory.id,
                    actor=project.project_id,
                )
                claim = SemanticClaim(
                    modality=ClaimModality.IS,
                    action="describe",
                    object=memory.kind.value,
                    outcome="legacy-unstructured-knowledge",
                )
                constraints = ["legacy-unstructured"]
                descriptor = SemanticDescriptor(
                    signature=semantic_signature(scope, claim, constraints),
                    scope=scope,
                    claim=claim,
                    constraints=constraints,
                    canonical_statement=f"{memory.title}. {memory.summary}",
                    aliases=[memory.title, *memory.tags],
                )
                seed = hashlib.sha256(memory.ref.qualified.encode()).hexdigest()[:12].upper()
            else:
                seed = descriptor.signature[:12].upper()
            identifier = f"SEM-{seed}"
            source_refs = [
                MemoryRef(namespace=memory.namespace, local_id=source_id)
                for source_id in memory.source_refs
            ]
            existing = anchors.get(identifier)
            anchor = SemanticAnchor(
                semantic_id=identifier,
                project=project,
                descriptor=descriptor,
                target_refs=sorted(
                    {
                        *(existing.target_refs if existing else []),
                        memory.ref,
                    },
                    key=lambda item: item.qualified,
                ),
                source_refs=sorted(
                    {
                        *(existing.source_refs if existing else []),
                        *source_refs,
                    },
                    key=lambda item: item.qualified,
                ),
                provisional=False,
            )
            anchors[identifier] = anchor
        return sorted(anchors.values(), key=lambda item: item.semantic_id)
