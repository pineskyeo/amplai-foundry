"""V3-059 — Frontend/docs actual-render acceptance suite (design/14 §4-§5, T-057-T-063).

Real rendered views under specs/012 are inspected through the release → view manifest →
bytes chain and structural checks. The browser layer runs only with a qualified renderer;
on this host it is recorded as BROWSER_UNAVAILABLE and the suite is `inconclusive`, not pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplai_foundry.runtime.errors import Hold, RuntimeFault
from amplai_foundry.verification.runtime.render_acceptance import (
    RenderAcceptance,
    inspect_page,
    sha256_file,
)

REPO = Path(__file__).resolve().parents[3]
VIEWS = [
    "specs/012-portable-document-lifecycle/html-developer-r6",
    "specs/012-portable-document-lifecycle/html-operator-r6",
]
REGISTRY = "tests/e2e/artifacts/golden-registry.json"


def test_shipped_views_pass_digest_chain_and_structure():
    ra = RenderAcceptance(REPO)
    for view in VIEWS:
        chain = ra.chain(view)
        assert chain["outcome"] == "pass", chain
        assert chain["release_id"] == "kit-2.5.0-candidate-r6" and chain["release_drift"] == []
        for page in ra.pages(view):
            assert page["outcome"] == "pass", page
            assert page["lang"] == "ko" and page["headings"][0][0] == 1


def test_browser_layer_is_not_run_without_a_qualified_renderer_and_never_pass():
    report = RenderAcceptance(REPO).run(VIEWS)
    assert report["structural_outcome"] == "pass"
    if report["browser_outcome"] == "not_run":
        assert report["outcome"] == "inconclusive"
        assert all(v["browser"]["status"] == "BROWSER_UNAVAILABLE" for v in report["views"])
    else:
        assert report["outcome"] in {"pass", "fail"}


def test_t061_byte_drift_and_untracked_files_fail_the_chain(tmp_path):
    src = REPO / VIEWS[0]
    view = tmp_path / "specs" / "view"
    view.mkdir(parents=True)
    for p in src.iterdir():
        (view / p.name).write_bytes(p.read_bytes())
    ra = RenderAcceptance(tmp_path)
    assert ra.chain("specs/view")["byte_drift"] == []
    (view / "index.html").write_bytes(
        (view / "index.html").read_bytes() + b"<!-- regenerated later -->"
    )
    chain = ra.chain("specs/view")
    assert chain["outcome"] == "fail" and [d["path"] for d in chain["byte_drift"]] == ["index.html"]
    (view / "extra.html").write_text("<html></html>")
    assert "extra.html" in ra.chain("specs/view")["untracked_files"]


def test_structure_findings_are_named_not_pixels(tmp_path):
    page = tmp_path / "p.html"
    page.write_text(
        "<!doctype html><html><head><title></title></head><body><h2>Jump</h2>"
        '<a href="#nowhere">x</a><div id="a"></div><div id="a"></div>'
        '<script>alert(1)</script><img src="https://cdn.example/x.png"></body></html>'
    )
    result = inspect_page(page)
    assert result["outcome"] == "fail"
    names = {f.split(":")[0] for f in result["findings"]}
    assert {
        "missing_title",
        "missing_lang",
        "missing_main",
        "missing_skip_link",
        "first_heading_not_h1",
        "duplicate_ids",
        "script_present",
        "external_resource",
        "missing_csp",
        "broken_anchors",
    } <= names


def test_t059_golden_registry_is_human_sealed_and_builder_cannot_rewrite(tmp_path):
    ra = RenderAcceptance(REPO)
    actual = {}
    for view in VIEWS:
        for entry in json.loads((REPO / view / "manifest.json").read_bytes())["files"]:
            actual[f"{view}/{entry['path']}"] = sha256_file(REPO / view / entry["path"])
    ok = ra.golden_guard(REGISTRY, actual, actor_role="verifier")
    assert (
        ok["outcome"] == "pass"
        and ok["mismatches"] == []
        and ok["aesthetic_acceptance"] == "not_assessed"
    )
    with pytest.raises(RuntimeFault):
        ra.golden_guard(REGISTRY, actual, actor_role="builder")
    # a builder "erases the diff" by rewriting the baseline → seal breaks
    registry = json.loads((REPO / REGISTRY).read_bytes())
    key = next(iter(registry["goldens"]))
    tampered = {**registry, "goldens": {**registry["goldens"], key: "0" * 64}}
    (tmp_path / "golden.json").write_text(json.dumps(tampered))
    with pytest.raises(Hold) as exc:
        RenderAcceptance(tmp_path).golden_guard("golden.json", actual, actor_role="verifier")
    assert exc.value.code == "GOLDEN_TAMPERED"
    # an actual render that differs from the approved baseline is a fail, not a re-baseline
    changed = {**actual, key: "1" * 64}
    assert ra.golden_guard(REGISTRY, changed, actor_role="verifier")["mismatches"] == [key]
    unapproved = {**registry, "approved_by_role": "builder"}
    unapproved["registry_sha256"] = RenderAcceptance.seal_registry(
        registry["goldens"], approval_ref=registry["approval_ref"]
    )["registry_sha256"]
    (tmp_path / "g2.json").write_text(json.dumps(unapproved))
    with pytest.raises(Hold) as exc:
        RenderAcceptance(tmp_path).golden_guard("g2.json", actual, actor_role="verifier")
    assert exc.value.code in {"GOLDEN_UNAPPROVED", "GOLDEN_TAMPERED"}


def test_missing_manifest_holds():
    with pytest.raises(Hold) as exc:
        RenderAcceptance(REPO).chain("specs/013-amplai-v3")
    assert exc.value.code == "VIEW_MANIFEST_MISSING"
