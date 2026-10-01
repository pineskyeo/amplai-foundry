"""Work 033 S9: the host-side merge of same-base part patches (interfaces.md §3.6, §5.2 strategy 8).

Contract: ``IntegrationQueue.merge(base, parts) -> MergeResult`` materializes the base in a scratch
copy, applies each part's cumulative patch with ``git apply --3way`` in part order, records clean
parts and the conflicting paths, and admits the merged tree as a base snapshot. Host only, no
agent. A clean merge gives the integration node that base; on conflicts it is the base of the
clean parts. The queue never writes outside the workspace manager's root and never touches the
operator's checkout. ``Hold MERGE_BASE`` when a part was made on another base commit,
``Hold GIT_COPY`` when git cannot finish in the scratch copy.

Real: a git repository, ``GitWorkspaceManager`` (collect, snapshot, materialize), the artifact
store of the reference deployment and ``git apply --3way``. Nothing is stubbed: the queue is a
pure host component. The re-verification of the merged change by the integration node (the suite
after the merge) is covered end to end in ``tests/e2e/test_033_s9_strategies.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from amplai_foundry.runtime.errors import Hold
from amplai_foundry.runtime.execution.integration_queue import IntegrationQueue, MergeResult
from amplai_foundry.sandbox.git_workspace import (
    CHANGE_MEDIA,
    PATCH_BINDING,
    GitWorkspaceManager,
)

NODE = {"produces": [{"name": "change", "media_type": CHANGE_MEDIA, "required": True}]}
BLOB = bytes(range(256))
A_PY = "def one():\n    return 1\n\n\ndef two():\n    return 2\n\n\ndef three():\n    return 3\n"


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}  # fmt: skip
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "app"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text(A_PY)
    (r / "b.py").write_text("B = 1\n")
    (r / "docs").mkdir()
    (r / "docs" / "guide.md").write_text("guide\n")
    (r / "data.bin").write_bytes(BLOB)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    return r


@pytest.fixture
def mgr(deployment: Any, tmp_path: Path, repo: Path) -> GitWorkspaceManager:
    return GitWorkspaceManager(tmp_path / "work", deployment.artifacts, {"app": repo})


@pytest.fixture
def queue(deployment: Any, mgr: GitWorkspaceManager) -> IntegrationQueue:
    return IntegrationQueue(mgr, deployment.scope)


class Parts:
    """Makes part changes on a base the way a run does: materialize, edit, collect."""

    def __init__(self, deployment: Any, mgr: GitWorkspaceManager) -> None:
        self.scope, self.mgr, self.count = deployment.scope, mgr, 0

    def change(self, base: dict[str, Any], edits: dict[str, bytes | str | None]) -> dict[str, Any]:
        self.count += 1
        ws = self.mgr.materialize(self.scope, f"part-{self.count}", base)
        for rel, content in edits.items():
            target = ws / rel
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content)
        out = self.mgr.collect(
            self.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True
        )
        self.mgr.discard(ws)
        change: dict[str, Any] = out["change"]
        return change

    def tree(self, snapshot: dict[str, Any]) -> dict[str, bytes]:
        """The files a copy of ``snapshot`` holds (base + its cumulative patch)."""
        self.count += 1
        ws = self.mgr.materialize(self.scope, f"look-{self.count}", snapshot)
        try:
            return {
                str(p.relative_to(ws)): p.read_bytes()
                for p in sorted(ws.rglob("*"))
                if p.is_file() and ".git" not in p.relative_to(ws).parts
            }
        finally:
            self.mgr.discard(ws)


@pytest.fixture
def parts(deployment: Any, mgr: GitWorkspaceManager) -> Parts:
    return Parts(deployment, mgr)


@pytest.fixture
def base(deployment: Any, mgr: GitWorkspaceManager) -> dict[str, Any]:
    return mgr.base_snapshot(deployment.scope, "app")


def operator_state(repo: Path) -> tuple[str, str]:
    return git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain")


# -- clean merges -----------------------------------------------------------------------------
def test_disjoint_parts_merge_cleanly_in_part_order(
    queue: IntegrationQueue,
    parts: Parts,
    base: dict[str, Any],
    repo: Path,
    mgr: GitWorkspaceManager,
) -> None:
    before, root = operator_state(repo), sorted(os.listdir(mgr.root))
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"docs/guide.md": "guide\nmore\n", "docs/new.md": "new\n"})
    root = sorted(os.listdir(mgr.root))
    result = queue.merge(base, [("node-app.p1", p1), ("node-app.p2", p2)])
    assert isinstance(result, MergeResult)
    assert result.applied == ("node-app.p1", "node-app.p2") and result.conflicts == ()
    merged = parts.tree(result.snapshot)
    assert merged["a.py"].decode() == A_PY.replace("return 1", "return 10")
    assert merged["docs/guide.md"] == b"guide\nmore\n" and merged["docs/new.md"] == b"new\n"
    assert merged["b.py"] == b"B = 1\n" and merged["data.bin"] == BLOB  # untouched files stay
    # the scratch copy is gone and the operator's checkout never moved
    assert sorted(os.listdir(mgr.root)) == root
    assert operator_state(repo) == before


def test_the_merged_snapshot_is_a_base_of_the_same_commit_with_a_cumulative_patch(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any], deployment: Any,
    mgr: GitWorkspaceManager,
) -> None:  # fmt: skip
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"b.py": "B = 2\n"})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    descriptor = mgr._descriptor(deployment.scope, result.snapshot)
    assert (descriptor["repo"], descriptor["commit"]) == ("app", mgr._descriptor(
        deployment.scope, base)["commit"])  # fmt: skip
    assert "patch" in descriptor  # base + the merged parts, applied on the same commit
    # a node that starts on the snapshot and changes nothing yields the cumulative change
    ws = mgr.materialize(deployment.scope, "integration", result.snapshot)
    out = mgr.collect(deployment.scope, ws, {"change": PATCH_BINDING}, NODE, process_stopped=True)
    patch = mgr.read_change(deployment.scope, deployment.artifacts.read(
        deployment.scope, out["change"]))[1].decode()  # fmt: skip
    assert "return 10" in patch and "+B = 2" in patch and patch.count("diff --git") == 2


def test_two_parts_of_one_file_merge_when_their_hunks_do_not_overlap(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"a.py": A_PY.replace("return 3", "return 30")})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    assert result.applied == ("p1", "p2") and result.conflicts == ()
    text = parts.tree(result.snapshot)["a.py"].decode()
    assert "return 10" in text and "return 30" in text and "<<<<" not in text


def test_deletes_new_files_and_binary_changes_merge(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"data.bin": bytes(reversed(range(256))), "docs/guide.md": None})
    p2 = parts.change(base, {"extra/new.py": "X = 1\n"})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    assert result.applied == ("p1", "p2") and result.conflicts == ()
    merged = parts.tree(result.snapshot)
    assert merged["data.bin"] == bytes(reversed(range(256)))
    assert "docs/guide.md" not in merged and merged["extra/new.py"] == b"X = 1\n"


def test_a_part_with_an_empty_patch_counts_as_merged_and_changes_nothing(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    nothing = parts.change(base, {})
    p2 = parts.change(base, {"b.py": "B = 2\n"})
    result = queue.merge(base, [("empty", nothing), ("p2", p2)])
    assert result.applied == ("empty", "p2") and result.conflicts == ()
    assert parts.tree(result.snapshot)["b.py"] == b"B = 2\n"


def test_merging_no_parts_gives_the_base_tree(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    result = queue.merge(base, [])
    assert result.applied == () and result.conflicts == ()
    assert parts.tree(result.snapshot) == parts.tree(base)


def test_a_base_that_carries_a_patch_is_committed_before_the_parts_apply(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any], deployment: Any,
    mgr: GitWorkspaceManager,
) -> None:  # fmt: skip
    ws = mgr.materialize(deployment.scope, "prior", base)
    (ws / "b.py").write_text("B = 5\n")  # the plan base is a repair copy holding an earlier change
    carrying = mgr.snapshot(deployment.scope, ws)
    p1 = parts.change(base, {"docs/guide.md": "guide\nmore\n"})
    result = queue.merge(carrying, [("p1", p1)])
    assert result.applied == ("p1",) and result.conflicts == ()
    merged = parts.tree(result.snapshot)
    assert merged["b.py"] == b"B = 5\n" and merged["docs/guide.md"] == b"guide\nmore\n"


# -- conflicts --------------------------------------------------------------------------------
def test_a_conflicting_part_is_reported_and_left_out_of_the_merged_tree(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 11"), "b.py": "B = 2\n"})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    assert result.applied == ("p1",)
    assert [(c["node_id"], c["paths"]) for c in result.conflicts] == [("p2", ["a.py"])]
    merged = parts.tree(result.snapshot)
    # the base of the clean parts: p1 only, no conflict markers, none of p2 (not even b.py)
    assert merged["a.py"].decode() == A_PY.replace("return 1", "return 10")
    assert merged["b.py"] == b"B = 1\n"
    assert all(b"<<<<<<<" not in content for content in merged.values())


def test_part_order_decides_which_of_two_conflicting_parts_wins(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 11")})
    forward = queue.merge(base, [("p1", p1), ("p2", p2)])
    backward = queue.merge(base, [("p2", p2), ("p1", p1)])
    assert (forward.applied, [c["node_id"] for c in forward.conflicts]) == (("p1",), ["p2"])
    assert (backward.applied, [c["node_id"] for c in backward.conflicts]) == (("p2",), ["p1"])
    assert "return 11" in parts.tree(backward.snapshot)["a.py"].decode()


def test_a_part_after_a_conflict_still_merges_on_the_rolled_back_tree(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10")})
    p2 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 11"), "b.py": "B = 2\n"})
    p3 = parts.change(base, {"docs/guide.md": "guide\nmore\n"})
    result = queue.merge(base, [("p1", p1), ("p2", p2), ("p3", p3)])
    assert result.applied == ("p1", "p3")
    assert [c["node_id"] for c in result.conflicts] == ["p2"]
    merged = parts.tree(result.snapshot)
    assert merged["docs/guide.md"] == b"guide\nmore\n" and merged["b.py"] == b"B = 1\n"


def test_two_parts_that_add_the_same_new_file_differently_conflict(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"new.py": "X = 1\n"})
    p2 = parts.change(base, {"new.py": "X = 2\n"})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    assert result.applied == ("p1",) and [c["node_id"] for c in result.conflicts] == ["p2"]
    assert result.conflicts[0]["paths"] == ["new.py"]
    assert parts.tree(result.snapshot)["new.py"] == b"X = 1\n"


def test_a_delete_against_an_edit_is_a_conflict(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"b.py": "B = 2\n"})
    p2 = parts.change(base, {"b.py": None})
    result = queue.merge(base, [("p1", p1), ("p2", p2)])
    assert result.applied == ("p1",) and [c["node_id"] for c in result.conflicts] == ["p2"]
    assert result.conflicts[0]["paths"] == ["b.py"]


# -- negative: Hold codes ---------------------------------------------------------------------
def test_a_part_made_on_another_base_commit_holds_merge_base(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any], repo: Path, deployment: Any,
    mgr: GitWorkspaceManager,
) -> None:  # fmt: skip
    (repo / "b.py").write_text("B = 9\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    later = mgr.base_snapshot(deployment.scope, "app")
    ok = parts.change(base, {"docs/guide.md": "guide\nmore\n"})
    other = parts.change(later, {"a.py": A_PY.replace("return 1", "return 10")})
    with pytest.raises(Hold) as held:
        queue.merge(base, [("node-app.p1", ok), ("node-app.p2", other)])
    assert held.value.code == "MERGE_BASE" and held.value.details == {"node_id": "node-app.p2"}


def test_a_git_failure_in_the_scratch_copy_holds_git_copy(tmp_path: Path) -> None:
    with pytest.raises(Hold) as held:
        IntegrationQueue._git(tmp_path, "no-such-subcommand")
    assert held.value.code == "GIT_COPY" and held.value.details["args"] == ["no-such-subcommand"]


def test_the_merge_environment_is_built_from_nothing_but_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amplai_foundry.runtime.execution import integration_queue

    monkeypatch.setenv("GIT_DIR", "/operator/checkout/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/operator/checkout")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    env = integration_queue._env()
    assert env["HOME"] == "/nonexistent" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_TERMINAL_PROMPT"] == "0"
    assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_COUNT"} & set(env)


def test_a_git_environment_pointing_at_the_operators_checkout_changes_nothing_there(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any], repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    p1 = parts.change(base, {"b.py": "B = 2\n"})
    before = operator_state(repo)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    result = queue.merge(base, [("p1", p1)])
    assert result.applied == ("p1",)
    monkeypatch.undo()
    assert operator_state(repo) == before and (repo / "b.py").read_text() == "B = 1\n"
    assert git(repo, "log", "--oneline").count("\n") == 1  # no scratch commit reached it


def test_patch_text_returns_the_cumulative_patch_of_a_part(
    queue: IntegrationQueue, parts: Parts, base: dict[str, Any]
) -> None:
    p1 = parts.change(base, {"a.py": A_PY.replace("return 1", "return 10"), "b.py": "B = 2\n"})
    text = queue.patch_text(p1)
    assert text.startswith("diff --git a/a.py b/a.py") and "+    return 10" in text
    assert "diff --git a/b.py b/b.py" in text
