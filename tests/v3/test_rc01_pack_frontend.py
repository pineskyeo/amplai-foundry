"""V3-037 — Frontend functional/visual/UX pack (design/14 §4, T-057-T-063)."""

from __future__ import annotations

import json
from pathlib import Path

from amplai_foundry.packs.catalog import PackCatalog

REPO = Path(__file__).resolve().parents[2]


def load():
    return PackCatalog(REPO / "packs").load("frontend")


def test_t060_accessibility_is_an_independent_deterministic_criterion():
    _manifest, files = load()
    a11y = json.loads(files["verifiers/definitions/frontend-a11y.json"])
    assert a11y["kind"] == "deterministic" and a11y["runner"] == "structured_command"
    cases = {
        json.loads(v)["case_id"]: json.loads(v)
        for k, v in files.items()
        if k.startswith("eval/cases/")
    }
    assert cases["fe-002"]["verifier_definition"] == "frontend-a11y"
    assert cases["fe-001"]["verifier_definition"] == "frontend-golden"


def test_no_aesthetic_auto_approval_and_human_rubric_is_task_based():
    _manifest, files = load()
    iface = json.loads(files["context/interface.json"])
    assert iface["auto_approval"] is False
    assert any("aesthetics score" in item for item in iface["not_core"])
    rubric = json.loads(files["verifiers/definitions/frontend-ux-rubric.json"])
    assert rubric["kind"] == "human" and "task-based" in rubric["notes"]
