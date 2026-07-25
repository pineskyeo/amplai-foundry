from datetime import date

import pytest
from pydantic import ValidationError

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.domain.lifecycle import validate_lifecycle
from amplai_foundry.domain.models import MemoryObject


def memory_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": 1,
        "id": "DEC-0001",
        "namespace": "org/default/project/amplai",
        "project": "amplai",
        "kind": "decision",
        "status": "active",
        "title": "Test decision",
        "summary": "A complete storage-independent memory object.",
        "created_at": date(2026, 7, 11),
        "updated_at": date(2026, 7, 11),
        "source_refs": ["SRC-20260711-86C363BF"],
        "relations": [{"type": "related_to", "target": "CON-0001"}],
        "revision": 1,
        "tags": ["test"],
        "content": "# Test\n\n## 결정\n\nUse the tested contract.",
    }
    data.update(overrides)
    return data


def test_memory_object_validates_domain_types() -> None:
    memory = MemoryObject.model_validate(memory_data())

    assert memory.kind is MemoryKind.DECISION
    assert memory.status is MemoryStatus.ACTIVE
    assert memory.revision == 1


def test_revision_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        MemoryObject.model_validate(memory_data(revision=0))


def test_superseded_requires_target() -> None:
    memory = MemoryObject.model_validate(memory_data(status="superseded"))

    codes = {violation.code for violation in validate_lifecycle(memory)}

    assert "LIFECYCLE_SUPERSEDED_BY_REQUIRED" in codes
