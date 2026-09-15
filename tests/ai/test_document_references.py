"""S12: complete reference boundaries and exact URI identity, disposable fixtures only."""

from __future__ import annotations

import json
import subprocess
import sys
from urllib.parse import quote

import pytest

from ai import test_document_html as views
from ai import test_document_lifecycle as docs
from ai import test_document_review as reviews

engine = docs.engine
runtime = reviews.runtime


def test_non_reference_html_like_placeholder_does_not_erase_later_attribute(engine):
    assert engine.document_reference_tokens("Use <file / symbol / role> here.") == []
    tokens = engine.document_reference_tokens('<a / href="PRIVATE_CANARY.md">label</a>')
    assert [x["destination"] for x in tokens] == ["PRIVATE_CANARY.md"]


@pytest.mark.parametrize("name", ["file.md;", "#file.md", "file.md?x", "file.md[1]", "file.md*"])
def test_literal_uri_dependency_does_not_expand_globs_or_drop_punctuation(engine, tmp_path, name):
    root = docs.repository(tmp_path)
    target = root / "docs" / name
    target.write_text("Literal target version one.\n")
    if name.endswith(".md"):
        docs.document(
            root,
            "docs/" + name,
            doc_id="canary:target",
            topic_id="canary:target",
            security="PUBLIC",
            lifecycle="archived",
            release_ids=["release-1"],
        )
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["document_source_routes"] = [
        {
            "path": "docs/" + name,
            "kind": "historical",
            "security": "PUBLIC",
            "reason": "Exact local synthetic dependency, no wildcard classification.",
        }
    ]
    docs.put_json(policy_path, policy)
    docs.document(root, security="PUBLIC", body="[target]( " + quote(name, safe="") + " )")
    docs.reviewed(engine, root)
    before = engine.read_document(root, "docs/guide.md")
    assert before["freshness"] == "verified"
    decoy = root / "docs/file.md1"
    decoy.write_text("This is not the URI target.\n")
    assert engine.read_document(root, "docs/guide.md")["snapshot"] == before["snapshot"]
    target.write_text(target.read_text() + "Literal target version two.\n")
    assert engine.read_document(root, "docs/guide.md")["freshness"] == "stale"


@pytest.mark.parametrize(
    "target,path",
    [
        (" other(a(b)).md ", "docs/other(a(b)).md"),
        (r" other\(a\).md ", "docs/other(a).md"),
        ("\nother.md%23hidden.md\n", "docs/other.md#hidden.md"),
        (" other&amp;amp;part.md ", "docs/other&amp;part.md"),
    ],
)
def test_inline_renderer_uses_exact_same_uri_interpretation(engine, target, path):
    result = engine.view_inline(
        "[Other](" + target + ")",
        {"source_path": "docs/guide.md"},
        {path: {"file": "exact.html", "anchors": []}},
    )
    assert 'href="exact.html"' in result
    assert result.endswith(">Other</a>")


@pytest.mark.parametrize(
    "text",
    [
        "[PRIVATE_CANARY](<unterminated.md)",
        '<a href="PRIVATE_CANARY.md>',
        "[PRIVATE_CANARY](" + "(" * 129 + "file.md" + ")" * 130,
        "[" * 1025 + "PRIVATE_CANARY",
    ],
)
def test_ambiguous_or_overbudget_reference_fails_without_identifiers(engine, text):
    with pytest.raises(
        engine.DocumentError, match=r"UNSUPPORTED_PARSER|SCOPE_INCOMPLETE"
    ) as caught:
        engine.document_reference_tokens(text)
    assert "PRIVATE" not in str(caught.value)


def test_extensionless_html_target_requires_visibility(engine, tmp_path):
    root = docs.repository(tmp_path)
    (root / "docs/private").write_text("Restricted synthetic target.\n")
    docs.document(root, security="PUBLIC", body='<a\nhref="private">PRIVATE_TITLE_CANARY</a>')
    docs.reviewed(engine, root)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC")


@pytest.mark.parametrize(
    "body",
    [
        "[label ![image](https://example.invalid/a])]( PRIVATE_CANARY.md )",
        "[label <span title=']'>safe</span>]( PRIVATE_CANARY.md )",
    ],
)
def test_brackets_inside_opaque_uri_or_html_cannot_hide_outer_destination(engine, tmp_path, body):
    root = docs.private_link_fixture(engine, tmp_path, body)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC")


# Each form names the same exact file. Literal code remains disclosure-bearing,
# while dependency extraction intentionally excludes fenced/inline code examples.
FORMS = [
    "[PRIVATE_TITLE_CANARY]( {target} )",
    "[PRIVATE_TITLE_CANARY](\n{target}\n)",
    "[PRIVATE_TITLE_CANARY](\r\n\t{target}\r\n)",
    "[PRIVATE_TITLE_CANARY][p]\n\n[p]:\n  {target}\n",
    "[PRIVATE_TITLE_CANARY][p]\n\n> [p]:\n>   {target}\n",
    "[PRIVATE_TITLE_CANARY][p]\n\n- [p]:\n    {target}\n",
    "[PRIVATE_TITLE_CANARY\nlabel]( {target}\n'line\ntitle' )",
    "[outer [nested] PRIVATE_TITLE_CANARY]( {target} (title) )",
    '![PRIVATE_TITLE_CANARY]( <{target}> "image title" )',
    '<a\nhref="{target}">PRIVATE_TITLE_CANARY</a>',
    "<img\r\nSRC = '{target}' alt=\"PRIVATE_TITLE_CANARY\">",
    '<a title="a > b"\nHREF={target}>PRIVATE_TITLE_CANARY</a>',
    '[PRIVATE_TITLE_CANARY](<{target}>\n"multi\nline title")',
]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("history", [False, True])
def test_full_document_links_block_all_consumers_before_output(engine, tmp_path, form, history):
    root = docs.private_link_fixture(
        engine, tmp_path, form.format(target="PRIVATE_CANARY.md"), history=history
    )
    for consumer in engine.CURRENT_CONSUMERS:
        with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE") as caught:
            engine.select_documents(
                root,
                "release-2",
                security="PUBLIC",
                consumer=consumer,
                history=history,
                query="unrelated-query-no-hit",
            )
        assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("form", FORMS)
def test_same_security_links_bind_exact_dependency_and_invalidate_review(engine, tmp_path, form):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "docs/target.md",
        doc_id="canary:target",
        topic_id="canary:target",
        security="PUBLIC",
        lifecycle="archived",
        release_ids=["release-1"],
    )
    docs.document(root, security="PUBLIC", body=form.format(target="target.md"))
    docs.reviewed(engine, root)
    assert engine.select_documents(root, "release-2", security="PUBLIC")["count"] == 1
    before = engine.read_document(root, "docs/guide.md")
    target = root / "docs/target.md"
    target.write_text(target.read_text() + "\nChanged target-only bytes.\n")
    after = engine.read_document(root, "docs/guide.md")
    assert before["snapshot"] != after["snapshot"]
    assert after["freshness"] == "stale"
    assert engine.select_documents(root, "release-2", security="PUBLIC")["count"] == 0


@pytest.mark.parametrize(
    "name",
    [
        "visible.md#HIDDEN.md",
        "visible.md;",
        "visible.md ",
        "visible.md,",
        "#hidden.md",
        "visible.md?hidden.md",
        "visible.md[1]",
        "visible.md*",
    ],
)
def test_encoded_literal_name_never_binds_public_decoy(engine, tmp_path, name):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "docs/visible.md",
        doc_id="canary:decoy",
        topic_id="canary:decoy",
        security="PUBLIC",
        lifecycle="archived",
        release_ids=["release-1"],
    )
    target = root / "docs" / name
    target.write_text("Restricted filename fixture.\n")
    policy_path = root / ".ai-team/policy/documentation.json"
    policy = json.loads(policy_path.read_text())
    policy["document_source_routes"] = [
        {
            "path": "docs/" + name,
            "kind": "historical",
            "security": "RESTRICTED",
            "reason": "Exact synthetic restricted path, not a PUBLIC alias.",
        }
    ]
    docs.put_json(policy_path, policy)
    # A Markdown filename that the catalog sees still needs its one metadata owner.
    if name.endswith(".md"):
        docs.document(
            root,
            "docs/" + name,
            doc_id="canary:private",
            topic_id="canary:private",
            security="RESTRICTED",
        )
    encoded = quote(name, safe="")
    assert engine.local_markdown_target(encoded) == name
    docs.document(root, security="PUBLIC", body="[PRIVATE_TITLE_CANARY](" + encoded + ")")
    docs.reviewed(engine, root)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE") as caught:
        engine.select_documents(root, "release-2", security="PUBLIC")
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize(
    "target,expected",
    [
        (r"a\(b\(c\)\).md", "a(b(c)).md"),
        ("a(b(c)).md", "a(b(c)).md"),
        ("private&#95;file.md", "private_file.md"),
        (r"a\&amp;b.md", "a&amp;b.md"),
        ("a&amp;amp;b.md", "a&amp;b.md"),
        ("visible.md%2523HIDDEN.md", "visible.md%23HIDDEN.md"),
        ("visible.md%23HIDDEN.md#section", "visible.md#HIDDEN.md"),
    ],
)
def test_markdown_escapes_entities_and_percent_encoding_are_decoded_once(engine, target, expected):
    assert engine.local_markdown_target(target) == expected
    items = engine.extract_typed_references("Requires [label]( " + target + " )")
    assert len(items) == 1
    assert items[0]["reference"] == expected
    assert engine.classify_document_reference(items[0], {"docs"})["required"]


def test_nested_link_cannot_hide_private_inner_destination(engine, tmp_path):
    root = docs.private_link_fixture(
        engine, tmp_path, "[outer [PRIVATE_TITLE_CANARY]( PRIVATE_CANARY.md )](#part)"
    )
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC")


@pytest.mark.parametrize(
    "body",
    [
        "`[PRIVATE_TITLE_CANARY]( PRIVATE_CANARY.md )`",
        "```md\n[PRIVATE_TITLE_CANARY](\nPRIVATE_CANARY.md\n)\n```",
        '<a\nhref="PRIVATE&#95;CANARY.md">PRIVATE_TITLE_CANARY</a>',
    ],
)
def test_literal_and_entity_reference_text_is_still_disclosure_bearing(engine, tmp_path, body):
    root = docs.private_link_fixture(engine, tmp_path, body)
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE"):
        engine.select_documents(root, "release-2", security="PUBLIC")


@pytest.mark.parametrize(
    "body",
    [
        "Requires [external](\nhttps://example.invalid/manual\n)",
        'Requires <a\nhref="https://example.invalid/manual">external</a>',
    ],
)
def test_multiline_required_external_reference_is_not_downgraded(engine, tmp_path, body):
    root = docs.repository(tmp_path)
    docs.document(root, body=body)
    with pytest.raises(engine.DocumentError, match="EXTERNAL_REFERENCE_UNAVAILABLE"):
        engine.read_document(root, "docs/guide.md")


def test_dependency_code_and_tree_rules_remain_distinct_from_visibility(engine):
    body = (
        "# Top\n`[literal]( missing.md )`\n```md\n[example](\nmissing.md\n)\n```\n"
        "Requires [live](\nreal.md\n)\n```text\nsrc/\n└── api.py\n```\n"
    )
    items = engine.extract_typed_references(body)
    links = [x for x in items if x["source_kind"] == "markdown_link"]
    assert [(x["reference"], x["line"]) for x in links] == [("real.md", 8)]
    assert engine.classify_document_reference(links[0], {"src"})["explicit_dependency"]
    assert any(x["reference"] == "src/api.py" and x["source_kind"] == "tree" for x in items)


@pytest.mark.parametrize("form", [FORMS[0], FORMS[1], FORMS[3], FORMS[9]])
@pytest.mark.parametrize("history", [False, True])
def test_actual_cli_rejects_multiline_private_references(engine, tmp_path, form, history):
    root = docs.private_link_fixture(
        engine, tmp_path, form.format(target="PRIVATE_CANARY.md"), history=history
    )
    docs.prepare_loop(root)
    for command in ("inventory", "query", "index"):
        args = ("--history",) if history else ()
        result = docs.loop_command(
            root, "docs", command, "--release", "release-2", "--security", "PUBLIC", *args
        )
        assert result.returncode != 0
        assert "CROSS_BOUNDARY_REFERENCE" in result.stdout + result.stderr
        assert "PRIVATE" not in result.stdout + result.stderr


@pytest.mark.parametrize("form", [FORMS[0], FORMS[1], FORMS[3], FORMS[9]])
def test_actual_internal_html_blocks_private_reference_before_creating_bundle(
    engine, tmp_path, form
):
    root, release = views.fixture(
        engine,
        tmp_path,
        body=form.format(target="PRIVATE_CANARY.md"),
        linked={
            "path": "docs/PRIVATE_CANARY.md",
            "doc_id": "canary:private",
            "topic_id": "canary:private",
            "security": "RESTRICTED",
        },
    )
    output = tmp_path / "site"
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_VIEW_REFERENCE") as caught:
        engine.build_view(root, release, output, audience="operator")
    assert "PRIVATE" not in str(caught.value)
    assert not output.exists()


def test_actual_installed_payload_rejects_whitespace_link(engine, tmp_path):
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
    assert install.returncode == 0, install.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == docs.SCRIPT.read_bytes()
    source = docs.private_link_fixture(
        engine, tmp_path, FORMS[0].format(target="PRIVATE_CANARY.md")
    )
    # Copy only synthetic authored fixture inputs; installed runtime remains untouched.
    import shutil

    for folder in ("docs", "src", "evidence"):
        shutil.copytree(source / folder, target / folder, dirs_exist_ok=True)
    for path in (".ai-team/policy/documentation.json", ".ai-team/knowledge/document-reviews.json"):
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        (target / path).write_bytes((source / path).read_bytes())
    # The installed profile/candidate differs from the source fixture. Review the
    # installed snapshot, and prove it is fresh before testing the output guard.
    docs.reviewed(engine, target)
    assert engine.read_document(target, "docs/guide.md")["freshness"] == "verified"
    for command in ("inventory", "query", "index"):
        result = docs.loop_command(
            target, "docs", command, "--release", "release-2", "--security", "PUBLIC"
        )
        assert result.returncode != 0
        assert "CROSS_BOUNDARY_REFERENCE" in result.stdout + result.stderr
        assert "PRIVATE" not in result.stdout + result.stderr
