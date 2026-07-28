import hashlib
from pathlib import Path

import yaml

from amplai_foundry.domain.enums import MemoryKind
from amplai_foundry.domain.identity import MemoryRef, ProjectRef
from amplai_foundry.domain.semantic import (
    ClaimModality,
    SemanticClaim,
    SemanticDescriptor,
    SemanticScope,
    semantic_signature,
)
from amplai_foundry.repositories.markdown import MarkdownMemoryRepository
from amplai_foundry.semantics.comparison import SemanticComparator
from amplai_foundry.semantics.models import (
    CandidateEvidence,
    ComparisonRelation,
    KnowledgeCandidate,
    SemanticAnchor,
)
from amplai_foundry.semantics.repository import SemanticAnchorRepository

FIXTURE = Path(__file__).parent / "fixtures/semantic-golden-set.yaml"
AMPLAI = ProjectRef(
    project_id="amplai",
    namespace="org/pinesky/project/amplai",
)
CORTEX = ProjectRef(
    project_id="cortex",
    namespace="org/pinesky/project/cortex",
)


def _scope(case: dict[str, object], prefix: str) -> SemanticScope:
    return SemanticScope(
        domain=str(case.get(f"{prefix}domain", case.get("domain", "knowledge-governance"))),
        target=str(case.get(f"{prefix}target", case.get("target", "roadmap-tracker"))),
        actor=str(case.get(f"{prefix}actor", case.get("actor", "amplai"))),
    )


def _claim(case: dict[str, object], prefix: str) -> SemanticClaim:
    return SemanticClaim(
        modality=ClaimModality(str(case.get(f"{prefix}modality", case.get("modality", "must")))),
        action=str(case.get(f"{prefix}action", case.get("action", "update"))),
        object=str(case.get(f"{prefix}object", case.get("object", "roadmap-tracker"))),
        outcome="consistent-state",
    )


def _constraints(case: dict[str, object], prefix: str) -> list[str]:
    value = case.get(f"{prefix}constraints", case.get("constraints", []))
    assert isinstance(value, list)
    return [str(item) for item in value]


def _candidate(case: dict[str, object]) -> KnowledgeCandidate:
    scope = _scope(case, "incoming_")
    claim = _claim(case, "incoming_")
    constraints = _constraints(case, "incoming_")
    return KnowledgeCandidate.create(
        project=AMPLAI,
        scope=scope,
        claim=claim,
        constraints=constraints,
        canonical_statement=str(case["incoming_statement"]),
        aliases=[],
        suggested_kinds=[MemoryKind.CONCEPT],
        evidence=[
            CandidateEvidence(
                source=MemoryRef(
                    namespace=AMPLAI.namespace,
                    local_id="SRC-20260728-AAAAAAAA",
                ),
                start_line=1,
                end_line=1,
            )
        ],
    )


def _anchor(case: dict[str, object]) -> SemanticAnchor:
    project = CORTEX if case.get("different_project") else AMPLAI
    scope = _scope(case, "existing_")
    claim = _claim(case, "existing_")
    constraints = _constraints(case, "existing_")
    descriptor = SemanticDescriptor(
        signature=semantic_signature(scope, claim, constraints),
        scope=scope,
        claim=claim,
        constraints=constraints,
        canonical_statement=str(case["existing_statement"]),
        aliases=[],
    )
    return SemanticAnchor(
        semantic_id=f"SEM-{hashlib.sha256(str(case['id']).encode()).hexdigest()[:12].upper()}",
        project=project,
        descriptor=descriptor,
        target_refs=[
            MemoryRef(
                namespace=project.namespace,
                local_id="CON-0001",
            )
        ],
        provisional=False,
    )


def test_semantic_golden_set_has_at_least_thirty_cases() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == 1
    assert len(fixture["cases"]) >= 30


def test_semantic_golden_set_relations_are_deterministic() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    comparator = SemanticComparator()

    for case in fixture["cases"]:
        result = comparator.compare(_candidate(case), [_anchor(case)])
        assert result.relation is ComparisonRelation(case["expected"]), case["id"]


def test_uncertain_default_never_recommends_create() -> None:
    candidate = KnowledgeCandidate.create(
        project=AMPLAI,
        scope=SemanticScope(
            domain="planning",
            target="roadmap-ui",
            actor="amplai",
        ),
        claim=SemanticClaim(
            modality=ClaimModality.MUST,
            action="display",
            object="tracker",
        ),
        constraints=[],
        canonical_statement="Roadmap display updates tracker colors.",
        aliases=[],
        suggested_kinds=[MemoryKind.CONCEPT],
        evidence=[
            CandidateEvidence(
                source=MemoryRef(
                    namespace=AMPLAI.namespace,
                    local_id="SRC-20260728-BBBBBBBB",
                ),
                start_line=1,
                end_line=1,
            )
        ],
    )
    case = {
        "id": "uncertain-safety",
        "existing_statement": "Roadmap changes update tracker state.",
        "incoming_statement": candidate.canonical_statement,
    }

    result = SemanticComparator().compare(candidate, [_anchor(case)])

    assert result.relation is ComparisonRelation.UNCERTAIN
    assert result.recommended_operation == "HOLD"
    assert result.requires_review


def test_memory_anchors_include_only_active_semantic_knowledge(tmp_path: Path) -> None:
    fixtures = [
        ("10-concepts/CON-0001.md", "CON-0001", "concept", "active"),
        ("10-concepts/CON-0002.md", "CON-0002", "concept", "rejected"),
        ("70-maps/MAP-0001.md", "MAP-0001", "map", "active"),
    ]
    for relative, identifier, kind, status in fixtures:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "schema_version": 1,
            "id": identifier,
            "namespace": AMPLAI.namespace,
            "project": AMPLAI.project_id,
            "kind": kind,
            "status": status,
            "title": f"{identifier} lifecycle fixture",
            "summary": "Lifecycle filtering must preserve only authoritative semantic knowledge.",
            "created_at": "2026-07-28",
            "updated_at": "2026-07-28",
            "source_refs": [],
            "relations": [],
            "revision": 1,
            "tags": ["semantic-fixture"],
        }
        path.write_text(
            "---\n"
            + yaml.safe_dump(metadata, sort_keys=False)
            + "---\n# Fixture\n\nLifecycle-aware semantic anchor content.\n",
            encoding="utf-8",
        )

    anchors = SemanticAnchorRepository.from_memory(
        MarkdownMemoryRepository(tmp_path),
        AMPLAI,
    )

    assert [target.local_id for anchor in anchors for target in anchor.target_refs] == ["CON-0001"]
