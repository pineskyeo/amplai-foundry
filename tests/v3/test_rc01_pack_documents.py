"""V3-038 — Document lifecycle/render/golden pack (design/14 §5, T-057/061/092-098)."""

from __future__ import annotations

import json
from pathlib import Path

from amplai_foundry.packs.catalog import PackCatalog
from amplai_foundry.verification.runtime.visual import DocumentFreshness

REPO = Path(__file__).resolve().parents[2]


def load():
    return PackCatalog(REPO / "packs").load("documents")


def test_t057_missing_font_is_hold_not_substitution():
    _manifest, files = load()
    font = json.loads(files["verifiers/definitions/documents-font.json"])
    assert "on_missing_font: hold" in font["notes"]
    assert "HOLD" in files["context/font-policy.md"].decode()
    case = json.loads(files["eval/cases/doc-001.json"])
    assert case["verifier_definition"] == "documents-font" and "not PASS" in case["expected"]


def test_t061_freshness_runner_detects_stale_inputs():
    manifest = {
        "source_digests": {"a.md": "sha256:aa"},
        "renderer_version": "r1",
        "layout_profile": "p1",
        "output_digest": "sha256:out",
        "golden_ref": None,
    }
    fresh = DocumentFreshness.verify(manifest, {"a.md": "sha256:aa"})
    stale = DocumentFreshness.verify(manifest, {"a.md": "sha256:bb"})
    assert fresh.outcome == "pass"
    assert stale.outcome in {"fail", "inconclusive"}
    incomplete = DocumentFreshness.verify({"source_digests": {}}, {})
    assert incomplete.outcome == "inconclusive"
