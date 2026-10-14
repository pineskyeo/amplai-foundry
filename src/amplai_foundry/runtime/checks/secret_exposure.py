"""``check.secret_exposure``: no tracked file carries a credential (Work 034 M-16, M-19; protected).

It reuses the scanner the artifact store and pack install already use:
``runtime/evidence/cas.py`` ``scan_secrets`` (``SECRET_PATTERNS``: a PEM private key header, an
``api_key``/``access_token``/``password`` assignment of 24+ characters, an ``sk-proj-``/
``sk-ant-api03-`` key). The files are ``git ls-files`` of the checkout (symlinks not followed).
Config ``allow`` lists ``<path>::<pattern id>`` pairs that carry a pattern on purpose (the
scanners' own tests); adding one loosens the check (M-19). A finding names the path and the
pattern id, never the matched text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..evidence.cas import scan_secrets
from .interface import BuiltinCheck, CheckContext, CheckResult, Finding, result

MAX_FILE_BYTES = 8 * 1024 * 1024


def tracked_files(root: Path) -> list[str]:
    done = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=120,
                          check=False)  # fmt: skip
    if done.returncode != 0:
        raise RuntimeError("git ls-files failed: the check needs a git checkout")
    return [p.decode() for p in done.stdout.split(b"\0") if p]


class SecretExposureCheck(BuiltinCheck):
    def run(self, context: CheckContext) -> CheckResult:
        allow = set(self.check.config.get("allow", ()))
        findings: list[Finding] = []
        used: set[str] = set()
        scanned = skipped_large = 0
        for rel in tracked_files(context.root):
            path = context.root / rel
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                skipped_large += 1
                findings.append(
                    Finding(
                        "SECRET_SCAN_TOO_LARGE", "a tracked file is larger than the scan bound", rel
                    )
                )
                continue
            scanned += 1
            for pattern in scan_secrets(path.read_bytes()):
                key = f"{rel}::{pattern}"
                if key in allow:
                    used.add(key)
                    continue
                findings.append(Finding("SECRET_PATTERN_TRACKED",
                                        f"a tracked file matches {pattern}", rel,
                                        {"pattern": pattern}))  # fmt: skip
        return result(findings, scanned=scanned, allowed=sorted(used),
                      allow_unused=sorted(allow - used))  # fmt: skip
