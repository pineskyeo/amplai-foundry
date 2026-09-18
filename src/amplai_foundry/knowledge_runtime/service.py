"""Read-only knowledge resolution, progressive context and governed write routing."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.runtime.contracts.identity import digest
from amplai_foundry.runtime.contracts.semantics import (
    READINESS_AREAS,
    check_context,
)
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store


class KnowledgeService:
    def __init__(
        self, store: Store, contracts: Any, *, governed_submit: Callable[..., Any] | None = None
    ) -> None:
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
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if trust not in {"observed", "untrusted", "legacy_imported"}:
            raise RuntimeFault(
                "KNOWLEDGE_AUTHORITY", "Observation ingestion cannot mint canonical authority"
            )
        reserved = {"locator", "text", "kind", "trust", "classification", "observation_id", "scope"}
        if metadata and (set(metadata) & reserved):
            raise RuntimeFault(
                "OBSERVATION_METADATA", "Metadata may not override observation fields"
            )
        content: dict[str, Any] = {
            "locator": locator,
            "text": text,
            "kind": kind,
            "trust": trust,
            "classification": classification,
            # Repository and external material is evidence, never an instruction channel.
            "untrusted_as_instructions": True,
            **(metadata or {}),
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

    def import_vault_readonly(self, scope: Scope, root: Path) -> list[dict[str, Any]]:
        # Canonical writes stay in the existing governed Foundry path; imported files
        # remain observations unless checked against an actual applied decision.
        root = root.resolve()
        results: list[dict[str, Any]] = []
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

    def propose_canonical_change(
        self, actor: Any, source_refs: list[dict[str, Any]], patch: dict[str, Any]
    ) -> Any:
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
        evidence_by_area: dict[str, list[dict[str, Any]]],
        *,
        conflicts: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        conflicts = conflicts or {}
        entries: list[dict[str, Any]] = []
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
        core_refs: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        invariant_registry_ref: dict[str, Any],
        token_budget: int,
        assembled_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
                "Core context cannot fit; increase budget or approved summarization, "
                "never silently truncate",
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

    def progressive_bundle(
        self,
        scope: Scope,
        *,
        bundle_id: str,
        core_refs: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        invariant_registry_ref: dict[str, Any],
        token_budget: int,
        assembled_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Core + map + bounded excerpts (design/12 §3).

        Optional excerpts that do not fit are not dropped silently: they stay in the
        bundle as map entries (address/version/digest, ``excerpt`` = null) so the model can
        request them explicitly. Mandatory entries are never demoted.
        """
        from amplai_foundry.runtime.contracts.semantics import resolve_ref

        for ref in core_refs:
            resolve_ref(self.store, scope, ref)
        mandatory = [e for e in entries if e["mandatory"]]
        optional = [e for e in entries if not e["mandatory"]]

        def size(entry: dict[str, Any]) -> int:
            return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))

        used = sum(size(e) for e in mandatory) + len(json.dumps(core_refs).encode())
        if used > token_budget:
            raise Hold(
                "CORE_CONTEXT_BUDGET",
                "Core context cannot fit; increase budget or approved summarization, "
                "never silently truncate",
            )
        selected: list[dict[str, Any]] = list(mandatory)
        for entry in optional:
            if used + size(entry) <= token_budget:
                selected.append(entry)
                used += size(entry)
            else:
                mapped = {**entry, "excerpt": None, "mandatory": False}
                selected.append(mapped)
                used += size(mapped)
        value: dict[str, Any] = {
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
        self, scope: Scope, bundle_ref: dict[str, Any], *, allowed_classes: set[str]
    ) -> dict[str, Any]:
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
                    "Actual mandatory context is larger than the conservative byte bound; "
                    "no silent truncation",
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
    """Compatibility facade; the resolver lives in knowledge_runtime.repo_facts."""

    EXCLUDED = frozenset({".git", ".venv", "node_modules", "__pycache__", ".env"})

    @classmethod
    def inspect(cls, root: Path, *, max_files: int = 4096) -> dict[str, Any]:
        from amplai_foundry.knowledge_runtime.repo_facts import RepoFactsResolver

        return RepoFactsResolver(root, max_files=max_files).inspect()
