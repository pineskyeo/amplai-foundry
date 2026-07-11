from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.lint.engine import KnowledgeLinter
from amplai_foundry.lint.rules.hygiene import HygieneConfig

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def codes(path: Path, config: HygieneConfig | None = None) -> set[str]:
    report = KnowledgeLinter(config, today=date(2026, 7, 11)).lint(path)
    return {issue.code for issue in report.issues}


def test_sample_vault_passes_all_rules() -> None:
    report = KnowledgeLinter(today=date(2026, 7, 11)).lint(PROJECT_ROOT / "vault")

    assert report.notes_scanned >= 15
    assert report.error_count == 0
    assert report.warning_count == 0


def test_valid_fixture_passes() -> None:
    report = KnowledgeLinter(today=date(2026, 7, 11)).lint(FIXTURES / "valid-vault")

    assert report.error_count == 0
    assert report.warning_count == 0


def test_front_matter_parse_error_is_detected() -> None:
    assert "FRONTMATTER_PARSE" in codes(FIXTURES / "invalid-frontmatter")


def test_missing_required_field_is_detected(tmp_path: Path) -> None:
    note = tmp_path / "10-concepts" / "missing.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        """---
schema_version: 1
id: CON-0905
project: test
kind: concept
status: active
title: Missing namespace
summary: Missing namespace fixture.
created_at: 2026-07-11
updated_at: 2026-07-11
source_refs: []
relations: []
revision: 1
tags: []
---
# Missing namespace
""",
        encoding="utf-8",
    )

    assert "SCHEMA_REQUIRED_FIELD" in codes(tmp_path)


def test_duplicate_id_is_detected() -> None:
    assert "IDENTIFIER_DUPLICATE" in codes(FIXTURES / "duplicate-id")


def test_broken_relation_is_detected() -> None:
    assert "LINK_RELATION_TARGET_MISSING" in codes(FIXTURES / "broken-link")


def test_missing_source_reference_is_detected(tmp_path: Path) -> None:
    source = (FIXTURES / "valid-vault" / "10-concepts" / "concept.md").read_text(encoding="utf-8")
    note = tmp_path / "10-concepts" / "concept.md"
    note.parent.mkdir(parents=True)
    note.write_text(source.replace("SRC-20260711-900", "SRC-20260711-999"), encoding="utf-8")

    assert "LINK_SOURCE_REF_MISSING" in codes(tmp_path)


def test_invalid_lifecycle_is_detected() -> None:
    assert "LIFECYCLE_SUPERSEDED_BY_REQUIRED" in codes(FIXTURES / "invalid-lifecycle")


def test_orphan_warning_is_detected() -> None:
    assert "HYGIENE_ORPHAN" in codes(FIXTURES / "broken-link")


def test_short_body_warning_is_configurable() -> None:
    strict = HygieneConfig(min_body_characters=1_000)

    assert "HYGIENE_BODY_SHORT" in codes(FIXTURES / "valid-vault", strict)


def test_cli_exit_codes() -> None:
    valid = runner.invoke(app, ["lint", str(FIXTURES / "valid-vault")])
    invalid = runner.invoke(app, ["lint", str(FIXTURES / "invalid-frontmatter")])
    fatal = runner.invoke(app, ["lint", str(FIXTURES / "does-not-exist")])

    assert valid.exit_code == 0
    assert invalid.exit_code == 1
    assert fatal.exit_code == 2


def test_stats_reports_inventory() -> None:
    result = runner.invoke(app, ["stats", str(FIXTURES / "valid-vault")])

    assert result.exit_code == 0
    assert "Total notes: 3" in result.stdout
    assert "Open questions: 0" in result.stdout
