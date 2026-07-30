from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from amplai_foundry.governance import (
    ChannelProvider,
    ChannelRef,
    ExternalActorIdentity,
)
from amplai_foundry.intake.models import ArtifactRef, IntentRequest

SOURCE_ROOT = Path("src/amplai_foundry")


def _python_sources() -> tuple[Path, ...]:
    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def test_removed_direct_mutation_symbols_do_not_reenter_production_code() -> None:
    forbidden = {
        "ProposalActionService",
        "ProposalApplyService",
        "approve_proposal",
        "AuthorityKind",
    }
    findings: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden:
                findings.append(f"{path}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                findings.append(f"{path}:{node.lineno}:{node.attr}")

    assert findings == []


def test_governance_authority_context_is_constructed_only_by_authority_service() -> None:
    findings: list[str] = []
    allowed = SOURCE_ROOT / "governance/authority.py"
    for path in _python_sources():
        if path == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name == "AuthorityContext":
                findings.append(f"{path}:{node.lineno}")

    assert findings == []


def test_legacy_apply_engines_are_private_and_have_no_production_callers() -> None:
    proposal_apply = ast.parse((SOURCE_ROOT / "proposals/apply.py").read_text(encoding="utf-8"))
    roadmap_service = ast.parse((SOURCE_ROOT / "roadmaps/service.py").read_text(encoding="utf-8"))
    active_proposals = ast.parse(
        (SOURCE_ROOT / "governance/active_proposals.py").read_text(encoding="utf-8")
    )
    roadmap_repository = ast.parse(
        (SOURCE_ROOT / "roadmaps/repository.py").read_text(encoding="utf-8")
    )
    proposal_public = {
        node.name
        for node in proposal_apply.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and not node.name.startswith("_")
    }
    legacy_apply_class = next(
        node
        for node in proposal_apply.body
        if isinstance(node, ast.ClassDef) and node.name == "_LegacyProposalApplyEngine"
    )
    legacy_apply_public = {
        node.name
        for node in legacy_apply_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    roadmap_class = next(
        node
        for node in roadmap_service.body
        if isinstance(node, ast.ClassDef) and node.name == "RoadmapService"
    )
    roadmap_public = {
        node.name
        for node in roadmap_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    active_class = next(
        node
        for node in active_proposals.body
        if isinstance(node, ast.ClassDef) and node.name == "ActiveProposalRepository"
    )
    active_public = {
        node.name
        for node in active_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }
    roadmap_repository_class = next(
        node
        for node in roadmap_repository.body
        if isinstance(node, ast.ClassDef) and node.name == "RoadmapRepository"
    )
    roadmap_repository_public = {
        node.name
        for node in roadmap_repository_class.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
    }

    assert "ProposalApplyService" not in proposal_public
    assert "approve_proposal" not in proposal_public
    assert "apply" not in legacy_apply_public
    assert "approve" not in roadmap_public
    assert "apply" not in roadmap_public
    assert "transition_state" not in active_public
    assert "save" not in roadmap_repository_public
    for path in _python_sources():
        if path in {
            SOURCE_ROOT / "proposals/apply.py",
            SOURCE_ROOT / "roadmaps/service.py",
        }:
            continue
        text = path.read_text(encoding="utf-8")
        assert "_LegacyProposalApplyEngine" not in text, path
        assert "_approve_legacy_proposal" not in text, path
        assert "_approve_legacy_proposal(" not in text, path
        assert "_apply_legacy_proposal(" not in text, path


@pytest.mark.parametrize("field", ("authority", "permissions", "authority_context"))
def test_public_intent_request_rejects_authority_injection(field: str) -> None:
    payload: dict[str, object] = {
        "instruction": "prepare this proposal for review",
        "artifacts": [ArtifactRef(path="proposal.md")],
        "identity": ExternalActorIdentity(
            provider=ChannelProvider.CLI,
            provider_installation_ref="local:test",
            external_actor_id="tester",
            request_id="INTAKE-TEST",
            channel=ChannelRef(provider=ChannelProvider.CLI, message_id="message-1"),
        ),
        field: {},
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IntentRequest.model_validate(payload)
