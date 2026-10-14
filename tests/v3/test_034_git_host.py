"""Work 034 OD-15 — the ``git_host`` module kind and its GitHub implementation (D-126, D-130, M-13).

- The kind has interface v1 and a conformance suite; the GitHub module is a built-in that passes it
  only with a test transport and only then can be enabled (M-2). The suite checks the codes
  callers store, the host check and the ``-`` refusal.
- The GitHub module runs the push and PR commands the publisher and tracker ran before the module
  (argv, working directory, environment, timeout, refusal codes), after one read-only remote URL
  lookup for the host check; another host adds only ``GH_HOST`` to the ``gh`` commands.
- Host check: a remote or PR URL on another host than the configured one is
  ``GIT_HOST_MISMATCH`` before anything is written or read (https, ssh, scp-like; case and port
  ignored).
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
from amplai_foundry.runtime.git_host.interface import check_refs, url_host
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
    """A test transport: the conformance host of ``git_host/interface.py`` on ``host``. It
    answers the ``git`` and ``gh`` commands of the GitHub module like a host with branches and
    PRs, and records every call."""

    def __init__(self, host: str = "github.com") -> None:
        self.host = host
        self.calls: list[tuple[list[str], Path, dict[str, str] | None, float]] = []
        self.branches: dict[str, str] = {}
        self.prs: dict[str, dict[str, Any]] = {}
        # remote name -> its push URLs (``git remote get-url --push --all``)
        self.remotes = {
            "origin": [f"git@{host}:o/r.git"],
            "conformance-down": [f"https://{host}/o/down.git"],
            "conformance-readonly": [f"ssh://git@{host.upper()}:22/o/ro.git"],
            "conformance-elsewhere": ["https://elsewhere.example/o/r.git"],
            "conformance-pushurl": [f"https://{host}/o/r.git", "git@elsewhere.example:o/r.git"],
        }

    def __call__(
        self, args: list[str], cwd: Path, env: dict[str, str] | None, timeout: float
    ) -> Proc[bytes]:
        self.calls.append((list(args), cwd, env, timeout))
        match args:
            case ["git", "remote", "get-url", "--push", "--all", name] if name in self.remotes:
                return done("".join(u + "\n" for u in self.remotes[name]).encode())
            case ["git", "remote", "get-url", *_]:
                return done(code=2, stderr=b"error: No such remote")
            case ["git", "ls-remote", "conformance-down", _ref]:
                return done(code=128, stderr=b"unreachable")
            case ["git", "ls-remote", _remote, ref]:
                commit = self.branches.get(ref)
                return done(f"{commit}\t{ref}\n".encode() if commit else b"")
            case ["git", "push", "conformance-readonly", _spec]:
                return done(code=1, stderr=b"denied")
            case ["git", "push", _remote, spec]:
                commit, ref = spec.split(":", 1)
                self.branches[ref] = commit
                return done()
            case ["gh", "pr", "list", "--head", head, "--json", "url", "--limit", "1"]:
                found = [{"url": u} for u, p in self.prs.items() if p["head"] == head]
                return done(json.dumps(found[:1]).encode())
            case ["gh", "pr", "create", "--draft", "--head", head, "--base", base, "--title", _t,
                  "--body", _body] if base != "conformance-refused":  # fmt: skip
                url = f"https://{self.host}/o/r/pull/{len(self.prs) + 1}"
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
        "git_host", "git_host.github", "in_process",
    )  # fmt: skip
    assert spec.conformance_suite == SUITE.suite_id == "conformance.git_host.v1"


@pytest.mark.parametrize("host", ["github.com", "github.corp.example"])
def test_the_github_host_passes_the_suite_and_only_then_can_be_enabled(host: str) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    registry = ModuleRegistry(ledger)
    spec = github_spec()
    with pytest.raises(Hold) as held:
        registry.enable(*spec.key)
    assert held.value.code == "MODULE_UNQUALIFIED"
    fake = FakeGitHub(host)
    result = ledger.run(spec, GitHubHost(host=host, run=fake))
    assert result.passed, result.failure
    assert registry.enable(*spec.key) == spec
    pushes = [c for c in fake.calls if c[0][:3] == ["git", "push", "origin"]]
    creates = [c for c in fake.calls if c[0][:3] == ["gh", "pr", "create"]]
    assert len(pushes) == 1 and len(creates) == 2  # one refused, one made; then found
    assert not [c for c in fake.calls if "conformance-elsewhere" in c[0] and c[0][1] != "remote"]


class ForcesPush(GitHubHost):
    def push_branch(self, repo: Path, remote: str, branch: str, commit: str) -> None:
        try:
            super().push_branch(repo, remote, branch, commit)
        except Conflict:
            self.run(["git", "push", remote, f"{commit}:refs/heads/{branch}"], repo, None, 300)


class OpensTwice(GitHubHost):
    def open_draft_pr(
        self, repo: Path, remote: str, head: str, base: str, title: str, body: str
    ) -> str:
        check_refs(remote, head, base)
        self._check_remote(repo, remote)
        created = self.run(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--head",
                head,
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            repo,
            None,
            300,
        )  # fmt: skip  (never looks for the open PR first)
        if created.returncode != 0:
            raise Hold("PR_CREATE", "refused")
        return created.stdout.decode().strip().splitlines()[-1]


class StateAsDict(GitHubHost):
    def pr_state(self, repo: Path, url: str) -> Any:
        state = super().pr_state(repo, url)
        return {"state": state.state, "headRefOid": state.head_commit}


class RawErrors(GitHubHost):
    def pr_state(self, repo: Path, url: str) -> PullRequestState:
        if url.endswith("-missing"):
            raise KeyError(url)
        return super().pr_state(repo, url)


class NoHostCheck(GitHubHost):
    def _check_remote(self, repo: Path, remote: str) -> None:
        return None


class TakesOptions(GitHubHost):
    def push_branch(self, repo: Path, remote: str, branch: str, commit: str) -> None:
        if branch.startswith("-") or remote.startswith("-") or not remote:
            return None
        super().push_branch(repo, remote, branch, commit)


class OtherCodes(GitHubHost):
    def comment_pr(self, repo: Path, url: str, body: str) -> None:
        try:
            super().comment_pr(repo, url, body)
        except Hold as fault:
            raise Hold("COMMENT_FAILED", fault.message) from None


@pytest.mark.parametrize(
    "broken",
    [ForcesPush, OpensTwice, StateAsDict, RawErrors, NoHostCheck, TakesOptions, OtherCodes],
)
def test_the_suite_fails_a_host_that_breaks_a_v1_rule(broken: type[GitHubHost]) -> None:
    ledger = ConformanceLedger()
    ledger.register_suite(SUITE)
    result = ledger.run(github_spec(), broken(run=FakeGitHub()))
    assert not result.passed
    assert not ledger.passed(github_spec())


# -- the GitHub module runs the commands of before ------------------------------------------------

REPO = Path("/repo")
URL = "https://github.com/o/r/pull/7"
GET_URL = (["git", "remote", "get-url", "--push", "--all", "origin"], REPO, None, 30)


def test_default_host_runs_the_pre_module_commands_after_a_remote_lookup() -> None:
    """The argv, cwd, env and timeout of ``publish.py`` / ``outcomes.py`` at ``a31d66a``; the
    only added command is the read-only ``git remote get-url --push --all`` of the host check."""
    fake = FakeGitHub()
    host = GitHubHost(run=fake)
    host.push_branch(REPO, "origin", "amplai/g-1", "a" * 40)
    url = host.open_draft_pr(REPO, "origin", "amplai/g-1", "main", "[AMPLAI] t", "b")
    host.comment_pr(REPO, url, "Part of AMPLAI goal `g-1` with: x")
    host.pr_state(REPO, url)
    assert fake.calls == [
        GET_URL,
        (["git", "ls-remote", "origin", "refs/heads/amplai/g-1"], REPO, None, 300),
        (["git", "push", "origin", f"{'a' * 40}:refs/heads/amplai/g-1"], REPO, None, 300),
        GET_URL,
        (["gh", "pr", "list", "--head", "amplai/g-1", "--json", "url", "--limit", "1"],
         REPO, None, 300),
        (["gh", "pr", "create", "--draft", "--head", "amplai/g-1", "--base", "main", "--title",
          "[AMPLAI] t", "--body", "b"], REPO, None, 300),
        (["gh", "pr", "comment", url, "--body", "Part of AMPLAI goal `g-1` with: x"],
         REPO, None, 300),
        (["gh", "pr", "view", url, "--json", "state,headRefOid,mergedAt,closedAt"],
         REPO, None, 120),
    ]  # fmt: skip
    assert url == "https://github.com/o/r/pull/1"  # the last stdout line, as before


def test_another_host_adds_only_gh_host_to_the_gh_commands() -> None:
    fake = FakeGitHub("github.corp.example")
    host = GitHubHost(host="GitHub.Corp.Example", run=fake)
    host.push_branch(REPO, "origin", "amplai/g-1", "a" * 40)
    host.open_draft_pr(REPO, "origin", "amplai/g-1", "main", "t", "b")
    envs = {tuple(args[:3]): env for args, _, env, _ in fake.calls}
    assert envs[("git", "remote", "get-url")] is None
    assert envs[("git", "ls-remote", "origin")] is None
    assert envs[("git", "push", "origin")] is None
    for key in (("gh", "pr", "list"), ("gh", "pr", "create")):
        env = envs[key]
        assert env is not None and env["GH_HOST"] == "github.corp.example"
        assert {k: v for k, v in env.items() if k != "GH_HOST"} == {
            k: v for k, v in os.environ.items() if k != "GH_HOST"
        }


# -- host check ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, host",
    [
        ("https://github.com/o/r.git", "github.com"),
        ("https://user:token@GitHub.com:443/o/r", "github.com"),
        ("http://github.corp.example/o/r", "github.corp.example"),
        ("ssh://git@github.corp.example:2222/o/r.git", "github.corp.example"),
        ("git@GITHUB.COM:o/r.git", "github.com"),
        ("github.corp.example:o/r.git", "github.corp.example"),
        ("git://github.com/o/r", "github.com"),
        ("/tmp/remote.git", None),
        ("../remote.git", None),
        ("file:///tmp/remote.git", None),
    ],
)
def test_url_host_reads_https_ssh_and_scp_forms(url: str, host: str | None) -> None:
    assert url_host(url) == host


@pytest.mark.parametrize(
    "configured, remote",
    [
        ("github.com", "git@github.corp.example:o/r.git"),
        ("github.corp.example", "https://github.com/o/r.git"),
        ("github.com", "/tmp/remote.git"),
        ("github.com", "https://github.com.evil.example/o/r.git"),
    ],
)
def test_a_remote_on_another_host_is_refused_before_any_write(configured: str, remote: str) -> None:
    calls: list[list[str]] = []

    def run(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        calls.append(args)
        return done((remote + "\n").encode()) if args[1] == "remote" else done()

    host = GitHubHost(host=configured, run=run)
    for call in (
        lambda: host.push_branch(REPO, "origin", "amplai/g", "a" * 40),
        lambda: host.open_draft_pr(REPO, "origin", "amplai/g", "main", "t", "b"),
    ):
        with pytest.raises(Hold) as held:
            call()
        assert held.value.code == "GIT_HOST_MISMATCH"
        assert held.value.details == {"configured": [configured], "remote": url_host(remote)}
    assert all(args[:3] == ["git", "remote", "get-url"] for args in calls)


def test_a_pr_url_on_another_host_is_neither_read_nor_commented() -> None:
    calls: list[list[str]] = []
    host = GitHubHost(
        host="github.corp.example:8443", run=lambda args, *_: calls.append(args) or done()
    )
    for call in (
        lambda: host.pr_state(REPO, "https://github.com/o/r/pull/1"),
        lambda: host.comment_pr(REPO, "https://github.com/o/r/pull/1", "x"),
    ):
        with pytest.raises(Hold) as held:
            call()
        assert held.value.details == {"configured": ["github.corp.example"], "remote": "github.com"}
    assert calls == []
    assert GitHubHost(
        host="github.corp.example:8443",
        run=lambda *_: done(b'{"state": "OPEN", "headRefOid": "f"}'),
    ).pr_state(REPO, "https://GITHUB.corp.example:8443/o/r/pull/1") == PullRequestState("OPEN", "f")


def test_a_pr_url_gh_returns_on_another_host_is_refused() -> None:
    fake = FakeGitHub()
    fake.prs["https://elsewhere.example/o/r/pull/9"] = {"head": "amplai/g", "comments": []}
    with pytest.raises(Hold) as held:
        GitHubHost(run=fake).open_draft_pr(REPO, "origin", "amplai/g", "main", "t", "b")
    assert held.value.code == "GIT_HOST_MISMATCH"
    assert held.value.details == {  # the operator can find the PR that exists
        "configured": ["github.com"], "remote": "elsewhere.example",
        "pr_url": "https://elsewhere.example/o/r/pull/9",
    }  # fmt: skip


# -- push URLs as git uses them (real git, no network) ------------------------------------------


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's own git config (e.g. insteadOf rules) never reaches these checkouts."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def checkout(tmp_path: Path, *config: list[str]) -> Path:
    repo = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for args in config:
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


@pytest.mark.parametrize(
    "config, remote",
    [
        # fetch from github.com, push elsewhere
        ([["remote", "add", "origin", "https://github.com/o/r.git"],
          ["remote", "set-url", "--push", "origin", "git@evil.example:o/r.git"]], "evil.example"),
        # a second push URL elsewhere
        ([["remote", "add", "origin", "https://github.com/o/r.git"],
          ["remote", "set-url", "--add", "--push", "origin", "https://github.com/o/r.git"],
          ["remote", "set-url", "--add", "--push", "origin", "https://evil.example/o/r.git"]],
         "evil.example"),
        # pushInsteadOf rewrites the push URL
        ([["remote", "add", "origin", "https://github.com/o/r.git"],
          ["config", "url.git@evil.example:.pushInsteadOf", "https://github.com/"]],
         "evil.example"),
    ],
)  # fmt: skip
def test_every_push_url_must_be_on_the_host_before_a_push(
    tmp_path: Path, config: list[list[str]], remote: str
) -> None:
    repo = checkout(tmp_path, *config)
    with pytest.raises(Hold) as held:
        GitHubHost().push_branch(repo, "origin", "amplai/g", "a" * 40)
    assert held.value.code == "GIT_HOST_MISMATCH" and held.value.details["remote"] == remote


def test_a_mirror_fetch_url_with_a_github_push_url_is_allowed(tmp_path: Path) -> None:
    repo = checkout(
        tmp_path,
        ["remote", "add", "origin", "https://mirror.example/o/r.git"],
        ["remote", "set-url", "--push", "origin", "git@github.com:o/r.git"],
    )
    GitHubHost().check_remote(repo, "origin")


def test_an_unknown_remote_or_a_timed_out_lookup_is_publish_remote(tmp_path: Path) -> None:
    repo = checkout(tmp_path)
    with pytest.raises(Hold) as held:
        GitHubHost().check_remote(repo, "origin")
    assert held.value.code == "PUBLISH_REMOTE"

    def slow(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(Hold) as held:
        GitHubHost(run=slow).push_branch(REPO, "origin", "amplai/g", "a" * 40)
    assert held.value.code == "PUBLISH_REMOTE"


def test_remote_hosts_allow_an_ssh_host_but_pr_urls_stay_on_the_web_host() -> None:
    def run(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        if args[:3] == ["git", "remote", "get-url"]:
            return done(b"git@ssh.corp.example:o/r.git\n")
        return done()

    corp = GitHubHost(host="github.corp.example", remote_hosts=["SSH.Corp.Example"], run=run)
    corp.check_remote(REPO, "origin")
    with pytest.raises(Hold) as held:
        corp.pr_state(REPO, "https://ssh.corp.example/o/r/pull/1")
    assert held.value.details == {
        "configured": ["github.corp.example"],
        "remote": "ssh.corp.example",
    }
    web_only = GitHubHost(
        host="github.corp.example",
        remote_hosts=["ssh.corp.example"],
        run=lambda *a: done(b"https://github.corp.example/o/r.git\n"),
    )
    with pytest.raises(Hold) as held:  # the web host is not a remote host unless listed
        web_only.check_remote(REPO, "origin")
    assert held.value.details["configured"] == ["ssh.corp.example"]


@pytest.mark.parametrize(
    "url, host",
    [("https://github.com./o/r", "github.com"), ("git@github.com.:o/r.git", "github.com")],
)
def test_a_trailing_dot_is_ignored_in_urls(url: str, host: str) -> None:
    assert url_host(url) == host


@pytest.mark.parametrize(
    "call",
    [
        lambda h: h.push_branch(REPO, "--upload-pack=x", "b", "a" * 40),
        lambda h: h.push_branch(REPO, "origin", "-b", "a" * 40),
        lambda h: h.open_draft_pr(REPO, "origin", "-h", "main", "t", "b"),
        lambda h: h.open_draft_pr(REPO, "origin", "b", "-main", "t", "b"),
        lambda h: h.open_draft_pr(REPO, "-o", "b", "main", "t", "b"),
    ],
)
def test_names_that_read_as_options_are_refused_before_anything_runs(call: Any) -> None:
    calls: list[list[str]] = []
    with pytest.raises(Hold) as held:
        call(GitHubHost(run=lambda args, *_: calls.append(args) or done()))
    assert held.value.code == "PUBLISH_REF" and calls == []


# -- refusal codes --------------------------------------------------------------------------------


def on_host(code: int, stderr: bytes) -> Any:
    def run(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        if args[:3] == ["git", "remote", "get-url"]:
            return done(b"https://github.com/o/r.git\n")
        return done(code=code, stderr=stderr)

    return run


@pytest.mark.parametrize(
    "call, code",
    [
        (lambda h: h.push_branch(REPO, "origin", "b", "a" * 40), "PUBLISH_REMOTE"),
        (lambda h: h.open_draft_pr(REPO, "origin", "b", "main", "t", "x"), "PR_CREATE"),
        (lambda h: h.comment_pr(REPO, URL, "x"), "PR_COMMENT"),
        (lambda h: h.pr_state(REPO, URL), "PR_STATE"),
    ],
)
def test_host_failures_keep_their_pre_module_hold_codes(call: Any, code: str) -> None:
    host = GitHubHost(run=on_host(1, b"e" * 500))
    with pytest.raises(Hold) as held:
        call(host)
    assert held.value.code == code and held.value.details == "e" * 400


def test_a_refused_push_and_an_occupied_branch_keep_their_codes() -> None:
    def run(args: list[str], cwd: Path, env: Any, timeout: float) -> Proc[bytes]:
        if args[1] == "remote":
            return done(b"git@github.com:o/r.git\n")
        return done(code=0 if args[1] == "ls-remote" else 1, stderr=b"denied")

    with pytest.raises(Hold) as held:
        GitHubHost(run=run).push_branch(REPO, "origin", "b", "a" * 40)
    assert held.value.code == "PUBLISH_PUSH"
    fake = FakeGitHub()
    fake.branches["refs/heads/b"] = "c" * 40
    with pytest.raises(Conflict) as conflict:
        GitHubHost(run=fake).push_branch(REPO, "origin", "b", "a" * 40)
    assert conflict.value.code == "PUBLISH_BRANCH_EXISTS"
    assert [c[0][1] for c in fake.calls] == ["remote", "ls-remote"]  # never pushed over it


def test_pr_state_maps_the_gh_fields_and_refuses_a_non_object() -> None:
    value = {"state": "MERGED", "headRefOid": "f" * 40, "mergedAt": "t1", "closedAt": "t2"}
    host = GitHubHost(run=lambda *a: done(json.dumps(value).encode()))
    assert host.pr_state(REPO, URL) == PullRequestState("MERGED", "f" * 40, "t1", "t2")
    empty = {"state": "OPEN", "headRefOid": ""}  # "" reads as no head (None), as before (falsy)
    host = GitHubHost(run=lambda *a: done(json.dumps(empty).encode()))
    assert host.pr_state(REPO, URL).head_commit is None
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
    corp = LocalConfig.model_validate(config(home, git_host={"host": " GitHub.Corp.Example "}))
    assert corp.git_host is not None and corp.git_host.host == "github.corp.example"  # lowercase
    assert corp.git_host.remote_hosts is None  # absent: the host itself
    ssh = LocalConfig.model_validate(
        config(
            home, git_host={"host": "github.corp.example", "remote_hosts": [" SSH.corp.example"]}
        )
    )
    assert ssh.git_host is not None and ssh.git_host.remote_hosts == ["ssh.corp.example"]


@pytest.mark.parametrize(
    "entry",
    [
        {"module": "git_host.gitlab"},
        {"host": "https://github.com"},
        {"host": "github.com/evil"},
        {"host": "-github.com"},
        {"host": "github.com", "token": "x"},
        {"host": "github.com."},
        {"remote_hosts": []},
        {"remote_hosts": ["ssh host"]},
        {"remote_hosts": ["git@ssh.example"]},
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
