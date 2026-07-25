"""Deterministic in-memory lexical search."""

import re
from collections.abc import Callable

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.repositories.base import MemoryRepository
from amplai_foundry.search.base import SearchResult


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[\w-]+", text, flags=re.UNICODE)}


class LexicalKnowledgeSearch:
    """Rank repository objects with stable field-weighted token matching."""

    def __init__(
        self,
        repository: MemoryRepository,
        path_for: Callable[[str], str] | None = None,
    ) -> None:
        self.repository = repository
        self.path_for = path_for or (lambda _identifier: "")

    def search(
        self,
        query: str,
        *,
        project: str | None = None,
        status: str | None = "active",
        limit: int = 10,
    ) -> list[SearchResult]:
        normalized = query.strip().casefold()
        if not normalized or limit < 1:
            return []
        query_tokens = _tokens(normalized)
        ranked: list[tuple[int, MemoryObject]] = []
        for memory in self.repository.list():
            if project is not None and memory.project != project:
                continue
            if status is not None and memory.status.value != status:
                continue
            score = self._score(memory, normalized, query_tokens)
            if score:
                ranked.append((score, memory))
        ranked.sort(key=lambda item: (-item[0], item[1].id))
        return [
            SearchResult(
                score=score,
                id=memory.id,
                kind=memory.kind.value,
                status=memory.status.value,
                title=memory.title,
                summary=memory.summary,
                path=self.path_for(memory.id),
            )
            for score, memory in ranked[:limit]
        ]

    @staticmethod
    def _score(memory: MemoryObject, query: str, query_tokens: set[str]) -> int:
        identifier = memory.id.casefold()
        title = memory.title.casefold()
        summary = memory.summary.casefold()
        tags = {tag.casefold() for tag in memory.tags}
        body = memory.content.casefold()
        relation_targets = {relation.target.casefold() for relation in memory.relations}

        score = 0
        if identifier == query:
            score += 1_000
        if title == query:
            score += 500
        elif query in title:
            score += 200
        score += 80 * len(query_tokens & _tokens(title))
        score += 60 * len(query_tokens & tags)
        if query in summary:
            score += 40
        score += 20 * len(query_tokens & _tokens(summary))
        score += 5 * len(query_tokens & _tokens(body))
        score += 30 * len(query_tokens & relation_targets)
        return score
