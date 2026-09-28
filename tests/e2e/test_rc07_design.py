"""Work 019 S3 (F) — design mode ends in a reviewed design document, never implementation.

mode=design produces design artifacts and review evidence only (design/03:59, design/05:48).
Stand-ins as in the rc06 rig; the real design goal is recorded in specs/019-v3-completion/runs/.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from amplai_foundry.runtime.contracts.identity import canonical
from amplai_foundry.runtime.execution.product import DESIGN_CHECK
from amplai_foundry.runtime.execution.publish import GitPublisher
from amplai_foundry.sandbox.git_workspace import CHANGE_MEDIA, PATCH_BINDING
from amplai_foundry.verification.runtime.design_check import DesignDocumentCheck
from rc06_rig import DRAFT, git, rig_with_codex

DESIGN_DRAFT = {
    **DRAFT,
    "summary": "Design how value() should evolve",
    "objective": "Decide how value() should change and why",
    "in_scope": ["value() in app.py"],
    "acceptance": [
        {"statement": "The design states the current behaviour with sources", "verifier": "design"}
    ],
    "task_class": "architecture",
}


def design_goal(rig: Any) -> str:
    rig.planner.draft_value = dict(DESIGN_DRAFT)
    submitted = rig.d.goals.submit(
        rig.actors.service, text="design value()", mode="design", key="design-1"
    )
    goal: str = submitted["goal_id"]
    rig.service.plan(goal)
    rig.service.approve(rig.operator, goal)
    return goal


def patch_of(rig: Any, record: dict[str, Any]) -> bytes:
    d = rig.d
    change = json.loads(d.artifacts.read(d.scope, record["attempts"][-1]["change"]))
    raw: bytes = d.artifacts.read(d.scope, change["patch"])
    return raw


def test_a_design_goal_is_a_design_contract_with_design_capabilities_only(
    deployment: Any, tmp_path: Path
) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "design-ok")
    goal = design_goal(rig)
    plan = rig.service.plan_record(goal)
    assert plan["mode"] == "design" and plan["base_check"] is None
    assert rig.planner.calls[-1]["mode"] == "design"
    assert list(rig.planner.calls[-1]["verifiers"]) == [DESIGN_CHECK]
    d = rig.d
    contract = d.store.get(d.scope, "goal-contract", plan["contract_ref"])
    assert contract["mode"] == "design"
    assert [c["action"] for c in contract["requested_capabilities"]] == ["workspace.design_write"]
    assert any(c["id"] == "C-DESIGN" and c["protected"] for c in contract["constraints"])
    record = loop.run_goal(goal)
    assert record["status"] == "published"
    assert "DESIGNER" in container.prompts[0] and plan["design_dir"] in container.prompts[0]
    patch = patch_of(rig, record)
    assert b"specs/design/g1/design.md" in patch and b"app.py" not in patch.split(b"\n", 1)[0]
    assert all(
        line.split(b" b/")[1].startswith(b"specs/design/")
        for line in patch.splitlines()
        if line.startswith(b"diff --git")
    )


def test_a_design_change_that_touches_code_never_verifies(deployment: Any, tmp_path: Path) -> None:
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "design-code")
    record = loop.run_goal(design_goal(rig))
    assert record["status"] == "failed" and rig.published == []
    reasons = {v["reason"] for a in record["attempts"] for v in a["verdicts"]}
    assert reasons == {"Design mode may change only files under specs/design/"}


def test_missing_sources_are_repaired_with_exact_feedback(deployment: Any, tmp_path: Path) -> None:
    rig, loop, container = rig_with_codex(deployment, tmp_path, "design-thin-first")
    record = loop.run_goal(design_goal(rig))
    assert [a["outcome"] for a in record["attempts"]] == ["fail", "pass"]
    assert "1 resolved source(s), need 2" in container.prompts[1]


def test_a_design_pr_is_labelled_and_carries_documents_only(
    deployment: Any, tmp_path: Path
) -> None:
    titles: list[str] = []
    box: dict[str, GitPublisher] = {}
    rig, loop, _ = rig_with_codex(
        deployment, tmp_path, "design-ok", publisher=lambda g: box["p"](g)
    )
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(rig.repo, "remote", "add", "origin", str(remote))
    git(rig.repo, "push", "-q", "origin", "main")

    def prs(repo: Path, head: str, base: str, title: str, body: str) -> str:
        titles.append(title)
        return "https://example.invalid/pr/9"

    box["p"] = GitPublisher(rig.service, pr_creator=prs)
    record = loop.run_goal(design_goal(rig))
    assert record["status"] == "published" and titles[0].startswith("[AMPLAI design] ")
    changed = git(remote, "diff", "--name-only", "main", record["publication"]["branch"]).split()
    assert changed == ["specs/design/g1/design.md"]


def _change(rig: Any, files: dict[str, str]) -> bytes:
    """A change artifact built the way the worker does it: base snapshot + host diff."""
    d, ws = rig.d, rig.workspaces
    base = ws.base_snapshot(d.scope, "app", "main")
    workspace = ws.materialize(d.scope, "unit-design", base)
    for path, text in files.items():
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_text(text)
    node = {"produces": [{"name": "change", "media_type": CHANGE_MEDIA, "required": True}]}
    refs = ws.collect(
        d.scope, workspace, {"change": PATCH_BINDING}, node, process_stopped=True,
        base_snapshot=base,
    )  # fmt: skip
    ws.discard(workspace)
    raw: bytes = d.artifacts.read(d.scope, refs["change"])
    return raw


def test_the_design_check_names_what_is_missing(deployment: Any, tmp_path: Path) -> None:
    rig, _loop, _ = rig_with_codex(deployment, tmp_path, "right")
    check = DesignDocumentCheck(rig.workspaces, rig.d.scope, min_sources=3)
    doc = "## Goal\nx\n## Sources\n- app.py:1\n- app.py:99\n- specs/design/a/design.md:1\n"
    got = check(_change(rig, {"specs/design/a/design.md": doc}))
    assert got.outcome == "fail"
    assert got.details["missing_sections"] == [
        "Current State", "Options", "Decision", "Risks", "Implementation Plan"
    ]  # fmt: skip
    # a line past the end does not resolve; a citation into the design itself is not a source
    assert got.details["unresolved_sources"] == ["app.py:99", "specs/design/a/design.md:1"]
    assert got.details["sources"] == ["app.py:1"]
    two = _change(rig, {"specs/design/a/design.md": doc, "specs/design/b/design.md": doc})
    assert check(two).reason == "A design goal writes exactly one design directory"
    assert canonical({"ok": True})  # identity helpers stay importable alongside the check
