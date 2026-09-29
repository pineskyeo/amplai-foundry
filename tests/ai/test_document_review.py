"""S04 complete impact/review probes; Git and all writes are disposable fixtures."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ai import test_document_lifecycle as docs

ROOT = Path(__file__).resolve().parents[2]


def test_accumulated_review_budget_rejects_before_replacing_readable_store(runtime, tmp_path):
    root = fixture(tmp_path)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    limit = 8_388_608
    policy["portable_documents"]["max_file_bytes"] = limit
    docs.put_json(policy_path, policy)
    report = change(runtime, root)
    target = root / ".ai-team/knowledge/document-reviews.json"
    for number in range(2):
        value = request(report)
        value["reviews"][0]["reason"] = str(number) + "a" * 2_800_000
        assert len(json.dumps(value).encode("utf-8")) < limit
        assert runtime.docs_review_batch(str(root), "specs/canary", value)["status"] == "RESOLVED"
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    before_size, before_mode = target.stat().st_size, target.stat().st_mode
    value = request(report)
    value["reviews"][0]["reason"] = "2" + "a" * 2_800_000
    with pytest.raises(runtime.amplai_docs.DocumentError, match="SCOPE_INCOMPLETE"):
        runtime.docs_review_batch(str(root), "specs/canary", value)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before_hash
    assert (target.stat().st_size, target.stat().st_mode) == (before_size, before_mode)
    assert not list(target.parent.glob(target.name + ".pending-*"))
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    assert runtime.docs_impact(str(root), "specs/canary")["status"] == "RESOLVED"
    assert (
        runtime.docs_review_batch(str(root), "specs/canary", request(report))["status"]
        == "RESOLVED"
    )
    assert len(json.loads(target.read_text())["history"]) == 2


def test_review_store_follows_the_policy_limit_when_it_is_raised(runtime, tmp_path):
    # D-090: the writer and both readers of the store use the policy's max_file_bytes, not the
    # 8 MiB default; three 2.8 MB reviews are 8.4 MB and must be stored and read back
    root = fixture(tmp_path)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"]["max_file_bytes"] = 16_777_216
    docs.put_json(policy_path, policy)
    report = change(runtime, root)
    target = root / ".ai-team/knowledge/document-reviews.json"
    for number in range(3):
        value = request(report)
        value["reviews"][0]["reason"] = str(number) + "a" * 2_800_000
        assert runtime.docs_review_batch(str(root), "specs/canary", value)["status"] == "RESOLVED"
    assert target.stat().st_size > 8_388_608
    stored = json.loads(target.read_text())
    assert len(stored["history"]) == 2  # the superseded ones; the latest is in "reviews"
    assert stored["reviews"][0]["reason"].startswith("2")
    assert runtime.docs_impact(str(root), "specs/canary")["status"] == "RESOLVED"
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1


def test_impact_projection_respects_its_configured_reader_budget(runtime, tmp_path):
    root = fixture(tmp_path)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    limit = 262_144
    assert max(path.stat().st_size for path in (root / "scripts").glob("*.py")) < limit
    policy["portable_documents"]["max_file_bytes"] = limit
    docs.put_json(policy_path, policy)
    change(runtime, root)
    target = root / "specs/canary/doc-impact.json"
    before = target.read_bytes()
    before_mode = target.stat().st_mode
    added = []
    for number in range(700):
        path = root / "src" / ("overflow-" + str(number) + "-" + "x" * 80 + ".py")
        path.write_text("# fixture\n")
        added.append(path)
    proposed = runtime.docs_impact(str(root), "specs/canary", write=False)
    assert (
        len(json.dumps(proposed, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"))
        > limit
    )
    with pytest.raises(runtime.amplai_docs.DocumentError, match="SCOPE_INCOMPLETE"):
        runtime.docs_impact(str(root), "specs/canary")
    assert target.read_bytes() == before
    assert target.stat().st_mode == before_mode
    assert not list(target.parent.glob(target.name + ".pending-*"))
    with runtime.amplai_docs.SourceTree(root) as tree:
        runtime.amplai_docs.configuration(tree)
        assert json.loads(tree.read("specs/canary/doc-impact.json"))["scan_complete"]
    for path in added:
        path.unlink()  # Only the disposable generated fixture members above.
    assert runtime.docs_impact(str(root), "specs/canary")["scan_complete"]


def test_atomic_json_budget_counts_exact_utf8_pretty_bytes_before_any_stage(runtime, tmp_path):
    target = tmp_path / "record.json"
    target.write_bytes(b"{}\n")
    target.chmod(0o640)
    value = {"nested": [{"label": "문서" * 11}]}
    expected = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    )
    called = []
    with runtime.amplai_docs.SourceTree(tmp_path) as tree:
        with pytest.raises(runtime.amplai_docs.DocumentError, match="SCOPE_INCOMPLETE"):
            runtime.amplai_docs.atomic_json(
                tree,
                "record.json",
                value,
                b"{}\n",
                before_publish=lambda: called.append(True),
                max_bytes=len(expected) - 1,
            )
        assert target.read_bytes() == b"{}\n"
        assert called == []
        assert not list(tmp_path.glob("record.json.pending-*"))
        runtime.amplai_docs.atomic_json(
            tree, "record.json", value, b"{}\n", max_bytes=len(expected)
        )
        assert tree.read("record.json", limit=len(expected)) == expected
        assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("operation", ["review", "impact"])
def test_public_atomic_record_preserves_mode_with_restrictive_umask(runtime, tmp_path, operation):
    root = fixture(tmp_path)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    path = (
        ".ai-team/knowledge/document-reviews.json"
        if operation == "review"
        else "specs/canary/doc-impact.json"
    )
    target = root / path
    target.chmod(0o640)
    original_umask = os.umask(0o077)
    try:
        result = (
            runtime.docs_review_batch(str(root), "specs/canary", request(report))
            if operation == "review"
            else runtime.docs_impact(str(root), "specs/canary")
        )
    finally:
        os.umask(original_umask)
    assert result["status"] == "RESOLVED"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not list(target.parent.glob(target.name + ".pending-*"))


@pytest.mark.parametrize("operation", ["review", "impact"])
@pytest.mark.parametrize("fault", ["chmod", "same_bytes_replace", "content"])
def test_public_atomic_record_rejects_late_metadata_or_identity_drift(
    runtime, tmp_path, monkeypatch, operation, fault
):
    root = fixture(tmp_path)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    path = (
        ".ai-team/knowledge/document-reviews.json"
        if operation == "review"
        else "specs/canary/doc-impact.json"
    )
    target = root / path
    target.chmod(0o644)
    before = target.read_bytes()
    original = runtime.amplai_docs.atomic_json
    state = {}

    def intercepted(tree, name, *args, **kwargs):
        if name == path:
            callback = kwargs.get("before_publish")

            def inject():
                if callback:
                    callback()
                if fault == "chmod":
                    target.chmod(0o600)
                elif fault == "same_bytes_replace":
                    replacement = target.with_name("fixture-replacement.json")
                    replacement.write_bytes(before)
                    replacement.chmod(0o644)
                    replacement.replace(target)
                else:
                    target.write_bytes(before + b" \n")
                state.update(
                    inode=target.stat().st_ino, mode=target.stat().st_mode, body=target.read_bytes()
                )

            kwargs["before_publish"] = inject
        return original(tree, name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.amplai_docs, "atomic_json", intercepted)
        with pytest.raises(runtime.amplai_docs.DocumentError, match="SOURCE_DRIFT"):
            if operation == "review":
                runtime.docs_review_batch(str(root), "specs/canary", request(report))
            else:
                runtime.docs_impact(str(root), "specs/canary")
    assert target.read_bytes() == state["body"]
    assert target.stat().st_mode == state["mode"]
    assert target.stat().st_ino == state["inode"]
    assert not list(target.parent.glob(target.name + ".pending-*"))
    # External mutation remains; a fresh explicit retry may now use that preimage.
    assert runtime.docs_impact(str(root), "specs/canary")["scan_complete"]
    assert (
        runtime.docs_review_batch(str(root), "specs/canary", request(report))["status"]
        == "RESOLVED"
    )
    if fault == "chmod":
        assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_private_default_remains_readable_under_all_mask(runtime, tmp_path):
    original_umask = os.umask(0o777)
    try:
        with runtime.amplai_docs.SourceTree(tmp_path) as tree:
            runtime.amplai_docs.atomic_json(tree, "new.json", {"ready": True}, None)
    finally:
        os.umask(original_umask)
    target = tmp_path / "new.json"
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text()) == {"ready": True}


@pytest.mark.parametrize("operation", ["fchmod", "fsync", "replace"])
def test_atomic_stage_failure_preserves_old_identity_and_cleans_own_stage(
    runtime, tmp_path, monkeypatch, operation
):
    target = tmp_path / "record.json"
    target.write_bytes(b"{}\n")
    target.chmod(0o640)
    old = target.stat()

    def fail(*args, **kwargs):
        raise OSError("Synthetic atomic publication fault")

    with runtime.amplai_docs.SourceTree(tmp_path) as tree:
        with monkeypatch.context() as patch:
            patch.setattr(runtime.amplai_docs.os, operation, fail)
            with pytest.raises(OSError, match="Synthetic atomic publication fault"):
                runtime.amplai_docs.atomic_json(tree, "record.json", {"new": True}, b"{}\n")
        assert target.read_bytes() == b"{}\n"
        assert target.stat().st_ino == old.st_ino
        assert target.stat().st_mode == old.st_mode
        assert not list(tmp_path.glob("record.json.pending-*"))
        runtime.amplai_docs.atomic_json(tree, "record.json", {"retry": True}, b"{}\n")
    assert json.loads(target.read_text()) == {"retry": True}
    assert target.stat().st_mode == old.st_mode
    assert (target.stat().st_uid, target.stat().st_gid) == (old.st_uid, old.st_gid)


def test_atomic_same_inode_same_bytes_metadata_drift_is_not_an_equal_preimage(runtime, tmp_path):
    target = tmp_path / "record.json"
    target.write_bytes(b"{}\n")
    before = target.stat()

    def touch():
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    with (
        runtime.amplai_docs.SourceTree(tmp_path) as tree,
        pytest.raises(runtime.amplai_docs.DocumentError, match="SOURCE_DRIFT"),
    ):
        runtime.amplai_docs.atomic_json(
            tree, "record.json", {"new": True}, b"{}\n", before_publish=touch
        )
    assert target.read_bytes() == b"{}\n"
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns + 1_000_000
    assert not list(tmp_path.glob("record.json.pending-*"))


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "document_review_loop", ROOT / "scripts/loopv2.py"
    )
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def classify_fixture_sources(root, *paths):
    """Declare only finite synthetic inputs; unknown targets must remain blocked."""
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    routes = policy.setdefault("document_source_routes", [])
    for path in paths:
        if not any(row["path"] == path for row in routes):
            routes.append(
                {
                    "path": path,
                    "kind": "historical",
                    "security": "INTERNAL",
                    "reason": "Synthetic internal source for this finite review fixture.",
                }
            )
    docs.put_json(policy_path, policy)


def fixture(tmp_path, *, count=1):
    root = docs.repository(tmp_path)
    docs.document(root, body="# Guide\n\nExplains `src/api.py`.\n")
    docs.prepare_loop(root)
    config_path = root / ".ai-team/policy/documentation.json"
    config = json.loads(config_path.read_text())
    config.update(
        canonical_roots=[
            {"path": "docs", "category": "architecture", "kind": "canonical"},
            {"path": "specs", "category": "work_memory", "kind": "work_scoped"},
        ],
        semantic_change_paths=["src/**", "scripts/**"],
        non_semantic_change_paths=["docs/**"],
        impact_rules=[],
        historical_documents=[],
        keyword_stopwords=[],
        discovery_axes=[{"id": "doc_reference", "source": "all declared documents"}],
        reference_scan={"documents": ["docs/guide.md"], "known_absent": [], "ignore_patterns": []},
    )
    docs.put_json(config_path, config)
    classify_fixture_sources(root, "src/api.py")
    if count == 2:
        docs.document(
            root,
            "docs/second.md",
            doc_id="canary:second",
            topic_id="canary:second",
            body="# Second\n\nAlso explains `src/api.py`.\n",
        )
    evidence = root / "evidence/review.txt"
    evidence.parent.mkdir()
    evidence.write_text(
        "Fixture source and operating instructions compared with exact dependency bytes.\n"
    )
    git(root, "init", "--quiet", "-b", "main")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture baseline")
    return root


@pytest.mark.parametrize("security", [None, "RESTRICTED"])
def test_reviewed_fixture_never_grants_unknown_or_restricted_source_visibility(
    runtime, tmp_path, security
):
    root = fixture(tmp_path)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    routes = policy["document_source_routes"]
    source = next(row for row in routes if row["path"] == "src/api.py")
    if security is None:
        routes.remove(source)
    else:
        source["security"] = security
    docs.put_json(policy_path, policy)
    report = change(runtime, root)
    assert runtime.docs_review_batch(str(root), "specs/canary", request(report))["status"] == "RESOLVED"
    with pytest.raises(runtime.amplai_docs.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        runtime.amplai_docs.select_documents(root, "release-2")


def change(runtime, root):
    (root / "src/api.py").write_text("VERSION = 2\n")
    return runtime.docs_impact(str(root), "specs/canary")


def request(report, documents=("docs/guide.md",), *, outcome="reviewed_unchanged"):
    return {
        "snapshot": report["dependency_snapshot_hash"],
        "reference_reviews": [],
        "reviews": [
            {
                "document": path,
                "outcome": outcome,
                "reason": (
                    "The value changed but the documented source location and operator steps "
                    "remain valid."
                ),
                "reviewer": "fixture-reviewer",
                "method": "source comparison and executable regression",
                "evidence": ["evidence/review.txt"],
            }
            for path in documents
        ],
    }


def test_unchanged_review_is_exact_evidence_and_feeds_current_selection(runtime, tmp_path):
    root = fixture(tmp_path)
    report = change(runtime, root)
    assert report["status"] == "STALE"
    before = (root / "docs/guide.md").read_bytes()
    result = runtime.docs_review_batch(str(root), "specs/canary", request(report))
    assert result["status"] == "RESOLVED"
    assert result["scan_complete"] is True
    assert runtime.docs_validate(str(root), "specs/canary")["valid"]
    selected = runtime.amplai_docs.select_documents(root, "release-2")
    assert selected["count"] == 1
    assert (root / "docs/guide.md").read_bytes() == before
    store = json.loads((root / ".ai-team/knowledge/document-reviews.json").read_text())
    review = store["reviews"][0]
    assert review["method"] == "source comparison and executable regression"
    assert (
        review["evidence"][0]["sha256"]
        == hashlib.sha256((root / "evidence/review.txt").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "drift", ["source", "document", "policy", "evidence", "new_tool", "staged_only", "contract"]
)
def test_current_review_cannot_survive_its_input_drift(runtime, tmp_path, drift):
    root = fixture(tmp_path)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    if drift == "source":
        (root / "src/api.py").write_text("VERSION = 3\n")
    elif drift == "document":
        p = root / "docs/guide.md"
        p.write_text(p.read_text() + "\n")
    elif drift == "policy":
        p = root / ".ai-team/policy/documentation.json"
        v = json.loads(p.read_text())
        v["purpose"] = "Changed exact rules"
        docs.put_json(p, v)
    elif drift == "evidence":
        (root / "evidence/review.txt").write_text("Different observation.\n")
    elif drift == "new_tool":
        (root / "tools").mkdir()
        (root / "tools/new-helper.sh").write_text("exit 0\n")
    elif drift == "contract":
        path = root / "specs/canary/work-contract.json"
        value = json.loads(path.read_text())
        value["goal"] = "Changed review contract meaning"
        docs.put_json(path, value)
    else:
        (root / "src/api.py").write_text("VERSION = 999\n")
        git(root, "add", "src/api.py")
        (root / "src/api.py").write_text("VERSION = 2\n")
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0


def test_large_complete_scope_includes_late_path_and_no_40_item_shortcut(runtime, tmp_path):
    root = fixture(tmp_path)
    for number in range(45):
        (root / f"src/component-{number:02d}.py").write_text("value = 1\n")
    docs.document(
        root,
        dependencies=[{"repository": "canary", "path": "src/component-44.py", "kind": "code"}],
        body="# Guide\nUses `src/component-44.py`.\n",
    )
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture declared inputs")
    for number in range(45):
        (root / f"src/component-{number:02d}.py").write_text("value = 2\n")
    report = runtime.docs_impact(str(root), "specs/canary")
    assert report["scan_complete"] is True
    assert len(report["changed_sources"]) == 45
    assert {x["path"] for x in report["impacted_documents"]} == {"docs/guide.md"}
    assert report["completeness"]["unprocessed"] == []
    assert set(report["completeness"]["expected"]) == set(
        report["completeness"]["processed"]
    ) | set(report["completeness"]["excluded"])


def test_staged_unstaged_cancel_does_not_remove_candidate_path(runtime, tmp_path):
    root = fixture(tmp_path)
    original = (root / "src/api.py").read_bytes()
    (root / "src/api.py").write_text("VERSION = 55\n")
    git(root, "add", "src/api.py")
    (root / "src/api.py").write_bytes(original)
    report = runtime.docs_impact(str(root), "specs/canary")
    assert "src/api.py" in report["changed_sources"]
    assert report["status"] == "STALE"


def test_renames_deletes_untracked_and_document_only_edits_are_semantic(runtime, tmp_path):
    root = fixture(tmp_path)
    git(root, "mv", "src/api.py", "src/renamed.py")
    (root / "src/new.py").write_text("new = True\n")
    docs.document(
        root,
        dependencies=[{"repository": "canary", "path": "src/renamed.py", "kind": "code"}],
        body="# Guide\nUses `src/renamed.py`.\n",
    )
    report = runtime.docs_impact(str(root), "specs/canary")
    assert {"src/api.py", "src/renamed.py", "src/new.py", "docs/guide.md"} <= set(
        report["changed_sources"]
    )
    assert "docs/guide.md" in {x["path"] for x in report["impacted_documents"]}
    assert not report["impacted_documents"][0]["handled"]


@pytest.mark.parametrize("change_kind", ["whitespace", "date", "acknowledge"])
def test_touch_or_path_acknowledgement_is_not_semantic_review(runtime, tmp_path, change_kind):
    root = fixture(tmp_path)
    before = (root / "docs/guide.md").read_text()
    if change_kind != "acknowledge":
        (root / "docs/guide.md").write_text(
            before + ("\n \n" if change_kind == "whitespace" else "\nlast_reviewed: 2026-09-11\n")
        )
    report = change(runtime, root)
    if change_kind == "acknowledge":
        value = runtime.docs_impact(str(root), "specs/canary", acknowledged=["docs/guide.md"])
        assert value["status"] == "STALE"
        assert value["legacy_acknowledgements_accepted"] is False
    else:
        with pytest.raises((runtime.amplai_docs.DocumentError, RuntimeError)):
            runtime.docs_review_batch(str(root), "specs/canary", request(report, outcome="update"))


def test_new_tool_discovery_does_not_depend_on_user_naming_a_document(runtime, tmp_path):
    root = fixture(tmp_path)
    (root / "tools").mkdir()
    (root / "tools/deploy-helper.sh").write_text("exit 0\n")
    docs.document(
        root,
        dependencies=[{"repository": "canary", "path": "tools/deploy-helper.sh", "kind": "code"}],
        body="# Guide\nUses `tools/deploy-helper.sh`.\n",
    )
    report = runtime.docs_impact(str(root), "specs/canary")
    assert "tools/deploy-helper.sh" in report["changed_sources"]
    assert "docs/guide.md" in {x["path"] for x in report["impacted_documents"]}


@pytest.mark.parametrize("kind", ["file", "directory", "glob"])
def test_declared_ignored_dependency_and_membership_invalidate_review(runtime, tmp_path, kind):
    root = fixture(tmp_path)
    (root / "inputs").mkdir()
    (root / "inputs/model.json").write_text('{"revision": 1}\n')
    classify_fixture_sources(root, "inputs")
    (root / ".gitignore").write_text("inputs/\n")
    target = {"file": "inputs/model.json", "directory": "inputs/", "glob": "inputs/*.json"}[kind]
    docs.document(root, body="# Guide\nUses `src/api.py`.\nRequires `" + target + "`.\n")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture explicit ignored dependency")
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    (root / ("inputs/model.json" if kind == "file" else "inputs/new.json")).write_text(
        '{"revision": 2}\n'
    )
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0


@pytest.mark.parametrize("overlap", [False, True])
def test_concurrent_writers_reject_busy_then_merge_without_losing_reviews(
    runtime, tmp_path, overlap
):
    root = fixture(tmp_path, count=2)
    report = change(runtime, root)
    ctx = multiprocessing.get_context("fork")
    entered, release = ctx.Event(), ctx.Event()
    results = ctx.Queue()

    def first_writer():
        original = runtime.amplai_docs.atomic_json

        def paused(tree, path, *args, **kwargs):
            if path == ".ai-team/knowledge/document-reviews.json":
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("fixture timeout")
            return original(tree, path, *args, **kwargs)

        runtime.amplai_docs.atomic_json = paused
        try:
            results.put(
                runtime.docs_review_batch(str(root), "specs/canary", request(report))["status"]
            )
        except Exception as exc:
            results.put(type(exc).__name__)

    process = ctx.Process(target=first_writer)
    process.start()
    try:
        assert entered.wait(10)
        paths = ("docs/guide.md", "docs/second.md") if overlap else ("docs/second.md",)
        second = request(report, paths)
        for entry in second["reviews"]:
            entry["reviewer"] = "second-fixture-reviewer"
        with pytest.raises(runtime.amplai_docs.DocumentError, match="REVIEW_BUSY"):
            runtime.docs_review_batch(str(root), "specs/canary", second)
        release.set()
        process.join(10)
        assert process.exitcode == 0
        assert results.get(timeout=2) == "STALE"
        result = runtime.docs_review_batch(str(root), "specs/canary", second)
        assert result["status"] == "RESOLVED"
        store = json.loads((root / ".ai-team/knowledge/document-reviews.json").read_text())
        assert {x["path"] for x in store["reviews"]} == {"docs/guide.md", "docs/second.md"}
        if overlap:
            assert store["history"][0]["reviewer"] == "fixture-reviewer"
        else:
            assert (
                next(x for x in store["reviews"] if x["path"] == "docs/guide.md")["reviewer"]
                == "fixture-reviewer"
            )
    finally:
        release.set()
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)


def test_invalid_batch_never_partially_publishes(runtime, tmp_path):
    root = fixture(tmp_path, count=2)
    report = change(runtime, root)
    batch = request(report, ("docs/guide.md", "docs/second.md"))
    batch["reviews"][-1]["evidence"] = ["evidence/missing.txt"]
    before = (root / "specs/canary/doc-impact.json").read_bytes()
    with pytest.raises(runtime.amplai_docs.DocumentError):
        runtime.docs_review_batch(str(root), "specs/canary", batch)
    assert not (root / ".ai-team/knowledge/document-reviews.json").exists()
    assert (root / "specs/canary/doc-impact.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["source_drift", "replace_failure", "projection_failure"])
def test_publication_failure_preserves_canonical_evidence_and_is_recoverable(
    runtime, tmp_path, monkeypatch, fault
):
    root = fixture(tmp_path, count=2)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    store_path = root / ".ai-team/knowledge/document-reviews.json"
    before = store_path.read_bytes()
    original = runtime.amplai_docs.atomic_json

    def injected(tree, path, *args, **kwargs):
        if fault == "source_drift" and path == ".ai-team/knowledge/document-reviews.json":
            (root / "src/api.py").write_text("VERSION = 77\n")
        if fault == "projection_failure" and path == "specs/canary/doc-impact.json":
            raise OSError("fixture projection failure")
        return original(tree, path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.amplai_docs, "atomic_json", injected)
        if fault == "replace_failure":

            def fail(*args, **kwargs):
                raise OSError("fixture replace failure")

            patch.setattr(runtime.amplai_docs.os, "replace", fail)
        with pytest.raises((runtime.amplai_docs.DocumentError, OSError)):
            runtime.docs_review_batch(
                str(root), "specs/canary", request(report, ("docs/second.md",))
            )
    assert not list(store_path.parent.glob("document-reviews.json.pending-*"))
    if fault != "projection_failure":
        assert store_path.read_bytes() == before
    else:
        assert len(json.loads(store_path.read_text())["reviews"]) == 2
        assert runtime.docs_impact(str(root), "specs/canary")["status"] == "RESOLVED"
        assert runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_exact_retry_is_idempotent_and_does_not_invent_review_history(runtime, tmp_path):
    root = fixture(tmp_path)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    path = root / ".ai-team/knowledge/document-reviews.json"
    before = path.read_bytes()
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    assert path.read_bytes() == before


@pytest.mark.parametrize("command", ["review", "review-batch"])
def test_actual_cli_publishes_attributed_review(runtime, tmp_path, command):
    root = fixture(tmp_path)
    report = change(runtime, root)
    args = [sys.executable, "-B", "scripts/loopctl.py", "docs", command, "specs/canary"]
    if command == "review-batch":
        docs.put_json(root / "specs/canary/request.json", request(report))
        args.extend(["--input", "specs/canary/request.json"])
    else:
        args.extend(
            [
                "--document",
                "docs/guide.md",
                "--outcome",
                "reviewed_unchanged",
                "--reason",
                "Source compared",
                "--reviewer",
                "fixture-reviewer",
                "--method",
                "Source comparison",
                "--evidence",
                "evidence/review.txt",
                "--snapshot",
                report["dependency_snapshot_hash"],
            ]
        )
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "RESOLVED"


def test_forged_complete_ledger_is_rejected_even_with_rehashed_report(runtime, tmp_path):
    root = fixture(tmp_path)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    path = root / "specs/canary/doc-impact.json"
    value = json.loads(path.read_text())
    value["completeness"]["processed"] = []
    value["content_hash"] = runtime.object_sha(value)
    docs.put_json(path, value)
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_context_binds_every_claim_evidence_not_just_first_path(runtime, tmp_path):
    root = fixture(tmp_path)
    claim = {
        "id": "pin-claim",
        "status": "active",
        "scope": ["src"],
        "evidence": [{"path": "src/api.py"}, {"path": "evidence/review.txt"}],
    }
    (root / ".ai-team/knowledge/claims.jsonl").write_text(json.dumps(claim) + "\n")
    value = runtime.context_build(str(root), "specs/canary")
    assert any(x["id"] == "pin-claim" for x in value["required_knowledge"])
    assert runtime.context_validate(str(root), "specs/canary/context-pack.json")["valid"]
    (root / "evidence/review.txt").write_text("Changed second claim dependency\n")
    assert not runtime.context_validate(str(root), "specs/canary/context-pack.json")["valid"]


@pytest.mark.parametrize("kind", ["ontology", "binding"])
def test_configured_ignored_domain_pin_invalidates_actual_context_and_review(
    runtime, tmp_path, kind
):
    root = fixture(tmp_path)
    (root / "pins").mkdir()
    (root / "pins/current.json").write_text('{"version": "one"}\n')
    (root / ".gitignore").write_text("pins/\n")
    config_path = root / ".ai-team/policy/documentation.json"
    value = json.loads(config_path.read_text())
    value["portable_documents"]["context_dependencies"] = [
        {"repository": "canary", "path": "pins/current.json", "kind": kind}
    ]
    docs.put_json(config_path, value)
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture configured local domain pin")
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    runtime.context_build(str(root), "specs/canary")
    assert runtime.context_validate(str(root), "specs/canary/context-pack.json")["valid"]
    (root / "pins/current.json").write_text('{"version": "two"}\n')
    assert not runtime.context_validate(str(root), "specs/canary/context-pack.json")["valid"]
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_missing_configured_domain_pin_is_incomplete_not_zero_impact(runtime, tmp_path):
    root = fixture(tmp_path)
    path = root / ".ai-team/policy/documentation.json"
    value = json.loads(path.read_text())
    value["portable_documents"]["context_dependencies"] = [
        {"repository": "canary", "path": "pins/missing.json", "kind": "ontology"}
    ]
    docs.put_json(path, value)
    with pytest.raises(runtime.amplai_docs.DocumentError):
        change(runtime, root)


def test_duplicate_canonical_cannot_satisfy_impact_gate(runtime, tmp_path):
    root = fixture(tmp_path, count=2)
    docs.document(
        root,
        "docs/second.md",
        doc_id="canary:second",
        topic_id="canary:runbook",
        body="# Another guide\nUses `src/api.py`.\n",
    )
    with pytest.raises(runtime.amplai_docs.DocumentError, match="CANONICAL_CONFLICT"):
        change(runtime, root)


def test_prose_disposition_is_attributed_atomic_and_invalidated_by_evidence_drift(
    runtime, tmp_path
):
    root = fixture(tmp_path)
    docs.document(root, body="# Guide\nUses `src/api.py`.\nOld example: `src/retired.py`.\n")
    report = change(runtime, root)
    finding = next(x for x in report["broken_references"] if x["reference"] == "src/retired.py")
    batch = request(report)
    batch["reference_reviews"] = [
        {
            "document": finding["document"],
            "reference": finding["reference"],
            "line": finding["line"],
            "disposition": "example",
            "reason": "The sentence explicitly demonstrates a former example path.",
            "reviewer": "fixture-reviewer",
            "method": "Compare prose meaning with current source inventory",
            "evidence": ["evidence/review.txt"],
        }
    ]
    result = runtime.docs_review_batch(str(root), "specs/canary", batch)
    assert result["status"] == "RESOLVED"
    assert result["unresolved_broken_references"] == []
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 1
    (root / "evidence/review.txt").write_text("Changed evidence\n")
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]
    assert runtime.amplai_docs.select_documents(root, "release-2")["count"] == 0


@pytest.mark.parametrize(
    "body",
    [
        "Requires `src/missing.py`.",
        "[Required target](src/missing.py)",
        "Dependency: `external://other/src/file.py`.",
    ],
)
def test_missing_required_dependency_is_not_waivable(runtime, tmp_path, body):
    root = fixture(tmp_path)
    docs.document(root, body="# Guide\nUses `src/api.py`.\n" + body + "\n")
    with pytest.raises(runtime.amplai_docs.DocumentError):
        change(runtime, root)
    assert not (root / ".ai-team/knowledge/document-reviews.json").exists()


def test_restricted_unrelated_document_metadata_never_leaks_in_impact(runtime, tmp_path):
    root = fixture(tmp_path)
    docs.document(
        root,
        "docs/PRIVATE_PATH_CANARY.md",
        doc_id="canary:PRIVATE_ID_CANARY",
        topic_id="canary:private",
        security="RESTRICTED",
        title="PRIVATE_TITLE_CANARY",
        dependencies=[],
        body="# Private\nNo shared source references.\n",
    )
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture restricted document")
    report = change(runtime, root)
    encoded = json.dumps(report)
    assert "PRIVATE_PATH_CANARY" not in encoded
    assert "PRIVATE_ID_CANARY" not in encoded
    assert "PRIVATE_TITLE_CANARY" not in encoded


def test_partial_changed_override_and_failed_git_read_are_not_empty_pass(
    runtime, tmp_path, monkeypatch
):
    root = fixture(tmp_path)
    (root / "src/api.py").write_text("VERSION = 2\n")
    with pytest.raises(runtime.amplai_docs.DocumentError, match="SCOPE_INCOMPLETE"):
        runtime.docs_impact(str(root), "specs/canary", changed=[])

    def fail(*args, **kwargs):
        raise runtime.amplai_docs.DocumentError("GIT_READ_FAILED")

    monkeypatch.setattr(runtime.amplai_docs, "strict_git", fail)
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_process_interruption_preserves_owner_and_retry_ignores_uncommitted_stage(
    runtime, tmp_path
):
    root = fixture(tmp_path, count=2)
    report = change(runtime, root)
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    path = root / ".ai-team/knowledge/document-reviews.json"
    before = path.read_bytes()
    ctx = multiprocessing.get_context("fork")
    entered, resume = ctx.Event(), ctx.Event()

    def interrupted_writer():
        original = runtime.amplai_docs.atomic_json

        def pause(tree, target, *args, **kwargs):
            if target == ".ai-team/knowledge/document-reviews.json":
                callback = kwargs.get("before_publish")

                def before_publish():
                    entered.set()
                    if not resume.wait(15):
                        raise RuntimeError("fixture timeout")
                    callback()

                kwargs["before_publish"] = before_publish
            return original(tree, target, *args, **kwargs)

        runtime.amplai_docs.atomic_json = pause
        runtime.docs_review_batch(str(root), "specs/canary", request(report, ("docs/second.md",)))

    process = ctx.Process(target=interrupted_writer)
    process.start()
    try:
        assert entered.wait(15)
        process.kill()
        process.join(5)
        assert process.exitcode != 0
        assert path.read_bytes() == before
        stages = list(path.parent.glob("document-reviews.json.pending-*"))
        assert len(stages) == 1
        assert stages[0].stat().st_mode & 0o777 == 0o600
        result = runtime.docs_review_batch(
            str(root), "specs/canary", request(report, ("docs/second.md",))
        )
        assert result["status"] == "RESOLVED"
        assert stages[0].exists()  # Never apply/delete uncommitted evidence as current authority.
        assert len(json.loads(path.read_text())["reviews"]) == 2
    finally:
        if process.is_alive():
            process.terminate()
        process.join(2)


def test_gitlink_pointer_and_local_checkout_changes_bind_review(runtime, tmp_path):
    root = fixture(tmp_path)
    module = root / "modules/helper"
    module.mkdir(parents=True)
    (module / "run.py").write_text("VERSION = 1\n")
    git(module, "init", "--quiet", "-b", "main")
    git(module, "add", "--all")
    git(module, "commit", "--quiet", "-m", "Fixture module baseline")
    oid = git(module, "rev-parse", "HEAD")
    git(root, "update-index", "--add", "--cacheinfo", "160000," + oid + ",modules/helper")
    docs.document(root, body="# Guide\nUses `src/api.py`.\nRequires `modules/helper/run.py`.\n")
    git(root, "add", "docs/guide.md")
    git(root, "commit", "--quiet", "-m", "Fixture local gitlink")
    (module / "run.py").write_text("VERSION = 2\n")
    git(module, "add", "--all")
    git(module, "commit", "--quiet", "-m", "Fixture module update")
    report = runtime.docs_impact(str(root), "specs/canary")
    assert "modules/helper" in report["changed_sources"]
    runtime.docs_review_batch(str(root), "specs/canary", request(report))
    assert runtime.docs_validate(str(root), "specs/canary")["valid"]
    (module / "run.py").write_text("VERSION = 3\n")
    assert not runtime.docs_validate(str(root), "specs/canary")["valid"]


def test_candidate_symlink_is_typed_without_following_outside_checkout(runtime, tmp_path):
    root = fixture(tmp_path)
    (root / "tools").mkdir()
    (root / "tools/local-helper").symlink_to("../src/api.py")
    report = change(runtime, root)
    item = next(
        x for x in report["dependency_snapshot"]["sources"] if x["path"] == "tools/local-helper"
    )
    assert item["state"] == "SYMLINK"
    outside = tmp_path / "PRIVATE_SYMLINK_CANARY"
    outside.write_text("PRIVATE_VALUE_CANARY")
    (root / "tools/local-helper").unlink()
    (root / "tools/local-helper").symlink_to(outside)
    with pytest.raises(runtime.amplai_docs.DocumentError) as exc:
        runtime.docs_impact(str(root), "specs/canary")
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.parametrize(
    "kind", ["missing_rule_target", "malformed_knowledge", "malformed_decisions"]
)
def test_unavailable_discovery_inputs_are_not_silently_empty(runtime, tmp_path, kind):
    root = fixture(tmp_path)
    if kind == "missing_rule_target":
        path = root / ".ai-team/policy/documentation.json"
        value = json.loads(path.read_text())
        value["impact_rules"] = [
            {
                "id": "required-guide",
                "when_changed": ["src/**"],
                "impacted": ["docs/missing.md"],
                "reason": "Required guide",
            }
        ]
        docs.put_json(path, value)
    else:
        path = "map.json" if kind == "malformed_knowledge" else "decisions.index.json"
        (root / ".ai-team/knowledge" / path).write_text("{broken json")
    with pytest.raises((ValueError, RuntimeError)):
        change(runtime, root)


def test_glob_impact_rule_expands_all_owned_documents(runtime, tmp_path):
    root = fixture(tmp_path, count=2)
    docs.document(
        root,
        "docs/second.md",
        doc_id="canary:second",
        topic_id="canary:second",
        body="# Second\nNo direct dependency.\n",
        dependencies=[],
    )
    path = root / ".ai-team/policy/documentation.json"
    value = json.loads(path.read_text())
    value["impact_rules"] = [
        {
            "id": "all-guides",
            "when_changed": ["src/**"],
            "impacted": ["docs/*.md"],
            "reason": "Shared behavior",
        }
    ]
    docs.put_json(path, value)
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture glob guide contract")
    report = change(runtime, root)
    assert {x["path"] for x in report["impacted_documents"]} == {"docs/guide.md", "docs/second.md"}


def test_installed_package_runs_actual_review_and_context_commands(tmp_path):
    root = tmp_path / "installed"
    root.mkdir()
    installed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(root),
            "--bootstrap-baseline",
            "--app-id",
            "review-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, installed.stderr
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src/api.py").write_text("VERSION = 1\n")
    path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(path.read_text())
    policy["portable_documents"].update(repository_id="canary", current_release_id="release-2")
    docs.put_json(path, policy)
    docs.document(root)
    docs.put_json(
        root / "specs/canary/work-contract.json",
        {
            "id": "INSTALLED-REVIEW",
            "goal": "Validate installed document review",
            "work_type": "tiny_change",
            "scope": {"include": ["src/api.py"]},
            "verification": {"profile": "fast"},
        },
    )
    (root / "evidence").mkdir()
    (root / "evidence/review.txt").write_text("Source value compared; guide remains valid.\n")
    git(root, "init", "--quiet", "-b", "main")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "-m", "Fixture installed baseline")
    (root / "src/api.py").write_text("VERSION = 2\n")
    impact = docs.loop_command(root, "docs", "impact", "specs/canary")
    assert impact.returncode == 3, impact.stdout + impact.stderr
    report = json.loads(impact.stdout)
    docs.put_json(root / "specs/canary/review.json", request(report))
    reviewed = docs.loop_command(
        root, "docs", "review-batch", "specs/canary", "--input", "specs/canary/review.json"
    )
    assert reviewed.returncode == 0, reviewed.stdout + reviewed.stderr
    assert docs.loop_command(root, "docs", "validate", "specs/canary").returncode == 0
    assert docs.loop_command(root, "context", "build", "specs/canary").returncode == 0
    assert (
        docs.loop_command(root, "context", "validate", "specs/canary/context-pack.json").returncode
        == 0
    )
    assert (
        json.loads(docs.loop_command(root, "docs", "index", "--release", "release-2").stdout)[
            "count"
        ]
        == 1
    )
