"""The ``git_host`` module kind, interface v1 (Work 034 OD-15, D-126, D-130; spec M-10, Q-15).

A git host is the code-host connection: where a verified change is pushed and its draft PR
opened, and where the PR's outcome is read back (D-071, D-078, D-081). The core publisher
(``execution/publish.py``) and outcome tracker (``execution/outcomes.py``) call only this
interface; which host runs is the deployment's ``git_host`` configuration. Interface v1 holds
the operations the product used before the module existed::

    push_branch(repo, remote, branch, commit) -> None
    open_draft_pr(repo, remote, head, base, title, body) -> url
    comment_pr(repo, url, body) -> None
    pr_state(repo, url) -> PullRequestState

- ``push_branch`` never forces. The branch already at ``commit`` is a reconciled push (no
  second push); the branch at another commit is ``Conflict PUBLISH_BRANCH_EXISTS``. An
  unreachable remote is ``Hold PUBLISH_REMOTE``, a refused push ``Hold PUBLISH_PUSH``.
- ``open_draft_pr`` finds or creates: an open PR for ``head`` is returned, not a second one. A
  failed create is ``Hold PR_CREATE``. ``remote`` is the remote the head was pushed to.
- ``comment_pr`` adds one comment (the multi-app link, D-081). Failure is ``Hold PR_COMMENT``.
- ``pr_state`` is read-only. It returns a :class:`PullRequestState`; a PR whose state cannot be
  read is ``Hold PR_STATE`` (the tracker keeps it unknown and reads it again).
- Host check: before any read or write, the remote's URL (push, open) or the PR URL (comment,
  read) must be on the module's configured host, else ``Hold GIT_HOST_MISMATCH`` with details
  ``{"configured": <host>, "remote": <host or null>}``. A PR URL a host returns is checked the
  same way. Hosts compare case-insensitively without the port (:func:`url_host`).
- A remote, branch, head or base starting with ``-`` is ``Hold PUBLISH_REF`` before anything
  runs (it would read as an option, as ``execution/product.py`` ``git_fresh_base`` refuses).
- ``repo`` is the operator's checkout of the app; a host runs its commands there.

The codes above, except ``GIT_HOST_MISMATCH`` and ``PUBLISH_REF``, are the codes the product
recorded before this kind existed, so a publication record or an operator view reads the same
whichever host runs.

Not in v1 (design only, S2a): reading the git server's review approval as human-approval
evidence for a knowledge space (D-115, spec K-9). That needs the approver identity and the
approved commit from the host API, under the D-115 conditions (protected branch, CODEOWNERS
per space, stale approvals dismissed, admin bypass not counted). It is added by raising this
kind's interface version with a new suite; a proposed shape is in
``specs/034-hermes-modular-platform/plan.md`` §3.9 and needs an operator decision then.

``SUITE`` is the v1 conformance suite (``conformance.git_host.v1``). Like the notifier suite it
needs an implementation built with a test transport. That transport is the *conformance host*:

- remote ``origin`` is on the module's configured host and accepts pushes;
- remote ``conformance-down`` is unreachable; ``conformance-readonly`` refuses pushes;
  ``conformance-elsewhere`` is on another host than the configured one;
- a PR create with base ``conformance-refused`` is refused;
- a PR URL the host does not know (on its own host) cannot be read or commented on.

The suite checks every code above against it, so callers can rely on the codes they store.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from ..errors import Hold, RuntimeFault
from ..modules.builtin import suite_id
from ..modules.conformance import ConformanceSuite
from ..modules.spec import INTERFACE_VERSIONS, Module

KIND = "git_host"

# The PR states the outcome tracker acts on (D-078). Any other value is read as unknown.
PR_STATES: tuple[str, ...] = ("OPEN", "MERGED", "CLOSED")

MISMATCH = "GIT_HOST_MISMATCH"
BAD_REF = "PUBLISH_REF"


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

    def open_draft_pr(
        self, repo: Path, remote: str, head: str, base: str, title: str, body: str
    ) -> str: ...

    def comment_pr(self, repo: Path, url: str, body: str) -> None: ...

    def pr_state(self, repo: Path, url: str) -> PullRequestState: ...


# -- shared rules every git host applies ----------------------------------------------------------

# scp-like git address: ``[user@]host:path`` (no scheme)
_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)")


def bare_host(host: str) -> str:
    """A host name for comparison: lowercase, without a port."""
    return host.strip().lower().split(":", 1)[0]


def url_host(url: str) -> str | None:
    """The host of a remote or PR URL (``https://``, ``ssh://``, ``git://``, ``user@host:path``),
    lowercase and without user or port; ``None`` for a local path or ``file://``."""
    value = url.strip()
    if "://" in value:
        try:
            return urlsplit(value).hostname or None
        except ValueError:
            return None
    found = _SCP.match(value)
    return found.group(1).lower() if found else None


def check_refs(*names: str) -> None:
    """Remote and branch names never start with ``-`` (they would read as options)."""
    if any(not name or name.startswith("-") for name in names):
        raise Hold(BAD_REF, "Remote and branch names cannot be empty or start with '-'")


def check_host(configured: str, url: str) -> None:
    """``GIT_HOST_MISMATCH`` unless ``url`` is on the configured host."""
    found = url_host(url)
    if found is None or found != bare_host(configured):
        raise Hold(
            MISMATCH,
            "The remote is not on the configured git host",
            details={"configured": bare_host(configured), "remote": found},
        )


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
    title = "[AMPLAI] conformance"
    try:
        # refusals that must happen before anything runs
        _expect(BAD_REF, lambda: push(repo, "-o", BRANCH, FIRST))
        _expect(BAD_REF, lambda: push(repo, "origin", "--force", FIRST))
        _expect(BAD_REF, lambda: push(repo, "", BRANCH, FIRST))
        _expect(BAD_REF, lambda: open_pr(repo, "origin", "-h", "main", title, "body"))
        _expect(BAD_REF, lambda: open_pr(repo, "origin", BRANCH, "--base", title, "body"))
        _expect(MISMATCH, lambda: push(repo, "conformance-elsewhere", BRANCH, FIRST))
        _expect(MISMATCH, lambda: open_pr(repo, "conformance-elsewhere", BRANCH, "main", title, ""))
        # the host's own refusals keep the codes callers store
        _expect("PUBLISH_REMOTE", lambda: push(repo, "conformance-down", BRANCH, FIRST))
        _expect("PUBLISH_PUSH", lambda: push(repo, "conformance-readonly", BRANCH, FIRST))
        # push: once, reconciled, never forced
        if push(repo, "origin", BRANCH, FIRST) is not None:
            raise _fail("push_branch returns nothing")
        push(repo, "origin", BRANCH, FIRST)  # reconciled, not an error
        _expect("PUBLISH_BRANCH_EXISTS", lambda: push(repo, "origin", BRANCH, OTHER))
        # PR: refused create, then find or create
        _expect(
            "PR_CREATE",
            lambda: open_pr(repo, "origin", BRANCH, "conformance-refused", title, "body"),
        )
        url = open_pr(repo, "origin", BRANCH, "main", title, "body")
        if not isinstance(url, str) or not url.strip():
            raise _fail("open_draft_pr returns the PR url")
        if open_pr(repo, "origin", BRANCH, "main", title, "body") != url:
            raise _fail("open_draft_pr finds the open PR of a head instead of opening another")
        state = read(repo, url)
        if type(state) is not PullRequestState:
            raise _fail("pr_state returns a PullRequestState")
        if state.state != "OPEN" or state.head_commit != FIRST:
            raise _fail("a new draft PR is OPEN at the pushed commit")
        if comment(repo, url, "Part of AMPLAI goal `conformance`") is not None:
            raise _fail("comment_pr returns nothing")
        _expect("PR_COMMENT", lambda: comment(repo, url + "-missing", "x"))
        _expect("PR_STATE", lambda: read(repo, url + "-missing"))
        elsewhere = "https://conformance-elsewhere.invalid/o/r/pull/1"
        _expect(MISMATCH, lambda: read(repo, elsewhere))
        _expect(MISMATCH, lambda: comment(repo, elsewhere, "x"))
    finally:
        shutil.rmtree(repo, ignore_errors=True)


SUITE = ConformanceSuite(
    suite_id=suite_id(KIND), kind=KIND, interface_version=INTERFACE_VERSIONS[KIND], check=_check
)
