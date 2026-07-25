"""Generate committed JSON Schemas from Pydantic contract models."""

import json
from pathlib import Path
from typing import Any

from amplai_foundry.domain.models import MemoryMetadata
from amplai_foundry.proposals.models import Proposal


def _schema(
    model: type[MemoryMetadata] | type[Proposal], *, identifier: str, title: str
) -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = identifier
    schema["title"] = title
    return schema


def generated_schemas() -> dict[Path, str]:
    """Return deterministic schema paths and JSON payloads."""
    definitions = {
        Path("schemas/note.schema.json"): _schema(
            MemoryMetadata,
            identifier="https://amplai.local/schemas/note.schema.json",
            title="AMPLAI Memory Object Front Matter",
        ),
        Path("schemas/proposal.schema.json"): _schema(
            Proposal,
            identifier="https://amplai.local/schemas/proposal.schema.json",
            title="AMPLAI Knowledge Proposal",
        ),
    }
    return {
        path: json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for path, schema in definitions.items()
    }


def write_schemas(root: Path = Path(".")) -> list[Path]:
    paths: list[Path] = []
    for relative, payload in generated_schemas().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        paths.append(path)
    return paths


def check_schemas(root: Path = Path(".")) -> list[Path]:
    """Return committed schema files that differ from generated contracts."""
    changed: list[Path] = []
    for relative, expected in generated_schemas().items():
        path = root / relative
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            changed.append(path)
            continue
        if actual != expected:
            changed.append(path)
    return changed
