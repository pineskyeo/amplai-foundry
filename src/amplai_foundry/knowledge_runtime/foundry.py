"""Adapters preserve the original eight memory kinds and proposal/apply lifecycle."""

from __future__ import annotations

import os

from amplai_foundry.intake.models import ArtifactRef, IntentRequest
from amplai_foundry.runtime.contracts.identity import canonical, digest, new_id
from amplai_foundry.runtime.errors import Hold, RuntimeFault


class GovernedKnowledgeWriter:
    def __init__(self, intake_service, *, identity_resolver, project_resolver):
        self.intake = intake_service
        self.identity_resolver = identity_resolver
        self.project_resolver = project_resolver

    def __call__(self, actor, source_refs, patch):
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
            "instruction": "Review and propose this knowledge change; do not bypass Decision/Apply.",
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
            instruction="Review the proposed knowledge change using the existing Source → Proposal → Decision → Apply policy.",
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

    def __init__(self, store):
        self.store = store

    def neighbors(self, scope, node_id, *, depth=1, limit=128, allow_untrusted=False):
        if not 0 <= depth <= 3 or not 1 <= limit <= 512:
            raise RuntimeFault("ONTOLOGY_BOUND", "Traversal bounds are invalid")
        nodes = {}
        edges = []
        latest = {}
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

    def register_reviewed(self, actor, value, *, approval_ref, approval_check):
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
