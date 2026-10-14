"""The GitHub module wired to a test transport, for its conformance suite (Work 034 M-2, M-16).

:class:`ConformanceHost` is the *conformance host* of ``git_host/interface.py`` for the GitHub
module: it answers the module's ``git`` and ``gh`` commands like a host with branches and PRs, and
records every call. No process runs and no host is reached. The kind conformance check
(``runtime/checks/kind_conformance.py``) builds the module with :func:`subject`;
``tests/v3/test_034_git_host.py`` uses the same transport.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .github import GitHubHost

Proc = subprocess.CompletedProcess


def done(stdout: bytes = b"", code: int = 0, stderr: bytes = b"") -> Proc[bytes]:
    return Proc(args=[], returncode=code, stdout=stdout, stderr=stderr)


class ConformanceHost:
    """The conformance host on ``host``: the GitHub module's process runner in a test."""

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
            case ["git", "remote", "get-url", name] if name in self.remotes:
                return done((self.remotes[name][0] + "\n").encode())  # the fetch URL only
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
                tip = self.branches.get("refs/heads/" + self.prs[url]["head"])
                value = {"state": "OPEN", "headRefOid": tip, "mergedAt": None, "closedAt": None}
                return done(json.dumps(value).encode())
        return done(code=1, stderr=b"not found")


def subject(workdir: Path) -> GitHubHost:
    """The GitHub module on ``github.com`` with the conformance host as its process runner. The
    suite makes its own scratch directory; ``workdir`` is not needed."""
    del workdir
    return GitHubHost(run=ConformanceHost())
