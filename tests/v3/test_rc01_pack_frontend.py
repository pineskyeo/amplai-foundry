"""V3-037 — Frontend functional/visual/UX pack (design/14 §4, T-057-T-063)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplai_foundry.packs.catalog import PackCatalog
from amplai_foundry.packs.conformance import DEFAULT_RUNNERS, PackConformance
from amplai_foundry.runtime.contracts.registry import Contracts
from amplai_foundry.runtime.errors import Hold

REPO = Path(__file__).resolve().parents[2]
BROWSER_HOST = DEFAULT_RUNNERS | {"visual_render"}


def load():
    return PackCatalog(REPO / "packs").load("frontend")


def test_frontend_requires_a_real_browser_runner_not_a_skipped_pass():
    manifest, files = load()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts()).check(manifest, files)
    assert exc.value.code == "PACK_RUNNER_UNAVAILABLE"
    assert exc.value.details["runner"] == "visual_render"
    report = PackConformance(Contracts(), installed_runners=BROWSER_HOST).check(manifest, files)
    assert report["status"] == "conformant"
    assert report["runners"]["frontend-render"] == "available"


def test_t059_golden_baselines_are_human_approved_and_protected():
    manifest, files = load()
    golden = json.loads(files["verifiers/definitions/frontend-golden.json"])
    assert golden["protected"] is True and golden["golden_governance"] == "human_approved_baseline"
    files = dict(files)
    golden["golden_governance"] = "agent_updated"
    files["verifiers/definitions/frontend-golden.json"] = json.dumps(golden).encode()
    with pytest.raises(Hold) as exc:
        PackConformance(Contracts(), installed_runners=BROWSER_HOST).check(manifest, files)
    assert exc.value.code == "PACK_GOLDEN_GOVERNANCE"


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
