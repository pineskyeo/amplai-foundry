"""Storage-independent knowledge search contract."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchResult:
    score: int
    id: str
    kind: str
    status: str
    title: str
    summary: str
    path: str


class KnowledgeSearch(Protocol):
    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        status: str | None = "active",
        limit: int = 10,
    ) -> list[SearchResult]: ...
