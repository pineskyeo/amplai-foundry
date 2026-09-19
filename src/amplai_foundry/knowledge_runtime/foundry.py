"""Adapters preserve the original eight memory kinds and proposal/apply lifecycle."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, ClassVar

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.intake.models import ArtifactRef, IntentRequest
from amplai_foundry.repositories.base import MemoryRepository
from amplai_foundry.runtime.contracts.identity import canonical, digest, digest_bytes, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.runtime.storage.store import Scope, Store

Ref = dict[str, Any]


class GovernedKnowledgeWriter:
    def __init__(
        self,
        intake_service: Any,
        *,
        identity_resolver: Callable[[Any], Any],
        project_resolver: Callable[[Any], Any],
    ) -> None:
        self.intake = intake_service
        self.identity_resolver = identity_resolver
        self.project_resolver = project_resolver

    def __call__(self, actor: Any, source_refs: list[dict[str, Any]], patch: dict[str, Any]) -> Any:
        actor.require("knowledge.propose")
        project = self.project_resolver(actor.scope)
        identity = self.identity_resolver(actor)
        if project is None or identity is None:
            raise Hold(
                "KNOWLEDGE_BINDING",
                "Canonical writes need existing project and Foundry actor bindings",
            )
        payload = {
            "source_refs": source_refs,
            "requested_change": patch,
            "instruction": (
                "Review and propose this knowledge change; do not bypass Decision/Apply."
            ),
        }
        data = canonical(payload)
        root = self.intake.workspace_root / ".amplai-intake"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise Hold("INTAKE_PATH", "Intake staging cannot be a symlink")
        path = root / (new_id("source") + ".json")
        with open(path, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        request = IntentRequest(
            instruction=(
                "Review the proposed knowledge change using the existing "
                "Source → Proposal → Decision → Apply policy."
            ),
            artifacts=[
                ArtifactRef(
                    path=str(path),
                    source_type="runtime-v3",
                    title="V3 knowledge change proposal",
                    media_type="application/json",
                )
            ],
            project_hint=project.project_id,
            expected_outcome="A governed proposal or explicit hold; no direct canonical mutation",
            identity=identity,
        )
        return self.intake.process(request)


class OntologyResolver:
    """Deterministic graph of reviewed domain facts; not an execution WorkGraph."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def neighbors(
        self,
        scope: Scope,
        node_id: str,
        *,
        depth: int = 1,
        limit: int = 128,
        allow_untrusted: bool = False,
    ) -> dict[str, Any]:
        if not 0 <= depth <= 3 or not 1 <= limit <= 512:
            raise RuntimeFault("ONTOLOGY_BOUND", "Traversal bounds are invalid")
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        latest: dict[str, tuple[Ref, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(scope, "domain-fact"):
            if (
                value["node_id"] not in latest
                or ref["revision"] > latest[value["node_id"]][0]["revision"]
            ):
                latest[value["node_id"]] = (ref, value)
        for ref, value in latest.values():
            if value.get("superseded_by") or (
                value.get("trust") != "governed" and not allow_untrusted
            ):
                continue
            nodes[value["node_id"]] = {"ref": ref, **value}
            edges.extend(value.get("relations", []))
        selected = {node_id}
        frontier = {node_id}
        for _ in range(depth):
            next_nodes = {e["target"] for e in edges if e.get("source") in frontier}
            selected |= next_nodes
            frontier = next_nodes
            if len(selected) > limit:
                raise Hold("ONTOLOGY_LIMIT", "Domain traversal exceeds the explicit context bound")
        return {
            "nodes": [nodes[n] for n in sorted(selected) if n in nodes],
            "edges": [
                e for e in edges if e.get("source") in selected and e.get("target") in selected
            ],
            "execution_graph": False,
        }

    def register_reviewed(
        self,
        actor: Any,
        value: dict[str, Any],
        *,
        approval_ref: Ref,
        approval_check: Callable[[Scope, Ref, str, str], Any],
    ) -> Ref:
        actor.require("knowledge.propose")
        required = {"node_id", "kind", "statement", "source_refs", "relations", "superseded_by"}
        if set(value) != required or not value["source_refs"]:
            raise Hold("DOMAIN_FACT", "Reviewed domain facts need exact fields and provenance")
        from amplai_foundry.domain.enums import MemoryKind

        if value["kind"] not in {k.value for k in MemoryKind}:
            raise RuntimeFault("MEMORY_KIND", "No ninth memory kind may be invented")
        approval_check(actor.scope, approval_ref, "knowledge.domain_fact", digest(value))
        from amplai_foundry.runtime.contracts.semantics import check_refs

        check_refs(self.store, actor.scope, value)
        record = {
            **value,
            "scope": actor.scope.wire(),
            "trust": "governed",
            "approval_ref": approval_ref,
        }
        with self.store.tx() as db:
            existing = self.store.list_objects(actor.scope, "domain-fact")
            revision = (
                max([r["revision"] for r, v in existing if r["id"] == value["node_id"]] + [0]) + 1
            )
            return self.store.put(
                db, actor.scope, "domain-fact", value["node_id"], revision, record
            )


class FoundryKnowledgeReader:
    """Read-only stable/decision/work/evidence views over pinned canonical snapshots.

    The eight Foundry memory kinds stay exactly as they are (design/12 §1). Views are
    retrieval and lifecycle projections, not new stores, and this class has no write
    method: canonical changes go through ``KnowledgeService.propose_canonical_change``.
    """

    KIND_VIEWS: ClassVar[dict[str, tuple[str, ...]]] = {
        "stable": ("concept", "principle", "architecture", "map"),
        "decision": ("decision",),
        "work": ("work",),
        "evidence": ("evidence",),
    }

    def __init__(self, repository: MemoryRepository, store: Store, contracts: Any) -> None:
        self.repository, self.store, self.contracts = repository, store, contracts

    def snapshot(self, scope: Scope, *, namespace: str) -> list[dict[str, Any]]:
        """Pin every canonical object of ``namespace`` as an immutable, digest-bound record."""
        kinds = {kind.value for kind in MemoryKind}
        pinned: list[dict[str, Any]] = []
        with self.store.tx() as db:
            existing: dict[str, list[tuple[Ref, dict[str, Any]]]] = {}
            for ref, value in self.store.list_objects(scope, "knowledge-canonical"):
                existing.setdefault(str(value["memory_id"]), []).append((ref, value))
            for obj in self.repository.list(namespace):
                if obj.kind.value not in kinds:
                    raise RuntimeFault("MEMORY_KIND", "No ninth memory kind may be invented")
                record: dict[str, Any] = {
                    "memory_id": obj.id,
                    "namespace": obj.namespace,
                    "project": obj.project,
                    "kind": obj.kind.value,
                    "status": obj.status.value,
                    "memory_revision": obj.revision,
                    "title": obj.title,
                    "summary": obj.summary,
                    "content_digest": digest_bytes(obj.content.encode("utf-8")),
                    "superseded_by": obj.superseded_by,
                    "merged_into": obj.merged_into,
                    "source_refs": list(obj.source_refs),
                    "relations": [
                        {
                            "type": relation.type.value,
                            "target": relation.target,
                            "target_namespace": relation.target_namespace,
                        }
                        for relation in obj.relations
                    ],
                    "source_timestamp": obj.updated_at.isoformat(),
                    "trust": "canonical",
                    "source_locator": f"{obj.namespace}#{obj.id}",
                }
                wanted = digest(record)
                prior = [r for r, _ in existing.get(obj.id, []) if r["digest"] == wanted]
                if prior:
                    ref = prior[0]
                else:
                    revision = max([r["revision"] for r, _ in existing.get(obj.id, [])] + [0]) + 1
                    ref = self.store.put(db, scope, "knowledge-canonical", obj.id, revision, record)
                    existing.setdefault(obj.id, []).append((ref, record))
                pinned.append({**record, "ref": ref})
        return pinned

    def _latest(self, scope: Scope) -> dict[str, tuple[Ref, dict[str, Any]]]:
        latest: dict[str, tuple[Ref, dict[str, Any]]] = {}
        for ref, value in self.store.list_objects(scope, "knowledge-canonical"):
            memory_id = str(value["memory_id"])
            if memory_id not in latest or ref["revision"] > latest[memory_id][0]["revision"]:
                latest[memory_id] = (ref, value)
        return latest

    @staticmethod
    def _stale(value: dict[str, Any]) -> bool:
        return value.get("status") != "active" or bool(value.get("superseded_by"))

    @staticmethod
    def _pointer(
        latest: dict[str, tuple[Ref, dict[str, Any]]], memory_id: str | None
    ) -> dict[str, Any] | None:
        if not memory_id:
            return None
        if memory_id in latest:
            return dict(latest[memory_id][0])
        return {"id": memory_id, "revision": None, "digest": None}

    def view(
        self, scope: Scope, view: str, *, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        if view not in self.KIND_VIEWS:
            raise RuntimeFault("VIEW_KIND", "Unknown knowledge view")
        if view == "work":
            with self.store._lock:
                rows = self.store.conn.execute(
                    "SELECT kind,id,state,row_version FROM heads "
                    "WHERE tenant=? AND project=? AND kind IN ('goal','work','run') "
                    "ORDER BY kind,id",
                    scope.keys(),
                ).fetchall()
            return [
                {
                    "kind": "work",
                    "trust": "control",
                    "ref": None,
                    "memory_id": None,
                    "aggregate": {"kind": r["kind"], "id": r["id"], "state": r["state"]},
                    "row_version": r["row_version"],
                    "freshness": "current",
                    "superseded_by": None,
                    "source_locator": f"runtime:{r['kind']}:{r['id']}",
                }
                for r in rows
            ]
        if view == "evidence":
            newest: dict[str, tuple[Ref, dict[str, Any]]] = {}
            for ref, value in self.store.list_objects(scope, "knowledge-observation"):
                if ref["id"] not in newest or ref["revision"] > newest[ref["id"]][0]["revision"]:
                    newest[ref["id"]] = (ref, value)
            return [
                {
                    "kind": "evidence",
                    "trust": "untrusted" if value["trust"] != "observed" else "observed",
                    "ref": ref,
                    "memory_id": None,
                    "observation_kind": value["kind"],
                    "freshness": "current",
                    "superseded_by": None,
                    "source_locator": value["locator"],
                }
                for ref, value in sorted(newest.values(), key=lambda pair: pair[0]["id"])
            ]
        latest = self._latest(scope)
        entries: list[dict[str, Any]] = []
        for memory_id in sorted(latest):
            ref, value = latest[memory_id]
            if value["kind"] not in self.KIND_VIEWS[view]:
                continue
            stale = self._stale(value)
            if stale and not include_superseded:
                continue
            entries.append(
                {
                    **value,
                    "ref": ref,
                    "freshness": "stale" if stale else "current",
                    "superseded_by": self._pointer(latest, value.get("superseded_by")),
                }
            )
        return entries

    def current(
        self, scope: Scope, memory_id: str, *, strict: bool = False
    ) -> dict[str, Any] | None:
        """Default retrieval: the current pinned record, or None when superseded/absent (T-018)."""
        pair = self._latest(scope).get(memory_id)
        if pair is None:
            if strict:
                raise Hold("KNOWLEDGE_UNKNOWN", "No pinned canonical record for this memory id")
            return None
        ref, value = pair
        if self._stale(value):
            if strict:
                raise Hold("KNOWLEDGE_SUPERSEDED", "Only the current revision answers by default")
            return None
        return {**value, "ref": ref, "freshness": "current"}
