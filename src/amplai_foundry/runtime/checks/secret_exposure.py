"""``check.secret_exposure``: no tracked file carries a credential (Work 034 M-16, M-19; protected).

Patterns: the scanner the artifact store and pack install already use (``runtime/evidence/cas.py``
``SECRET_PATTERNS``, ids ``secret-pattern-N``: a PEM private key header, an
``api_key``/``access_token``/``password`` assignment of 24+ characters, an ``sk-proj-``/
``sk-ant-api03-`` key) plus this check's own (:data:`CHECK_PATTERNS`: a Slack webhook URL, a GitHub
token, an AWS access key id, an encrypted or DSA PEM key). The check's own patterns are not added
to ``cas.py``: that would change what the artifact store quarantines and what trace capture drops.

The files are ``git ls-files`` of the checkout (symlinks not followed). Config ``allow`` lists
occurrences that are there on purpose, per line content: ``<path>::<pattern id>::<line hash>``
(the first 16 hex digits of the SHA-256 of the line that holds the match, without its end of
line). A match on a line with other content is a finding; the same line content repeated in the
file shares one entry. Adding an entry loosens the check (M-19). A finding names
the path, the line number and the pattern id, never the matched text.

What it does not see: an untracked or ignored file of a local work tree (only ``git ls-files``;
in CI the checkout has tracked files only), a credential no pattern matches, and the history (a
credential removed in a later commit is still in git). A tracked file over 8 MiB is not read; it is
the finding ``SECRET_SCAN_TOO_LARGE``, so it fails rather than passes. A root that is not a git
work tree is ``SECRET_SCAN_NOT_GIT`` and a scan of no file is ``SECRET_SCAN_EMPTY``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from ..evidence.cas import SECRET_PATTERNS
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, result

MAX_FILE_BYTES = 8 * 1024 * 1024

CHECK_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("slack-webhook",
     re.compile(rb"https://hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36}(?![A-Za-z0-9])")),
    ("github-pat", re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{40,}")),
    ("aws-access-key-id", re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("pem-encrypted-or-dsa", re.compile(rb"-----BEGIN (?:ENCRYPTED|DSA) PRIVATE KEY-----")),
)  # fmt: skip


def patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    shared = tuple((f"secret-pattern-{i + 1}", p) for i, p in enumerate(SECRET_PATTERNS))
    return shared + CHECK_PATTERNS


def occurrences(data: bytes) -> Iterator[tuple[str, int, str]]:
    """``(pattern id, line number, line hash)`` of every match. The line is hashed without its
    end of line (``\\n``, and a ``\\r`` before it), so a CRLF checkout keeps the same keys."""
    for pattern_id, pattern in patterns():
        for match in pattern.finditer(data):
            start = data.rfind(b"\n", 0, match.start()) + 1
            end = data.find(b"\n", match.start())
            line = data[start : len(data) if end < 0 else end].rstrip(b"\r")
            number = data.count(b"\n", 0, match.start()) + 1
            yield pattern_id, number, hashlib.sha256(line).hexdigest()[:16]


def tracked_files(root: Path) -> list[str] | None:
    """``git ls-files`` of ``root``; ``None`` when ``root`` is not a git work tree."""
    try:
        done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                              timeout=120, check=False)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [p.decode() for p in done.stdout.split(b"\0") if p]


class SecretExposureCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        allow = set(self.check.config.get("allow", ()))
        findings: list[Finding] = []
        used: set[str] = set()
        scanned = 0
        files = tracked_files(context.root)
        if files is None:
            findings.append(Finding("SECRET_SCAN_NOT_GIT", "the root is not a git work tree: "
                                    "there is no tracked file list to scan",
                                    details={"root": str(context.root)}))  # fmt: skip
            files = []
        for rel in files:
            path = context.root / rel
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                findings.append(
                    Finding(
                        "SECRET_SCAN_TOO_LARGE", "a tracked file is larger than the scan bound", rel
                    )
                )
                continue
            scanned += 1
            for pattern_id, line, line_hash in occurrences(path.read_bytes()):
                key = f"{rel}::{pattern_id}::{line_hash}"
                if key in allow:
                    used.add(key)
                    continue
                findings.append(Finding("SECRET_PATTERN_TRACKED",
                                        f"a tracked file matches {pattern_id}", rel,
                                        {"pattern": pattern_id, "line": line,
                                         "allow_key": key}))  # fmt: skip
        if scanned == 0 and not any(f.code == "SECRET_SCAN_NOT_GIT" for f in findings):
            findings.append(Finding("SECRET_SCAN_EMPTY", "no tracked regular file was scanned"))
        return result(findings, scanned=scanned, allowed=sorted(used),
                      allow_unused=sorted(allow - used))  # fmt: skip
