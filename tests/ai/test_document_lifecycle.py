"""S03: actual portable document consumers, using disposable repository inputs."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/amplai_docs.py"


@pytest.fixture
def engine():
    assert SCRIPT.is_file(), "S03 requires the consumed document engine, not a plan-only registry"
    spec = importlib.util.spec_from_file_location("document_lifecycle_canary", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "src/api.py").write_text("VERSION = 1\n")
    put_json(
        root / ".ai-team/policy/documentation.json",
        {
            "schema_version": "1.0",
            "portable_documents": {
                "schema_version": "1.0",
                "repository_id": "canary",
                "roots": ["docs"],
                "review_store": ".ai-team/knowledge/document-reviews.json",
                "default_security": "INTERNAL",
                "maximum_security": "INTERNAL",
                "current_release_id": "release-2",
                "max_files": 1000,
                "max_file_bytes": 1048576,
            },
        },
    )
    put_json(
        root / ".ai-team/runtime/repository-profile.json",
        {"id": "generic", "schema_version": "1.0"},
    )
    return root


def metadata(path="docs/guide.md", **overrides):
    value = {
        "schema_version": "1.0",
        "doc_id": "canary:guide",
        "topic_id": "canary:runbook",
        "source": {"repository": "canary", "path": path},
        "title": "Current runbook",
        "owner": "operator",
        "kind": "runbook",
        "authority": "canonical",
        "lifecycle": "active",
        "security": "INTERNAL",
        "release_ids": ["release-2"],
        "audiences": ["operator"],
        "dependencies": [{"repository": "canary", "path": "src/api.py", "kind": "code"}],
        "retention": {"policy": "preserve", "hold": False},
    }
    value.update(overrides)
    if "legacy_status" in overrides and "lifecycle" not in overrides:
        value.pop("lifecycle", None)
    return value


def document(
    root,
    path="docs/guide.md",
    *,
    sidecar=False,
    body="# Guide\n\nSafe instructions.\n",
    **overrides,
):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    value = metadata(path, **overrides)
    if sidecar:
        target.write_text(body)
        put_json(root / (path + ".amplai.json"), {"amplai_document": value})
    else:
        target.write_text(
            "---\n" + json.dumps({"amplai_document": value}, sort_keys=True) + "\n---\n" + body
        )
    return value


def reviewed(engine, root, path="docs/guide.md", *, outcome="reviewed_unchanged"):
    record = engine.read_document(root, path)
    observation = root / "evidence/observation.txt"
    observation.parent.mkdir(exist_ok=True)
    observation.write_text(
        "Fixture reviewer compared the actual source behavior and document semantics.\n"
    )
    evidence = {
        "path": "evidence/observation.txt",
        "sha256": hashlib.sha256(observation.read_bytes()).hexdigest(),
    }
    review = {
        "doc_id": record["metadata"]["doc_id"],
        "snapshot": record["snapshot"],
        "outcome": outcome,
        "reason": "Fixture reviewer checked commands against the exact dependency bytes.",
        "reviewer": "fixture-reviewer",
        "method": "source and regression comparison",
        "evidence": [evidence],
    }
    store = root / ".ai-team/knowledge/document-reviews.json"
    value = (
        json.loads(store.read_text())
        if store.exists()
        else {"schema_version": "1.0", "reviews": []}
    )
    value["reviews"] = [x for x in value["reviews"] if x["doc_id"] != review["doc_id"]] + [review]
    put_json(store, value)
    return review


def selected(engine, root, **kwargs):
    return engine.select_documents(root, release_id="release-2", security="INTERNAL", **kwargs)


def test_new_frontmatter_and_legacy_sidecar_are_one_owner(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    document(
        root,
        "docs/legacy.md",
        sidecar=True,
        doc_id="canary:legacy",
        topic_id="canary:legacy",
        body="---\ntitle: legacy title\nstatus: ACTIVE\n---\n# Retained legacy text\n",
    )
    assert engine.read_document(root, "docs/guide.md")["metadata_owner"] == "frontmatter"
    legacy = engine.read_document(root, "docs/legacy.md")
    assert legacy["metadata_owner"] == "sidecar"
    assert legacy["freshness"] == "unknown"
    assert "status: ACTIVE" in (root / "docs/legacy.md").read_text()
    put_json(root / "docs/guide.md.amplai.json", {"amplai_document": metadata()})
    with pytest.raises(engine.DocumentError, match="DUPLICATE_AUTHORITY"):
        selected(engine, root)


@pytest.mark.parametrize(
    "invalid",
    ["unsupported_yaml", "duplicate_json_key", "self_hash", "unknown_state", "missing_owner"],
)
def test_unprocessed_metadata_never_becomes_current(engine, tmp_path, invalid):
    root = repository(tmp_path)
    if invalid == "unsupported_yaml":
        (root / "docs/guide.md").write_text(
            "---\namplai_document:\n  title: PRIVATE_CANARY\n---\nsecret\n"
        )
    elif invalid == "duplicate_json_key":
        (root / "docs/guide.md").write_text(
            '---\n{"amplai_document": {}, "amplai_document": {}}\n---\nsecret\n'
        )
    elif invalid == "missing_owner":
        (root / "docs/guide.md").write_text("# PRIVATE_CANARY\n")
    else:
        document(
            root,
            **({"source_sha256": "f" * 64} if invalid == "self_hash" else {"lifecycle": "unknown"}),
        )
    with pytest.raises(engine.DocumentError) as error:
        selected(engine, root)
    assert "PRIVATE_CANARY" not in str(error.value)
    assert "guide.md" not in str(error.value)


def test_authority_lifecycle_freshness_release_are_independent(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    first = engine.read_document(root, "docs/guide.md")
    assert first["metadata"]["authority"] == "canonical"
    assert first["metadata"]["lifecycle"] == "active"
    assert first["freshness"] == "unknown"
    assert selected(engine, root)["documents"] == []
    reviewed(engine, root)
    assert len(selected(engine, root)["documents"]) == 1
    (root / "src/api.py").write_text("VERSION = 2\n")
    stale = engine.read_document(root, "docs/guide.md")
    assert stale["metadata"]["authority"] == "canonical"
    assert stale["metadata"]["lifecycle"] == "active"
    assert stale["freshness"] == "stale"
    assert selected(engine, root)["documents"] == []


def test_old_release_runbook_remains_valid_without_date_based_expiry(engine, tmp_path):
    root = repository(tmp_path)
    document(root, release_ids=["release-1"])
    reviewed(engine, root)
    os.utime(root / "docs/guide.md", (1, 1))
    assert selected(engine, root)["documents"] == []
    old = engine.select_documents(root, release_id="release-1", security="INTERNAL")
    assert len(old["documents"]) == 1
    assert old["documents"][0]["freshness"] == "verified"


@pytest.mark.parametrize("state", ["draft", "candidate", "deprecated", "superseded", "archived"])
def test_current_excludes_nonactive_before_title_snippet_or_ranking(engine, tmp_path, state):
    root = repository(tmp_path)
    changes = {"lifecycle": state, "title": "PRIVATE_STATE_CANARY", "authority": "historical"}
    if state == "candidate":
        changes.pop("lifecycle")
        changes["legacy_status"] = "CANDIDATE"
    if state == "superseded":
        changes["replacement"] = "canary:replacement"
    document(root, body="# PRIVATE_STATE_CANARY\nPRIVATE_BODY_CANARY\n", **changes)
    reviewed(engine, root)
    for consumer in engine.CURRENT_CONSUMERS:
        result = selected(engine, root, consumer=consumer, query="PRIVATE_STATE_CANARY")
        assert result["documents"] == []
        assert "PRIVATE_" not in json.dumps(result)
    historical = selected(engine, root, history=True)
    assert len(historical["documents"]) == 1
    assert historical["mode"] == "history"


def test_security_filters_private_identity_body_and_statistics(engine, tmp_path):
    root = repository(tmp_path)
    document(root, "docs/public.md", doc_id="canary:public", security="PUBLIC")
    document(
        root,
        "docs/PRIVATE_PATH.md",
        doc_id="canary:PRIVATE_ID",
        topic_id="canary:private",
        security="RESTRICTED",
        title="PRIVATE_TITLE",
        body="# PRIVATE_TITLE\nPRIVATE_BODY\n",
    )
    reviewed(engine, root, "docs/public.md")
    reviewed(engine, root, "docs/PRIVATE_PATH.md")
    for consumer in engine.CURRENT_CONSUMERS:
        result = engine.select_documents(
            root, release_id="release-2", security="PUBLIC", consumer=consumer
        )
        assert len(result["documents"]) == 1
        assert "PRIVATE_" not in json.dumps(result)
        assert result["count"] == 1
    with pytest.raises(engine.DocumentError, match="DISCLOSURE_DENIED"):
        engine.select_documents(root, release_id="release-2", security="RESTRICTED", history=True)


def test_canonical_uniqueness_is_qualified_not_global_topic(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    document(root, "docs/v1.md", doc_id="canary:old", release_ids=["release-1"])
    document(root, "docs/public.md", doc_id="canary:public", security="PUBLIC")
    for path in ["docs/guide.md", "docs/v1.md", "docs/public.md"]:
        reviewed(engine, root, path)
    assert len(selected(engine, root)["documents"]) == 2
    document(root, "docs/duplicate.md", doc_id="canary:duplicate")
    reviewed(engine, root, "docs/duplicate.md")
    with pytest.raises(engine.DocumentError, match="CANONICAL_CONFLICT"):
        selected(engine, root)


@pytest.mark.parametrize("case", ["doc_id", "alias", "hardlink", "casefold"])
def test_identity_and_path_alias_collisions_fail_closed(engine, tmp_path, case):
    root = repository(tmp_path)
    document(root)
    if case == "hardlink":
        os.link(root / "docs/guide.md", root / "docs/other.md")
    elif case == "casefold":
        config = json.loads((root / ".ai-team/policy/documentation.json").read_text())
        config["portable_documents"]["roots"] = ["docs", "Docs"]
        put_json(root / ".ai-team/policy/documentation.json", config)
    else:
        document(
            root,
            "docs/other.md",
            topic_id="canary:other",
            doc_id="canary:guide" if case == "doc_id" else "canary:other",
            **({"aliases": ["canary:guide"]} if case == "alias" else {}),
        )
    with pytest.raises(engine.DocumentError):
        selected(engine, root)


@pytest.mark.parametrize(
    "case",
    ["dependency_symlink", "document_symlink", "sidecar_symlink", "parent_symlink", "traversal"],
)
def test_unvalidated_paths_never_read_outside_content(engine, tmp_path, case):
    root = repository(tmp_path)
    outside = tmp_path / "PRIVATE_OUTSIDE"
    outside.mkdir()
    (outside / "secret.md").write_text("PRIVATE_OUTSIDE_CONTENT")
    document(root)
    if case == "dependency_symlink":
        (root / "src/api.py").unlink()
        (root / "src/api.py").symlink_to(outside / "secret.md")
    elif case == "document_symlink":
        (root / "docs/guide.md").unlink()
        (root / "docs/guide.md").symlink_to(outside / "secret.md")
    elif case == "sidecar_symlink":
        (root / "docs/guide.md.amplai.json").symlink_to(outside / "secret.md")
    elif case == "parent_symlink":
        (root / "docs/link").symlink_to(outside, target_is_directory=True)
    else:
        document(
            root,
            dependencies=[
                {"repository": "canary", "path": "../PRIVATE_OUTSIDE/secret.md", "kind": "code"}
            ],
        )
    with pytest.raises(engine.DocumentError) as error:
        selected(engine, root)
    assert "PRIVATE_OUTSIDE" not in str(error.value)
    assert (outside / "secret.md").read_text() == "PRIVATE_OUTSIDE_CONTENT"


def test_rename_move_and_stale_derived_index_cannot_reuse_old_guidance(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    reviewed(engine, root)
    index = selected(engine, root, consumer="index")
    document(root, "docs/moved.md", lifecycle="superseded", replacement="canary:new")
    (root / "docs/guide.md").unlink()
    assert selected(engine, root, consumer="index")["documents"] == []
    assert not engine.validate_index(root, index)["valid"]
    assert "Safe instructions" not in json.dumps(selected(engine, root, consumer="query"))


def test_review_is_external_and_exact_not_a_status_or_date(engine, tmp_path):
    root = repository(tmp_path)
    document(root, sidecar=True, legacy_status="ACTIVE")
    assert engine.read_document(root, "docs/guide.md")["freshness"] == "unknown"
    reviewed(engine, root)
    assert len(selected(engine, root)["documents"]) == 1
    source = root / "docs/guide.md"
    source.write_text(source.read_text() + "\n")
    assert selected(engine, root)["documents"] == []
    reviewed(engine, root)
    (root / "evidence/observation.txt").write_text("Unrelated replacement observation.\n")
    assert selected(engine, root)["documents"] == []


def test_scope_caps_and_unavailable_inputs_never_report_empty_success(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    config = json.loads((root / ".ai-team/policy/documentation.json").read_text())
    config["portable_documents"]["max_file_bytes"] = 10
    put_json(root / ".ai-team/policy/documentation.json", config)
    with pytest.raises(engine.DocumentError, match="SCOPE_INCOMPLETE"):
        selected(engine, root)


def test_existing_loop_source_claim_decision_consumers_use_same_filter(engine, tmp_path):
    root = repository(tmp_path)
    document(root, security="PUBLIC")
    document(
        root,
        "docs/old.md",
        doc_id="canary:old",
        topic_id="canary:old",
        lifecycle="superseded",
        replacement="canary:guide",
        title="OLD_PRIVATE_CANARY",
    )
    reviewed(engine, root)
    reviewed(engine, root, "docs/old.md")
    put_json(
        root / ".ai-team/knowledge/map.json",
        {
            "selection": {},
            "sources": [
                {"id": "current", "path": "docs/guide.md", "status": "active", "priority": 100},
                {
                    "id": "OLD_PRIVATE_CANARY",
                    "path": "docs/old.md",
                    "status": "active",
                    "priority": 1000,
                },
                {
                    "id": "UNKNOWN_CANARY",
                    "path": "docs/guide.md",
                    "status": "candidate",
                    "priority": 1000,
                },
            ],
        },
    )
    claims = root / ".ai-team/knowledge/claims.jsonl"
    claims.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "id": "current-claim",
                    "status": "active",
                    "evidence": [{"path": "docs/guide.md"}],
                },
                {
                    "id": "OLD_PRIVATE_CANARY",
                    "status": "active",
                    "evidence": [{"path": "docs/old.md"}],
                },
                {
                    "id": "UNKNOWN_CANARY",
                    "status": "candidate",
                    "evidence": [{"path": "docs/guide.md"}],
                },
            ]
        )
        + "\n"
    )
    put_json(
        root / ".ai-team/knowledge/decisions.index.json",
        {
            "entries": [
                {"id": "current-decision", "path": "docs/guide.md", "status": "active"},
                {"id": "OLD_PRIVATE_CANARY", "path": "docs/old.md", "status": "active"},
            ]
        },
    )
    scripts = root / "scripts"
    scripts.mkdir()
    for name in ["loopctl.py", "loopv2.py", "amplai_docs.py"]:
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    code = (
        "import json,loopv2; c={'goal':'canary','scope':{'include':[]}}; "
        "print(json.dumps({'sources':loopv2.selected_sources('.',c),"
        "'claims':loopv2.active_claims('.',c),'decisions':loopv2.active_decisions('.',c)}))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(scripts)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert len(value["sources"]) == len(value["claims"]) == len(value["decisions"]) == 1
    assert "CANARY" not in result.stdout


def test_actual_loopctl_inventory_query_index_commands(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    reviewed(engine, root)
    scripts = root / "scripts"
    scripts.mkdir()
    for name in ["loopctl.py", "loopv2.py", "amplai_docs.py"]:
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    for command in ["inventory", "query", "index"]:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(scripts / "loopctl.py"),
                "docs",
                command,
                "--release",
                "release-2",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (command, result.stdout, result.stderr)
        assert json.loads(result.stdout)["count"] == 1


def loop_command(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "AMPLAI_", "PYTHON"))}
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(root / "scripts/loopctl.py"), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def prepare_loop(root):
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    for name in ["loopctl.py", "loopv2.py", "amplai_docs.py"]:
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    put_json(
        root / ".ai-team/knowledge/map.json",
        {
            "selection": {},
            "sources": [
                {"id": "current", "path": "docs/guide.md", "status": "active", "priority": 100},
            ],
        },
    )
    (root / ".ai-team/knowledge/claims.jsonl").write_text("")
    put_json(root / ".ai-team/knowledge/decisions.index.json", {"entries": []})
    put_json(
        root / "specs/canary/work-contract.json",
        {
            "id": "DOC-CANARY",
            "goal": "Check selected document evidence",
            "work_type": "tiny_change",
            "scope": {"include": ["src/api.py"]},
            "verification": {"profile": "fast"},
        },
    )


@pytest.mark.parametrize("drift", ["dependency", "security", "lifecycle", "review", "release"])
def test_actual_context_invalidates_document_selection_without_private_output(
    engine, tmp_path, drift
):
    root = repository(tmp_path)
    document(root)
    reviewed(engine, root)
    prepare_loop(root)
    result = loop_command(root, "context", "build", "specs/canary")
    assert result.returncode == 0, result.stdout + result.stderr
    original = json.loads(result.stdout)
    assert len(original["required_knowledge"]) == 1
    assert (
        loop_command(root, "context", "validate", "specs/canary/context-pack.json").returncode == 0
    )
    if drift == "dependency":
        (root / "src/api.py").write_text("VERSION = 77\n")
    elif drift == "review":
        (root / "evidence/observation.txt").write_text("Changed observation.\n")
    elif drift == "release":
        document(root, release_ids=["release-3"])
    else:
        document(
            root,
            **(
                {"security": "RESTRICTED", "title": "PRIVATE_RECLASSIFIED_TITLE"}
                if drift == "security"
                else {"lifecycle": "archived"}
            ),
        )
    check = loop_command(root, "context", "validate", "specs/canary/context-pack.json")
    assert check.returncode == 1, check.stdout + check.stderr
    assert "PRIVATE_RECLASSIFIED_TITLE" not in check.stdout + check.stderr
    assert not json.loads(check.stdout)["valid"]


def test_actual_sealed_installer_supplies_document_commands_without_source_checkout(
    engine, tmp_path
):
    target = tmp_path / "installed"
    target.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "document-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == SCRIPT.read_bytes()
    (target / "docs").mkdir()
    (target / "src").mkdir()
    (target / "src/api.py").write_text("VERSION = 1\n")
    config_path = target / ".ai-team/policy/documentation.json"
    config = json.loads(config_path.read_text())
    config["portable_documents"].update(repository_id="canary", current_release_id="release-2")
    put_json(config_path, config)
    document(target)
    reviewed(engine, target)
    for command in ["inventory", "query", "index"]:
        check = loop_command(target, "docs", command, "--release", "release-2")
        assert check.returncode == 0, check.stderr
        assert json.loads(check.stdout)["count"] == 1
    (target / "scripts/amplai_docs.py").unlink()
    assert loop_command(target, "doctor").returncode != 0


def test_no_exact_release_does_not_promote_unmanaged_legacy_guidance(engine, tmp_path):
    root = repository(tmp_path)
    (root / "docs/guide.md").write_text("# LEGACY_UNVERIFIED_CANARY\n")
    config_path = root / ".ai-team/policy/documentation.json"
    config = json.loads(config_path.read_text())
    config["portable_documents"]["current_release_id"] = None
    put_json(config_path, config)
    prepare_loop(root)
    result = loop_command(root, "context", "build", "specs/canary")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["required_knowledge"] == []
    assert "LEGACY_UNVERIFIED_CANARY" not in result.stdout
    assert (root / "docs/guide.md").read_text().startswith("# LEGACY_")


@pytest.mark.parametrize("value", [None, False, [], "unsupported"])
def test_present_but_invalid_frontmatter_cannot_hide_a_second_owner(engine, tmp_path, value):
    root = repository(tmp_path)
    (root / "docs/guide.md").write_text(
        "---\n" + json.dumps({"amplai_document": value}) + "\n---\n# Body\n"
    )
    put_json(root / "docs/guide.md.amplai.json", {"amplai_document": metadata()})
    with pytest.raises(engine.DocumentError, match="DUPLICATE_AUTHORITY"):
        selected(engine, root)


def test_source_outside_declared_roots_is_not_an_authoring_shortcut(engine, tmp_path):
    root = repository(tmp_path)
    document(root, "unapproved/guide.md")
    with pytest.raises(engine.DocumentError, match="SCOPE_INCOMPLETE"):
        engine.read_document(root, "unapproved/guide.md")


def test_total_inventory_byte_cap_is_not_a_successful_truncated_selection(engine, tmp_path):
    root = repository(tmp_path)
    document(root)
    config_path = root / ".ai-team/policy/documentation.json"
    config = json.loads(config_path.read_text())
    config["portable_documents"]["max_total_bytes"] = 1
    put_json(config_path, config)
    with pytest.raises(engine.DocumentError, match="SCOPE_INCOMPLETE"):
        selected(engine, root)


def test_missing_declared_root_is_an_error_unless_explicitly_optional(engine, tmp_path):
    root = repository(tmp_path)
    (root / "docs").rmdir()
    with pytest.raises(engine.DocumentError, match="MISSING_SOURCE"):
        selected(engine, root)
    config_path = root / ".ai-team/policy/documentation.json"
    config = json.loads(config_path.read_text())
    config["portable_documents"]["optional_roots"] = ["docs"]
    put_json(config_path, config)
    assert selected(engine, root)["documents"] == []


def test_document_engine_uses_python36_grammar_without_site_dependencies(engine):
    source = SCRIPT.read_text()
    tree = ast.parse(source, feature_version=(3, 6))
    assert not any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" for node in ast.walk(tree)
    )
    assert "capture_output=" not in source
    assert "from pathlib" not in source


def private_link_fixture(engine, tmp_path, body=None, *, history=False, title="Public guide"):
    root = repository(tmp_path)
    document(
        root,
        "docs/PRIVATE_CANARY.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security="RESTRICTED",
        title="PRIVATE_TITLE_CANARY",
        body="# Restricted body\n",
    )
    document(
        root,
        security="PUBLIC",
        title=title,
        lifecycle="archived" if history else "active",
        body=body or "# Guide\n[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md)\n",
    )
    reviewed(engine, root)
    return root


@pytest.mark.parametrize(
    "consumer",
    ["inventory", "query", "index", "view", "loop-context", "loop-claim", "loop-decision"],
)
@pytest.mark.parametrize("history", [False, True])
def test_private_link_is_blocked_before_any_consumer_output(engine, tmp_path, consumer, history):
    root = private_link_fixture(engine, tmp_path, history=history)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE") as error:
        engine.select_documents(
            root,
            "release-2",
            security="PUBLIC",
            consumer=consumer,
            history=history,
            query="PRIVATE_TITLE_CANARY",
        )
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        "[PRIVATE_TITLE_CANARY](%50RIVATE_CANARY.md)",
        "[PRIVATE_TITLE_CANARY](PRIVATE_CANARY%2Emd)",
        "[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md?download=1#part)",
        "[PRIVATE_TITLE_CANARY](<PRIVATE_CANARY.md>)",
        '[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md "Title")',
        "`[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md)`",
        "```md\n[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md)\n```",
        "[PRIVATE_TITLE_CANARY][private]\n\n[private]: PRIVATE_CANARY.md",
        '<a href="PRIVATE_CANARY.md">PRIVATE_TITLE_CANARY</a>',
        '<img src=PRIVATE_CANARY.md alt="PRIVATE_TITLE_CANARY">',
    ],
)
def test_encoded_literal_and_reference_form_links_obey_security(engine, tmp_path, body):
    root = private_link_fixture(engine, tmp_path, body)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC", consumer="query")


def test_reference_filter_runs_before_query_relevance_and_checks_titles(engine, tmp_path):
    root = private_link_fixture(
        engine,
        tmp_path,
        body="# Ordinary instructions\n",
        title="[PRIVATE_TITLE_CANARY](PRIVATE_CANARY.md)",
    )
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC", query="unrelated-no-hit")


@pytest.mark.parametrize("command", ["inventory", "query", "index"])
def test_actual_cli_does_not_emit_private_link_identifiers(engine, tmp_path, command):
    root = private_link_fixture(engine, tmp_path)
    prepare_loop(root)
    result = loop_command(root, "docs", command, "--release", "release-2", "--security", "PUBLIC")
    assert result.returncode != 0
    assert "CROSS_BOUNDARY_REFERENCE" in result.stdout + result.stderr
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    assert "PRIVATE_TITLE_CANARY" not in result.stdout + result.stderr


@pytest.mark.parametrize("target", ["archive.md", "%61rchive.md", "archive.md?raw=1#history"])
def test_same_security_history_reference_does_not_promote_target_as_current(
    engine, tmp_path, target
):
    root = repository(tmp_path)
    document(
        root,
        "docs/archive.md",
        doc_id="canary:archive",
        topic_id="canary:archive",
        security="PUBLIC",
        release_ids=["release-1"],
        lifecycle="archived",
    )
    document(root, security="PUBLIC", body="# Guide\n[Historical source](" + target + ")\n")
    reviewed(engine, root)
    result = engine.select_documents(root, "release-2", security="PUBLIC")
    assert result["count"] == 1
    assert result["documents"][0]["doc_id"] == "canary:guide"


@pytest.mark.parametrize("classified", [False, True])
def test_unmanaged_reference_requires_explicit_visibility_without_guide_promotion(
    engine, tmp_path, classified
):
    root = repository(tmp_path)
    archive = root / "archive/retained.md"
    archive.parent.mkdir()
    archive.write_text("# Retained source, not current instructions\n")
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    if classified:
        policy["document_source_routes"] = [
            {
                "path": "archive",
                "kind": "historical",
                "security": "PUBLIC",
                "reason": "Explicit fixture visibility",
            }
        ]
    put_json(policy_path, policy)
    document(root, security="PUBLIC", body="# Guide\n[History](../archive/retained.md)\n")
    reviewed(engine, root)
    if classified:
        value = engine.select_documents(root, "release-2", security="PUBLIC")
        assert [x["doc_id"] for x in value["documents"]] == ["canary:guide"]
    else:
        with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
            engine.select_documents(root, "release-2", security="PUBLIC")
