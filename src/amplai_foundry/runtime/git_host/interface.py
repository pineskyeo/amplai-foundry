"""The ``git_host`` module kind, interface v1 (Work 034 OD-15, D-126; spec M-10, Q-15).

A git host is the code-host connection: where a verified change is pushed and its draft PR
opened, and where the PR's outcome is read back (D-071, D-078, D-081). The core publisher
(``execution/publish.py``) and outcome tracker (``execution/outcomes.py``) call only this
interface; which host runs is the deployment's ``git_host`` configuration. Interface v1 holds
exactly the operations the product used before the module existed::

    push_branch(repo, remote, branch, commit) -> None
    open_draft_pr(repo, head, base, title, body) -> url
    comment_pr(repo, url, body) -> None
    pr_state(repo, url) -> PullRequestState

- ``push_branch`` never forces. The branch already at ``commit`` is a reconciled push (no
  second push); the branch at another commit is ``Conflict PUBLISH_BRANCH_EXISTS``. An
  unreachable remote is ``Hold PUBLISH_REMOTE``, a refused push ``Hold PUBLISH_PUSH``.
- ``open_draft_pr`` finds or creates: an open PR for ``head`` is returned, not a second one. A
  failed create is ``Hold PR_CREATE``.
- ``comment_pr`` adds one comment (the multi-app link, D-081). Failure is ``Hold PR_COMMENT``.
- ``pr_state`` is read-only. It returns a :class:`PullRequestState`; a PR whose state cannot be
  read is ``Hold PR_STATE`` (the tracker keeps it unknown and reads it again).
- ``repo`` is the operator's checkout of the app; a host runs its commands there.

The codes above are the codes the product recorded before this kind existed, so a publication
record or an operator view reads the same whichever host runs.

Not in v1 (design only, S2a): reading the git server's review approval as human-approval
evidence for a knowledge space (D-115, spec K-9). That needs the approver identity and the
approved commit from the host API, under the D-115 conditions (protected branch, CODEOWNERS
per space, stale approvals dismissed, admin bypass not counted). It is added by raising this
kind's interface version with a new suite; a proposed shape is in
``specs/034-hermes-modular-platform/plan.md`` §3.9 and is not decided here.

``SUITE`` is the v1 conformance suite (``conformance.git_host.v1``). Like the notifier suite it
needs an implementation built with a test transport: it pushes, opens and reads a synthetic PR.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import RuntimeFault
from ..modules.builtin import suite_id
from ..modules.conformance import ConformanceSuite
from ..modules.spec import INTERFACE_VERSIONS, Module

KIND = "git_host"

# The PR states the outcome tracker acts on (D-078). Any other value is read as unknown.
PR_STATES: tuple[str, ...] = ("OPEN", "MERGED", "CLOSED")


@dataclass(frozen=True)
class PullRequestState:
    """One read of a PR. ``state`` is one of :data:`PR_STATES` or another host value (unknown);
    ``head_commit`` is the commit the PR's branch points at now (``None`` if the host gave none);
    the times are the host's, as text."""

    state: str
    head_commit: str | None = None
    merged_at: str | None = None
    closed_at: str | None = None


class GitHost(Module, Protocol):
    def push_branch(self, repo: Path, remote: str, branch: str, commit: str) -> None: ...

    def open_draft_pr(self, repo: Path, head: str, base: str, title: str, body: str) -> str: ...

    def comment_pr(self, repo: Path, url: str, body: str) -> None: ...

    def pr_state(self, repo: Path, url: str) -> PullRequestState: ...


# -- conformance (M-2) ----------------------------------------------------------------------------

BRANCH = "amplai/conformance"
FIRST = "1" * 40
OTHER = "2" * 40


def _fail(why: str) -> RuntimeFault:
    return RuntimeFault("CONFORMANCE_FAILED", why)


def _expect(code: str, call: Callable[[], object]) -> None:
    try:
        call()
    except RuntimeFault as fault:
        if fault.code != code:
            raise _fail(f"expected {code}, got {fault.code}") from None
        return
    raise _fail(f"expected {code}, nothing was refused")


def _check(module: Module) -> None:
    ops: dict[str, Callable[..., object]] = {}
    for name in ("push_branch", "open_draft_pr", "comment_pr", "pr_state"):
        op = getattr(module, name, None)
        if not callable(op):
            raise _fail(f"a git host has {name}")
        ops[name] = op
    push, open_pr, comment, read = (
        ops["push_branch"], ops["open_draft_pr"], ops["comment_pr"], ops["pr_state"]
    )  # fmt: skip
    repo = Path(tempfile.mkdtemp(prefix="amplai-git-host-conformance-"))
    try:
        if push(repo, "origin", BRANCH, FIRST) is not None:
            raise _fail("push_branch returns nothing")
        push(repo, "origin", BRANCH, FIRST)  # reconciled, not an error
        _expect("PUBLISH_BRANCH_EXISTS", lambda: push(repo, "origin", BRANCH, OTHER))
        url = open_pr(repo, BRANCH, "main", "[AMPLAI] conformance", "body")
        if not isinstance(url, str) or not url.strip():
            raise _fail("open_draft_pr returns the PR url")
        if open_pr(repo, BRANCH, "main", "[AMPLAI] conformance", "body") != url:
            raise _fail("open_draft_pr finds the open PR of a head instead of opening another")
        state = read(repo, url)
        if type(state) is not PullRequestState:
            raise _fail("pr_state returns a PullRequestState")
        if state.state != "OPEN" or state.head_commit != FIRST:
            raise _fail("a new draft PR is OPEN at the pushed commit")
        if comment(repo, url, "Part of AMPLAI goal `conformance`") is not None:
            raise _fail("comment_pr returns nothing")
        _expect("PR_STATE", lambda: read(repo, url + "-missing"))
    finally:
        shutil.rmtree(repo, ignore_errors=True)


SUITE = ConformanceSuite(
    suite_id=suite_id(KIND), kind=KIND, interface_version=INTERFACE_VERSIONS[KIND], check=_check
)
