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


def _symbol_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = alias.name
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            resolved = _qualified_name(node.value, bindings)
            if resolved.rsplit(".", 1)[-1] != "AuthorityContext":
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and bindings.get(target.id) != resolved:
                    bindings[target.id] = resolved
                    changed = True
    return bindings


def _qualified_name(node: ast.expr, bindings: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _qualified_name(node.value, bindings)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _reflective_symbol(node: ast.Call) -> str | None:
    if (
        isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    if isinstance(node.func, ast.Subscript):
        index = node.func.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return index.value
    return None


def _authority_context_findings(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    bindings = _symbol_bindings(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _qualified_name(node.func, bindings).rsplit(".", 1)[-1]
        if resolved == "AuthorityContext" or _reflective_symbol(node) == "AuthorityContext":
            findings.append(f"{filename}:{node.lineno}")
    return findings


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
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name.rsplit(".", 1)[-1] in forbidden:
                        findings.append(f"{path}:{node.lineno}:{alias.name}")
            if isinstance(node, ast.Name) and node.id in forbidden:
                findings.append(f"{path}:{node.lineno}:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                findings.append(f"{path}:{node.lineno}:{node.attr}")
            if isinstance(node, ast.Call) and _reflective_symbol(node) in forbidden:
                findings.append(f"{path}:{node.lineno}:{_reflective_symbol(node)}")

    assert findings == []


def test_governance_authority_context_is_constructed_only_by_authority_service() -> None:
    findings: list[str] = []
    allowed = SOURCE_ROOT / "governance/authority.py"
    for path in _python_sources():
        if path == allowed:
            continue
        findings.extend(
            _authority_context_findings(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
        )

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
    assert "submit_for_review" not in active_public
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


def test_private_fixture_mutators_have_no_external_production_callers() -> None:
    defining_modules = {
        "_LegacyProposalApplyEngine": {SOURCE_ROOT / "proposals/apply.py"},
        "_approve_legacy_proposal": {
            SOURCE_ROOT / "proposals/apply.py",
            SOURCE_ROOT / "roadmaps/service.py",
        },
        "_apply_legacy_fixture": {SOURCE_ROOT / "proposals/apply.py"},
        "_apply_locked": {SOURCE_ROOT / "roadmaps/service.py"},
        "_apply_legacy_proposal": {SOURCE_ROOT / "roadmaps/service.py"},
        "_save_legacy_fixture": {
            SOURCE_ROOT / "proposals/apply.py",
            SOURCE_ROOT / "roadmaps/service.py",
        },
    }
    findings: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            symbol: str | None = None
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported = alias.name.rsplit(".", 1)[-1]
                    if imported in defining_modules and path not in defining_modules[imported]:
                        findings.append(f"{path}:{node.lineno}:{imported}")
                continue
            if isinstance(node, ast.Name):
                symbol = node.id
            elif isinstance(node, ast.Attribute):
                symbol = node.attr
            elif isinstance(node, ast.Call):
                symbol = _reflective_symbol(node)
            if symbol not in defining_modules:
                continue
            allowed = defining_modules[symbol]
            if path not in allowed:
                findings.append(f"{path}:{node.lineno}:{symbol}")
    assert findings == []


@pytest.mark.parametrize(
    "source",
    (
        "from amplai_foundry.governance.models import AuthorityContext as AC\nAC()\n",
        "from amplai_foundry.governance.models import AuthorityContext\n"
        "AC = AuthorityContext\nAC()\n",
        "import amplai_foundry.governance.models as models\n"
        'getattr(models, "AuthorityContext")()\n',
    ),
)
def test_architecture_name_resolution_detects_authority_aliases(source: str) -> None:
    assert _authority_context_findings(source, filename="synthetic.py")


def test_private_fixture_guard_detects_alias_and_reflective_access() -> None:
    private_symbols = {"_approve_legacy_proposal", "_save_legacy_fixture"}
    source = (
        "from amplai_foundry.proposals.apply import "
        "_approve_legacy_proposal as approve\n"
        "approve(None)\n"
        'getattr(repository, "_save_legacy_fixture")(None)\n'
    )
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update(
                alias.name.rsplit(".", 1)[-1]
                for alias in node.names
                if alias.name.rsplit(".", 1)[-1] in private_symbols
            )
        elif isinstance(node, ast.Call):
            reflected = _reflective_symbol(node)
            if reflected in private_symbols:
                found.add(reflected)
    assert found == private_symbols


def test_cli_intake_does_not_expose_authority_selector_options() -> None:
    tree = ast.parse((SOURCE_ROOT / "cli.py").read_text(encoding="utf-8"))
    command = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "intake_process_command"
    )
    parameters = {argument.arg for argument in command.args.args}
    assert parameters.isdisjoint(
        {"provider_installation_ref", "external_actor_id", "governance_db"}
    )


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
