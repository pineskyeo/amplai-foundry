"""Legacy retirement candidates and exact deletion proposal (design/19 §3-§4, design/25 §2, V3-060).

Nothing here deletes. Each candidate carries path, current digest, owner, reason,
replacement, live references (static imports, dynamic entry points, tests), backup
reference and required test ids. A candidate with any live reference, no replacement
parity evidence or no backup is ``blocked``; the rest are ``proposed`` and still need
the ``destructive_change`` human gate (T-092). Name-based retirement is refused.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import digest_bytes, now
from amplai_foundry.runtime.errors import RuntimeFault

SCAN_SUFFIXES = (".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".mjs")
SCAN_EXCLUDE = (".git", ".venv", "node_modules", "__pycache__", "design-reference", ".pytest_cache")
# Entry points static grep cannot see (gardening policy: no static reference is not dead).
DYNAMIC_ENTRY_FILES = (
    "pyproject.toml",
    "skills-lock.json",
    ".ai-team/runtime/repository-profile.json",
)
NEVER_RETIRE_PREFIXES = (
    "src/amplai_foundry/governance/",  # design/25 §2: preserve guards until parity is proven
    "vault/",
    ".ai-team/policy/approvals.jsonl",
    "docs/decisions/",
    "docs/workstreams/",
)


class RetirementPlanner:
    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root).resolve()

    # -- reference scan ------------------------------------------------------------
    def _files(self) -> list[Path]:
        out: list[Path] = []
        for path in self.root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(self.root)
            if any(part in SCAN_EXCLUDE for part in rel.parts):
                continue
            if path.suffix in SCAN_SUFFIXES and path.stat().st_size <= 4 * 1024 * 1024:
                out.append(path)
        return out

    @staticmethod
    def _needles(candidate: str) -> list[str]:
        path = Path(candidate)
        stem = path.stem if path.suffix else path.name
        needles = {candidate, path.name, stem}
        if candidate.startswith("src/amplai_foundry/") and path.suffix == ".py":
            module = candidate[len("src/") : -len(".py")].replace("/", ".")
            needles.add(module)
            needles.add(module.rsplit(".", 1)[-1])
        return sorted(n for n in needles if len(n) >= 4)

    def live_refs(self, candidate: str, *, files: list[Path] | None = None) -> dict[str, list[str]]:
        needles = self._needles(candidate)
        pattern = re.compile("|".join(re.escape(n) for n in needles))
        static: list[str] = []
        tests: list[str] = []
        dynamic: list[str] = []
        for path in files or self._files():
            rel = path.relative_to(self.root).as_posix()
            if rel == candidate:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            hits = [i + 1 for i, line in enumerate(text.splitlines()) if pattern.search(line)]
            if not hits:
                continue
            locator = f"{rel}:{hits[0]}" + (f" (+{len(hits) - 1})" if len(hits) > 1 else "")
            if rel in DYNAMIC_ENTRY_FILES:
                dynamic.append(locator)
            elif rel.startswith("tests/"):
                tests.append(locator)
            else:
                static.append(locator)
        return {"static": static, "tests": tests, "dynamic_entry": dynamic}

    # -- candidates ------------------------------------------------------------------
    def candidate(
        self,
        path: str,
        *,
        owner: str,
        reason: str,
        replacement: str | None,
        parity_evidence: list[str],
        backup_ref: str | None,
        required_test_ids: list[str],
        files: list[Path] | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"T-\d{3}", required_test_ids[0] if required_test_ids else ""):
            raise RuntimeFault(
                "RETIREMENT_TESTS", "Each candidate names at least one T-xxx test id"
            )
        if "*" in path or "?" in path:
            raise RuntimeFault(
                "RETIREMENT_GLOB", "A glob is not a deletion candidate; name exact paths"
            )
        target = self.root / path
        exists = target.is_file() or target.is_dir()
        old_digest = digest_bytes(target.read_bytes()) if target.is_file() else None
        refs = self.live_refs(path, files=files)
        blockers: list[str] = []
        if any(path.startswith(p) for p in NEVER_RETIRE_PREFIXES):
            blockers.append("protected_prefix")
        if not exists:
            blockers.append("path_missing")
        if refs["static"]:
            blockers.append("live_static_refs")
        if refs["dynamic_entry"]:
            blockers.append("dynamic_entry_refs")
        if refs["tests"] and not parity_evidence:
            blockers.append("tests_reference_without_parity")
        if replacement is None:
            blockers.append("no_replacement")
        if not parity_evidence:
            blockers.append("no_parity_evidence")
        if backup_ref is None:
            blockers.append("no_backup")
        return {
            "path": path,
            "old_digest": old_digest,
            "owner": owner,
            "reason": reason,
            "replacement": replacement,
            "parity_evidence": parity_evidence,
            "live_refs": refs,
            "backup_ref": backup_ref,
            "required_test_ids": required_test_ids,
            "blockers": blockers,
            "status": "blocked" if blockers else "proposed",
            "approval_required": True,
            "automatic_delete": False,
        }

    def plan(
        self, candidates: list[dict[str, Any]], *, component_map: str | Path | None = None
    ) -> dict[str, Any]:
        dispositions = {}
        if component_map:
            with open(self.root / component_map, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    dispositions[row["source_path_or_prefix"]] = row
        items = []
        for c in candidates:
            design = next((v for k, v in dispositions.items() if c["path"].startswith(k)), None)
            items.append(
                {
                    **c,
                    "design_disposition": design["action"] if design else None,
                    "design_delete_authorized": (design or {}).get("delete_authorized", "False")
                    == "True",
                }
            )
        return {
            "schema_version": "3.0.0",
            "kind": "legacy_retirement_proposal",
            "generated_at": now(),
            "candidates": items,
            "counts": {
                "proposed": sum(1 for i in items if i["status"] == "proposed"),
                "blocked": sum(1 for i in items if i["status"] == "blocked"),
            },
            "human_gate": "destructive_change",
            "deletes_executed": 0,
            "note": "Design dispositions are recommendations, not a delete list (design/25 §4).",
        }

    def write(self, plan: dict[str, Any], destination: str | Path) -> Path:
        target = self.root / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        body = {k: v for k, v in plan.items() if k != "generated_at"}
        target.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        return target
