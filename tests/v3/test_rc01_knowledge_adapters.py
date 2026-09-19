"""V3-012 — Foundry read/context knowledge adapters (design/12 §1, T-018, T-019).

Eight memory kinds are preserved as-is; stable/decision/work/evidence are *views*
over pinned canonical snapshots, never a new database. The adapter has no write path.
"""

from __future__ import annotations

from datetime import date

import pytest

from amplai_foundry.domain.enums import MemoryKind, MemoryStatus, RelationType
from amplai_foundry.domain.identity import MemoryRef
from amplai_foundry.domain.models import MemoryObject, MemoryRelation
from amplai_foundry.knowledge_runtime.service import KnowledgeService
from amplai_foundry.runtime.errors import Hold

NS = "vault/project/amplai"


def memory(mid, kind, status=MemoryStatus.ACTIVE, *, superseded_by=None, relations=(), revision=1):
    return MemoryObject(
        id=mid,
        namespace=NS,
        project="amplai",
        kind=kind,
        status=status,
        title=f"{mid} title",
        summary=f"{mid} summary",
        created_at=date(2026, 9, 1),
        updated_at=date(2026, 9, 1),
        relations=list(relations),
        superseded_by=superseded_by,
        revision=revision,
        content=f"# {mid}\n\nbody of {mid}\n",
    )


class FakeRepository:
    """In-memory MemoryRepository port; canonical objects are read-only here."""

    def __init__(self, objects):
        self.objects = {o.ref.qualified: o for o in objects}

    def get(self, memory_ref, *, namespace=None):
        ref = (
            memory_ref
            if isinstance(memory_ref, MemoryRef)
            else MemoryRef.parse(memory_ref, default_namespace=namespace)
        )
        return self.objects.get(ref.qualified)

    def list(self, namespace=None):
        return sorted(
            (o for o in self.objects.values() if namespace in (None, o.namespace)),
            key=lambda o: o.id,
        )

    def find_by_kind(self, kind, *, namespace=None):
        return [o for o in self.list(namespace) if o.kind is kind]

    def find_referencing(self, target_ref, *, namespace=None):
        target = (
            target_ref
            if isinstance(target_ref, MemoryRef)
            else MemoryRef.parse(target_ref, default_namespace=namespace)
        )
        return [o for o in self.list(namespace) if target in o.referenced_refs()]

    def exists(self, memory_ref, *, namespace=None):
        return self.get(memory_ref, namespace=namespace) is not None


@pytest.fixture
def repo():
    return FakeRepository(
        [
            memory("CON-0001", MemoryKind.CONCEPT),
            memory("PRI-0001", MemoryKind.PRINCIPLE),
            memory("ARC-0001", MemoryKind.ARCHITECTURE),
            memory("MAP-0001", MemoryKind.MAP),
            memory(
                "DEC-0001", MemoryKind.DECISION, MemoryStatus.SUPERSEDED, superseded_by="DEC-0002"
            ),
            memory(
                "DEC-0002",
                MemoryKind.DECISION,
                relations=[MemoryRelation(type=RelationType.SUPERSEDES, target="DEC-0001")],
                revision=2,
            ),
            memory("QUE-0001", MemoryKind.QUESTION),
            memory("EXP-0001", MemoryKind.EXPERIMENT),
        ]
    )


def test_eight_kinds_preserved_and_no_ninth(deployment, repo):
    from amplai_foundry.knowledge_runtime.foundry import FoundryKnowledgeReader

    reader = FoundryKnowledgeReader(repo, deployment.store, deployment.contracts)
    assert KnowledgeService.memory_kinds() == [k.value for k in MemoryKind]
    assert len(KnowledgeService.memory_kinds()) == 8
    assert set(reader.KIND_VIEWS) == {"stable", "decision", "work", "evidence"}
    assert set(reader.KIND_VIEWS["stable"]) == {"concept", "principle", "architecture", "map"}
    assert reader.KIND_VIEWS["decision"] == ("decision",)
    assert (
        not hasattr(reader, "write") and not hasattr(reader, "put") and not hasattr(reader, "apply")
    )


def test_snapshot_pins_canonical_objects_with_provenance(deployment, repo):
    from amplai_foundry.knowledge_runtime.foundry import FoundryKnowledgeReader

    reader = FoundryKnowledgeReader(repo, deployment.store, deployment.contracts)
    pinned = reader.snapshot(deployment.scope, namespace=NS)
    assert len(pinned) == 8
    entry = next(p for p in pinned if p["memory_id"] == "CON-0001")
    assert entry["trust"] == "canonical"
    assert entry["kind"] == "concept"
    assert entry["ref"].keys() == {"id", "revision", "digest"}
    assert entry["source_locator"] == f"{NS}#CON-0001"
    assert entry["content_digest"].startswith("sha256:")
    assert entry["source_timestamp"] == "2026-09-01"
    # Second snapshot of unchanged canonical content is the same immutable ref (idempotent).
    again = reader.snapshot(deployment.scope, namespace=NS)
    assert [p["ref"] for p in again] == [p["ref"] for p in pinned]


def test_views_separate_stable_decision_work_evidence(deployment, repo):
    from amplai_foundry.knowledge_runtime.foundry import FoundryKnowledgeReader

    d = deployment
    reader = FoundryKnowledgeReader(repo, d.store, d.contracts)
    reader.snapshot(d.scope, namespace=NS)
    stable = reader.view(d.scope, "stable")
    assert {e["memory_id"] for e in stable} == {"CON-0001", "PRI-0001", "ARC-0001", "MAP-0001"}
    assert all(e["trust"] == "canonical" and e["freshness"] == "current" for e in stable)
    decisions = reader.view(d.scope, "decision")
    assert {e["memory_id"] for e in decisions} == {"DEC-0002"}, (
        "superseded decisions leave the default view"
    )
    history = reader.view(d.scope, "decision", include_superseded=True)
    old = next(e for e in history if e["memory_id"] == "DEC-0001")
    assert old["freshness"] == "stale" and old["superseded_by"]["id"] == "DEC-0002"
    # work/session view is runtime state, never fact or authority
    work = reader.view(d.scope, "work")
    assert all(e["trust"] in {"observed", "control"} and e["kind"] == "work" for e in work)
    obs = d.knowledge.record_observation(d.scope, "repo:README.md", "observed text")
    evidence = reader.view(d.scope, "evidence")
    assert any(e["ref"] == obs for e in evidence)
    assert all(e["trust"] in {"observed", "untrusted"} for e in evidence)


def test_t018_default_retrieval_current_but_historical_run_resolves_old_pin(deployment, repo):
    from amplai_foundry.knowledge_runtime.foundry import FoundryKnowledgeReader
    from amplai_foundry.runtime.contracts.semantics import resolve_ref

    d = deployment
    reader = FoundryKnowledgeReader(repo, d.store, d.contracts)
    first = reader.snapshot(d.scope, namespace=NS)
    old_pin = next(p["ref"] for p in first if p["memory_id"] == "DEC-0001")
    # Canonical content moves on: DEC-0002 gets a new revision.
    repo.objects[f"{NS}#DEC-0002"] = memory("DEC-0002", MemoryKind.DECISION, revision=3)
    reader.snapshot(d.scope, namespace=NS)
    current = reader.current(d.scope, "DEC-0002")
    assert current["memory_revision"] == 3
    assert reader.current(d.scope, "DEC-0001") is None, "superseded is never the default answer"
    kind, value = resolve_ref(d.store, d.scope, old_pin)
    assert kind == "knowledge-canonical" and value["memory_id"] == "DEC-0001"
    with pytest.raises(Hold):
        reader.current(d.scope, "DEC-9999", strict=True)


def test_t019_worker_observation_never_becomes_canonical(deployment, repo):
    from amplai_foundry.knowledge_runtime.foundry import FoundryKnowledgeReader

    d = deployment
    reader = FoundryKnowledgeReader(repo, d.store, d.contracts)
    reader.snapshot(d.scope, namespace=NS)
    for _ in range(3):
        d.knowledge.record_observation(
            d.scope, "repo:rule.md", "always run lint first", kind="repo_fact"
        )
    assert {e["memory_id"] for e in reader.view(d.scope, "stable")} == {
        "CON-0001",
        "PRI-0001",
        "ARC-0001",
        "MAP-0001",
    }
    with pytest.raises(Hold) as exc:
        d.knowledge.propose_canonical_change(d.actor, [], {"add": "rule"})
    assert exc.value.code == "GOVERNANCE_REQUIRED"
    assert repo.list() == sorted(repo.objects.values(), key=lambda o: o.id)
    submitted = []
    governed = KnowledgeService(
        d.store,
        d.contracts,
        governed_submit=lambda a, s, p: submitted.append(p) or {"proposal": "P-1"},
    )
    assert governed.propose_canonical_change(d.actor, [], {"add": "rule"}) == {"proposal": "P-1"}
    assert submitted == [{"add": "rule"}]
    assert len(repo.list()) == 8, "no canonical mutation from the runtime side"
