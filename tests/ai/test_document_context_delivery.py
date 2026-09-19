"""R5 repair: selected guides must reach the real generated and stored Work Context."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from ai import test_document_governance as governance
from ai import test_document_lifecycle as docs

engine = docs.engine
GUIDES = tuple(f"docs/current/guide-{i}.md" for i in range(3))
RELEASE = "release-2"
VERIFIER = docs.ROOT / "specs/012-portable-document-lifecycle/verify-kit-guide-selection.py"
PRIVATE = "PRIVATE_CONTEXT_DELIVERY_CANARY"


def prepared(engine, tmp_path):
    root = governance.governed_repository(tmp_path)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"]["current_release_id"] = RELEASE
    docs.put_json(policy_path, policy)
    map_path = root / ".ai-team/knowledge/map.json"
    mapping = json.loads(map_path.read_text())
    for index, path in enumerate(GUIDES):
        docs.document(
            root,
            path,
            doc_id=f"canary:guide-{index}",
            topic_id=f"canary:topic-{index}",
        )
        mapping["sources"].append(
            {
                "id": f"guide-{index}",
                "path": path,
                "memory_type": "stable_knowledge",
                "status": "active",
                "priority": 90,
                "keywords": ["portable"],
            }
        )
        docs.reviewed(engine, root, path)
    docs.put_json(map_path, mapping)
    return root


def verify(root):
    # No imports or sys.path edits leak into the shared pytest process. The actual
    # verifier and both actual consumer modules execute in a fresh stdlib-only process.
    program = """
import json, runpy, sys
from pathlib import Path
namespace = runpy.run_path(sys.argv[1])
source_root = Path(sys.argv[1]).resolve().parents[2]
for name in ('docs', 'loopv2'):
    filename = 'amplai_docs.py' if name == 'docs' else 'loopv2.py'
    assert Path(namespace[name].__file__).resolve() == source_root / 'scripts' / filename
try:
    report = namespace['verify_context_delivery'](
        Path(sys.argv[2]), Path(sys.argv[2]) / 'specs/canary',
        set(json.loads(sys.argv[3])), sys.argv[4],
    )
except AssertionError as error:
    print(json.dumps({'verdict': 'REJECTED', 'reason': str(error)}))
    sys.exit(1)
print(json.dumps({'verdict': 'PASS', 'delivery': report}))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AMPLAI_", "PYTHON"))}
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            program,
            str(VERIFIER),
            str(root),
            json.dumps(GUIDES),
            RELEASE,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def rejected(result):
    assert result.returncode == 1, result.stdout + result.stderr
    assert json.loads(result.stdout)["verdict"] == "REJECTED"
    assert result.stderr == ""
    assert PRIVATE not in result.stdout


def test_real_generated_and_stored_context_delivers_exact_guide_and_instruction_bytes(
    engine, tmp_path
):
    root = prepared(engine, tmp_path)
    value = governance.build(root)
    assert {row["path"] for row in value["required_knowledge"]} == set(GUIDES) | set(
        governance.INSTRUCTIONS
    )
    for row in value["required_knowledge"]:
        assert row["source_sha256"] == hashlib.sha256((root / row["path"]).read_bytes()).hexdigest()
    result = verify(root)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)["delivery"]
    assert report["context_hash"] == value["content_hash"]
    assert report["context_validation"] == "MATCH"
    assert report["generated_records"] == report["stored_records"] == 6
    assert report["guides"] == sorted(GUIDES)
    assert report["required_instructions"] == sorted(governance.INSTRUCTIONS)


@pytest.mark.parametrize("path", GUIDES)
def test_selector_and_normal_match_cannot_hide_a_missing_guide_mapping(engine, tmp_path, path):
    root = prepared(engine, tmp_path)
    target = root / ".ai-team/knowledge/map.json"
    mapping = json.loads(target.read_text())
    mapping["sources"] = [row for row in mapping["sources"] if row["path"] != path]
    docs.put_json(target, mapping)
    value = governance.build(root)
    contract = json.loads((root / "specs/canary/work-contract.json").read_text())
    selection = engine.context_selection(root, contract)
    assert {row["path"] for row in selection["documents"]} == set(GUIDES)
    assert path not in {row["path"] for row in value["required_knowledge"]}
    assert governance.validate(root).returncode == 0  # The precise R5 blind spot.
    rejected(verify(root))


@pytest.mark.parametrize("path", GUIDES + governance.INSTRUCTIONS)
def test_rehashed_stored_context_cannot_omit_a_guide_or_instruction(engine, tmp_path, path):
    root = prepared(engine, tmp_path)
    value = governance.build(root)
    value["required_knowledge"] = [
        row for row in value["required_knowledge"] if row["path"] != path
    ]
    governance.rehash(value)
    docs.put_json(root / "specs/canary/context-pack.json", value)
    rejected(verify(root))


@pytest.mark.parametrize("path", GUIDES + governance.INSTRUCTIONS)
def test_rehashed_stored_context_cannot_forge_current_source_identity(engine, tmp_path, path):
    root = prepared(engine, tmp_path)
    value = governance.build(root)
    next(row for row in value["required_knowledge"] if row["path"] == path)["source_sha256"] = (
        "f" * 64
    )
    governance.rehash(value)
    docs.put_json(root / "specs/canary/context-pack.json", value)
    rejected(verify(root))


@pytest.mark.parametrize("path", governance.INSTRUCTIONS)
def test_actual_instruction_mapping_drift_is_denied(engine, tmp_path, path):
    root = prepared(engine, tmp_path)
    governance.build(root)
    target = root / ".ai-team/knowledge/map.json"
    mapping = json.loads(target.read_text())
    mapping["sources"] = [row for row in mapping["sources"] if row["path"] != path]
    docs.put_json(target, mapping)
    rejected(verify(root))
    assert docs.loop_command(root, "context", "build", "specs/canary", "--force").returncode == 2


@pytest.mark.parametrize(
    "change",
    ["dependency", "source", "release", "policy_release", "review", "security", "lifecycle"],
)
def test_source_review_release_and_security_drift_cannot_deliver_old_context(
    engine, tmp_path, change
):
    root = prepared(engine, tmp_path)
    governance.build(root)
    if change == "dependency":
        (root / "src/api.py").write_text("VERSION = 2\n")
    elif change == "source":
        target = root / GUIDES[0]
        target.write_text(target.read_text() + "\n" + PRIVATE + "\n")
    elif change == "review":
        (root / "evidence/observation.txt").write_text("Changed evidence.\n")
    elif change == "policy_release":
        target = root / ".ai-team/policy/documentation.json"
        policy = json.loads(target.read_text())
        policy["portable_documents"]["current_release_id"] = "release-3"
        docs.put_json(target, policy)
    else:
        overrides = {
            "release": {"release_ids": ["release-3"]},
            "security": {"security": "RESTRICTED", "title": PRIVATE},
            "lifecycle": {"lifecycle": "archived"},
        }[change]
        docs.document(
            root, GUIDES[0], doc_id="canary:guide-0", topic_id="canary:topic-0", **overrides
        )
    rejected(verify(root))


@pytest.mark.parametrize("field", ["path", "id", "reason", "active_decisions", "content_hash"])
def test_forged_context_fields_do_not_bypass_real_validation_or_echo_values(
    engine, tmp_path, field
):
    root = prepared(engine, tmp_path)
    value = governance.build(root)
    if field == "content_hash":
        value["content_hash"] = "f" * 64
    else:
        if field == "active_decisions":
            value["active_decisions"][0]["reason"] = PRIVATE
        else:
            value["required_knowledge"][0][field] = PRIVATE
        governance.rehash(value)
    docs.put_json(root / "specs/canary/context-pack.json", value)
    rejected(verify(root))


def test_owning_foundry_guide_sources_are_registered_once_with_ordinary_priority():
    mapping = json.loads((docs.ROOT / ".ai-team/knowledge/map.json").read_text())
    expected = {
        "docs/PORTABLE-DEVELOPMENT.md",
        "tools/amplai-loop-kit/README.md",
        "tools/amplai-loop-kit/CHANGELOG.md",
    }
    rows = [row for row in mapping["sources"] if row["path"] in expected]
    assert {row["path"] for row in rows} == expected
    assert len(rows) == 3
    assert len({row["id"] for row in mapping["sources"]}) == len(mapping["sources"])
    assert all(row["status"] == "active" and row["priority"] == 90 for row in rows)
    assert mapping["selection"]["max_sources"] == 24
