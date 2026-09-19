"""R1 repairs: real Context consumers must retain their governing inputs."""

from __future__ import annotations

import hashlib
import json

import pytest

from ai import test_document_lifecycle as docs

engine = docs.engine


INSTRUCTIONS = ("AGENTS.md", ".ai-team/runtime/WORKFLOW.md", ".ai-team/verifiers/registry.json")
LEDGER = "docs/history/DECISIONS.md"


def governed_repository(tmp_path):
    root = docs.repository(tmp_path)
    docs.prepare_loop(root)
    for path in INSTRUCTIONS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n" if path.endswith(".json") else "# Existing governing rule\n")
    target = root / LEDGER
    target.parent.mkdir(parents=True)
    target.write_text(
        "# History\n\n## D-001\nCurrent rule.\n\n## D-002\nRetained superseded rule.\n"
    )
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"].update(
        current_release_id=None,
        roots=["docs/current"],
        optional_roots=["docs/current"],
        governing_inputs=[
            {"path": path, "role": "instruction", "security": "INTERNAL"} for path in INSTRUCTIONS
        ]
        + [{"path": LEDGER, "role": "decision_ledger", "security": "INTERNAL"}],
    )
    docs.put_json(policy_path, policy)
    docs.put_json(
        root / ".ai-team/knowledge/map.json",
        {
            "selection": {"max_sources": 24},
            "sources": [
                {"id": "instruction-" + str(i), "path": path, "status": "active", "priority": 100}
                for i, path in enumerate(INSTRUCTIONS)
            ]
            + [{"id": "whole-history", "path": LEDGER, "status": "active", "priority": 1000}],
        },
    )
    docs.put_json(
        root / ".ai-team/knowledge/decisions.index.json",
        {
            "entries": [
                {
                    "id": "D-001",
                    "status": "active",
                    "title": "Current rule",
                    "path": LEDGER,
                    "scope": ["src"],
                },
                {
                    "id": "D-002",
                    "status": "superseded",
                    "title": "OLD_PRIVATE_CANARY",
                    "path": LEDGER,
                    "scope": ["src"],
                },
                {
                    "id": "D-003",
                    "status": "active",
                    "title": "UNRELATED_CANARY",
                    "path": "absent/unrelated.md",
                    "scope": ["unrelated"],
                },
                {
                    "id": "D-004",
                    "status": "active",
                    "title": "PRIVATE_CANARY",
                    "path": LEDGER,
                    "scope": ["src"],
                    "security": "RESTRICTED",
                },
            ]
        },
    )
    return root


def build(root):
    result = docs.loop_command(root, "context", "build", "specs/canary", "--force")
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def validate(root):
    return docs.loop_command(root, "context", "validate", "specs/canary/context-pack.json")


def rehash(value):
    clean = {k: v for k, v in value.items() if k != "content_hash"}
    # This deliberately uses the public runtime's established Context hash format.
    body = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["content_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_governing_inputs_survive_no_release_without_promoting_history(tmp_path):
    root = governed_repository(tmp_path)
    value = build(root)
    assert {x["path"] for x in value["required_knowledge"]} == set(INSTRUCTIONS)
    assert [x["id"] for x in value["active_decisions"]] == ["D-001"]
    assert (
        value["active_decisions"][0]["source_sha256"]
        == hashlib.sha256((root / LEDGER).read_bytes()).hexdigest()
    )
    assert "PRIVATE_CANARY" not in json.dumps(value)
    assert "UNRELATED_CANARY" not in json.dumps(value)
    assert validate(root).returncode == 0


@pytest.mark.parametrize(
    "mutation", ["missing_decision", "forged_decision", "missing_instruction", "forged_reason"]
)
def test_context_rejects_self_consistent_missing_or_forged_authority(tmp_path, mutation):
    root = governed_repository(tmp_path)
    value = build(root)
    if mutation == "missing_decision":
        value["active_decisions"] = []
    elif mutation == "forged_decision":
        value["active_decisions"][0]["id"] = "NOT_IN_ACTIVE_INDEX"
    elif mutation == "missing_instruction":
        value["required_knowledge"].pop()
    else:
        value["active_decisions"][0]["reason"] = "FORGED_PRIVATE_REASON"
    rehash(value)
    docs.put_json(root / "specs/canary/context-pack.json", value)
    result = validate(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FORGED_PRIVATE_REASON" not in result.stdout + result.stderr
    assert "NOT_IN_ACTIVE_INDEX" not in result.stdout + result.stderr


def test_existing_guide_backed_decision_cannot_be_removed_with_recomputed_hash(engine, tmp_path):
    root = docs.repository(tmp_path)
    docs.document(root)
    docs.reviewed(engine, root)
    docs.prepare_loop(root)
    docs.put_json(
        root / ".ai-team/knowledge/decisions.index.json",
        {
            "entries": [
                {
                    "id": "CURRENT",
                    "status": "active",
                    "title": "Existing indexed authority",
                    "path": "docs/guide.md",
                },
            ]
        },
    )
    value = build(root)
    assert len(value["active_decisions"]) == 1
    value["active_decisions"] = []
    rehash(value)
    docs.put_json(root / "specs/canary/context-pack.json", value)
    assert validate(root).returncode == 1


@pytest.mark.parametrize(
    "mutation",
    ["missing_file", "missing_map", "inactive_map", "over_limit", "unclassified_decision"],
)
def test_unavailable_required_governance_fails_closed(tmp_path, mutation):
    root = governed_repository(tmp_path)
    if mutation == "missing_file":
        (root / INSTRUCTIONS[0]).unlink()  # Disposable fixture only.
    elif mutation == "unclassified_decision":
        path = root / ".ai-team/policy/documentation.json"
        policy = json.loads(path.read_text())
        policy["portable_documents"]["governing_inputs"].pop()
        docs.put_json(path, policy)
    else:
        path = root / ".ai-team/knowledge/map.json"
        value = json.loads(path.read_text())
        if mutation == "missing_map":
            value["sources"].pop(0)
        elif mutation == "inactive_map":
            value["sources"][0]["status"] = "superseded"
        else:
            value["selection"]["max_sources"] = 1
        docs.put_json(path, value)
    result = docs.loop_command(root, "context", "build", "specs/canary")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "GOVERNING_INPUT_UNAVAILABLE" in result.stdout
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    assert not (root / "specs/canary/context-pack.json").exists()


@pytest.mark.parametrize("change", ["policy", "source", "index"])
def test_governing_control_input_drift_invalidates_context(tmp_path, change):
    root = governed_repository(tmp_path)
    build(root)
    if change == "source":
        path = root / LEDGER
        path.write_text(path.read_text() + "\nChanged rule.\n")
    elif change == "policy":
        path = root / ".ai-team/policy/documentation.json"
        value = json.loads(path.read_text())
        value["portable_documents"]["governing_inputs"][-1]["security"] = "RESTRICTED"
        docs.put_json(path, value)
    else:
        path = root / ".ai-team/knowledge/decisions.index.json"
        value = json.loads(path.read_text())
        value["entries"][0]["status"] = "superseded"
        docs.put_json(path, value)
    result = validate(root)
    assert result.returncode == (2 if change == "policy" else 1)
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "locator,eligible", [("D-001", True), ("D-002", False), ("D-404", False), (None, False)]
)
def test_historical_claim_evidence_requires_exact_active_index_entry(tmp_path, locator, eligible):
    root = governed_repository(tmp_path)
    evidence = {"type": "decision", "path": LEDGER}
    if locator is not None:
        evidence["locator"] = locator
    claim = {"id": "typed-claim", "status": "active", "scope": ["src"], "evidence": [evidence]}
    (root / ".ai-team/knowledge/claims.jsonl").write_text(json.dumps(claim) + "\n")
    value = build(root)
    assert ("typed-claim" in {x["id"] for x in value["required_knowledge"]}) is eligible
    assert validate(root).returncode == 0


def test_private_governing_path_cannot_be_lowered_by_index_security(tmp_path):
    root = governed_repository(tmp_path)
    path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(path.read_text())
    policy["portable_documents"]["governing_inputs"][-1]["security"] = "RESTRICTED"
    docs.put_json(path, policy)
    result = docs.loop_command(root, "context", "build", "specs/canary")
    assert result.returncode == 2
    assert "GOVERNING_INPUT_UNAVAILABLE" in result.stdout
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr


def test_forged_claim_cannot_use_ledger_as_plain_source(tmp_path):
    root = governed_repository(tmp_path)
    path = root / ".ai-team/knowledge/claims.jsonl"
    path.write_text(
        json.dumps({"id": "whole-ledger", "status": "active", "evidence": [{"path": LEDGER}]})
        + "\n"
    )
    value = build(root)
    assert {x["path"] for x in value["required_knowledge"]} == set(INSTRUCTIONS)
    assert validate(root).returncode == 0


@pytest.mark.parametrize("metadata_change", [{"security": "RESTRICTED"}, {"lifecycle": "archived"}])
def test_control_role_cannot_override_explicit_source_security_or_retirement(
    tmp_path, metadata_change
):
    root = governed_repository(tmp_path)
    docs.document(root, "AGENTS.md", sidecar=True, **metadata_change)
    result = docs.loop_command(root, "context", "build", "specs/canary")
    assert result.returncode == 2
    assert "GOVERNING_INPUT_UNAVAILABLE" in result.stdout
