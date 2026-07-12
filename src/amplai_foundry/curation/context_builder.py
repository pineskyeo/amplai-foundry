"""Build a self-contained Codex curation context bundle."""

from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus
from amplai_foundry.ingestion.service import SourceIngestionService, extract_original_content
from amplai_foundry.parsing.markdown import parse_markdown_file
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository
from amplai_foundry.search.lexical import LexicalKnowledgeSearch

RULE_FILES = (
    ("Knowledge Rules", Path("docs/KNOWLEDGE-RULES.md")),
    ("Lifecycle Rules", Path("docs/LIFECYCLE.md")),
    ("Relationship Rules", Path("docs/RELATIONSHIPS.md")),
)


class ContextBuilderError(RuntimeError):
    """A curation context bundle cannot be built."""


class CurateContextBuilder:
    def __init__(self, vault: Path = Path("vault"), repository_root: Path = Path(".")) -> None:
        self.vault = vault
        self.repository_root = repository_root

    def build(self, source_id: str, *, project: str) -> str:
        service = SourceIngestionService(self.vault)
        found = service.find(source_id)
        if found is None:
            raise ContextBuilderError(f"Source ID가 존재하지 않습니다: {source_id}")
        source_path, _metadata = found
        document = parse_markdown_file(source_path)
        if document.metadata.get("project") != project:
            raise ContextBuilderError("Source project와 요청 project가 다릅니다.")
        original = extract_original_content(source_path).decode("utf-8")

        repository = MarkdownMemoryRepository(self.vault)
        memories = repository.list()
        source_title = str(document.metadata["title"])
        retrieval_query = f"{source_title} {original[:2_000]}"
        results = LexicalKnowledgeSearch(repository, repository.path_for).search(
            retrieval_query, project=project, status="active", limit=10
        )
        decisions = sorted(
            (
                memory
                for memory in memories
                if memory.project == project
                and memory.kind is MemoryKind.DECISION
                and memory.status is MemoryStatus.ACTIVE
            ),
            key=lambda memory: memory.id,
        )
        questions = sorted(
            (
                memory
                for memory in memories
                if memory.project == project
                and memory.kind is MemoryKind.QUESTION
                and memory.status is MemoryStatus.ACTIVE
            ),
            key=lambda memory: memory.id,
        )

        sections = [
            "# Codex Curate Context Bundle",
            "",
            "## Source Metadata",
            "",
            "```yaml",
            source_path.read_text(encoding="utf-8").split("---", 2)[1].strip(),
            "```",
            "",
            "## Source Original Content",
            "",
            original,
        ]
        for heading, relative in RULE_FILES:
            path = self.repository_root / relative
            content = path.read_text(encoding="utf-8") if path.exists() else "미확인"
            sections.extend(["", f"## {heading}", "", content])
        sections.extend(["", "## Active Decisions", ""])
        sections.extend(
            [f"- `{item.id}` {item.title}: {item.summary}" for item in decisions] or ["- 없음"]
        )
        sections.extend(["", "## Related Search Results", ""])
        sections.extend(
            [
                (
                    f"- score={result.score} `{result.id}` [{result.kind}] "
                    f"{result.title} — {result.summary}"
                )
                for result in results
                if result.id != source_id
            ]
            or ["- 없음"]
        )
        sections.extend(["", "## Open Questions", ""])
        sections.extend(
            [f"- `{item.id}` {item.title}: {item.summary}" for item in questions] or ["- 없음"]
        )
        sections.extend(
            [
                "",
                "## Proposal Schema Summary",
                "",
                (
                    "Proposal은 `proposal_version=1`, project, namespace, "
                    "source_ids, operations를 가진다."
                ),
                "각 operation은 evidence line range와 confidence를 가진다.",
                "",
                "## Allowed Operations",
                "",
                "`CREATE`, `UPDATE`, `LINK`, `MERGE`, `SPLIT`, `SUPERSEDE`, `CONFLICT`, `IGNORE`",
                "",
                "## Codex Procedure",
                "",
                "1. Source ID와 원문을 확인한다.",
                "2. 기존 active knowledge를 먼저 검색한다.",
                "3. 원자 지식 후보를 추출하고 허용 operation으로 분류한다.",
                "4. 공식 Vault를 직접 수정하지 않는다.",
                "5. `.amplai/proposals/` 아래 Proposal과 draft만 생성한다.",
                "6. Proposal validate, diff, Vault lint를 실행한다.",
                "7. 중복, 충돌, 미해결 질문을 사용자에게 보고한다.",
                "8. 명시적 승인 요청이 있을 때만 approve/apply를 실행한다.",
                "",
                "## Safety Rule",
                "",
                "공식 지식을 직접 수정하지 않는다. 충돌을 임의로 해결하지 않는다.",
                "",
            ]
        )
        return "\n".join(sections)

    def write(self, source_id: str, *, project: str, output: Path) -> Path:
        content = self.build(source_id, project=project)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
        return output
