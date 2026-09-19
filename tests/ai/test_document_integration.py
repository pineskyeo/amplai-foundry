"""S08 cross-slice probes; all writes and retirement run in disposable fixtures."""

from __future__ import annotations

import json
import os
import re

import pytest

from ai import test_document_preservation as preservation
from ai import test_document_review as review

runtime = review.runtime


def classify_fixture_inputs(root, *paths):
    """Positive fixtures declare source security without weakening freshness checks."""
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    routes = policy.setdefault("document_source_routes", [])
    for path in ("src/api.py", *paths):
        if not any(row["path"] == path for row in routes):
            routes.append(
                {
                    "path": path,
                    "kind": "historical",
                    "security": "INTERNAL",
                    "reason": "Synthetic internal source/evidence for this finite fixture.",
                }
            )
    policy_path.write_text(json.dumps(policy))


def test_impact_excludes_existing_work_memory_before_parsing_raw_review_examples(runtime, tmp_path):
    root = review.fixture(tmp_path)
    report = root / "specs/older/review.md"
    report.parent.mkdir()
    original = b'# Retained review\n<img srcset="PRIVATE_CANARY.md 1x">\n'
    report.write_bytes(original)
    result = review.change(runtime, root)
    assert result["scan_complete"] is True
    assert "specs/older/review.md" in result["completeness"]["excluded"]
    assert all(row["path"] != "specs/older/review.md" for row in result["impacted_documents"])
    assert report.read_bytes() == original
    reviewed = runtime.docs_review_batch(str(root), "specs/canary", review.request(result))
    assert reviewed["status"] == "RESOLVED"
    assert runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert report.read_bytes() == original


def test_default_back_reference_scan_does_not_implicitly_skip_work_paths(runtime, tmp_path):
    root = review.fixture(tmp_path)
    report = root / "specs/older/review.md"
    report.parent.mkdir()
    report.write_text('<img srcset="PRIVATE_CANARY.md 1x">\n')
    with pytest.raises(runtime.amplai_docs.DocumentError, match="UNSUPPORTED_PARSER"):
        runtime.amplai_docs.docs_referencing(root, ["src/api.py"])


def test_back_reference_candidates_do_not_retain_quadratic_declaration_context(runtime, tmp_path):
    root = review.fixture(tmp_path)
    notes = root / "notes"
    notes.mkdir()
    (notes / "extensionless").write_text("Changed local source.\n")
    document = notes / "history.md"
    original = "`unrelated` " * 1000 + "Requires the file `extensionless`.\n"
    document.write_text(original)
    found = runtime.amplai_docs.docs_referencing(root, ["notes/extensionless"])
    assert found["notes/history.md"] == ["notes/extensionless"]
    assert document.read_text() == original
    # Exact dependency extraction still retains declaration context and budgets;
    # the lean candidate pass is not permitted to replace that validation.
    with pytest.raises(runtime.amplai_docs.DocumentError, match="SCOPE_INCOMPLETE"):
        runtime.amplai_docs.extract_typed_references(original)


def test_candidate_discovery_keeps_literal_names_when_legacy_glob_rejects_notation(
    runtime, tmp_path, monkeypatch
):
    root = review.fixture(tmp_path)
    notes = root / "notes"
    notes.mkdir()
    (notes / "state[out-of-repo]").write_text("Literal bracketed name.\n")
    (notes / "extensionless").write_text("A second local dependency.\n")
    (notes / "history.md").write_text("Use `state[out-of-repo]` and `extensionless`.\n")
    original_match = runtime.amplai_docs.path_matches

    def legacy_match(path, pattern):
        if "[out-of-repo]" in pattern:
            raise re.error("bad character range t-o")
        return original_match(path, pattern)

    monkeypatch.setattr(runtime.amplai_docs, "path_matches", legacy_match)
    found = runtime.amplai_docs.docs_referencing(
        root, ["notes/state[out-of-repo]", "notes/extensionless", "src/api.py"]
    )
    assert found["notes/history.md"] == ["notes/extensionless", "notes/state[out-of-repo]"]


def test_candidate_discovery_retains_local_links_beside_an_opaque_origin(runtime, tmp_path):
    root = review.fixture(tmp_path)
    (root / "notes").mkdir()
    source = root / "notes/source.md"
    original = "[Origin](conversation-canary://synthetic-id) and [local](../src/api.py).\n"
    source.write_text(original)
    found = runtime.amplai_docs.docs_referencing(root, ["src/api.py"])
    assert found["notes/source.md"] == ["src/api.py"]
    assert source.read_text() == original
    with pytest.raises(runtime.amplai_docs.DocumentError, match="UNSUPPORTED_PARSER"):
        runtime.amplai_docs.extract_typed_references(original)


def test_opaque_origin_candidate_does_not_approve_a_current_guide(runtime, tmp_path):
    root = review.fixture(tmp_path)
    guide = root / "docs/guide.md"
    guide.write_text(guide.read_text() + "\n[Origin](conversation-canary://synthetic-id)\n")
    with pytest.raises(runtime.amplai_docs.DocumentError, match="UNSUPPORTED_PARSER"):
        review.change(runtime, root)


@pytest.mark.parametrize("path", ["docs/guide.md", "notes.md", "specs-not-work/review.md"])
def test_impact_role_ordering_keeps_current_and_unknown_sources_strict(runtime, tmp_path, path):
    root = review.fixture(tmp_path)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if path == "docs/guide.md":
        target.write_text(target.read_text() + '\n<img srcset="PRIVATE_CANARY.md 1x">\n')
    else:
        target.write_text('<img srcset="PRIVATE_CANARY.md 1x">\n')
    with pytest.raises(runtime.amplai_docs.DocumentError, match="UNSUPPORTED_PARSER") as caught:
        review.change(runtime, root)
    assert "PRIVATE" not in str(caught.value)


def test_recovery_preserves_mode_under_restrictive_umask(runtime, tmp_path):
    root = preservation.fixture(tmp_path)
    engine = runtime.amplai_docs
    source = root / "docs/legacy.md"
    source.chmod(0o644)
    original = source.read_bytes()
    plan = engine.retirement_plan(root, preservation.request(engine, root))
    backup = tmp_path / "quarantine"
    engine._retire_approved(
        root,
        plan,
        backup,
        authorize=preservation.authorize(engine, root, plan, backup),
        verify=preservation.verifier,
    )
    previous_umask = os.umask(0o077)
    try:
        recovered = engine._recover_retirement(root, plan, backup, verify=preservation.verifier)
    finally:
        os.umask(previous_umask)
    assert recovered["status"] == "RECOVERED"
    assert source.read_bytes() == original
    assert source.stat().st_mode & 0o777 == 0o644


def test_same_source_path_across_revisions_cannot_erase_first_read_observation(
    runtime, tmp_path, monkeypatch
):
    root = preservation.fixture(tmp_path)
    engine = runtime.amplai_docs
    source = root / "docs/legacy.md"
    first_bytes = source.read_bytes()
    first = preservation.sources(root)[0]
    second_bytes = b"# Different contract\nPreserve both original revisions.\n"
    source.write_bytes(second_bytes)
    review.git(root, "add", "docs/legacy.md")
    review.git(root, "commit", "--quiet", "-m", "Second original revision")
    second = preservation.sources(root)[0]
    source.write_bytes(first_bytes)
    original_read = engine.SourceTree.read
    observed = []

    def concurrent_read(tree, path, *args, **kwargs):
        result = original_read(tree, path, *args, **kwargs)
        if path == "docs/legacy.md":
            observed.append(result)
            if len(observed) == 1:
                source.write_bytes(second_bytes)
        return result

    monkeypatch.setattr(engine.SourceTree, "read", concurrent_read)
    with pytest.raises(ValueError, match="SOURCE_DRIFT"):
        engine.preservation_ledger(root, [first, second])


def test_request_spec_plan_memory_family_preserves_every_unique_section(runtime, tmp_path):
    root = preservation.fixture(tmp_path)
    engine = runtime.amplai_docs
    originals = {
        "docs/REQUEST.md": "# User Need\nKeep independently deployable apps.\n",
        "docs/SPEC.md": "# Invariant\nExisting response fields stay unchanged.\n",
        "docs/PLAN.md": "# Recovery\nRestore the exact prior artifact before retry.\n",
        ".ai-team/memory/lessons-learned.md": (
            "# Incident\nA stale source selection once bypassed review.\n\n"
            "## Constraint\nDo not infer deletion permission from age or Git history.\n"
        ),
    }
    for path, text in originals.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Original document family")
    revision = review.git(root, "rev-parse", "HEAD")
    sources = [
        {
            "repository_id": "canary",
            "commit": revision,
            "path": path,
            "doc_id": "canary:original-" + str(index),
        }
        for index, path in enumerate(originals)
    ]
    retained = engine.preservation_ledger(root, sources)
    assert retained["complete"] is True
    assert len(retained["sections"]) == 5
    assert {row["source"]["path"] for row in retained["sections"]} == set(originals)
    mappings = []
    for index, row in enumerate(retained["sections"]):
        data = (root / row["source"]["path"]).read_bytes().splitlines(keepends=True)
        body = b"".join(data[row["line_start"] - 1 : row["line_end"]])
        target = "docs/family-retained-" + str(index) + ".md"
        (root / target).write_bytes(body)
        mappings.append(
            {
                "section_id": row["section_id"],
                "action": "copied",
                "destination": {
                    "path": target,
                    "doc_id": "canary:retained-" + str(index),
                    "line_start": 1,
                    "line_end": len(body.splitlines()),
                },
                "reason": "Exact original constraint or incident retained, not summarized.",
                "reviewer": "fixture-owner",
                "method": "complete original-byte comparison",
                "evidence": ["evidence/review.txt"],
            }
        )
    for path in originals:
        (root / path).write_text("# Moved\nSee the lossless preservation ledger.\n")
    copied = engine.preservation_ledger(root, sources, mappings)
    assert copied["complete"] is True
    assert all(row["action"] == "copied" for row in copied["sections"])
    omitted = engine.preservation_ledger(root, sources, mappings[:-1])
    assert omitted["complete"] is False
    assert omitted["unprocessed_sections"]


def role_fixture(runtime, tmp_path):
    root = review.fixture(tmp_path)
    # Only the known current owner is selected. Other sources retain explicit roles.
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"]["roots"] = ["docs/guide.md"]
    policy["document_source_routes"] = [
        {
            "path": "tools/templates",
            "kind": "source_asset",
            "owner": "docs/guide.md",
            "security": "INTERNAL",
            "reason": "Installed templates are inputs to the current guide.",
        },
        {
            "path": "docs/history",
            "kind": "historical",
            "security": "INTERNAL",
            "reason": (
                "Retain the original incident record without rewriting it as current guidance."
            ),
        },
    ]
    policy_path.write_text(json.dumps(policy))
    for path in ("tools/templates/work.md", "docs/history/incident.md"):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Preserved source\nExplains `src/api.py`.\n")
    classify_fixture_inputs(root)
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Explicit document source roles")
    return root


def test_current_guide_review_routes_source_assets_and_retains_history(runtime, tmp_path):
    root = role_fixture(runtime, tmp_path)
    original = (root / "docs/history/incident.md").read_bytes()
    report = review.change(runtime, root)
    assert [d["path"] for d in report["impacted_documents"]] == ["docs/guide.md"]
    records = report["source_role_records"]
    assert {r["kind"] for r in records} == {"historical", "source_asset"}
    assert all(r["source"]["sha256"] for r in records)
    done = runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    assert done["status"] == "RESOLVED"
    assert (root / "docs/history/incident.md").read_bytes() == original
    (root / "tools/templates/work.md").write_text("# Changed source\nExplains `src/api.py`.\n")
    assert runtime.docs_impact(str(root), "specs/canary")["status"] == "STALE"


@pytest.mark.parametrize("fault", ["mandatory", "missing_owner", "private", "unclassified"])
def test_source_roles_cannot_bypass_mandatory_owner_security_or_unknown_metadata(
    runtime, tmp_path, fault
):
    root = role_fixture(runtime, tmp_path)
    path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(path.read_text())
    if fault == "mandatory":
        policy["impact_rules"] = [
            {
                "id": "required-history",
                "when_changed": ["src/**"],
                "impacted": ["docs/history/incident.md"],
            }
        ]
    elif fault == "missing_owner":
        policy["document_source_routes"][0]["owner"] = "docs/missing.md"
    elif fault == "private":
        policy["document_source_routes"][0]["security"] = "RESTRICTED"
    else:
        policy["document_source_routes"] = []
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        review.change(runtime, root)


def test_external_executable_symlink_is_not_read_or_treated_as_verified_dependency(
    runtime, tmp_path
):
    root = review.fixture(tmp_path)
    outside = tmp_path / "external-runtime"
    outside.write_text("EXTERNAL_PRIVATE_CANARY\n")
    link = root / ".venv/bin/python"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    (root / "notes.md").write_text("Local command example: `.venv/bin/python`. `src/api.py`.\n")
    # Back-reference discovery may inspect literal text; it must not dereference a host binary.
    found = runtime.amplai_docs.docs_referencing(root, ["src/api.py"])
    assert "notes.md" in found
    guide = root / "docs/guide.md"
    body = guide.read_text().replace("# Guide", "# Guide\nRequires: `.venv/bin/python`.")
    guide.write_text(body)
    with pytest.raises(
        ValueError, match=r"EXTERNAL_REFERENCE_UNAVAILABLE|DEPENDENCY_MISSING|UNSAFE_PATH"
    ):
        runtime.amplai_docs.read_document(root, "docs/guide.md")


def test_changed_history_is_not_silently_accepted_as_preserved(runtime, tmp_path):
    root = role_fixture(runtime, tmp_path)
    (root / "docs/history/incident.md").write_text("# Rewritten history\nExplains `src/api.py`.\n")
    with pytest.raises(ValueError, match="HISTORICAL_SOURCE_CHANGED"):
        review.change(runtime, root)


def test_ignored_new_source_asset_invalidates_owner_selection(runtime, tmp_path):
    root = role_fixture(runtime, tmp_path)
    report = review.change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    (root / ".git/info/exclude").write_text("tools/templates/local.md\n")
    (root / "tools/templates/local.md").write_text("# New ignored source\n")
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0


@pytest.mark.parametrize("prefix", ["$PROJECT_ROOT", "${PROJECT_ROOT}"])
def test_symbolic_tree_root_is_template_unless_explicitly_required(runtime, prefix):
    engine = runtime.amplai_docs
    reference = {
        "reference": prefix + "/project.json",
        "raw_reference": prefix + "/project.json",
        "source_kind": "tree",
        "line": 3,
        "prefix": "",
        "context": "",
    }
    classified = engine.classify_document_reference(reference, {"docs"})
    assert classified["kind"] == "template_path"
    assert classified["required"] is False
    reference["prefix"] = "Requires "
    required = engine.classify_document_reference(reference, {"docs"})
    assert required["required"] is True
    assert required["kind"] != "template_path"


def test_work_folder_hint_cannot_depend_on_its_own_review_projection(runtime, tmp_path):
    root = review.fixture(tmp_path)
    classify_fixture_inputs(root, "specs")
    review.docs.document(
        root, body="# Guide\nUses `src/api.py`.\nWork evidence is kept in `specs/`.\n"
    )
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Folder hint before actual feature change")
    report = review.change(runtime, root)
    assert report["status"] == "STALE"
    done = runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    assert done["status"] == "RESOLVED"
    canonical_review = (root / ".ai-team/knowledge/document-reviews.json").read_bytes()
    (root / "specs/canary/after-review.json").write_text('{"observation":"retained"}\n')
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    assert runtime.docs_validate(str(root), "specs/canary")["errors"] == ["DERIVED_REPORT_DRIFT"]
    assert runtime.docs_impact(str(root), "specs/canary")["status"] == "RESOLVED"
    assert runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert (root / ".ai-team/knowledge/document-reviews.json").read_bytes() == canonical_review


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_explicit_work_inputs_still_bind_file_bytes_and_full_directory_membership(
    runtime, tmp_path, kind
):
    root = review.fixture(tmp_path)
    classify_fixture_inputs(root, "specs/canary/inputs")
    inputs = root / "specs/canary/inputs"
    inputs.mkdir()
    (inputs / "model.json").write_text('{"version":1}\n')
    target = "specs/canary/inputs/model.json" if kind == "file" else "specs/canary/inputs/"
    review.docs.document(root, body="# Guide\nUses `src/api.py`.\nRequires `" + target + "`.\n")
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Explicit work input is not a folder hint")
    report = review.change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    (inputs / ("model.json" if kind == "file" else "added.json")).write_text('{"version":2}\n')
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


@pytest.mark.parametrize("name", ["last.json", "history.jsonl", "verifier-last.json"])
def test_named_evaluator_outputs_do_not_change_candidate_but_unknown_files_do(
    runtime, tmp_path, name
):
    root = review.fixture(tmp_path)
    classify_fixture_inputs(root)
    directory = root / ".specify/eval"
    directory.mkdir(parents=True)
    output = directory / name
    output.write_text('{"fixture_run":1}\n')
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Known evaluator output baseline")
    report = review.change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    output.write_text('{"fixture_run":2}\n')
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    refreshed = runtime.docs_impact(str(root), "specs/canary")
    assert refreshed["status"] == "RESOLVED"
    assert name in refreshed["completeness"]["excluded"][".specify/eval/" + name]
    (directory / "unexpected.py").write_text("SOURCE_CHANGE = True\n")
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0


@pytest.mark.parametrize("kind", ["tree", "required_directory"])
@pytest.mark.parametrize(
    "store_path",
    [".ai-team/knowledge/document-reviews.json", ".ai-team/records/semantic-reviews.json"],
)
def test_directory_structure_does_not_depend_on_its_own_review_transaction(
    runtime, tmp_path, kind, store_path
):
    root = review.fixture(tmp_path)
    engine = runtime.amplai_docs
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"]["review_store"] = store_path
    policy_path.write_text(json.dumps(policy))
    directory = store_path.rsplit("/", 1)[0]
    classify_fixture_inputs(root, directory)
    (root / directory).mkdir(parents=True, exist_ok=True)
    parent, leaf = directory.rsplit("/", 1)
    declaration = (
        "```text\n" + parent + "/\n└── " + leaf + "/\n```\n"
        if kind == "tree"
        else "Requires `" + directory + "/`.\n"
    )
    assert any(
        reference["reference"] == directory + "/" and reference["required"]
        for reference in (
            engine.classify_document_reference(item, {".ai-team"})
            for item in engine.extract_typed_references(declaration)
        )
    )
    review.docs.document(root, body="# Guide\nUses `src/api.py`.\n" + declaration)
    review.git(root, "add", "--all")
    review.git(root, "commit", "--quiet", "-m", "Review store directory is structural")
    report = review.change(runtime, root)
    result = runtime.docs_review_batch(str(root), "specs/canary", review.request(report))
    assert result["status"] == "RESOLVED"
    assert engine.select_documents(root, "release-2")["count"] == 1
    assert runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert (root / store_path).is_file()
    assert not list((root / directory).glob("*.pending-*"))
    # The exclusion is an exact output role, not permission to miss other inputs.
    unknown = directory + "/new-required-input.json"
    (root / ".git/info/exclude").write_text(unknown + "\n")
    (root / unknown).write_text('{"source_change":true}\n')
    assert engine.select_documents(root, "release-2")["count"] == 0
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_explicit_review_store_file_reference_still_binds_its_bytes(runtime, tmp_path):
    root = review.fixture(tmp_path)
    engine = runtime.amplai_docs
    store_path = ".ai-team/knowledge/document-reviews.json"
    target = root / store_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '{"schema_version":"1.0","reviews":[],"history":[],"reference_reviews":[],"reference_history":[]}\n'
    )
    body = "# Guide\nRequires `" + store_path + "`.\n"
    with engine.SourceTree(root) as tree:
        config, _ = engine.configuration(tree)
        before = engine.declared_reference_snapshot(tree, "docs/guide.md", body, config)
    target.write_text(target.read_text() + "\n")
    with engine.SourceTree(root) as tree:
        config, _ = engine.configuration(tree)
        after = engine.declared_reference_snapshot(tree, "docs/guide.md", body, config)
    assert before != after
