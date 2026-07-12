from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from amplai_foundry.cli import app
from amplai_foundry.curation.context_builder import CurateContextBuilder
from amplai_foundry.ingestion.service import SourceIngestionService
from amplai_foundry.schema import check_schemas, write_schemas

runner = CliRunner()


def test_curate_context_bundle_contains_required_sections(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "projects/amplai/00-sources").mkdir(parents=True)
    source = SourceIngestionService(vault).ingest(
        "Memory와 Context의 차이를 정리한다.".encode(),
        project="amplai",
        source_type="chatgpt",
        title="Memory와 Context",
        now=datetime(2026, 7, 12, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    bundle = CurateContextBuilder(vault).build(source.source_id, project="amplai")

    assert "## Source Metadata" in bundle
    assert "## Source Original Content" in bundle
    assert "Memory와 Context의 차이를 정리한다." in bundle
    assert "## Knowledge Rules" in bundle
    assert "## Active Decisions" in bundle
    assert "## Related Search Results" in bundle
    assert "## Open Questions" in bundle
    assert "## Allowed Operations" in bundle
    assert "공식 지식을 직접 수정하지 않는다" in bundle


def test_curate_prepare_cli_writes_bundle(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "projects/amplai/00-sources").mkdir(parents=True)
    source = SourceIngestionService(vault).ingest(
        b"Curate CLI fixture",
        project="amplai",
        source_type="chatgpt",
        title="Curate CLI",
    )
    output = tmp_path / ".amplai/jobs" / f"CURATE-{source.source_id}.md"

    result = runner.invoke(
        app,
        [
            "curate",
            "prepare",
            source.source_id,
            "--project",
            "amplai",
            "--vault",
            str(vault),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert output.exists()
    assert source.source_id in output.read_text(encoding="utf-8")


def test_schema_generate_and_check_use_pydantic_contracts(tmp_path: Path) -> None:
    assert check_schemas(tmp_path)

    paths = write_schemas(tmp_path)

    assert len(paths) == 2
    assert not check_schemas(tmp_path)
    assert '"source_metadata"' in (tmp_path / "schemas/note.schema.json").read_text(
        encoding="utf-8"
    )
    assert '"CONFLICT"' in (tmp_path / "schemas/proposal.schema.json").read_text(encoding="utf-8")


def test_schema_check_cli_passes_committed_schemas() -> None:
    result = runner.invoke(app, ["schema", "check"])

    assert result.exit_code == 0
    assert "OK" in result.stdout
