#!/usr/bin/env python3
"""Read-only S17 real-guide reference visibility; not release or view approval."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import amplai_docs as docs  # noqa: E402

PATHS = (
    "docs/PORTABLE-DEVELOPMENT.md",
    "tools/amplai-loop-kit/README.md",
    "tools/amplai-loop-kit/CHANGELOG.md",
)


def main():
    records = [docs.read_document(ROOT, path) for path in PATHS]
    checks = []
    with docs.SourceTree(ROOT) as tree:
        config, _ = docs.configuration(tree)
        for record in records:
            try:
                docs.reference_visibility(
                    tree,
                    config,
                    records,
                    record["path"],
                    record["metadata"]["title"] + "\n" + record["body"],
                    "INTERNAL",
                )
            except docs.DocumentError as error:
                checks.append({"path": record["path"], "error": error.code})
            else:
                checks.append(
                    {
                        "path": record["path"],
                        "status": "PASS",
                        "source_sha256": record["source_sha256"],
                        "snapshot_sha256": docs.object_digest(record["snapshot"]),
                        "freshness": record["freshness"],
                    }
                )
    passed = all(row.get("status") == "PASS" for row in checks)
    print(
        json.dumps(
            {
                "verdict": "PASS_GUIDE_REFERENCE_VISIBILITY" if passed else "FAIL",
                "scope": ("Reference classification only. S08 owns current review/release/HTML."),
                "checks": checks,
            },
            indent=2,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
