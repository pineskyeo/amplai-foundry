"""Generate committed JSON Schemas from Pydantic contract models."""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from amplai_foundry.domain.models import MemoryMetadata
from amplai_foundry.governance.models import (
    AuthorityContext as GovernanceAuthorityContext,
)
from amplai_foundry.governance.models import (
    ExternalActorBinding,
    ProposalAction,
    ProposalActionAuditEvent,
)
from amplai_foundry.intake.models import IntakeRun, IntentRequest, ResolutionHoldRecord
from amplai_foundry.projects.models import DomainLock, ProjectManifest
from amplai_foundry.proposals.models import Proposal
from amplai_foundry.roadmaps.models import RoadmapChangeProposal, RoadmapDefinition
from amplai_foundry.semantics.models import (
    ComparisonResult,
    KnowledgeCandidate,
    SemanticAnchor,
)
from amplai_foundry.semantics.repository import SemanticAnchorSet


def _schema(model: type[BaseModel], *, identifier: str, title: str) -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = identifier
    schema["title"] = title
    return schema


def generated_schemas() -> dict[Path, str]:
    """Return deterministic schema paths and JSON payloads."""
    definitions = {
        Path("schemas/note.schema.json"): _schema(
            MemoryMetadata,
            identifier="https://amplai.local/schemas/note.schema.json",
            title="AMPLAI Memory Object Front Matter",
        ),
        Path("schemas/proposal.schema.json"): _schema(
            Proposal,
            identifier="https://amplai.local/schemas/proposal.schema.json",
            title="AMPLAI Knowledge Proposal",
        ),
        Path("schemas/proposal-action.schema.json"): _schema(
            ProposalAction,
            identifier="https://amplai.local/schemas/proposal-action.schema.json",
            title="AMPLAI Proposal Action",
        ),
        Path("schemas/proposal-action-audit.schema.json"): _schema(
            ProposalActionAuditEvent,
            identifier="https://amplai.local/schemas/proposal-action-audit.schema.json",
            title="AMPLAI Proposal Action Audit Event",
        ),
        Path("schemas/authority-context.schema.json"): _schema(
            GovernanceAuthorityContext,
            identifier="https://amplai.local/schemas/authority-context.schema.json",
            title="AMPLAI Governance Authority Context",
        ),
        Path("schemas/external-actor-binding.schema.json"): _schema(
            ExternalActorBinding,
            identifier="https://amplai.local/schemas/external-actor-binding.schema.json",
            title="AMPLAI External Actor Binding",
        ),
        Path("schemas/project-manifest.schema.json"): _schema(
            ProjectManifest,
            identifier="https://amplai.local/schemas/project-manifest.schema.json",
            title="AMPLAI Project Pack Manifest",
        ),
        Path("schemas/domain-lock.schema.json"): _schema(
            DomainLock,
            identifier="https://amplai.local/schemas/domain-lock.schema.json",
            title="AMPLAI Domain Lock",
        ),
        Path("schemas/intent-request.schema.json"): _schema(
            IntentRequest,
            identifier="https://amplai.local/schemas/intent-request.schema.json",
            title="AMPLAI Intent Request",
        ),
        Path("schemas/intake-run.schema.json"): _schema(
            IntakeRun,
            identifier="https://amplai.local/schemas/intake-run.schema.json",
            title="AMPLAI Intake Run",
        ),
        Path("schemas/resolution-hold.schema.json"): _schema(
            ResolutionHoldRecord,
            identifier="https://amplai.local/schemas/resolution-hold.schema.json",
            title="AMPLAI Resolution Hold",
        ),
        Path("schemas/knowledge-candidate.schema.json"): _schema(
            KnowledgeCandidate,
            identifier="https://amplai.local/schemas/knowledge-candidate.schema.json",
            title="AMPLAI Knowledge Candidate",
        ),
        Path("schemas/comparison-result.schema.json"): _schema(
            ComparisonResult,
            identifier="https://amplai.local/schemas/comparison-result.schema.json",
            title="AMPLAI Semantic Comparison Result",
        ),
        Path("schemas/semantic-anchor.schema.json"): _schema(
            SemanticAnchor,
            identifier="https://amplai.local/schemas/semantic-anchor.schema.json",
            title="AMPLAI Semantic Anchor",
        ),
        Path("schemas/semantic-anchor-set.schema.json"): _schema(
            SemanticAnchorSet,
            identifier="https://amplai.local/schemas/semantic-anchor-set.schema.json",
            title="AMPLAI Semantic Anchor Set",
        ),
        Path("schemas/roadmap.schema.json"): _schema(
            RoadmapDefinition,
            identifier="https://amplai.local/schemas/roadmap.schema.json",
            title="AMPLAI Roadmap Definition",
        ),
        Path("schemas/roadmap-proposal.schema.json"): _schema(
            RoadmapChangeProposal,
            identifier="https://amplai.local/schemas/roadmap-proposal.schema.json",
            title="AMPLAI Roadmap Change Proposal",
        ),
    }
    return {
        path: json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        for path, schema in definitions.items()
    }


def write_schemas(root: Path = Path(".")) -> list[Path]:
    paths: list[Path] = []
    for relative, payload in generated_schemas().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        paths.append(path)
    return paths


def check_schemas(root: Path = Path(".")) -> list[Path]:
    """Return committed schema files that differ from generated contracts."""
    changed: list[Path] = []
    for relative, expected in generated_schemas().items():
        path = root / relative
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            changed.append(path)
            continue
        if actual != expected:
            changed.append(path)
    return changed
