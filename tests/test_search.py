from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.search.lexical import LexicalKnowledgeSearch

runner = CliRunner()


class StubRepository:
    def __init__(self, memories: list[MemoryObject]) -> None:
        self.memories = memories

    def list(self, namespace: str | None = None) -> list[MemoryObject]:
        return [
            memory for memory in self.memories if namespace is None or memory.namespace == namespace
        ]

    def get(self, memory_id: str) -> MemoryObject | None:
        return next((memory for memory in self.memories if memory.id == memory_id), None)

    def find_by_kind(self, kind):
        return [memory for memory in self.memories if memory.kind is kind]

    def find_referencing(self, target_id: str) -> list[MemoryObject]:
        return [memory for memory in self.memories if target_id in memory.referenced_ids()]

    def exists(self, memory_id: str) -> bool:
        return self.get(memory_id) is not None


def memory(
    identifier: str,
    title: str,
    *,
    summary: str = "unrelated summary",
    tags: list[str] | None = None,
    content: str = "unrelated body",
    project: str = "amplai",
    status: str = "active",
    relations: list[dict[str, str]] | None = None,
) -> MemoryObject:
    return MemoryObject.model_validate(
        {
            "schema_version": 1,
            "id": identifier,
            "namespace": f"org/default/project/{project}",
            "project": project,
            "kind": "concept",
            "status": status,
            "title": title,
            "summary": summary,
            "created_at": date(2026, 7, 12),
            "updated_at": date(2026, 7, 12),
            "source_refs": [],
            "relations": relations or [],
            "revision": 1,
            "tags": tags or [],
            "content": content,
        }
    )


def test_field_weights_and_stable_order() -> None:
    memories = [
        memory("CON-0004", "Other", content="memory body"),
        memory("CON-0003", "Other", summary="memory summary"),
        memory("CON-0002", "Other", tags=["memory"]),
        memory("CON-0001", "Memory"),
    ]
    search = LexicalKnowledgeSearch(StubRepository(memories), lambda item: f"/{item}.md")

    results = search.search("memory")

    assert [result.id for result in results] == ["CON-0001", "CON-0002", "CON-0003", "CON-0004"]
    assert all(results[index].score >= results[index + 1].score for index in range(3))
    assert results[0].path == "/CON-0001.md"


def test_id_exact_match_wins() -> None:
    memories = [memory("CON-0001", "CON-0002"), memory("CON-0002", "Other")]

    results = LexicalKnowledgeSearch(StubRepository(memories)).search("CON-0002")

    assert results[0].id == "CON-0002"


def test_project_status_filters_and_default_exclusion() -> None:
    memories = [
        memory("CON-0001", "Memory", project="amplai"),
        memory("CON-0002", "Memory", project="other"),
        memory("CON-0003", "Memory", status="superseded"),
        memory("CON-0004", "Memory", status="archived"),
    ]
    search = LexicalKnowledgeSearch(StubRepository(memories))

    assert [item.id for item in search.search("Memory", project="amplai")] == ["CON-0001"]
    assert {item.id for item in search.search("Memory", status=None)} == {
        "CON-0001",
        "CON-0002",
        "CON-0003",
        "CON-0004",
    }


def test_relation_target_is_searchable() -> None:
    memories = [
        memory(
            "CON-0001",
            "Other",
            relations=[{"type": "related_to", "target": "QUE-0042"}],
        )
    ]

    results = LexicalKnowledgeSearch(StubRepository(memories)).search("QUE-0042")

    assert [result.id for result in results] == ["CON-0001"]


def test_cli_search_returns_expected_note() -> None:
    vault = Path(__file__).resolve().parents[1] / "vault"

    result = runner.invoke(
        app, ["search", "Memory와 Context", "--project", "amplai", "--vault", str(vault)]
    )

    assert result.exit_code == 0
    assert "CON-0001" in result.stdout
