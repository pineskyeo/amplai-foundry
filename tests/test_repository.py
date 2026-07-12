from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.repositories.base import MemoryRepository
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository

FIXTURES = Path(__file__).parent / "fixtures"


def accepts_repository_port(repository: MemoryRepository) -> MemoryRepository:
    return repository


def test_markdown_repository_implements_read_contract() -> None:
    repository = accepts_repository_port(MarkdownMemoryRepository(FIXTURES / "valid-vault"))

    concept = repository.get("CON-0900")

    assert concept is not None
    assert concept.kind is MemoryKind.CONCEPT
    assert len(repository.list(namespace="org/default/project/test")) == 3
    assert [item.id for item in repository.find_by_kind(MemoryKind.MAP)] == ["MAP-0900"]
    assert [item.id for item in repository.find_referencing("CON-0900")] == ["MAP-0900"]
    assert repository.exists("SRC-20260711-6635D803")
    assert not repository.exists("CON-9999")
