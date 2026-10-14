"""GitHub git host: the operator's authenticated ``gh`` and ``git`` (Work 034 OD-15, D-130, Q-15).

The push and PR commands, their arguments, working directory, timeouts and refusal codes are the
ones the publisher and outcome tracker ran before the ``git_host`` kind existed
(``execution/publish.py`` ``gh_pr_creator``/``gh_pr_linker``/``GitPublisher._push`` and
``execution/outcomes.py`` ``gh_pr_state`` at ``a31d66a``). Nothing here forces a push or
writes to the operator's checkout.

``host`` names the GitHub instance (default ``github.com``). Inside a checkout ``gh`` reads the
host from the repository remote, so ``host`` alone would not choose where a PR goes
(``gh help environment``, gh 2.88.1: ``GH_HOST`` applies "for commands where a hostname has not
been provided, or cannot be inferred from the context of a local Git repository"). The module
therefore checks the host itself (interface v1 host check): before a push or a PR create it
reads every push URL of the remote (``git remote get-url --push --all <remote>``, 30 s, as
``execution/product.py`` ``git_fresh_base``) and requires each to be on ``remote_hosts``
(default ``[host]``); before a PR comment or read it parses the PR URL against ``host``; another
host is ``Hold GIT_HOST_MISMATCH``. A PR URL ``gh`` returns is checked against ``host`` too. An
unknown remote, an unreadable one or a timed-out read is ``Hold PUBLISH_REMOTE``. For a host
other than ``github.com`` the ``gh`` commands also run with ``GH_HOST=<host>`` (the configured
value, port included; whether ``gh`` accepts a port there is not verified); for ``github.com``
they run with the process environment unchanged. Running against a company instance (GitHub
Enterprise Server or not is unconfirmed) is not verified yet (Q-15, S4c).

The process runner is injected (``run``): tests and the conformance suite never reach a host.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from ..errors import Conflict, Hold
from ..modules.builtin import BUILTIN_MODULES
from ..modules.spec import ModuleSpec
from .interface import PullRequestState, bare_host, check_host, check_refs, is_remote_url

MODULE_ID = "git_host.github"
DEFAULT_HOST = "github.com"

# (argv, cwd, env or None for the process environment, timeout seconds) -> the finished process
Run = Callable[[list[str], Path, dict[str, str] | None, float], subprocess.CompletedProcess[bytes]]

PUBLISH_TIMEOUT = 300.0  # push and PR writes (publish.py ``_run`` before the module)
READ_TIMEOUT = 120.0  # the PR state read (outcomes.py ``gh_pr_state`` before the module)
REMOTE_TIMEOUT = 30.0  # ``git remote get-url`` (as product.py ``git_fresh_base``)
STATE_FIELDS = "state,headRefOid,mergedAt,closedAt"


def run_process(
    args: list[str], cwd: Path, env: dict[str, str] | None, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    """The production runner: one process, output captured, never raises on a non-zero exit."""
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)


def spec() -> ModuleSpec:
    (found,) = (s for s in BUILTIN_MODULES if s.module_id == MODULE_ID)
    return found


class GitHubHost:
    """``host`` is the web host: PR URLs must be on it, and ``gh`` gets it as ``GH_HOST`` when it
    is not ``github.com`` (verbatim, lowercase, port included if configured). ``remote_hosts`` are
    the hosts a remote may push to (default: ``host``), for an SSH alias or a server whose SSH
    host differs from its web host."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        remote_hosts: Iterable[str] | None = None,
        run: Run = run_process,
    ) -> None:
        self.host, self.run = host.strip().lower(), run
        self.remote_hosts = tuple(h.strip().lower() for h in remote_hosts or (self.host,))
        self._spec = spec()

    @property
    def spec(self) -> ModuleSpec:
        return self._spec

    def _gh_env(self) -> dict[str, str] | None:
        if bare_host(self.host) == DEFAULT_HOST:
            return None  # unchanged process environment: the behaviour before the module
        return {**os.environ, "GH_HOST": self.host}

    def _gh(
        self, args: list[str], repo: Path, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        return self.run(["gh", *args], repo, self._gh_env(), timeout)

    def _check_remote(self, repo: Path, remote: str) -> None:
        """Every URL the remote pushes to is on a remote host. ``--push --all`` lists the push
        URLs as git uses them (``pushurl`` and ``pushInsteadOf`` applied; the fetch URL when there
        is no push URL). An app ``remote`` written as a URL (``git ls-remote``/``git push`` accept
        one, as before the module) is checked directly; a local path has no host."""
        if is_remote_url(remote):
            check_host(self.remote_hosts, remote)
            return
        args = ["git", "remote", "get-url", "--push", "--all", remote]
        try:
            named = self.run(args, repo, None, REMOTE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise Hold("PUBLISH_REMOTE", "Reading the remote URL timed out") from None
        urls = named.stdout.decode().split() if named.returncode == 0 else []
        if not urls:
            raise Hold(
                "PUBLISH_REMOTE",
                "The remote is not configured in the checkout",
                details=named.stderr.decode()[-400:],
            )
        for url in urls:
            check_host(self.remote_hosts, url)

    # -- interface v1 --------------------------------------------------------------------------
    def check_remote(self, repo: Path, remote: str) -> None:
        check_refs(remote)
        self._check_remote(repo, remote)

    def push_branch(self, repo: Path, remote: str, branch: str, commit: str) -> None:
        check_refs(remote, branch)
        self._check_remote(repo, remote)
        ref = "refs/heads/" + branch
        found = self.run(["git", "ls-remote", remote, ref], repo, None, PUBLISH_TIMEOUT)
        if found.returncode != 0:
            raise Hold(
                "PUBLISH_REMOTE", "Remote is unreachable", details=found.stderr.decode()[-400:]
            )
        existing = found.stdout.decode().split()
        if existing and existing[0] == commit:
            return  # reconciled: the push already happened
        if existing:
            raise Conflict(
                "PUBLISH_BRANCH_EXISTS", "Remote branch holds another commit; not forcing"
            )
        pushed = self.run(["git", "push", remote, f"{commit}:{ref}"], repo, None, PUBLISH_TIMEOUT)
        if pushed.returncode != 0:
            raise Hold("PUBLISH_PUSH", "git push failed", details=pushed.stderr.decode()[-400:])

    def open_draft_pr(
        self, repo: Path, remote: str, head: str, base: str, title: str, body: str
    ) -> str:
        """Create (or find) a draft PR with the operator's authenticated ``gh``."""
        check_refs(remote, head, base)
        self._check_remote(repo, remote)
        existing = self._gh(
            ["pr", "list", "--head", head, "--json", "url", "--limit", "1"], repo, PUBLISH_TIMEOUT
        )
        if existing.returncode == 0:
            found = json.loads(existing.stdout or b"[]")
            if found:
                url: str = found[0]["url"]
                check_host((self.host,), url, pr_url=url)
                return url
        created = self._gh(
            ["pr", "create", "--draft", "--head", head, "--base", base, "--title", title,
             "--body", body],
            repo, PUBLISH_TIMEOUT,
        )  # fmt: skip
        if created.returncode != 0:
            raise Hold("PR_CREATE", "gh pr create failed", details=created.stderr.decode()[-400:])
        url = created.stdout.decode().strip().splitlines()[-1]
        check_host((self.host,), url, pr_url=url)
        return url

    def comment_pr(self, repo: Path, url: str, body: str) -> None:
        """Comment on a draft PR (links the other PRs of a multi-app goal)."""
        check_host((self.host,), url)
        done = self._gh(["pr", "comment", url, "--body", body], repo, PUBLISH_TIMEOUT)
        if done.returncode != 0:
            raise Hold("PR_COMMENT", "gh pr comment failed", details=done.stderr.decode()[-400:])

    def pr_state(self, repo: Path, url: str) -> PullRequestState:
        """Read one PR with the operator's ``gh`` (read-only)."""
        check_host((self.host,), url)
        result = self._gh(["pr", "view", url, "--json", STATE_FIELDS], repo, READ_TIMEOUT)
        if result.returncode != 0:
            raise Hold("PR_STATE", "gh pr view failed", details=result.stderr.decode()[-400:])
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise Hold("PR_STATE", "gh pr view gave no PR object")
        state = value.get("state")
        return PullRequestState(
            state=state if isinstance(state, str) else "",
            head_commit=value.get("headRefOid") or None,
            merged_at=value.get("mergedAt") or None,
            closed_at=value.get("closedAt") or None,
        )
