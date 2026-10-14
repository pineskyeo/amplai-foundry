"""Work 034 OD-15 — the ``git_host`` module kind and its GitHub implementation (D-126, M-13).

- The kind has interface v1 and a conformance suite; the GitHub module is a built-in that passes it
  only with a test transport and only then can be enabled (M-2).
- The GitHub module runs exactly the commands the publisher and tracker ran before the module
  (argv, working directory, environment, timeout, refusal codes); another host adds only
  ``GH_HOST`` to the ``gh`` commands.
- Configuration: an absent ``git_host`` is GitHub (today's behaviour); ``null`` is no module.
- Removal test: without a git host a verified goal stays verified and its publication holds with
  ``PUBLISH_NO_GIT_HOST``; nothing is pushed or recorded, PR reads hold, the deployment never
  loads an implementation.

No host is reached: the process runner is injected (``FakeGitHub``) or real git runs against a
bare local remote (the e2e rig).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_034_s1a_intake_route import init_home, private, settle

from amplai_foundry.runtime.errors import Conflict, Hold, RuntimeFault
from amplai_foundry.runtime.execution.outcomes import PullRequestTracker
from amplai_foundry.runtime.execution.publish import NO_GIT_HOST, GitPublisher
from amplai_foundry.runtime.git_host import SUITE, PullRequestState
from amplai_foundry.runtime.git_host.github import GitHubHost
from amplai_foundry.runtime.git_host.github import spec as github_spec
from amplai_foundry.runtime.local_deployment import (
    GitHostEntry,
    LocalConfig,
    LocalProductDeployment,
)
from amplai_foundry.runtime.modules import KINDS, ConformanceLedger, ModuleRegistry
from rc06_rig import FixedPlanner, approved, git, rig_with_codex

Proc = subprocess.CompletedProcess


def done(stdout: bytes = b"", code: int = 0, stderr: bytes = b"") -> Proc[bytes]:
    return Proc(args=[], returncode=code, stdout=stdout, stderr=stderr)


class FakeGitHub:
    """A test transport: answers the ``git`` and ``gh`` commands of the GitHub module like a
    host with branches and PRs, and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str] | None, float]] = []
        self.branches: dict[str, str] = {}
        self.prs: dict[str, dict[str, Any]] = {}

    def __call__(
        self, args: list[str], cwd: Path, env: dict[str, str] | None, timeout: float
    ) -> Proc[bytes]:
        self.calls.append((list(args), cwd, env, timeout))
        match args:
            case ["git", "ls-remote", _remote, ref]:
                commit = self.branches.get(ref)
                return done(f"{commit}\t{ref}\n".encode() if commit else b"")
            case ["git", "push", _remote, spec]:
                commit, ref = spec.split(":", 1)
                self.branches[ref] = commit
                return done()
            case ["gh", "pr", "list", "--head", head, "--json", "url", "--limit", "1"]:
                found = [{"url": u} for u, p in self.prs.items() if p["head"] == head]
                return done(json.dumps(found[:1]).encode())
            case ["gh", "pr", "create", "--draft", "--head", head, "--base", _b, "--title", _t,
                  "--body", _body]:  # fmt: skip
                url = f"https://github.example/o/r/pull/{len(self.prs) + 1}"
                self.prs[url] = {"head": head, "comments": []}
                return done(f"Creating draft pull request\n{url}\n".encode())
            case ["gh", "pr", "comment", url, "--body", body] if url in self.prs:
                self.prs[url]["comments"].append(body)
                return done()
            case ["gh", "pr", "view", url, "--json", "state,headRefOid,mergedAt,closedAt"] if (
                url in self.prs
            ):
                head = self.branches.get("refs/heads/" + self.prs[url]["head"])
                value = {"state": "OPEN", "headRefOid": head, "mergedAt": None, "closedAt": None}
                return done(json.dumps(value).encode())
        return done(code=1, stderr=b"not found")


# -- the kind and its conformance suite ----------------------------------------------------------


def test_git_host_is_a_kind_and_github_is_a_built_in_in_process_module() -> None:
    assert "git_host" in KINDS
    spec = github_spec()
    assert (spec.kind, spec.module_id, spec.placement) == (
        "git_host",
        "git_host.github",
        "in_process",
    )
    assert spec.conformance_suite == SUITE.suite_id == "conformance.git_host.v1"


def test_the_github_host_passes_the_suite_and_only_then_can_be_enabled() -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    registry = ModuleRegistry(ledger)
    spec = github_spec()
    with pytest.raises(Hold) as held:
        registry.enable(*spec.key)
    assert held.value.code == "MODULE_UNQUALIFIED"
    fake = FakeGitHub()
    result = ledger.run(spec, GitHubHost(run=fake))
    assert result.passed, result.failure
    assert registry.enable(*spec.key) == spec
    pushes = [c for c in fake.calls if c[0][:2] == ["git", "push"]]
    creates = [c for c in fake.calls if c[0][:3] == ["gh", "pr", "create"]]
    assert len(pushes) == 1 and len(creates) == 1  # reconciled push, found PR: no repeats


class ForcesPush(GitHubHost):
    def push_branch(self, repo: Path, remote: str, branch: str, commit: str) -> None:
        self.run(["git", "push", remote, f"{commit}:refs/heads/{branch}"], repo, None, 300)


class OpensTwice(GitHubHost):
    def open_draft_pr(self, repo: Path, head: str, base: str, title: str, body: str) -> str:
        created = self.run(
            ["gh", "pr", "create", "--draft", "--head", head, "--base", base, "--title", title,
             "--body", body], repo, None, 300,
        )  # fmt: skip
        return created.stdout.decode().strip().splitlines()[-1]


class StateAsDict(GitHubHost):
    def pr_state(self, repo: Path, url: str) -> Any:
        return {"state": "OPEN", "headRefOid": "1" * 40}


class RawErrors(GitHubHost):
    def pr_state(self, repo: Path, url: str) -> PullRequestState:
        if url.endswith("-missing"):
            raise KeyError(url)
        return super().pr_state(repo, url)


@pytest.mark.parametrize("broken", [ForcesPush, OpensTwice, StateAsDict, RawErrors])
def test_the_suite_fails_a_host_that_forces_duplicates_or_leaks_its_shape(
    broken: type[GitHubHost],
) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    result = ledger.run(github_spec(), broken(run=FakeGitHub()))
    assert not result.passed
    assert not ledger.passed(github_spec())


# -- the GitHub module runs the commands of before, byte for byte --------------------------------

REPO = Path("/repo")
URL = "https://github.com/o/r/pull/7"


def test_default_host_runs_the_pre_module_commands_unchanged() -> None:
    """The argv, cwd, env and timeout of ``publish.py`` / ``outcomes.py`` at ``a31d66a``."""
    fake = FakeGitHub()
    host = GitHubHost(run=fake)
    host.push_branch(REPO, "origin", "amplai/g-1", "a" * 40)
    url = host.open_draft_pr(REPO, "amplai/g-1", "main", "[AMPLAI] t", "b")
    host.comment_pr(REPO, url, "Part of AMPLAI goal `g-1` with: x")
    host.pr_state(REPO, url)
    assert fake.calls == [
        (["git", "ls-remote", "origin", "refs/heads/amplai/g-1"], REPO, None, 300),
        (["git", "push", "origin", f"{'a' * 40}:refs/heads/amplai/g-1"], REPO, None, 300),
        (["gh", "pr", "list", "--head", "amplai/g-1", "--json", "url", "--limit", "1"],
         REPO, None, 300),
        (["gh", "pr", "create", "--draft", "--head", "amplai/g-1", "--base", "main", "--title",
          "[AMPLAI] t", "--body", "b"], REPO, None, 300),
        (["gh", "pr", "comment", url, "--body", "Part of AMPLAI goal `g-1` with: x"],
         REPO, None, 300),
        (["gh", "pr", "view", url, "--json", "state,headRefOid,mergedAt,closedAt"],
         REPO, None, 120),
    ]  # fmt: skip
    assert url == "https://github.example/o/r/pull/1"  # the last stdout line, as before


def test_another_host_adds_only_gh_host_to_the_gh_commands() -> None:
    fake = FakeGitHub()
    host = GitHubHost(host="github.corp.example", run=fake)
    host.push_branch(REPO, "origin", "amplai/g-1", "a" * 40)
    host.open_draft_pr(REPO, "amplai/g-1", "main", "t", "b")
    envs = {tuple(args[:3]): env for args, _, env, _ in fake.calls}
    assert envs[("git", "ls-remote", "origin")] is None
    assert envs[("git", "push", "origin")] is None
    for key in (("gh", "pr", "list"), ("gh", "pr", "create")):
        env = envs[key]
        assert env is not None and env["GH_HOST"] == "github.corp.example"
        assert {k: v for k, v in env.items() if k != "GH_HOST"} == {
            k: v for k, v in os.environ.items() if k != "GH_HOST"
        }


@pytest.mark.parametrize(
    "call, code",
    [
        (lambda h: h.push_branch(REPO, "origin", "b", "a" * 40), "PUBLISH_REMOTE"),
        (lambda h: h.open_draft_pr(REPO, "b", "main", "t", "x"), "PR_CREATE"),
        (lambda h: h.comment_pr(REPO, URL, "x"), "PR_COMMENT"),
        (lambda h: h.pr_state(REPO, URL), "PR_STATE"),
    ],
)
def test_host_failures_keep_their_pre_module_hold_codes(call: Any, code: str) -> None:
    host = GitHubHost(run=lambda args, cwd, env, timeout: done(code=1, stderr=b"e" * 500))
    with pytest.raises(Hold) as held:
        call(host)
    assert held.value.code == code and held.value.details == "e" * 400


def test_a_refused_push_and_an_occupied_branch_keep_their_codes() -> None:
    def run(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        return done(code=0 if args[1] == "ls-remote" else 1, stderr=b"denied")

    with pytest.raises(Hold) as held:
        GitHubHost(run=run).push_branch(REPO, "origin", "b", "a" * 40)
    assert held.value.code == "PUBLISH_PUSH"
    fake = FakeGitHub()
    fake.branches["refs/heads/b"] = "c" * 40
    with pytest.raises(Conflict) as conflict:
        GitHubHost(run=fake).push_branch(REPO, "origin", "b", "a" * 40)
    assert conflict.value.code == "PUBLISH_BRANCH_EXISTS"
    assert [c[0][1] for c in fake.calls] == ["ls-remote"]  # never pushed over it


def test_pr_state_maps_the_gh_fields_and_refuses_a_non_object() -> None:
    value = {"state": "MERGED", "headRefOid": "f" * 40, "mergedAt": "t1", "closedAt": "t2"}
    host = GitHubHost(run=lambda *a: done(json.dumps(value).encode()))
    assert host.pr_state(REPO, URL) == PullRequestState("MERGED", "f" * 40, "t1", "t2")
    host = GitHubHost(run=lambda *a: done(b"[]"))
    with pytest.raises(Hold) as held:
        host.pr_state(REPO, URL)
    assert held.value.code == "PR_STATE"


# -- configuration -------------------------------------------------------------------------------


def config(home: Path, **change: Any) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((home / "local.json").read_text())
    value.update(change)
    return value


def test_absent_git_host_is_github_and_null_is_none(tmp_path: Path) -> None:
    home = init_home(tmp_path)
    assert "git_host" not in json.loads((home / "local.json").read_text())  # local-init output
    assert LocalConfig.model_validate(config(home)).git_host == GitHostEntry(
        module="git_host.github", host="github.com"
    )
    assert LocalConfig.model_validate(config(home, git_host=None)).git_host is None
    corp = LocalConfig.model_validate(config(home, git_host={"host": "github.corp.example"}))
    assert corp.git_host is not None and corp.git_host.host == "github.corp.example"


@pytest.mark.parametrize(
    "entry",
    [
        {"module": "git_host.gitlab"},
        {"host": "https://github.com"},
        {"host": "github.com/evil"},
        {"host": "-github.com"},
        {"host": "github.com", "token": "x"},
    ],
)
def test_a_git_host_entry_is_strict(tmp_path: Path, entry: dict[str, Any]) -> None:
    home = init_home(tmp_path)
    with pytest.raises(ValidationError):
        LocalConfig.model_validate(config(home, git_host=entry))


def deploy(home: Path, **change: Any) -> LocalProductDeployment:
    private(home / "local.json", json.dumps(config(home, **change)))
    dep = LocalProductDeployment(home / "local.json", start_loop=False)
    for installed in dep.service.apps.values():
        installed.planners["codex-cli"] = FixedPlanner()
    return dep


def test_the_default_deployment_publishes_through_github(tmp_path: Path) -> None:
    dep = deploy(init_home(tmp_path))
    try:
        assert isinstance(dep.git_host, GitHubHost) and dep.git_host.host == "github.com"
        publisher = dep.loop.publisher
        assert isinstance(publisher, GitPublisher) and publisher.git_host is dep.git_host
        assert dep.tracker is not None and dep.tracker.reader == dep.git_host.pr_state
    finally:
        dep.close()


# -- removal test (M-13): no git host module -----------------------------------------------------


def test_without_a_git_host_the_deployment_runs_and_never_loads_an_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = init_home(tmp_path)
    monkeypatch.delitem(sys.modules, "amplai_foundry.runtime.git_host.github", raising=False)
    monkeypatch.setitem(sys.modules, "amplai_foundry.runtime.git_host.github", None)  # removed
    dep = deploy(home, git_host=None)
    try:
        assert dep.git_host is None and dep.tracker is None
        publisher = dep.loop.publisher
        assert isinstance(publisher, GitPublisher) and publisher.git_host is None
        operator = (home / "operator.token").read_text().strip()
        with TestClient(dep.app) as client:
            response = client.post(
                "/api/v3/local/publications/sync",
                headers={"Authorization": "Bearer " + operator, "Idempotency-Key": "k"},
            )
            assert NO_GIT_HOST in response.text, response.text
            assert (
                client.get(
                    "/api/v3/local/goals", headers={"Authorization": "Bearer " + operator}
                ).status_code
                == 200
            )
        dep.loop.idle_tick()  # no tracker: nothing to read, nothing raised
    finally:
        settle(dep)
        dep.close()


def test_the_core_imports_without_the_github_module() -> None:
    """A fresh interpreter with the implementation removed still loads the publisher, tracker,
    loop and deployment: only the composition root names it, and only when it is selected."""
    code = (
        "import sys; sys.modules['amplai_foundry.runtime.git_host.github'] = None\n"
        "import amplai_foundry.runtime.execution.publish\n"
        "import amplai_foundry.runtime.execution.outcomes\n"
        "import amplai_foundry.runtime.execution.loop\n"
        "import amplai_foundry.runtime.local_deployment\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode()[-2000:]


def test_without_a_git_host_a_verified_goal_holds_its_publication(
    deployment: Any, tmp_path: Path
) -> None:
    box: dict[str, GitPublisher] = {}
    rig, loop, _ = rig_with_codex(deployment, tmp_path, "right", publisher=lambda g: box["p"](g))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(rig.repo, "remote", "add", "origin", str(remote))
    git(rig.repo, "push", "-q", "origin", "main")
    box["p"] = GitPublisher(rig.service, git_host=None)
    goal = approved(rig)
    record = loop.run_goal(goal)
    assert record["status"] == "verified"  # the goal's own result is untouched
    assert record["publication"]["error"] == NO_GIT_HOST
    assert git(remote, "for-each-ref", "refs/heads/amplai").strip() == ""  # nothing pushed
    with pytest.raises(RuntimeFault):
        rig.service.store.head(rig.service.scope, "publication", goal)  # nothing recorded
    with pytest.raises(Hold) as held:
        PullRequestTracker(rig.service, git_host=None).sync()
    assert held.value.code == NO_GIT_HOST
