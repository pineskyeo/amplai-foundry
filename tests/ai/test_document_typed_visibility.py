"""S17 typed-reference visibility through exact source, payload and installed CLI."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai import test_document_html as views
from ai import test_document_lifecycle as docs


@pytest.fixture(
    params=[
        "scripts/amplai_docs.py",
        "tools/amplai-loop-kit/baseline/payload/scripts/amplai_docs.py",
    ]
)
def typed_engine(request):
    source = docs.ROOT / request.param
    spec = importlib.util.spec_from_file_location("typed_visibility_engine", source)
    engine = importlib.util.module_from_spec(spec)
    exec(compile(source.read_bytes(), str(source), "exec", dont_inherit=True), engine.__dict__)
    assert Path(engine.__file__).resolve() == source.resolve()
    return engine


def route(path, security):
    return {
        "path": path,
        "kind": "historical",
        "security": security,
        "reason": "Finite synthetic classification fixture, not a real project source.",
    }


def policy_routes(root, routes, hint=None):
    path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(path.read_text())
    policy["document_source_routes"] = routes
    if hint:
        policy["reference_scan"] = {hint: ["assets/PRIVATE_CANARY.bin"]}
    docs.put_json(path, policy)


def select(engine, root, **kwargs):
    return engine.select_documents(root, "release-2", security="PUBLIC", **kwargs)


def assert_denied(engine, operation, code="CROSS_BOUNDARY_REFERENCE"):
    with pytest.raises(engine.DocumentError) as caught:
        operation()
    assert caught.value.code == code
    assert "PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.parametrize("position", ["inline", "tree", "title"])
@pytest.mark.parametrize("target_security", ["RESTRICTED", "PUBLIC"])
def test_typed_security_precedes_all_consumers_and_history(
    typed_engine, tmp_path, position, target_security
):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "docs/PRIVATE_CANARY.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security=target_security,
    )
    body = {
        "inline": "# Guide\nRequires " + chr(96) + "PRIVATE_CANARY.md" + chr(96) + ".\n",
        "tree": "# Guide\ndocs/\n└── PRIVATE_CANARY.md\n",
        "title": "# Guide\nOrdinary public instructions.\n",
    }[position]
    extra = (
        {"title": "Requires " + chr(96) + "PRIVATE_CANARY.md" + chr(96)}
        if position == "title"
        else {}
    )
    docs.document(root, security="PUBLIC", body=body, **extra)
    docs.reviewed(typed_engine, root)
    record = typed_engine.read_document(root, "docs/guide.md")
    assert record["freshness"] == "verified"
    if position != "title":
        assert "docs/PRIVATE_CANARY.md" in [row["path"] for row in record["declared_dependencies"]]
    for consumer in typed_engine.CURRENT_CONSUMERS:
        for history in (False, True):

            def call(consumer=consumer, history=history):
                return select(typed_engine, root, consumer=consumer, history=history)

            if target_security == "RESTRICTED":
                assert_denied(typed_engine, call)
            else:
                result = call()
                expected = {"docs/guide.md"}
                if history:
                    expected.add("docs/PRIVATE_CANARY.md")
                assert {row["path"] for row in result["documents"]} == expected


@pytest.mark.parametrize("target", ["assets/", "assets/*.bin"])
@pytest.mark.parametrize("target_security", ["RESTRICTED", "PUBLIC"])
def test_directory_and_glob_check_every_classified_member(
    typed_engine, tmp_path, target, target_security
):
    root = docs.repository(tmp_path)
    (root / "assets").mkdir()
    (root / "assets/public.bin").write_bytes(b"public fixture\n")
    (root / "assets/PRIVATE_CANARY.bin").write_bytes(b"synthetic fixture\n")
    policy_routes(
        root,
        [
            route("assets", "PUBLIC"),
            route("assets/PRIVATE_CANARY.bin", target_security),
        ],
    )
    docs.document(root, security="PUBLIC", body="Requires " + chr(96) + target + chr(96) + ".\n")
    if target_security == "RESTRICTED":
        assert_denied(typed_engine, lambda: select(typed_engine, root, history=True))
    else:
        assert select(typed_engine, root, history=True)["count"] == 1


@pytest.mark.parametrize(
    "alias_security,target_security",
    [
        ("PUBLIC", "RESTRICTED"),
        ("RESTRICTED", "PUBLIC"),
        ("PUBLIC", "PUBLIC"),
        (None, "PUBLIC"),
    ],
)
def test_logical_alias_cannot_weaken_canonical_target(
    typed_engine, tmp_path, alias_security, target_security
):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "assets/PRIVATE_CANARY.md",
        doc_id="canary:asset",
        topic_id="canary:asset",
        security=target_security,
    )
    (root / "alias").symlink_to("assets", target_is_directory=True)
    policy_routes(root, [route("alias", alias_security)] if alias_security else [])
    docs.document(
        root,
        security="PUBLIC",
        body="Requires " + chr(96) + "alias/PRIVATE_CANARY.md" + chr(96) + ".\n",
    )
    if "RESTRICTED" in (alias_security, target_security):
        assert_denied(typed_engine, lambda: select(typed_engine, root, history=True))
    else:
        assert select(typed_engine, root, history=True)["count"] == 1


@pytest.mark.parametrize("hint", ["ignore_patterns", "known_absent"])
@pytest.mark.parametrize("exists", [False, True])
def test_optional_hint_is_not_existing_target_security_approval(
    typed_engine, tmp_path, hint, exists
):
    root = docs.repository(tmp_path)
    (root / "assets").mkdir()
    if exists:
        (root / "assets/PRIVATE_CANARY.bin").write_bytes(b"synthetic fixture\n")
    policy_routes(root, [route("assets/PRIVATE_CANARY.bin", "RESTRICTED")], hint)
    docs.document(
        root,
        security="PUBLIC",
        body="Optional example " + chr(96) + "assets/PRIVATE_CANARY.bin" + chr(96) + ".\n",
    )
    if exists:
        assert_denied(typed_engine, lambda: select(typed_engine, root, history=True))
    else:
        assert select(typed_engine, root, history=True)["count"] == 1


def test_unknown_existing_typed_input_is_not_default_public(typed_engine, tmp_path):
    root = docs.repository(tmp_path)
    (root / "src/PRIVATE_CANARY.bin").write_bytes(b"synthetic unclassified input\n")
    docs.document(
        root,
        security="PUBLIC",
        body="Requires " + chr(96) + "src/PRIVATE_CANARY.bin" + chr(96) + ".\n",
    )
    assert_denied(typed_engine, lambda: select(typed_engine, root, history=True))


def test_typed_visibility_target_walk_is_bounded(typed_engine, tmp_path):
    root = docs.repository(tmp_path)
    (root / "assets").mkdir()
    for number in range(12):
        (root / f"assets/file-{number}.bin").write_bytes(b"bounded fixture\n")
    policy_routes(root, [route("assets", "PUBLIC")])
    with typed_engine.SourceTree(root) as tree:
        config, _ = typed_engine.configuration(tree)
        config["max_files"] = 3
        assert_denied(
            typed_engine,
            lambda: typed_engine.reference_visibility(
                tree,
                config,
                [],
                "docs/guide.md",
                "Requires " + chr(96) + "assets/*.bin" + chr(96),
                "PUBLIC",
            ),
            "SCOPE_INCOMPLETE",
        )


@pytest.mark.parametrize("operation", ["snapshot", "read"])
def test_dependency_walk_is_bounded_before_visibility(
    typed_engine, tmp_path, monkeypatch, operation
):
    root = docs.repository(tmp_path)
    (root / "assets").mkdir()
    for number in range(12):
        (root / f"assets/file-{number}.bin").write_bytes(b"bounded fixture\n")
    policy_routes(root, [route("assets", "PUBLIC")])
    body = "Requires `assets/*.bin`.\n"
    docs.document(root, security="PUBLIC", body=body)
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["portable_documents"]["max_files"] = 3
    docs.put_json(policy_path, policy)
    entries = typed_engine.reference_directory_entries
    observed = []

    def guarded(directory, spend=None):
        assert spend is not None, "dependency walk must be bounded before visibility runs"
        observed.append(str(directory))
        return entries(directory, spend)

    monkeypatch.setattr(typed_engine, "reference_directory_entries", guarded)
    with typed_engine.SourceTree(root) as tree:
        config, _ = typed_engine.configuration(tree)
        with pytest.raises(typed_engine.DocumentError, match="SCOPE_INCOMPLETE"):
            if operation == "snapshot":
                typed_engine.declared_reference_snapshot(tree, "docs/guide.md", body, config)
            else:
                typed_engine.read_document(root, "docs/guide.md")
    assert observed


@pytest.mark.parametrize(
    "body",
    [
        "# Guide\nRequires " + chr(96) + "PRIVATE_CANARY.md" + chr(96) + ".\n",
        "# Guide\ndocs/\n└── PRIVATE_CANARY.md\n",
    ],
)
@pytest.mark.parametrize("target_security", ["RESTRICTED", "INTERNAL"])
def test_lower_security_html_rejects_typed_inputs_without_promoting_links(
    typed_engine, tmp_path, body, target_security
):
    root, release = views.fixture(
        typed_engine,
        tmp_path,
        body=body,
        security="INTERNAL",
        linked={
            "path": "docs/PRIVATE_CANARY.md",
            "doc_id": "canary:private",
            "topic_id": "canary:private",
            "security": target_security,
        },
    )
    output = tmp_path / "view"
    if target_security == "RESTRICTED":
        assert_denied(
            typed_engine,
            lambda: typed_engine.build_view(root, release, output, audience="operator"),
            "CROSS_BOUNDARY_VIEW_REFERENCE",
        )
        assert not output.exists()
    else:
        assert typed_engine.build_view(root, release, output, audience="operator")["complete"]
        assert typed_engine.validate_view(root, release, output, audience="operator")["valid"]
        # The typed target is ordinary text, not an extra selected release page.
        rendered = "\n".join(path.read_text() for path in output.glob("*.html"))
        assert "PRIVATE_CANARY.md" in rendered


@pytest.mark.parametrize(
    "body",
    [
        "# Guide\nRequires " + chr(96) + "PRIVATE_CANARY.md" + chr(96) + ".\n",
        "# Guide\ndocs/\n└── PRIVATE_CANARY.md\n",
    ],
)
@pytest.mark.parametrize("target_security", ["RESTRICTED", "PUBLIC"])
def test_installed_cli_uses_typed_visibility(tmp_path, body, target_security):
    target = tmp_path / "installed"
    target.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(docs.ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "typed-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == docs.SCRIPT.read_bytes()
    source = docs.repository(tmp_path)
    docs.document(
        source,
        "docs/PRIVATE_CANARY.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security=target_security,
    )
    docs.document(source, security="PUBLIC", body=body)
    for folder in ("docs", "src"):
        shutil.copytree(source / folder, target / folder, dirs_exist_ok=True)
    (target / ".ai-team/policy/documentation.json").write_bytes(
        (source / ".ai-team/policy/documentation.json").read_bytes()
    )
    for command in ("inventory", "query", "index"):
        check = docs.loop_command(
            target, "docs", command, "--release", "release-2", "--security", "PUBLIC", "--history"
        )
        if target_security == "RESTRICTED":
            assert check.returncode != 0
            assert "CROSS_BOUNDARY_REFERENCE" in check.stdout + check.stderr
            assert "PRIVATE_CANARY" not in check.stdout + check.stderr
        else:
            assert check.returncode == 0, check.stdout + check.stderr
            assert json.loads(check.stdout)["count"] == 2


@pytest.mark.parametrize("literal", ["/work", "/design", "/hooks", "/tmp"])
def test_reserved_public_notation_is_not_an_implicit_file_dependency(
    typed_engine, tmp_path, literal
):
    root = docs.repository(tmp_path)
    body = "Public workflow/system notation " + chr(96) + literal + chr(96) + ".\n"
    docs.document(root, security="PUBLIC", body=body)
    raw = typed_engine.extract_typed_references(body)[0]
    value = typed_engine.classify_document_reference(raw, {"docs"})
    assert value["kind"] == "code_token"
    assert value["required"] is False
    assert select(typed_engine, root, history=True)["count"] == 1


@pytest.mark.parametrize("literal", ["/work", "/design", "/hooks", "/tmp"])
@pytest.mark.parametrize("source_kind", ["inline_code", "markdown_link", "tree"])
def test_reserved_notation_never_weakens_explicit_or_structural_targets(
    typed_engine, literal, source_kind
):
    item = {
        "reference": literal,
        "raw_reference": literal,
        "line": 1,
        "source_kind": source_kind,
        "prefix": "Requires " if source_kind == "inline_code" else "",
        "context": "",
    }
    result = typed_engine.classify_document_reference(item, {"docs"})
    assert result["kind"] == "repository_path"
    assert result["required"] is True


@pytest.mark.parametrize(
    "target",
    [
        "/work/PRIVATE_CANARY.md",
        "/design/PRIVATE_CANARY.md",
        "/hooks/PRIVATE_CANARY.md",
        "/tmp/PRIVATE_CANARY.md",
        "/PRIVATE_CANARY.md",
        "/tmp#PRIVATE_CANARY",
        "/tmp:123",
    ],
)
def test_reserved_notation_is_not_an_absolute_path_prefix_allowlist(typed_engine, tmp_path, target):
    root = docs.repository(tmp_path)
    docs.document(root, security="PUBLIC", body="Example " + chr(96) + target + chr(96))
    assert_denied(typed_engine, lambda: select(typed_engine, root, history=True))
