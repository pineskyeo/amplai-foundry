"""Final V3 conformance and requirement evidence closure (design/21, design/22, V3-061).

The ledger ties every REQ / task / test-catalog id to *executed* evidence: a junit
report produced on this tree, a git revision, and the test case names that mention the
catalog id. A design-package check, a test that merely exists, or a mention in prose is
never counted as a runtime pass (design/21 §1, §4). Remaining blockers stay explicit.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import now

CATALOG_ID = re.compile(r"\bT-\d{3}\b")


def executed_cases(junit_paths: list[Path]) -> dict[str, dict[str, Any]]:
    """test id (classname::name) → outcome from real junit files."""
    out: dict[str, dict[str, Any]] = {}
    for path in junit_paths:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            key = f"{case.get('classname')}::{case.get('name')}"
            outcome = "pass"
            if case.find("failure") is not None or case.find("error") is not None:
                outcome = "fail"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            out[key] = {"outcome": outcome, "file": path.name, "time": case.get("time")}
    return out


def catalog_mentions(test_roots: list[Path]) -> dict[str, list[tuple[str, str]]]:
    """catalog id → [(module classname, test function)].

    Only ids carried by a test function count: written inside its body, or encoded in
    its name (``test_t003_...`` names T-003). A file docstring that lists ids is prose
    and attributes nothing (design/21 §4: a mention is not evidence).
    """
    mentions: dict[str, list[tuple[str, str]]] = {}
    for root in test_roots:
        for path in sorted(root.rglob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            parts = path.with_suffix("").parts
            depth = 3 if "e2e" in parts else 2
            module = ".".join(parts[-depth:])
            for match in re.finditer(r"\ndef (test_\w+)\(.*?(?=\ndef |\Z)", text, re.S):
                func = match.group(1)
                ids = set(CATALOG_ID.findall(match.group(0)))
                ids.update(f"T-{num}" for num in re.findall(r"(?:^|_)t(\d{3})(?=_)", func))
                for tid in ids:
                    mentions.setdefault(tid, []).append((module, func))
    return mentions


class ClosureLedger:
    def __init__(self, repo_root: str | Path, design_dir: str = "design-reference") -> None:
        self.root = Path(repo_root).resolve()
        self.design = self.root / design_dir
        self.catalog = {
            t["id"]: t
            for t in json.loads((self.design / "eval" / "test-catalog.json").read_bytes())["tests"]
        }
        self.requirements = json.loads(
            (self.design / "implementation" / "requirements-traceability.json").read_bytes()
        )["requirements"]
        import yaml

        self.tasks = yaml.safe_load((self.design / "implementation" / "tasks.yaml").read_text())[
            "tasks"
        ]

    def _revision(self) -> str:
        try:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def build(self, junit_paths: list[Path], *, external_pending: list[str]) -> dict[str, Any]:
        executed = executed_cases(junit_paths)
        mentions = catalog_mentions(
            [
                self.root / "tests" / "v3",
                self.root / "tests" / "e2e",
                self.root / "tests" / "runtime_storage",
            ]
        )
        tests: dict[str, dict[str, Any]] = {}
        for tid, spec in self.catalog.items():
            tied = []
            for _module, func in mentions.get(tid, []):
                matched = [
                    k
                    for k in executed
                    if k.endswith("::" + func)
                    or k.endswith("::" + func.split("[")[0])
                    or (func + "[") in k
                ]
                for key in matched:
                    tied.append({"case": key, **executed[key]})
            outcomes = {t["outcome"] for t in tied}
            if not tied:
                status = "not_run"
            elif "fail" in outcomes:
                status = "fail"
            elif outcomes <= {"skipped"}:
                status = "not_run"
            else:
                status = "local_pass"
            tests[tid] = {
                "name": spec["name"],
                "status": status,
                "executed_cases": sorted({t["case"] for t in tied}),
                "qualification": (
                    "local automated evidence; not live provider/container/holdout qualification"
                ),
            }
        reqs = []
        for r in self.requirements:
            ids = r["test_ids"]
            statuses = [tests[t]["status"] for t in ids]
            local = sum(1 for s in statuses if s == "local_pass")
            reqs.append(
                {
                    "id": r["id"],
                    "requirement": r["requirement"],
                    "tests_total": len(ids),
                    "tests_local_pass": local,
                    "tests_not_run": sum(1 for s in statuses if s == "not_run"),
                    "tests_fail": sum(1 for s in statuses if s == "fail"),
                    "status": "fully_verified"
                    if local == len(ids) and local > 0
                    else ("partial" if local else "not_run"),
                    "design_status": r["status"],
                    "note": (
                        "design check is not a runtime pass; fully_verified requires every "
                        "listed test to have local executed evidence"
                    ),
                }
            )
        tasks = []
        for t in self.tasks:
            ids = t["test_ids"]
            statuses = [tests[i]["status"] for i in ids]
            local = sum(1 for s in statuses if s == "local_pass")
            tasks.append(
                {
                    "id": t["id"],
                    "title": t["title"],
                    "wave": t["wave"],
                    "tests_total": len(ids),
                    "tests_local_pass": local,
                    "status": "fully_accepted"
                    if local == len(ids) and local > 0
                    else ("partial" if local else "not_started"),
                }
            )
        summary = {
            "tests": {
                "total": len(tests),
                "local_pass": sum(1 for v in tests.values() if v["status"] == "local_pass"),
                "not_run": sum(1 for v in tests.values() if v["status"] == "not_run"),
                "fail": sum(1 for v in tests.values() if v["status"] == "fail"),
            },
            "requirements": {
                "total": len(reqs),
                "fully_verified": sum(1 for r in reqs if r["status"] == "fully_verified"),
                "partial": sum(1 for r in reqs if r["status"] == "partial"),
                "not_run": sum(1 for r in reqs if r["status"] == "not_run"),
            },
            "tasks": {
                "total": len(tasks),
                "fully_accepted": sum(1 for x in tasks if x["status"] == "fully_accepted"),
                "partial": sum(1 for x in tasks if x["status"] == "partial"),
                "not_started": sum(1 for x in tasks if x["status"] == "not_started"),
            },
        }
        return {
            "schema_version": "3.0.0",
            "kind": "v3_conformance_closure",
            "implementation_revision": self._revision(),
            "junit_reports": [p.name for p in junit_paths],
            "executed_case_count": len(executed),
            "generated_at": now(),
            "summary": summary,
            "tests": tests,
            "requirements": reqs,
            "tasks": tasks,
            "remaining_blockers": {
                "external_qualification_pending": external_pending,
                "tests_not_run": sorted(t for t, v in tests.items() if v["status"] == "not_run"),
                "note": (
                    "not_run means no executed local evidence names this catalog id; "
                    "a mention in prose or a design fixture does not count"
                ),
            },
            "release_status": "rc_candidate_not_final",
        }

    @staticmethod
    def write(ledger: dict[str, Any], destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        body = {k: v for k, v in ledger.items() if k != "generated_at"}
        destination.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        return destination
