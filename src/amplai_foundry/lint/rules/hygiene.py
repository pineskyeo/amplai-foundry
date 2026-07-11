"""Configurable quality warnings for atomic knowledge."""

from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType
from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.lint.result import LintIssue, Severity


@dataclass(frozen=True, slots=True)
class HygieneConfig:
    """Thresholds for subjective but deterministic hygiene rules."""

    min_body_characters: int = 80
    max_body_characters: int = 8_000
    similar_title_ratio: float = 0.9
    stale_question_days: int = 180


def validate_hygiene(
    records: list[tuple[Path, MemoryObject]],
    config: HygieneConfig,
    *,
    today: date,
) -> list[LintIssue]:
    """Evaluate size, connectivity, title, chronology, and question-age warnings."""
    issues: list[LintIssue] = []
    maps = [memory for _, memory in records if memory.kind is MemoryKind.MAP]
    mapped_ids = {relation.target for memory in maps for relation in memory.relations}

    for path, memory in records:
        body_length = len(memory.content.strip())
        if body_length < config.min_body_characters:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_BODY_SHORT",
                    f"본문이 지나치게 짧습니다({body_length}자).",
                    path,
                    memory.id,
                )
            )
        if body_length > config.max_body_characters:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_BODY_LARGE",
                    f"본문이 지나치게 큽니다({body_length}자).",
                    path,
                    memory.id,
                )
            )
        if memory.kind not in {MemoryKind.SOURCE, MemoryKind.MAP} and not memory.relations:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_NO_RELATIONS",
                    "원자 지식에 relation이 하나도 없습니다.",
                    path,
                    memory.id,
                )
            )
        if memory.kind not in {MemoryKind.SOURCE, MemoryKind.MAP} and memory.id not in mapped_ids:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_ORPHAN",
                    "어떤 Map/MOC에서도 참조되지 않습니다.",
                    path,
                    memory.id,
                )
            )
        if memory.kind is not MemoryKind.SOURCE and body_length == 0:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_EMPTY_BODY",
                    "source 노트가 아닌데 본문이 비어 있습니다.",
                    path,
                    memory.id,
                )
            )
        if memory.updated_at < memory.created_at:
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_DATE_ORDER",
                    "updated_at이 created_at보다 이전입니다.",
                    path,
                    memory.id,
                )
            )
        if (
            memory.kind is MemoryKind.QUESTION
            and memory.status is MemoryStatus.ACTIVE
            and (today - memory.updated_at).days > config.stale_question_days
            and not any(
                relation.type in {RelationType.RELATED_TO, RelationType.DEPENDS_ON}
                for relation in memory.relations
            )
        ):
            issues.append(
                LintIssue(
                    Severity.WARNING,
                    "HYGIENE_STALE_QUESTION",
                    "질문이 장기간 active이고 해결 관계가 없습니다.",
                    path,
                    memory.id,
                )
            )

    for index, (left_path, left) in enumerate(records):
        for right_path, right in records[index + 1 :]:
            ratio = SequenceMatcher(None, left.title.casefold(), right.title.casefold()).ratio()
            if ratio >= config.similar_title_ratio:
                issues.append(
                    LintIssue(
                        Severity.WARNING,
                        "HYGIENE_SIMILAR_TITLE",
                        f"{right.id}와 제목이 매우 유사합니다({ratio:.0%}).",
                        left_path,
                        left.id,
                    )
                )
                issues.append(
                    LintIssue(
                        Severity.WARNING,
                        "HYGIENE_SIMILAR_TITLE",
                        f"{left.id}와 제목이 매우 유사합니다({ratio:.0%}).",
                        right_path,
                        right.id,
                    )
                )
    return issues
