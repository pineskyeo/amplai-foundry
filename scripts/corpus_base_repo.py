"""Build the git base repository of a corpus v2 base (Work 033 S6-base, D-098, interfaces §10.1).

    .venv/bin/python scripts/corpus_base_repo.py --base bench [--dest DIR] [--write-commit]
    .venv/bin/python scripts/corpus_base_repo.py --base bench --verify
    .venv/bin/python scripts/corpus_base_repo.py --base demo --verify      # §14 Q17

The base tree ``<corpus>/<bases[base].dir>`` becomes one root commit with a fixed author,
committer, date and message, built with git plumbing (``hash-object``, ``update-index``,
``write-tree``, ``commit-tree``), so the same tree always gives the same commit hash whatever
the file modes, ignore rules or git configuration of the machine: every file is recorded as a
regular ``100644`` blob, user and system git configuration are not read, no hook runs and the
commit is never signed.

Files are taken as ``corpus_v2`` reads a base (``local_corpus._files``: every regular file,
``__pycache__`` skipped). A ``.git`` or ``.pytest_cache`` entry or a symbolic link in the tree is
refused: the base tree must be byte-identical to its commit (§10.1).

``--dest DIR`` keeps the repository (branch ``main``, working tree checked out); without it the
repository is built in a temporary directory and removed. ``--write-commit`` writes the commit to
the base's ``commit_file`` (``bases/bench.commit``); a base whose manifest entry pins
``base_commit`` (``demo``) is never rewritten. ``--verify`` exits 1 when the built commit differs
from the recorded one (``commit_file`` or ``base_commit``).

The report (JSON on stdout) names the commit, its tree and the recorded commit. For ``demo``
the recorded commit is the Work 030 hash, which the Work 030 manifest records without how it was
made (§10.1); ``reproduced: false`` means the meta deployment installs the existing
``amplai-demo-app`` repository at that commit instead (§14 Q17). The ``tree`` value is what to
compare with ``git rev-parse <base_commit>^{tree}`` in that repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CORPUS = Path(__file__).resolve().parents[1] / "specs" / "033-harness-taxonomy" / "corpus"

# The fixed commit identity (S6-base: "a fixed author, committer and date"). The date is git's
# internal format, 2026-10-01T00:00:00Z.
IDENTITY = {
    "GIT_AUTHOR_NAME": "AMPLAI Corpus",
    "GIT_AUTHOR_EMAIL": "corpus@amplai.invalid",
    "GIT_AUTHOR_DATE": "1790812800 +0000",
    "GIT_COMMITTER_NAME": "AMPLAI Corpus",
    "GIT_COMMITTER_EMAIL": "corpus@amplai.invalid",
    "GIT_COMMITTER_DATE": "1790812800 +0000",
}
BRANCH = "main"
FILE_MODE = "100644"
SKIPPED_PARTS = ("__pycache__",)  # as local_corpus._files
REFUSED_PARTS = (".git", ".pytest_cache")


class BaseRepoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def message_for(base_id: str) -> str:
    return f"amplai corpus base {base_id}\n"


def _git_env() -> dict[str, str]:
    # As sandbox/git_workspace.py _git_env: no user or system configuration, no prompts.
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
        **IDENTITY,
    }


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-C",
            str(repo),
            *args,
        ],
        input=stdin,
        env=_git_env(),
        capture_output=True,
        timeout=300,
        check=False,
    )
    if done.returncode != 0:
        raise BaseRepoError(
            "GIT", f"git {args[0]} failed: {done.stderr.decode(errors='replace')[-400:]}"
        )
    return done.stdout.decode()


def base_files(base_dir: Path) -> dict[str, bytes]:
    """``{posix relative path: bytes}`` of the base tree, sorted by path."""
    if not base_dir.is_dir():
        raise BaseRepoError("BASE_DIR", f"no base tree at {base_dir}")
    files: dict[str, bytes] = {}
    for path in sorted(base_dir.rglob("*")):
        rel = path.relative_to(base_dir)
        if any(part in SKIPPED_PARTS for part in rel.parts):
            continue
        if any(part in REFUSED_PARTS for part in rel.parts):
            raise BaseRepoError("BASE_TREE", f"{rel.as_posix()} must not be in a base tree")
        if path.is_symlink():
            raise BaseRepoError("BASE_TREE", f"{rel.as_posix()} is a symbolic link")
        if path.is_file():
            files[rel.as_posix()] = path.read_bytes()
    if not files:
        raise BaseRepoError("BASE_DIR", f"the base tree {base_dir} has no files")
    return dict(sorted(files.items()))


def build_repo(base_dir: Path, dest: Path, message: str) -> dict[str, Any]:
    """A new repository at ``dest`` whose ``main`` is one root commit of the base tree."""
    base_dir = base_dir.resolve()
    dest = dest.resolve()
    if dest == base_dir or base_dir in dest.parents:
        raise BaseRepoError("BASE_DEST", "the repository must be outside the base tree")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise BaseRepoError("BASE_DEST", f"{dest} exists and is not an empty directory")
    files = base_files(base_dir)
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q", "--object-format=sha1", "-b", BRANCH)
    for name, data in files.items():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # blobs in path order; one hash per line, in the same order
    blobs = _git(
        dest,
        "hash-object",
        "-w",
        "--no-filters",
        "--stdin-paths",
        stdin="".join(f"{name}\n" for name in files).encode(),
    ).split()
    if len(blobs) != len(files):
        raise BaseRepoError("GIT", "hash-object returned a different number of blobs")
    index = "".join(
        f"{FILE_MODE} {blob}\t{name}\n" for name, blob in zip(files, blobs, strict=True)
    )
    _git(dest, "update-index", "--add", "--index-info", stdin=index.encode())
    tree = _git(dest, "write-tree").strip()
    commit = _git(dest, "commit-tree", tree, stdin=message.encode()).strip()
    _git(dest, "update-ref", f"refs/heads/{BRANCH}", commit)
    _git(dest, "update-index", "-q", "--refresh")
    return {"commit": commit, "tree": tree, "files": len(files)}


def load_bases(corpus: Path) -> dict[str, dict[str, str]]:
    try:
        manifest = json.loads((corpus / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise BaseRepoError("MANIFEST", f"{corpus / 'manifest.json'}: unreadable JSON") from exc
    bases = manifest.get("bases") if isinstance(manifest, dict) else None
    if not isinstance(bases, dict):
        raise BaseRepoError("MANIFEST", "manifest.json has no bases")
    return bases


def recorded_commit(corpus: Path, entry: dict[str, str]) -> str | None:
    """The commit the manifest records for a base: ``base_commit`` or the ``commit_file``."""
    if "base_commit" in entry:
        return entry["base_commit"]
    if "commit_file" in entry:
        path = corpus / entry["commit_file"]
        return path.read_text().strip() if path.is_file() else None
    return None


def run(
    corpus: Path, base_id: str, *, dest: Path | None = None, write_commit: bool = False
) -> dict[str, Any]:
    bases = load_bases(corpus)
    if base_id not in bases:
        raise BaseRepoError("BASE_UNKNOWN", f"no base {base_id!r} in the manifest")
    entry = bases[base_id]
    if "dir" not in entry:
        raise BaseRepoError("MANIFEST", f"base {base_id!r} has no dir")
    if write_commit and "commit_file" not in entry:
        raise BaseRepoError(
            "BASE_PINNED", f"base {base_id!r} has no commit_file; its commit is not written here"
        )
    base_dir = corpus / entry["dir"]
    expected = recorded_commit(corpus, entry)
    if dest is None:
        with tempfile.TemporaryDirectory(prefix="amplai-base-repo-") as scratch:
            built = build_repo(base_dir, Path(scratch) / base_id, message_for(base_id))
    else:
        built = build_repo(base_dir, dest, message_for(base_id))
    if write_commit:
        (corpus / entry["commit_file"]).write_text(built["commit"] + "\n")
    return {
        "base": base_id,
        **built,
        "dest": str(dest.resolve()) if dest is not None else None,
        "recorded": expected,
        "reproduced": None if expected is None else built["commit"] == expected,
        "written": entry["commit_file"] if write_commit else None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a corpus base git repository.")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--base", required=True, help="a base id of the corpus manifest")
    parser.add_argument("--dest", type=Path, help="keep the repository here (new or empty)")
    parser.add_argument("--write-commit", action="store_true", help="write the commit_file")
    parser.add_argument(
        "--verify", action="store_true", help="exit 1 unless the recorded commit is reproduced"
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run(args.corpus, args.base, dest=args.dest, write_commit=args.write_commit)
    except BaseRepoError as exc:
        print(f"corpus_base_repo: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.verify and report["reproduced"] is not True:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
