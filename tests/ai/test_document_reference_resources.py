"""S14 allocation bounds through canonical, payload and installed consumers."""

from __future__ import annotations

import __future__
import gc
import importlib.util
import json
import shutil
import subprocess
import sys
import tracemalloc
from types import SimpleNamespace

import pytest

from ai import test_document_lifecycle as docs


@pytest.fixture(
    params=[
        "scripts/amplai_docs.py",
        "tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py",
    ]
)
def bounded_engine(request):
    path = docs.ROOT / request.param
    spec = importlib.util.spec_from_file_location("resource_engine", path)
    module = importlib.util.module_from_spec(spec)
    # Execute these exact source bytes without polluting the sealed payload with
    # an interpreter cache when the full verifier (unlike slices) omits -B.
    exec(compile(path.read_bytes(), str(path), "exec", dont_inherit=True), module.__dict__)
    assert module.__file__ == str(path)
    return module


def test_reference_fixture_import_never_writes_package_bytecode(tmp_path, monkeypatch):
    source = tmp_path / "amplai_docs.py"
    source.write_bytes((docs.ROOT / "scripts/amplai_docs.py").read_bytes())
    # Exercise normal interpreter behavior even when a slice is run with -B.
    with monkeypatch.context() as context:
        context.setattr(sys, "dont_write_bytecode", False)
        engine = bounded_engine.__wrapped__(SimpleNamespace(param=source))
    assert engine.document_reference_tokens("[x](target.md)")[0]["destination"] == "target.md"
    assert (
        not engine.document_reference_tokens.__code__.co_flags
        & __future__.annotations.compiler_flag
    )
    assert sorted(path.name for path in tmp_path.iterdir()) == ["amplai_docs.py"]


@pytest.mark.parametrize(
    "operation,unit",
    [
        ("document_reference_tokens", "[x](https://example.invalid) "),
        ("extract_typed_references", "`guide.md` "),
    ],
)
def test_retained_reference_memory_is_input_bounded(bounded_engine, operation, unit):
    body = unit * 1000
    gc.collect()
    tracemalloc.start()
    try:
        try:
            result = getattr(bounded_engine, operation)(body)
        except bounded_engine.DocumentError as error:
            assert error.code == "SCOPE_INCOMPLETE"
        else:
            assert len(result) == 1000
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak <= 2 * 1024 * 1024 + 40 * len(body.encode("utf-8"))


def test_typed_output_has_the_same_count_limit(bounded_engine):
    with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
        bounded_engine.extract_typed_references("`guide.md`\n" * 10001)


@pytest.mark.parametrize(
    "operation,unit",
    [
        ("document_reference_tokens", "[x](https://example.invalid) "),
        ("extract_typed_references", "`guide.md` "),
    ],
)
def test_lean_candidate_mode_preserves_count_with_bounded_memory(bounded_engine, operation, unit):
    body = unit * 1000
    gc.collect()
    tracemalloc.start()
    try:
        result = getattr(bounded_engine, operation)(body, retain_context=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result) == 1000
    assert all(not row["prefix"] and not row.get("context") for row in result)
    assert peak <= 2 * 1024 * 1024 + 40 * len(body.encode("utf-8"))
    with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
        getattr(bounded_engine, operation)(unit * 10001, retain_context=False)


def test_lean_candidate_mode_preserves_code_span_exclusion(bounded_engine):
    text = "Example `[literal](not-a-dependency.md)` then [real](current.md).\n"
    expected = bounded_engine.extract_typed_references(text)
    lean = bounded_engine.extract_typed_references(text, retain_context=False)
    assert [row["reference"] for row in lean if row["source_kind"] == "markdown_link"] == [
        "current.md"
    ]

    def without_context(rows):
        return [
            {key: value for key, value in row.items() if key not in ("prefix", "context")}
            for row in rows
        ]

    assert without_context(lean) == without_context(expected)


@pytest.mark.parametrize(
    "boundary", ["\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
@pytest.mark.parametrize("retain_context", [True, False])
def test_reference_locations_share_physical_lines(bounded_engine, boundary, retain_context):
    body = "noise" + boundary + "`abcd` and\n[link](target.md)"
    tokens = bounded_engine.document_reference_tokens(body, retain_context=retain_context)
    references = bounded_engine.extract_typed_references(body, retain_context=retain_context)
    links = [row for row in references if row["source_kind"] == "markdown_link"]
    assert [row["reference"] for row in links] == ["target.md"]
    assert links[0]["line"] == tokens[0]["line"]
    assert links[0]["line"] == (3 if boundary in ("\n", "\r", "\r\n") else 2)
    assert all(
        row["reference"] != "not-a-dependency.md"
        for row in bounded_engine.extract_typed_references(
            "noise" + boundary + "`[literal](not-a-dependency.md)`\n", retain_context=retain_context
        )
        if row["source_kind"] == "markdown_link"
    )


@pytest.mark.parametrize(
    "boundary", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_nonphysical_separator_cannot_hide_dependency_drift(bounded_engine, tmp_path, boundary):
    root = docs.repository(tmp_path)
    body = "noise" + boundary + "`abcd` and\n[link](target.md)"
    docs.document(root, body=body)
    docs.document(
        root, "docs/target.md", doc_id="canary:target", topic_id="canary:target", body="# First\n"
    )
    docs.reviewed(bounded_engine, root)
    before = bounded_engine.read_document(root, "docs/guide.md")
    assert before["freshness"] == "verified"
    docs.document(
        root, "docs/target.md", doc_id="canary:target", topic_id="canary:target", body="# Changed\n"
    )
    after = bounded_engine.read_document(root, "docs/guide.md")
    assert before["snapshot"] != after["snapshot"]
    assert after["freshness"] == "stale"
    assert "docs/target.md" in [row["path"] for row in before["declared_dependencies"]]
    # The snapshot fixture is deliberately standalone. Candidate discovery needs
    # a Git inventory, initialized only after the standalone freshness assertions.
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    found = bounded_engine.docs_referencing(root, ["docs/target.md"])
    assert found["docs/guide.md"] == ["docs/target.md"]


def test_lean_opaque_origin_is_external_not_dependency_or_visibility_approval(bounded_engine):
    text = "[Origin](conversation-canary://synthetic-id)\n"
    result = bounded_engine.extract_typed_references(text, retain_context=False)
    assert len(result) == 1 and result[0]["uri_local"] is False
    assert bounded_engine.classify_document_reference(result[0], set())["kind"] == "external_link"
    with pytest.raises(bounded_engine.DocumentError, match="UNSUPPORTED_PARSER"):
        bounded_engine.extract_typed_references(text)
    for uri in (
        "file://localhost/PRIVATE_CANARY",
        "javascript://PRIVATE_CANARY",
        "data://PRIVATE_CANARY",
        "conversation-canary:PRIVATE_CANARY",
    ):
        with pytest.raises(bounded_engine.DocumentError, match="UNSUPPORTED_PARSER"):
            bounded_engine.extract_typed_references("[x](" + uri + ")", retain_context=False)


def test_normal_required_reference_context_is_not_truncated(bounded_engine):
    result = bounded_engine.extract_typed_references(
        "Dependencies: `src/first.py`, `src/second.py` and [third](src/third.py)\n"
        "Do not require the path `missing.py`\n"
    )
    classified = [bounded_engine.classify_document_reference(row, {"src"}) for row in result]
    assert [row["required"] for row in classified] == [True, True, True, False]
    assert result[1]["prefix"] == "Dependencies: `src/first.py`, "
    assert [row["reference"] for row in result[:3]] == [
        "src/first.py",
        "src/second.py",
        "src/third.py",
    ]


def test_large_reference_input_fails_before_selection_or_review_publication(
    bounded_engine, tmp_path
):
    root = docs.repository(tmp_path)
    docs.document(root, security="PUBLIC", body="# Normal guide\n")
    docs.reviewed(bounded_engine, root)
    assert bounded_engine.select_documents(root, "release-2", security="PUBLIC")["count"] == 1
    review = root / ".ai-team/knowledge/document-reviews.json"
    before = review.read_bytes()
    docs.document(root, security="PUBLIC", body="[x](https://example.invalid) " * 1000)
    for consumer in bounded_engine.CURRENT_CONSUMERS:
        with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
            bounded_engine.select_documents(root, "release-2", security="PUBLIC", consumer=consumer)
    with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
        docs.reviewed(bounded_engine, root)
    assert review.read_bytes() == before


def test_actual_installed_cli_reports_limit_not_empty_success(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    install = subprocess.run(
        [
            sys.executable,
            "-B",
            str(docs.ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "resource-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert install.returncode == 0, install.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == docs.SCRIPT.read_bytes()
    source = docs.repository(tmp_path)
    docs.document(source, security="PUBLIC", body="[x](https://example.invalid) " * 1000)
    for folder in ("docs", "src"):
        shutil.copytree(source / folder, target / folder, dirs_exist_ok=True)
    (target / ".ai-team/policy/documentation.json").write_bytes(
        (source / ".ai-team/policy/documentation.json").read_bytes()
    )
    for command in ("inventory", "query", "index"):
        result = docs.loop_command(
            target, "docs", command, "--release", "release-2", "--security", "PUBLIC"
        )
        assert result.returncode != 0
        assert "SCOPE_INCOMPLETE" in result.stdout + result.stderr


S16_CHILD = """
import importlib.util, json, pathlib, sys
source = pathlib.Path(sys.argv[1]).resolve()
spec = importlib.util.spec_from_file_location("bounded_classification", source)
engine = importlib.util.module_from_spec(spec)
exec(compile(source.read_bytes(), str(source), "exec", dont_inherit=True), engine.__dict__)
assert pathlib.Path(engine.__file__).resolve() == source
root, operation = pathlib.Path(sys.argv[2]), sys.argv[3]
record = engine.read_document(root, "docs/guide.md")
assert "src/api.py" in [row["path"] for row in record["declared_dependencies"]]
if operation == "select":
    value = engine.select_documents(root, "release-2", security="PUBLIC", history=True)
    assert [row["path"] for row in value["documents"]] == ["docs/guide.md"]
print(json.dumps({"status": "PASS", "operation": operation}))
"""


def s16_repository(tmp_path, declaration=False):
    root = docs.repository(tmp_path)
    prefix = "Requires both files " if declaration else "Dependencies: " + " " * 30 + "notes "
    docs.document(root, security="PUBLIC", body="# Guide\n" + prefix + "`src/api.py`\n")
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["document_source_routes"] = [
        {
            "path": "src/api.py",
            "kind": "source_asset",
            "owner": "docs/guide.md",
            "security": "PUBLIC",
            "reason": "Synthetic same-security source used only by the bounded parser fixture.",
        }
    ]
    docs.put_json(policy_path, policy)
    return root


@pytest.mark.parametrize("operation", ["read", "select"])
@pytest.mark.parametrize("declaration", [False, True])
def test_s16_short_reference_classification_is_bounded(
    bounded_engine, tmp_path, operation, declaration
):
    root = s16_repository(tmp_path, declaration)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                S16_CHILD,
                bounded_engine.__file__,
                str(root),
                operation,
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("one short reference exceeded the local 3-second child-process bound")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "PASS", "operation": operation}


def test_s16_declaration_and_negation_controls(bounded_engine):
    for header in ("Requires both files ", "Dependencies: ", "depends on ", "의존 파일: "):
        for prefix, required in (("", True), ("Do not ", False), ("Never ", False)):
            body = prefix + header + "`src/first.py`, `src/second.py` and [third](src/third.py)"
            references = bounded_engine.extract_typed_references(body)
            assert len(references) == 3
            classified = [
                bounded_engine.classify_document_reference(row, {"src"}) for row in references
            ]
            assert [row["explicit_dependency"] for row in classified] == [required] * 3
            assert [row["required"] for row in classified] == [required, required, True]


def test_s16_classification_charges_before_prefix_processing(bounded_engine, monkeypatch):
    item = {
        "reference": "src/api.py",
        "raw_reference": "src/api.py",
        "source_kind": "inline_code",
        "line": 1,
        "prefix": "Requires ",
        "context": "Requires `src/api.py`",
    }
    budget = bounded_engine.ReferenceBudget("")
    budget.remaining = 0

    class NoProcessing:
        def sub(self, *args):
            pytest.fail("classification processed a prefix before charging its work budget")

    monkeypatch.setattr(bounded_engine, "PATH_IN_CODE", NoProcessing())
    with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
        bounded_engine.classify_document_reference(item, {"src"}, budget=budget)


def test_s16_installed_inventory_completes_for_near_declaration(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    install = subprocess.run(
        [
            sys.executable,
            "-B",
            str(docs.ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "bounded-classification",
            "--project-id",
            "canary",
            "--no-git",
        ],
        cwd=target,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert install.returncode == 0, install.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == docs.SCRIPT.read_bytes()
    source = s16_repository(tmp_path)
    for folder in ("docs", "src"):
        shutil.copytree(source / folder, target / folder, dirs_exist_ok=True)
    (target / ".ai-team/policy/documentation.json").write_bytes(
        (source / ".ai-team/policy/documentation.json").read_bytes()
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(target / "scripts/loopctl.py"),
                "docs",
                "inventory",
                "--release",
                "release-2",
                "--security",
                "PUBLIC",
                "--history",
            ],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("installed inventory exceeded the local 3-second child-process bound")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["count"] == 1


@pytest.mark.parametrize("operation", ["read", "scan", "impact"])
@pytest.mark.parametrize("exhaust", [False, True])
def test_s16_document_consumers_share_classification_budget(
    bounded_engine, tmp_path, monkeypatch, operation, exhaust
):
    root = docs.repository(tmp_path)
    docs.document(root, body="# Guide\nRequires `src/api.py` and `src/api.py`.\n")
    policy = json.loads((root / ".ai-team/policy/documentation.json").read_text())
    if operation == "impact":
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    budgets = []
    classify = bounded_engine.classify_document_reference

    def guarded(item, local_roots, budget=None):
        assert budget is not None, "document classification must share a caller budget"
        if budgets:
            assert budget is budgets[0]
        elif exhaust:
            budget.remaining = 1 + sum(
                len(item.get(key) or "")
                for key in ("prefix", "context", "reference", "raw_reference")
            )
        budgets.append(budget)
        return classify(item, local_roots, budget=budget)

    monkeypatch.setattr(bounded_engine, "classify_document_reference", guarded)
    operation_call = {
        "read": lambda: bounded_engine.read_document(root, "docs/guide.md"),
        "scan": lambda: bounded_engine.scan_broken_references(root, policy, ["docs/guide.md"]),
        "impact": lambda: bounded_engine.docs_referencing(root, ["src/api.py"]),
    }[operation]
    if exhaust:
        with pytest.raises(bounded_engine.DocumentError, match="SCOPE_INCOMPLETE"):
            operation_call()
    else:
        operation_call()
    assert len(budgets) == 2
