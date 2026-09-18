"""Read-only knowledge resolution, progressive context and governed write routing."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.contracts.semantics import (
    READINESS_AREAS,
    check_context,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store


class KnowledgeService:
    def __init__(self, store: Store, contracts, *, governed_submit: Callable | None = None):
        self.store, self.contracts, self.governed_submit = store, contracts, governed_submit

    def record_observation(
        self,
        scope: Scope,
        locator: str,
        text: str,
        *,
        kind: str = "repo_fact",
        trust: str = "observed",
        classification: str = "internal",
    ) -> dict:
        if trust not in {"observed", "untrusted", "legacy_imported"}:
            raise RuntimeFault(
                "KNOWLEDGE_AUTHORITY", "Observation ingestion cannot mint canonical authority"
            )
        content = {
            "locator": locator,
            "text": text,
            "kind": kind,
            "trust": trust,
            "classification": classification,
        }
        record = {
            "observation_id": "obs-" + digest(content)[7:31],
            "scope": scope.wire(),
            **content,
        }
        with self.store.tx() as db:
            return self.store.put(
                db, scope, "knowledge-observation", record["observation_id"], 1, record
            )

    def import_vault_readonly(self, scope: Scope, root: Path) -> list[dict]:
        # Canonical writes stay in the existing governed Foundry path; imported files
        # remain observations unless checked against an actual applied decision.
        root = root.resolve()
        results = []
        for path in sorted(root.rglob("*.md")):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            if path.stat().st_size > 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
            results.append(
                self.record_observation(
                    scope, str(path.relative_to(root)), text, trust="legacy_imported"
                )
            )
        return results

    def propose_canonical_change(self, actor, source_refs: list[dict], patch: dict):
        actor.require("knowledge.propose")
        if self.governed_submit is None:
            raise Hold(
                "GOVERNANCE_REQUIRED",
                "Canonical changes require the existing Source/Proposal/Decision/Apply service",
            )
        return self.governed_submit(actor, source_refs, patch)

    def readiness(
        self,
        scope: Scope,
        evidence_by_area: dict[str, list[dict]],
        *,
        conflicts: dict | None = None,
    ) -> list[dict]:
        conflicts = conflicts or {}
        entries = []
        from amplai_foundry.runtime.contracts.semantics import resolve_ref

        for area in sorted(READINESS_AREAS):
            refs = evidence_by_area.get(area, [])
            for ref in refs:
                resolve_ref(self.store, scope, ref)
            entries.append(
                {
                    "area": area,
                    "status": "conflicting"
                    if area in conflicts
                    else "ready"
                    if refs
                    else "missing",
                    "source_refs": refs,
                    "reason": str(
                        conflicts.get(area)
                        or (
                            "Current source-backed evidence"
                            if refs
                            else "No current evidence supplied"
                        )
                    ),
                }
            )
        return entries

    def bundle(
        self,
        scope: Scope,
        *,
        bundle_id: str,
        core_refs: list[dict],
        entries: list[dict],
        invariant_registry_ref: dict,
        token_budget: int,
        assembled_at: str,
    ) -> tuple[dict, dict]:
        from amplai_foundry.runtime.contracts.semantics import resolve_ref

        for ref in core_refs:
            resolve_ref(self.store, scope, ref)
        # Conservative bound, not an alleged model tokenizer: preserve every core ref.
        mandatory = [e for e in entries if e["mandatory"]]
        optional = [e for e in entries if not e["mandatory"]]
        used = sum(len(json.dumps(e, ensure_ascii=False).encode("utf-8")) for e in mandatory) + len(
            json.dumps(core_refs).encode()
        )
        if used > token_budget:
            raise Hold(
                "CORE_CONTEXT_BUDGET",
                "Core context cannot fit; increase budget or approved summarization, never silently truncate",
            )
        selected = list(mandatory)
        for entry in optional:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
            if used + size <= token_budget:
                selected.append(entry)
                used += size
        value = {
            "schema_version": "3.0.0",
            "bundle_id": bundle_id,
            "scope": scope.wire(),
            "contract_ref": None,
            "core_refs": core_refs,
            "entries": selected,
            "invariant_registry_ref": invariant_registry_ref,
            "governing_set_complete": True,
            "token_budget": token_budget,
            "assembled_at": assembled_at,
        }
        self.contracts.validate("context-bundle", value)
        check_context(value, core_refs)
        with self.store.tx() as db:
            ref = self.store.put(db, scope, "context-bundle", bundle_id, 1, value)
        return ref, value

    def materialize_context(
        self, scope: Scope, bundle_ref: dict, *, allowed_classes: set[str]
    ) -> dict:
        from amplai_foundry.runtime.contracts.semantics import resolve_ref
        from amplai_foundry.runtime.evidence.cas import scan_secrets

        bundle = self.store.get(scope, "context-bundle", bundle_ref)
        check_context(bundle, bundle["core_refs"])
        refs = bundle["core_refs"] + [e["ref"] for e in bundle["entries"]]
        seen = set()
        sources = []
        used = 0
        for ref in refs:
            marker = digest(ref)
            if marker in seen:
                continue
            seen.add(marker)
            kind, value = resolve_ref(self.store, scope, ref)
            classification = value.get(
                "classification", value.get("data_classification", "internal")
            )
            if classification not in allowed_classes:
                raise Hold(
                    "CONTEXT_DATA_CLASS",
                    "One current context source cannot be sent to the selected model",
                )
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
            if scan_secrets(raw):
                raise Hold("CONTEXT_SECRET", "A referenced source contains a credential pattern")
            used += len(raw)
            if used > bundle["token_budget"]:
                raise Hold(
                    "CONTEXT_PAYLOAD_BUDGET",
                    "Actual mandatory context is larger than the conservative byte bound; no silent truncation",
                )
            sources.append(
                {
                    "ref": ref,
                    "kind": kind,
                    "classification": classification,
                    "untrusted_as_instructions": True,
                    "value": value,
                }
            )
        return {"bundle_ref": bundle_ref, "sources": sources, "conservative_bytes": used}

    @staticmethod
    def memory_kinds() -> list[str]:
        return [kind.value for kind in MemoryKind]


class RepoFacts:
    EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", ".env"}

    @classmethod
    def inspect(cls, root: Path, *, max_files: int = 4096) -> dict:
        import os

        root = root.resolve()
        files = []
        for directory, subdirs, names in os.walk(root):
            subdirs[:] = sorted(
                d
                for d in subdirs
                if d not in cls.EXCLUDED and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if name in cls.EXCLUDED or path.is_symlink():
                    continue
                if not path.resolve().is_relative_to(root):
                    raise RuntimeFault(
                        "REPO_PATH_ESCAPE", "Repository path escapes its allowed root"
                    )
                files.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
                if len(files) > max_files:
                    raise Hold(
                        "REPO_FACT_LIMIT", "Repository inventory needs a narrower approved scope"
                    )
        return {
            "root": str(root),
            "files": files,
            "test_configs": [
                f["path"]
                for f in files
                if Path(f["path"]).name
                in {"pyproject.toml", "package.json", "Makefile", "go.mod", "Cargo.toml"}
            ],
        }
