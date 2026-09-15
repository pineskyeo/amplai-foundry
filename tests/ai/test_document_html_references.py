"""S15 finite reference grammar, exact dependencies and pre-output rejection."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit

import pytest

from ai import test_document_html as views
from ai import test_document_lifecycle as docs

engine = docs.engine

DIRECT = [
    '<img src="{target}" alt="image">',
    '<video poster="{target}"></video>',
    '<object data="{target}"></object>',
    '<svg:image xlink:href="{target}"/>',
    '<svg:a href="{target}">guide</svg:a>',
    '<form action="{target}"></form>',
    '<button formaction="{target}">send</button>',
    '<blockquote cite="{target}">quote</blockquote>',
    '<img longdesc="{target}" alt="image">',
    '<body background="{target}">guide</body>',
    '<html manifest="{target}">guide</html>',
    '<object codebase="{target}"></object>',
    '<object classid="{target}"></object>',
    '<img usemap="{target}">',
    '<div itemid="{target}">guide</div>',
    '<svg:image XLINK:HREF="{target}"/>',
]

UNSUPPORTED = [
    '<img srcset="PRIVATE_CANARY.md 1x">',
    '<link imagesrcset="https://example.invalid/a 1x, PRIVATE_CANARY.md 2x">',
    '<a href="https://example.invalid/" '
    'ping="https://example.invalid/a PRIVATE_CANARY.md">guide</a>',
    '<object archive="https://example.invalid/a PRIVATE_CANARY.md"></object>',
    '<div style="background-image:url(PRIVATE_CANARY.md)">guide</div>',
    "<style>div{background:url(PRIVATE_CANARY.md)}</style>",
    "<svg:style>div{background:url(PRIVATE_CANARY.md)}</svg:style>",
    '<rect fill="url(PRIVATE_CANARY.md#paint)"/>',
    '<rect filter="url(PRIVATE_CANARY.md#filter)"/>',
    '<rect stroke="u\\72l(PRIVATE_CANARY.md#paint)"/>',
    '<iframe srcdoc="&lt;a href=&quot;PRIVATE_CANARY.md&quot;&gt;guide&lt;/a&gt;"></iframe>',
    '<meta http-equiv="refresh" content="0;url=PRIVATE_CANARY.md">',
    '<script>fetch("PRIVATE_CANARY.md")</script>',
    "<img onerror=\"fetch('PRIVATE_CANARY.md')\">",
    '<animate attributeName="href" values="PRIVATE_CANARY.md"/>',
    '<param name="movie" value="PRIVATE_CANARY.md">',
    '<base href="PRIVATE_CANARY.md">',
    '<svg xml:base="PRIVATE_CANARY.md"/>',
    '<div unknown-reference="PRIVATE_CANARY.md">guide</div>',
    '<?xml-stylesheet href="PRIVATE_CANARY.md"?>',
    "[guide](file:PRIVATE_CANARY.md)",
    "[guide](file:///docs/PRIVATE_CANARY.md)",
    "[guide](javascript:PRIVATE_CANARY.md)",
    "[guide](data:text/html,PRIVATE_CANARY.md)",
    "[guide](https:PRIVATE_CANARY.md)",
    "[guide](conversation-canary://PRIVATE_CANARY)",
]


@pytest.mark.parametrize("form", DIRECT)
@pytest.mark.parametrize("history", [False, True])
def test_direct_destinations_block_every_consumer_before_ranking(engine, tmp_path, form, history):
    root = docs.private_link_fixture(
        engine, tmp_path, form.format(target="PRIVATE_CANARY.md"), history=history
    )
    assert engine.read_document(root, "docs/guide.md")["freshness"] == "verified"
    for consumer in engine.CURRENT_CONSUMERS:
        with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_REFERENCE") as caught:
            engine.select_documents(
                root,
                "release-2",
                security="PUBLIC",
                history=history,
                consumer=consumer,
                query="unrelated-no-match",
            )
        assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("form", DIRECT)
def test_supported_same_security_target_is_exact_and_review_bound(engine, tmp_path, form):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "docs/other&amp;part.md",
        doc_id="canary:target",
        topic_id="canary:target",
        security="PUBLIC",
        lifecycle="archived",
        release_ids=["release-1"],
    )
    docs.document(root, security="PUBLIC", body=form.format(target="other&amp;amp;part.md"))
    docs.reviewed(engine, root)
    assert engine.select_documents(root, "release-2", security="PUBLIC")["count"] == 1
    before = engine.read_document(root, "docs/guide.md")
    target = root / "docs/other&amp;part.md"
    target.write_text(target.read_text() + "\nTarget changed.\n")
    after = engine.read_document(root, "docs/guide.md")
    assert after["snapshot"] != before["snapshot"]
    assert after["freshness"] == "stale"


@pytest.mark.parametrize("body", UNSUPPORTED)
def test_unsupported_reference_language_fails_before_current_or_history_output(
    engine, tmp_path, body
):
    root = docs.repository(tmp_path)
    docs.document(root, security="PUBLIC", body="# Safe initial guide\n")
    docs.reviewed(engine, root)
    assert engine.select_documents(root, "release-2", security="PUBLIC")["count"] == 1
    before = (root / ".ai-team/knowledge/document-reviews.json").read_bytes()
    docs.document(root, security="PUBLIC", body=body)
    for history in (False, True):
        with pytest.raises(engine.DocumentError, match="UNSUPPORTED_PARSER") as caught:
            engine.select_documents(root, "release-2", security="PUBLIC", history=history)
        assert "PRIVATE" not in str(caught.value)
    with pytest.raises(engine.DocumentError, match="UNSUPPORTED_PARSER"):
        docs.reviewed(engine, root)
    assert (root / ".ai-team/knowledge/document-reviews.json").read_bytes() == before


@pytest.mark.parametrize("localhost", [False, True])
def test_file_uri_naming_actual_restricted_target_never_becomes_external_link(
    engine, tmp_path, localhost
):
    root = docs.repository(tmp_path)
    docs.document(
        root,
        "docs/PRIVATE_CANARY.md",
        doc_id="canary:private",
        topic_id="canary:private",
        security="RESTRICTED",
    )
    private = root / "docs/PRIVATE_CANARY.md"
    uri = private.resolve().as_uri()
    if localhost:
        uri = uri.replace("file:///", "file://localhost/", 1)
    assert private.samefile(unquote(urlsplit(uri).path))
    docs.document(root, security="PUBLIC", body="[link](" + uri + ")")
    with pytest.raises(engine.DocumentError, match="UNSUPPORTED_PARSER") as caught:
        docs.reviewed(engine, root)
        engine.select_documents(root, "release-2", security="PUBLIC")
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("form", DIRECT[1:5])
def test_real_cli_current_history_and_internal_html_guard(engine, tmp_path, form):
    root = docs.private_link_fixture(engine, tmp_path, form.format(target="PRIVATE_CANARY.md"))
    docs.prepare_loop(root)
    for command in ("inventory", "query", "index"):
        for history in ((), ("--history",)):
            result = docs.loop_command(
                root, "docs", command, "--release", "release-2", "--security", "PUBLIC", *history
            )
            assert result.returncode != 0
            assert "CROSS_BOUNDARY_REFERENCE" in result.stdout + result.stderr
            assert "PRIVATE" not in result.stdout + result.stderr
    html_base = tmp_path / "html-fixture"
    html_base.mkdir()
    view_root, release = views.fixture(
        engine,
        html_base,
        body=form.format(target="PRIVATE_CANARY.md"),
        linked={
            "path": "docs/PRIVATE_CANARY.md",
            "doc_id": "canary:private",
            "topic_id": "canary:private",
            "security": "RESTRICTED",
        },
    )
    output = tmp_path / "site"
    with pytest.raises(engine.DocumentError, match="CROSS_BOUNDARY_VIEW_REFERENCE"):
        engine.build_view(view_root, release, output, audience="operator")
    assert not output.exists()


@pytest.mark.parametrize("body", [UNSUPPORTED[0], UNSUPPORTED[4], UNSUPPORTED[10], UNSUPPORTED[21]])
def test_installed_cli_cannot_skip_unsupported_references(engine, tmp_path, body):
    target = tmp_path / "installed"
    target.mkdir()
    installed = subprocess.run(
        [
            sys.executable,
            "-B",
            str(docs.ROOT / "tools/amplai-loop-kit/install.py"),
            "--target",
            str(target),
            "--bootstrap-baseline",
            "--app-id",
            "html-canary",
            "--project-id",
            "canary",
            "--no-git",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert installed.returncode == 0, installed.stderr
    assert (target / "scripts/amplai_docs.py").read_bytes() == docs.SCRIPT.read_bytes()
    source = docs.repository(tmp_path)
    docs.document(source, security="PUBLIC", body=body)
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
        assert "UNSUPPORTED_PARSER" in result.stdout + result.stderr
        assert "PRIVATE" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "body",
    [
        '<span class="guide" title="Public guide" '
        'aria-label="label" data-note="opaque prose">text</span>',
        '<a href="https://example.invalid/guide">guide</a>',
        '<img src="https://example.invalid/image" alt="public">',
        "Use <file / symbol / role> here.",
    ],
)
def test_finite_inert_and_external_controls_still_select(engine, tmp_path, body):
    root = docs.repository(tmp_path)
    docs.document(root, security="PUBLIC", body=body)
    docs.reviewed(engine, root)
    result = engine.select_documents(root, "release-2", security="PUBLIC")
    assert result["count"] == 1
    assert "PRIVATE" not in json.dumps(result)
