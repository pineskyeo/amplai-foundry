#!/usr/bin/env python3
"""Read-only current Kit selection and actual generated/stored Context delivery."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import amplai_docs as docs  # noqa: E402
import loopv2  # noqa: E402

FEATURE = ROOT / "specs/012-portable-document-lifecycle"
RELEASE = "kit-2.5.0-candidate-r6"
GUIDES = {
    "docs/PORTABLE-DEVELOPMENT.md",
    "tools/amplai-loop-kit/README.md",
    "tools/amplai-loop-kit/CHANGELOG.md",
}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def verify_context_delivery(root, feature, expected_guides, release):
    """Inspect real consumers, not a selector stub or a self-rehashed stored pack."""
    try:
        root = Path(root).resolve()
        feature = Path(feature).resolve()
        context_file = feature / "context-pack.json"
        context_relative = str(context_file.relative_to(root))
        contract = json.loads((feature / "work-contract.json").read_text())
        selection = docs.context_selection(root, contract)
        require(selection["release_id"] == release, "Current guide release differs")
        require(
            {row["path"] for row in selection["documents"]} == set(expected_guides),
            "Current owning guide set is incomplete or changed",
        )
        require(
            all(row["freshness"] == "verified" for row in selection["documents"]),
            "Current owning guide review is not verified",
        )
        with docs.SourceTree(root) as tree:
            config, _ = docs.configuration(tree)
        instructions = {
            row["path"] for row in config["governing_inputs"] if row["role"] == "instruction"
        }
        require(bool(instructions), "Required governing instructions are absent")
        expected_paths = set(expected_guides) | instructions
        generated, _ = loopv2.context_records(root, contract)
        stored = json.loads(context_file.read_text())
        for records in (generated, stored["required_knowledge"]):
            require(
                {row["path"] for row in records} == expected_paths,
                "Actual Context guide/instruction delivery is incomplete or changed",
            )
            for row in records:
                require(
                    row["source_sha256"] == docs.file_sha(docs.safe_path(root, row["path"])),
                    "Actual Context source identity differs",
                )
        validation = loopv2.context_validate(root, context_relative)
        require(
            validation["valid"] and validation["verdict"] == "MATCH",
            "Actual Context validation did not match",
        )
        require(
            validation["expected_hash"] == stored["content_hash"],
            "Stored Context changed during verification",
        )
        require(
            docs.context_selection(root, contract) == selection,
            "Guide selection changed during verification",
        )
        return {
            "guides": sorted(expected_guides),
            "required_instructions": sorted(instructions),
            "unique_paths": sorted(expected_paths),
            "generated_records": len(generated),
            "stored_records": len(stored["required_knowledge"]),
            "source_hashes": "EXACT_CURRENT_BYTES",
            "context_validation": validation["verdict"],
            "context_hash": stored["content_hash"],
        }
    except (docs.DocumentError, OSError, ValueError, TypeError, KeyError):
        # Rejected input values, file identities and private titles are not diagnostics.
        raise AssertionError("Context delivery could not be verified") from None


def main():
    delivery = verify_context_delivery(ROOT, FEATURE, GUIDES, RELEASE)
    contract = json.loads((FEATURE / "work-contract.json").read_text())
    selection = docs.context_selection(ROOT, contract)
    assert selection["release_id"] == RELEASE
    assert {row["path"] for row in selection["documents"]} == GUIDES
    assert all(row["freshness"] == "verified" for row in selection["documents"])
    with docs.SourceTree(ROOT) as tree:
        config, _ = docs.configuration(tree)
    instructions = {
        row["path"] for row in config["governing_inputs"] if row["role"] == "instruction"
    }
    require(len(instructions) == 7, "The owning repository instruction set differs")
    governing = {row["path"] for row in selection["governing_inputs"]}
    assert instructions <= governing
    for path in ("AGENTS.md", "CLAUDE.md", ".ai-team/README.md"):
        assert path in governing
        assert RELEASE not in docs.read_document(ROOT, path)["metadata"]["release_ids"]
    # Retaining old product membership is not blanket approval of its raw targets.
    try:
        docs.select_documents(ROOT, "kit-2.5.0-candidate-r4", history=True, consumer="loop-context")
    except docs.DocumentError as error:
        assert error.code == "CROSS_BOUNDARY_REFERENCE"
    else:
        raise AssertionError("Unclassified retained product references must remain blocked")
    print(
        json.dumps(
            {
                "verdict": "PASS_KIT_GUIDE_SELECTION",
                "release_id": RELEASE,
                "guides": sorted(GUIDES),
                "required_instructions": sorted(instructions),
                "context_delivery": delivery,
                "retained_product_reference_guard": "BLOCKED_CROSS_BOUNDARY_REFERENCE",
                "scope": (
                    "Actual current Kit selection and generated/stored Context delivery; "
                    "not HTML or deployment approval."
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
