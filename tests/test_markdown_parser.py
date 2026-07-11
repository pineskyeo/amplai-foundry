from datetime import date

import pytest

from amplai_foundry.parsing.markdown import MarkdownParseError, parse_markdown


def test_parser_separates_front_matter_and_content() -> None:
    parsed = parse_markdown(
        """---
id: CON-0001
created_at: 2026-07-11
---
# Heading

Body.
"""
    )

    assert parsed.metadata["id"] == "CON-0001"
    assert parsed.metadata["created_at"] == date(2026, 7, 11)
    assert parsed.content == "# Heading\n\nBody."
    assert parsed.field_lines["id"] == 2


def test_parser_reports_invalid_yaml_line() -> None:
    with pytest.raises(MarkdownParseError) as caught:
        parse_markdown("---\nrelations: [broken\n---\n# Body")

    assert caught.value.line == 2
    assert "파싱할 수 없습니다" in str(caught.value)


def test_parser_requires_front_matter() -> None:
    with pytest.raises(MarkdownParseError):
        parse_markdown("# Plain Markdown")
