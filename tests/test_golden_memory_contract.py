import hashlib
import json
from pathlib import Path

import yaml

from amplai_foundry.domain.models import MemoryObject
from amplai_foundry.parsing.markdown import parse_markdown_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_phase_zero_memory_contract_matches_golden_snapshot() -> None:
    golden = yaml.safe_load((FIXTURES / "golden-memory-contract.yaml").read_text(encoding="utf-8"))
    vault = FIXTURES / "valid-vault"
    actual: list[dict[str, str]] = []

    for path in sorted(vault.rglob("*.md")):
        document = parse_markdown_file(path)
        memory = MemoryObject.model_validate({**document.metadata, "content": document.content})
        canonical_model = json.dumps(
            memory.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        actual.append(
            {
                "path": str(path.relative_to(vault)),
                "ref": memory.ref.qualified,
                "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "model_sha256": hashlib.sha256(canonical_model).hexdigest(),
            }
        )

    assert golden["schema_version"] == 1
    assert actual == golden["objects"]
