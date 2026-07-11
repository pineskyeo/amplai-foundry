"""YAML Front Matter and Markdown body parsing."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class MarkdownParseError(ValueError):
    """A recoverable error in a single Markdown document."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


@dataclass(frozen=True, slots=True)
class ParsedMarkdown:
    """Unvalidated front matter plus the Markdown body."""

    metadata: dict[str, Any]
    content: str
    field_lines: dict[str, int]


def parse_markdown(text: str) -> ParsedMarkdown:
    """Parse one Markdown string with mandatory YAML Front Matter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise MarkdownParseError(
            "파일이 YAML Front Matter 구분자 '---'로 시작하지 않습니다.", line=1
        )

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing_index = index
            break
    if closing_index is None:
        raise MarkdownParseError("YAML Front Matter 종료 구분자 '---'가 없습니다.", line=1)

    yaml_text = "\n".join(lines[1:closing_index])
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        error_line = int(mark.line) + 2 if mark is not None else 1
        raise MarkdownParseError(
            f"YAML Front Matter를 파싱할 수 없습니다: {error}", line=error_line
        ) from error
    if not isinstance(loaded, dict):
        raise MarkdownParseError("YAML Front Matter는 key-value object여야 합니다.", line=2)

    field_lines: dict[str, int] = {}
    for index, line in enumerate(lines[1:closing_index], start=2):
        if line and not line[0].isspace() and ":" in line:
            field_lines[line.split(":", 1)[0].strip()] = index
    content = "\n".join(lines[closing_index + 1 :]).strip()
    return ParsedMarkdown(metadata=loaded, content=content, field_lines=field_lines)


def parse_markdown_file(path: Path) -> ParsedMarkdown:
    """Read and parse a UTF-8 Markdown file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise MarkdownParseError(f"파일을 UTF-8로 읽을 수 없습니다: {error}") from error
    return parse_markdown(text)
