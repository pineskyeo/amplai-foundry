#!/usr/bin/env python
"""Rebuild release/rc01-conformance-closure.json and eval/test-catalog-status.json.

Run after
`pytest tests/v3 tests/runtime_storage --junitxml=specs/013-amplai-v3/junit/rc01-v3-e2e.xml`.
The ledger only ties catalog ids to cases that junit file actually executed (design/21 §4).
"""

from __future__ import annotations

import json
from pathlib import Path

from amplai_foundry.distribution.closure import ClosureLedger

REPO = Path(__file__).resolve().parents[1]
JUNIT = REPO / "specs" / "013-amplai-v3" / "junit" / "rc01-v3-e2e.xml"
LEDGER = REPO / "release" / "rc01-conformance-closure.json"
STATUS = REPO / "eval" / "test-catalog-status.json"


def main() -> None:
    evidence = json.loads(
        (REPO / "specs" / "013-amplai-v3" / "dev03-delivery-evidence.json").read_text()
    )
    status_path = (
        REPO / "specs" / "015-external-qualification" / "external-qualification-status.json"
    )
    if status_path.is_file():
        # Work 015 measured each item on this host; only unresolved ones stay pending.
        status = json.loads(status_path.read_text())
        # R701: the status file may refine the DEV-03 list, never shrink or rename it.
        original = set(evidence["external_qualification_pending"])
        listed = {x["item"] for x in status["items"]}
        if listed != original:
            raise SystemExit(
                "external-qualification-status.json must account for exactly the "
                f"{len(original)} DEV-03 items; missing={sorted(original - listed)} "
                f"extra={sorted(listed - original)}"
            )
        pending = [
            f"{x['item']} [{x['status']}: {x['note']}]"
            for x in status["items"]
            if x["status"] != "resolved"
        ]
    else:
        pending = evidence["external_qualification_pending"]
    closure = ClosureLedger(REPO)
    ledger = closure.build(
        [JUNIT], external_pending=pending, final_declaration=closure.final_declaration()
    )
    ClosureLedger.write(ledger, LEDGER)
    view = {
        "schema_version": ledger["schema_version"],
        "source_catalog": "design-reference/eval/test-catalog.json",
        "implementation_revision": ledger["implementation_revision"],
        "junit_reports": ledger["junit_reports"],
        "status_values": {
            "local_pass": "executed locally on this tree; not live qualification",
            "not_run": "no executed evidence names this id",
            "fail": "executed and failed",
        },
        "tests": {
            tid: {"status": v["status"], "cases": len(v["executed_cases"])}
            for tid, v in ledger["tests"].items()
        },
    }
    STATUS.write_text(json.dumps(view, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(ledger["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
