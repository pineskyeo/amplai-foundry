"""Terminal-Bench 2.0 adapter: scan, selection, per-task image spec, admission, task output.

Contract: ``specs/033-harness-taxonomy/interfaces.md`` §10.5, §3.11 (``tb2``), IC-12, §14 Q7/Q8;
source facts: ``specs/033-harness-taxonomy/research-terminal-bench-2.md``.

1. ``scan``: a local clone of ``harbor-framework/terminal-bench-2`` at a pinned commit; its
   ``LICENSE`` must be Apache-2.0 (``Hold TB2_LICENSE``); every ``<name>/task.toml`` is parsed and
   a task with ``gpus > 0`` is skipped.
2. ``select_candidates``: up to 60 candidates, spread over the TB2 categories.
3. ``image_spec``: ``FROM <task docker_image>`` plus the pinned driver layer copied from the
   worker base image, uid 65534, the task working directory replaced by a link to ``/workspace``.
   ``commit_base`` writes the extracted working directory into the one ``tb2`` base repository
   as a root commit per task (``refs/heads/tb2/<name>``, fixed identity, file modes and in-tree
   symbolic links kept).
4. ``admit`` (operator-run, needs docker): the tests fail on the unmodified workspace, the
   reference ``solution/`` produces a workspace patch that makes them pass on a fresh copy, and
   ``repeats`` repeats agree; every run is a fresh container with network ``none``, uid 65534 and
   a read-only root except ``/workspace``. Every fresh copy is materialized from the recorded base
   commit as ``sandbox/git_workspace.py`` materializes a trial workspace (``git archive``, a local
   ``.git`` with the base as its only commit, permissions opened for the unprivileged uid), so a
   task is admitted against the workspace the agent receives. The container runs are injected
   (``run``), so the logic is tested without docker.
5. ``write_task``: ``tb2/<name>/`` (``task.json`` v2, ``tests/``, ``solution/``,
   ``environment.json``, ``NOTICE``) that ``corpus_v2.load`` reads.

Unknowns (§14 Q7) are explicit parameters, never defaults: the image's working directory
(``workdir``), the driver-layer paths (``driver_paths``), and the test and solution entry
commands (``test_command``, ``solution_command``). ``image_spec`` lists the ones still missing
under ``unknowns`` with ``ready: false``; ``admit`` holds ``TB2_ADMISSION`` without them.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..runtime.contracts.identity import ID, digest, digest_bytes
from ..runtime.errors import Hold, RuntimeFault
from ..sandbox.container import ContainerProfile, ContainerSandbox

# The trial workspace code path itself, so admission sees what the agent receives.
from ..sandbox.git_workspace import GitWorkspaceManager, _safe_members
from ..sandbox.git_workspace import _git_env as _workspace_git_env

TB2_REPO = "harbor-framework/terminal-bench-2"
LICENSE_SPDX = "Apache-2.0"
# The first lines of the Apache License 2.0 text, compared with whitespace collapsed.
APACHE_HEADER = "Apache License Version 2.0, January 2004"
APACHE_URL = re.compile(r"https?://www\.apache\.org/licenses/")
MAX_CANDIDATES = 60  # §10.5 step 2
ADMISSION_REPEATS = 3  # §10.5 step 4 (c)
WORKSPACE = "/workspace"  # the mount point of every run (sandbox/container.py)
UID = GID = 65534
NETWORK = "none"
# Read-only inputs of an admission run; ContainerSandbox admits trusted mounts only under
# /amplai-input/ (sandbox/container.py). The operator's test and solution commands refer to them.
TESTS_MOUNT = "/amplai-input/tests"
SOLUTION_MOUNT = "/amplai-input/solution"
# The acceptance statement of every TB2 task: TB2 ships only instruction.md, while task.json v2
# requires a non-empty acceptance list (corpus_v2._load_task). Open issue: not named by §10.5.
TB2_ACCEPTANCE = "The task's verification tests pass in the task environment."
PINNED_IMAGE = re.compile(r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}")  # as ContainerSandbox
SHA1 = re.compile(r"[0-9a-f]{40}")
SAFE_PATH = re.compile(r"/[A-Za-z0-9._+@/-]*")
REQUIRED_ENTRIES = ("instruction.md", "task.toml", "tests", "solution")
# The tb2 base repository (§10.1 base ``tb2``, §10.5 step 3): one root commit per task.
BASE_REF_PREFIX = "refs/heads/tb2/"
BASE_BRANCH = "main"
MODE_FILE, MODE_EXEC, MODE_LINK = "100644", "100755", "120000"
IDENTITY_KEYS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
)
# A task name that is also a git ref component (ID allows ':' and '..', a ref does not).
REF_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]|\.(?!\.))*")
# GitWorkspaceManager's default max_tree_bytes: a larger base is refused at materialization.
MAX_TREE_BYTES = 512 * 1024 * 1024

Runner = Callable[["ContainerRun"], int | None]
HeadReader = Callable[[Path], str]


@dataclass(frozen=True)
class Tb2Task:
    name: str
    docker_image: str
    allow_internet: bool
    gpus: int
    category: str
    difficulty: str
    agent_timeout_sec: int | None
    verifier_timeout_sec: int | None
    # Not in the §3.11 field list; appended with defaults (as corpus_v2.TaskV2 does) so that
    # write_task can copy the task files and pin the source commit.
    source_dir: Path | None = None
    commit: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerRun:
    """One fresh admission container, as the injected runner receives it."""

    purpose: str  # "tests_unmodified" | "solution" | "tests_reference"
    repeat: int
    image: str
    argv: tuple[str, ...]
    workspace: Path  # host directory mounted read-write at /workspace
    inputs: dict[str, Path]  # read-only mounts: container path -> host path
    timeout_sec: int | None
    uid: int = UID
    gid: int = GID
    network: str = NETWORK
    read_only_root: bool = True
    writable: tuple[str, ...] = field(default=(WORKSPACE,))


# -- scan ----------------------------------------------------------------------------------------


def _source_error(message: str) -> Hold:
    return Hold("TB2_SOURCE", message)


def check_license(source: Path) -> str:
    """``Apache-2.0`` when ``source/LICENSE`` is the Apache License 2.0; else ``TB2_LICENSE``."""
    path = Path(source) / "LICENSE"
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        raise Hold("TB2_LICENSE", f"{path}: no readable LICENSE") from exc
    flat = " ".join(text.split())
    if APACHE_HEADER not in flat or not APACHE_URL.search(flat):
        raise Hold("TB2_LICENSE", f"{path}: not the Apache License 2.0")
    return LICENSE_SPDX


def git_head(source: Path) -> str:
    """The checked-out commit of a clone (read-only ``git rev-parse``)."""
    done = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if done.returncode != 0:
        raise _source_error(f"{source}: not a git clone")
    return done.stdout.strip()


def _int(value: Any, where: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or value < 0:
        raise _source_error(f"{where}: a non-negative integer is required")
    return value


def _table(meta: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = meta.get(key, {})
    if not isinstance(value, dict):
        raise _source_error(f"{where}: [{key}] is not a table")
    return value


def parse_task(folder: Path, *, commit: str | None = None) -> Tb2Task:
    """One task directory: ``task.toml`` fields (research-terminal-bench-2.md) and its files."""
    name = folder.name
    if not ID.fullmatch(name):
        raise _source_error(f"{name}: task name is not a valid id")
    missing = [entry for entry in REQUIRED_ENTRIES if not (folder / entry).exists()]
    if missing:
        raise _source_error(f"{name}: missing {missing}")
    if not any(p.is_file() for p in (folder / "tests").rglob("*")):
        raise _source_error(f"{name}: tests/ has no files")
    try:
        meta = tomllib.loads((folder / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _source_error(f"{name}: task.toml is not valid TOML") from exc
    where = f"{name}/task.toml"
    metadata = _table(meta, "metadata", where)
    environment = _table(meta, "environment", where)
    agent = _table(meta, "agent", where)
    verifier = _table(meta, "verifier", where)
    image = environment.get("docker_image")
    category, difficulty = metadata.get("category"), metadata.get("difficulty")
    if not all(isinstance(v, str) and v.strip() for v in (image, category, difficulty)):
        raise _source_error(f"{where}: docker_image, category and difficulty are required")
    allow = environment.get("allow_internet")
    if not isinstance(allow, bool):
        raise _source_error(f"{where}: [environment] allow_internet must be a boolean")
    gpus = _int(environment.get("gpus"), f"{where} [environment] gpus")
    tags = metadata.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise _source_error(f"{where}: [metadata] tags must be a list of strings")
    assert isinstance(image, str) and isinstance(category, str) and isinstance(difficulty, str)
    assert gpus is not None
    return Tb2Task(
        name,
        image,
        allow,
        gpus,
        category,
        difficulty,
        _int(agent.get("timeout_sec"), f"{where} [agent] timeout_sec", optional=True),
        _int(verifier.get("timeout_sec"), f"{where} [verifier] timeout_sec", optional=True),
        folder,
        commit,
        tuple(tags),
    )


def scan_all(
    source: Path, *, commit: str | None = None, head: HeadReader | None = None
) -> tuple[list[Tb2Task], list[dict[str, Any]]]:
    """``(tasks, skipped)``: every task directory of the clone, ``gpus > 0`` skipped with reason.

    With ``commit`` the clone's checked-out commit (``head``, default ``git rev-parse``) must equal
    it (``TB2_SOURCE``); the commit is then recorded on every task.
    """
    source = Path(source)
    if not source.is_dir():
        raise _source_error(f"{source}: no such directory")
    check_license(source)
    if commit is not None:
        if not SHA1.fullmatch(commit):
            raise _source_error(f"{commit!r}: a pinned commit is a 40-hex sha")
        actual = (head or git_head)(source)
        if actual != commit:
            raise _source_error(f"{source}: checked out {actual}, pinned {commit}")
    tasks: list[Tb2Task] = []
    skipped: list[dict[str, Any]] = []
    for folder in sorted(p for p in source.iterdir() if p.is_dir()):
        if not (folder / "task.toml").is_file():
            continue
        task = parse_task(folder, commit=commit)
        if task.gpus > 0:
            skipped.append({"name": task.name, "reason": "gpus", "gpus": task.gpus})
            continue
        tasks.append(task)
    if not tasks and not skipped:
        raise _source_error(f"{source}: no <name>/task.toml found")
    return tasks, skipped


def scan(
    source: Path, *, commit: str | None = None, head: HeadReader | None = None
) -> list[Tb2Task]:
    """The tasks of a pinned clone (§3.11); ``Hold TB2_LICENSE`` unless LICENSE is Apache-2.0."""
    return scan_all(source, commit=commit, head=head)[0]


# -- selection -----------------------------------------------------------------------------------


def select_candidates(tasks: Sequence[Tb2Task], *, limit: int = MAX_CANDIDATES) -> list[Tb2Task]:
    """Up to ``limit`` tasks spread over the categories (§10.5 step 2).

    Round robin over the categories in name order, one task per category per round, tasks of a
    category in name order; the result is in that pick order. Deterministic, no randomness.
    """
    if limit < 0:
        raise RuntimeFault("TB2_SELECT", "limit must be non-negative")
    by_category: dict[str, list[Tb2Task]] = {}
    for task in sorted(tasks, key=lambda t: t.name):
        by_category.setdefault(task.category, []).append(task)
    queues = [by_category[c] for c in sorted(by_category)]
    picked: list[Tb2Task] = []
    depth = 0
    while len(picked) < limit and any(depth < len(q) for q in queues):
        for queue in queues:
            if depth < len(queue) and len(picked) < limit:
                picked.append(queue[depth])
        depth += 1
    return picked


def category_counts(tasks: Sequence[Tb2Task]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.category] = counts.get(task.category, 0) + 1
    return dict(sorted(counts.items()))


# -- image spec ----------------------------------------------------------------------------------


def _safe_path(value: str, what: str) -> str:
    if (
        not SAFE_PATH.fullmatch(value)
        or ".." in value.split("/")
        or "//" in value
        or (len(value) > 1 and value.endswith("/"))
    ):
        raise RuntimeFault("TB2_SPEC", f"{what} {value!r} is not a plain absolute path")
    return value


def image_spec(
    task: Tb2Task,
    *,
    driver_layer_image: str,
    driver_paths: Sequence[str] | None = None,
    workdir: str | None = None,
) -> dict[str, Any]:
    """The per-task worker image build spec (§10.5 step 3).

    ``FROM`` the task image, the ``driver_paths`` copied from ``driver_layer_image`` (the worker
    base image, ``deployment/local-container-app-*.json`` ``base_image``), user 65534, ``WORKDIR
    /workspace`` and the task working directory ``workdir`` replaced by a link to ``/workspace``.
    ``workdir`` (the task image's configured ``WorkingDir``) and ``driver_paths`` are 확인 필요
    (§14 Q7): when either is missing the spec has ``ready: false``, ``dockerfile: null`` and
    names it under ``unknowns``.
    """
    if not PINNED_IMAGE.fullmatch(driver_layer_image):
        raise Hold("IMAGE_NOT_PINNED", "The driver layer image needs an immutable digest")
    if not task.docker_image or any(c in task.docker_image for c in " \t\r\n"):
        raise RuntimeFault("TB2_SPEC", f"{task.name}: invalid docker_image")
    paths = tuple(_safe_path(p, "driver path") for p in driver_paths or ())
    if any(p == "/" for p in paths):
        raise RuntimeFault("TB2_SPEC", "the driver layer cannot copy /")
    if workdir is not None:
        _safe_path(workdir, "workdir")
        if workdir == "/" or workdir.startswith(WORKSPACE + "/"):
            raise RuntimeFault("TB2_SPEC", f"{task.name}: workdir {workdir!r} cannot become a link")
    unknowns: list[str] = []
    if workdir is None:
        unknowns.append("workdir: the task image's WorkingDir (확인 필요, §14 Q7)")
    if not paths:
        unknowns.append("driver_paths: the driver layer paths of the worker base image (확인 필요)")
    # Always open until an operator admission run shows it (§10.5 step 3, §14 Q7).
    notes = [
        "whether the driver layer runs on the task's base distribution (확인 필요, §14 Q7)",
        "the task image must provide /bin/sh for the RUN step (확인 필요)",
    ]
    dockerfile: str | None = None
    if not unknowns:
        assert workdir is not None
        lines = [
            f"# AMPLAI Work 033 S7a: TB2 task {task.name} ({TB2_REPO}"
            + (f"@{task.commit}" if task.commit else "")
            + ")",
            f"FROM {task.docker_image}",
            *(f"COPY --from={driver_layer_image} {p} {p}" for p in paths),
            "USER 0:0",
        ]
        if workdir == WORKSPACE:
            lines.append(f"RUN mkdir -p {WORKSPACE}")
        else:
            lines.append(
                f"RUN mkdir -p {WORKSPACE} && rm -rf {workdir} && ln -s {WORKSPACE} {workdir}"
            )
        lines += [f"USER {UID}:{GID}", f"WORKDIR {WORKSPACE}", "ENTRYPOINT []", ""]
        dockerfile = "\n".join(lines)
    spec: dict[str, Any] = {
        "schema": "amplai.tb2-image-spec.v1",
        "task": task.name,
        "source_commit": task.commit,
        "from_image": task.docker_image,
        "from_pinned": bool(PINNED_IMAGE.fullmatch(task.docker_image)),
        "driver_layer_image": driver_layer_image,
        "driver_paths": list(paths),
        "uid": UID,
        "gid": GID,
        "workdir": workdir,
        "workspace": WORKSPACE,
        "ready": dockerfile is not None,
        "unknowns": unknowns,
        "notes": notes,
        "dockerfile": dockerfile,
    }
    spec["digest"] = digest(spec)
    return spec


def workdir_command(image: str, *, engine: str = "docker") -> list[str]:
    """Operator argv that prints the image's configured ``WorkingDir`` (§14 Q7)."""
    return [engine, "image", "inspect", "--format", "{{.Config.WorkingDir}}", image]


def extract_commands(
    image: str, workdir: str, dest: Path, *, engine: str = "docker"
) -> list[list[str]]:
    """Operator argvs that copy the initial contents of ``workdir`` from ``image`` into ``dest``
    (§10.5 step 3: they become the task's commit in the tb2 base repository)."""
    _safe_path(workdir, "workdir")
    name = f"amplai-tb2-extract-{uuid.uuid4().hex[:12]}"
    return [
        [engine, "create", "--name", name, image],
        [engine, "cp", f"{name}:{workdir.rstrip('/')}/.", str(dest)],
        [engine, "rm", name],
    ]


def record_base_commit(commits_file: Path, name: str, commit: str) -> dict[str, str]:
    """Set ``{name: commit}`` in ``bases/tb2.commits.json`` (sorted keys, §10.1)."""
    if not ID.fullmatch(name) or not SHA1.fullmatch(commit):
        raise RuntimeFault("TB2_SPEC", "a task name and a 40-hex commit are required")
    commits: dict[str, str] = {}
    if commits_file.exists():
        try:
            loaded = json.loads(commits_file.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeFault("TB2_SPEC", f"{commits_file}: unreadable JSON") from exc
        if not isinstance(loaded, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()
        ):
            raise RuntimeFault("TB2_SPEC", f"{commits_file}: not a {{task: sha}} object")
        commits = loaded
    commits[name] = commit
    commits = dict(sorted(commits.items()))
    commits_file.parent.mkdir(parents=True, exist_ok=True)
    commits_file.write_text(json.dumps(commits, indent=2) + "\n")
    return commits


# -- tb2 base repository -------------------------------------------------------------------------


def _git_run(
    repo: Path,
    *args: str,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    # as scripts/corpus_base_repo.py: no user or system configuration, hooks, signing or gc
    return subprocess.run(
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
        env={**_workspace_git_env(), **(env or {})},
        capture_output=True,
        timeout=300,
        check=False,
    )


def _git(repo: Path, *args: str, error: RuntimeFault, **kw: Any) -> bytes:
    done = _git_run(repo, *args, **kw)
    if done.returncode != 0:
        detail = done.stderr.decode(errors="replace")[-400:]
        raise type(error)(error.code, f"{error.message}: git {args[0]}: {detail}")
    return done.stdout


def _base_error(message: str) -> RuntimeFault:
    return RuntimeFault("TB2_SPEC", message)


def base_entries(extracted: Path) -> tuple[dict[str, tuple[str, Path | bytes]], list[str]]:
    """``({path: (mode, source)}, dropped_empty_dirs)`` of an extracted working directory.

    A regular file is ``100755`` when its owner execute bit is set (git's rule), else
    ``100644``; its source is the file. A relative symbolic link that stays inside the tree is
    ``120000`` with the link text as its blob (``git_workspace._safe_members`` materializes it).
    Refused (``TB2_SPEC``): a ``.git`` component (git cannot record it), a link that is absolute
    or leaves the tree (materialization would refuse it), a device, pipe or socket, a path with a
    control character or a leading double quote (index-info format). Empty directories cannot be
    recorded by git; they are returned so the caller can report them.
    """
    root = Path(extracted)
    if root.is_symlink() or not root.is_dir():
        raise _base_error(f"{root}: the extracted working directory is not a directory")
    entries: dict[str, tuple[str, Path | bytes]] = {}
    dirs: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        name = rel.as_posix()
        if any(part.lower() == ".git" for part in rel.parts):
            raise _base_error(f"{name}: a .git entry cannot be recorded in the base commit")
        if name.startswith('"') or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise _base_error(f"{name!r}: unsupported path in the base commit")
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if posixpath.isabs(target) or resolved.split("/")[0] == "..":
                raise _base_error(f"{name}: symbolic link {target!r} leaves the workspace")
            entries[name] = (MODE_LINK, os.fsencode(target))
        elif stat.S_ISREG(mode):
            entries[name] = (MODE_EXEC if mode & stat.S_IXUSR else MODE_FILE, path)
        elif stat.S_ISDIR(mode):
            dirs.append(name)
        else:
            raise _base_error(f"{name}: a device, pipe or socket cannot be recorded")
    nonempty = {parent.as_posix() for name in entries for parent in Path(name).parents}
    return entries, [d for d in dirs if d not in nonempty]


def commit_base(
    extracted: Path,
    repo: Path,
    name: str,
    *,
    identity: Mapping[str, str],
    message: str,
) -> dict[str, Any]:
    """Write ``extracted`` into the one tb2 base repository as a root commit (§10.5 step 3).

    ``repo`` is the ``amplai-tb2`` repository: an existing git checkout, or a new or empty
    directory where one is initialized (branch ``main`` stays unborn). The commit is built with
    plumbing and a temporary index (the checkout's own index and working tree are not touched),
    ``identity`` fixes author, committer and dates (``scripts/corpus_base_repo.IDENTITY``) so the
    hash is reproducible, and ``refs/heads/tb2/<name>`` points at it. An existing ref at another
    commit is refused: a recorded base is never replaced.
    """
    if not ID.fullmatch(name) or not REF_NAME.fullmatch(name) or name.endswith(".lock"):
        raise _base_error(f"{name!r}: not a task name usable as a git ref")
    missing = [k for k in IDENTITY_KEYS if not identity.get(k)]
    if missing:
        raise _base_error(f"the fixed commit identity misses {missing}")
    extracted = Path(extracted).absolute()
    repo = Path(repo).absolute()
    if extracted.resolve() != extracted or repo.resolve() != repo:
        raise _base_error("the extracted tree and the repository cannot be reached through links")
    if repo == extracted or extracted in repo.parents or repo in extracted.parents:
        raise _base_error("the repository and the extracted tree must not contain each other")
    entries, empty = base_entries(extracted)
    if not entries:
        raise _base_error(f"{extracted}: the extracted working directory has no files")
    fail = _base_error(f"tb2 base commit of {name}")
    if not repo.exists() or (repo.is_dir() and not any(repo.iterdir())):
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q", "--object-format=sha1", "-b", BASE_BRANCH, error=fail)
    elif not (repo / ".git").is_dir():
        raise _base_error(f"{repo}: not a git checkout (GitWorkspaceManager needs .git)")
    sources = {n: src for n, (_, src) in entries.items() if isinstance(src, Path)}
    files = list(sources)
    blobs: dict[str, str] = {}
    if files:
        paths = "".join(f"{sources[n]}\n" for n in files).encode()
        hashed = _git(
            repo, "hash-object", "-w", "--no-filters", "--stdin-paths", stdin=paths, error=fail
        )
        out = hashed.decode().split()
        if len(out) != len(files):
            raise _base_error("hash-object returned a different number of blobs")
        blobs.update(zip(files, out, strict=True))
    for n, (_, source) in entries.items():
        if isinstance(source, bytes):  # a symbolic link: its blob is the link text
            link = _git(
                repo, "hash-object", "-w", "--no-filters", "--stdin", stdin=source, error=fail
            )
            blobs[n] = link.decode().strip()
    index = "".join(f"{mode} {blobs[n]}\t{n}\n" for n, (mode, _) in entries.items()).encode()
    with tempfile.TemporaryDirectory(prefix="amplai-tb2-index-") as scratch:
        env = {**identity, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git(repo, "update-index", "--add", "--index-info", stdin=index, env=env, error=fail)
        tree = _git(repo, "write-tree", env=env, error=fail).decode().strip()
    commit = _git(repo, "commit-tree", tree, stdin=message.encode(), env=dict(identity), error=fail)
    commit_sha = commit.decode().strip()
    ref = BASE_REF_PREFIX + name
    existing = _git_run(repo, "rev-parse", "--verify", "-q", ref + "^{commit}")
    current = existing.stdout.decode().strip() if existing.returncode == 0 else None
    if current is not None and current != commit_sha:
        raise _base_error(f"{ref} exists at {current}; a recorded base is never replaced")
    if current is None:
        _git(repo, "update-ref", ref, commit_sha, "0" * 40, error=fail)  # create only
    modes = [mode for mode, _ in entries.values()]
    return {
        "task": name,
        "repo": str(repo),
        "ref": ref,
        "commit": commit_sha,
        "tree": tree,
        "files": len(entries),
        "executables": modes.count(MODE_EXEC),
        "symlinks": modes.count(MODE_LINK),
        "dropped_empty_dirs": empty,
    }


def resolve_base(repo: Path, commit: str) -> str:
    """The tree of ``commit`` in the tb2 base repository (``Hold TB2_ADMISSION`` otherwise).

    The repository must be a real git checkout, as ``GitWorkspaceManager`` registers it.
    """
    if not isinstance(commit, str) or not SHA1.fullmatch(commit):
        raise Hold("TB2_ADMISSION", "the recorded tb2 base commit is a 40-hex sha")
    repo = Path(repo).absolute()
    if repo.resolve() != repo or not (repo / ".git").exists():
        raise Hold("TB2_ADMISSION", f"{repo}: the tb2 base repository must be a real git checkout")
    found = _git_run(repo, "rev-parse", "--verify", "-q", commit + "^{commit}")
    if found.returncode != 0 or found.stdout.decode().strip() != commit:
        raise Hold("TB2_ADMISSION", f"{repo}: no commit {commit}")
    fail = Hold("TB2_ADMISSION", f"{repo}: no tree for {commit}")
    return _git(repo, "rev-parse", commit + "^{tree}", error=fail).decode().strip()


def base_archive(repo: Path, commit: str, *, max_tree_bytes: int = MAX_TREE_BYTES) -> bytes:
    """``git archive`` of the base commit, as ``GitWorkspaceManager._extract`` reads it."""
    fail = Hold("TB2_ADMISSION", f"{repo}: git archive of {commit}")
    archive = _git(Path(repo).absolute(), "archive", "--format=tar", commit, error=fail)
    if len(archive) > max_tree_bytes:
        raise Hold("TB2_ADMISSION", "the base tree exceeds the workspace byte budget")
    return archive


def materialize(
    archive: bytes, commit: str, dest: Path, patch: dict[str, Any] | None = None
) -> Path:
    """A fresh workspace at ``dest`` as ``GitWorkspaceManager.materialize`` builds one.

    The archive is extracted with the same member checks, ``dest`` becomes a local git checkout
    whose only commit is the base, ``patch`` (the reference copy) is applied, and the tree is
    opened for the unprivileged container uid (``GitWorkspaceManager._open``: directories
    ``0777``, files ``| 0666``), in that order.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = _safe_members(tar)
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, members=members, filter="data")
        else:  # pragma: no cover - Python < 3.11.4, as GitWorkspaceManager._extract
            tar.extractall(dest, members=members)
    GitWorkspaceManager._commit(dest, "amplai base " + commit)
    if patch is not None:
        apply_patch(dest, patch)
    GitWorkspaceManager._open(dest)
    return dest


# -- admission -----------------------------------------------------------------------------------


def snapshot(root: Path) -> dict[str, bytes]:
    """Regular files under ``root`` (symbolic links are not followed or recorded).

    The workspace's own top-level ``.git`` (made by materialization, as in a trial) is not part
    of the workspace contents: the trial patch is computed against the base commit, not it.
    """
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.relative_to(root).parts[0] != ".git" and p.is_file() and not p.is_symlink()
    }


def workspace_patch(before: dict[str, bytes], after: dict[str, bytes]) -> dict[str, Any]:
    """``{"changed": {path: bytes}, "deleted": [path]}`` from ``before`` to ``after``."""
    changed = {k: v for k, v in sorted(after.items()) if before.get(k) != v}
    deleted = sorted(set(before) - set(after))
    return {"changed": changed, "deleted": deleted}


def patch_digest(patch: dict[str, Any]) -> str:
    return digest(
        {
            "changed": {k: digest_bytes(v) for k, v in patch["changed"].items()},
            "deleted": patch["deleted"],
        }
    )


def _inside(root: Path, name: str) -> Path:
    target = root / name
    if root.resolve() not in target.resolve().parents:
        raise RuntimeFault("TB2_ADMISSION", f"patch path {name!r} leaves the workspace")
    return target


def apply_patch(root: Path, patch: dict[str, Any]) -> None:
    for name in patch["deleted"]:
        _inside(root, name).unlink(missing_ok=True)
    for name, data in patch["changed"].items():
        target = _inside(root, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def load_container_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise Hold("TB2_ADMISSION", f"{path}: unreadable container profile") from exc
    if not isinstance(value, dict):
        raise Hold("TB2_ADMISSION", f"{path}: a container profile is a JSON object")
    return value


def _check_profile(profile: dict[str, Any], image: str) -> None:
    if not PINNED_IMAGE.fullmatch(image):
        raise Hold("TB2_ADMISSION", "the admission image needs an immutable digest")
    if profile.get("image") != image:
        raise Hold("TB2_ADMISSION", "the container profile names another image")
    if profile.get("uid") != UID or profile.get("gid") != GID:
        raise Hold("TB2_ADMISSION", f"admission runs as uid/gid {UID}")
    if profile.get("network") != NETWORK:
        raise Hold("TB2_ADMISSION", "admission runs with network none")


def docker_runner(profile: dict[str, Any], *, engine: str = "docker") -> Runner:
    """The operator runner: one ``ContainerSandbox`` run per admission step (needs docker).

    ``ContainerSandbox.command`` gives ``--read-only``, ``--network none``, ``--user
    65534:65534``, ``--cap-drop=ALL`` and the ``/workspace`` bind; it also mounts tmpfs at
    ``/tmp`` and ``/home/agent`` (sandbox/container.py). The workspace must be a real
    (non-symlink) directory the engine can mount (colima shares ``$HOME`` only).
    """
    sandbox = ContainerSandbox(
        ContainerProfile(
            profile["image"],
            uid=profile["uid"],
            gid=profile["gid"],
            memory=profile.get("memory", "1g"),
            cpus=profile.get("cpus", 1.0),
            pids=profile.get("pids", 128),
            network=NETWORK,
        ),
        engine=engine,
    )

    def run(spec: ContainerRun) -> int | None:
        if spec.image != sandbox.profile.image or spec.network != NETWORK or spec.uid != UID:
            raise Hold("TB2_ADMISSION", "admission run outside the container profile")
        name = f"amplai-tb2-{spec.purpose.replace('_', '-')}-{uuid.uuid4().hex[:12]}"
        argv = sandbox.command(
            list(spec.argv),
            spec.workspace,
            name,
            readonly_mounts={k: v.resolve() for k, v in spec.inputs.items()},
        )
        code: int | None
        try:
            code = subprocess.run(
                argv, capture_output=True, timeout=spec.timeout_sec, check=False
            ).returncode
        except subprocess.TimeoutExpired:
            code = None
            sandbox.stop(name)
        sandbox.destroy(name)
        return code

    return run


def _reason(verdicts: list[dict[str, Any]]) -> str:
    if any(v != verdicts[0] for v in verdicts):
        return "repeats_disagree"
    first = verdicts[0]
    for key, reason in (
        ("no_timeout", "timeout"),
        ("tests_fail_unmodified", "tests_pass_unmodified"),
        ("solution_ok", "solution_failed"),
        ("patch_nonempty", "empty_patch"),
        ("tests_pass_reference", "reference_fails"),
    ):
        if not first[key]:
            return reason
    return "admitted"


def admit(
    task: Tb2Task,
    *,
    image: str,
    container_profile: Path,
    repeats: int = ADMISSION_REPEATS,
    test_command: Sequence[str] | None = None,
    solution_command: Sequence[str] | None = None,
    base_repo: Path | None = None,
    base_commit: str | None = None,
    run: Runner | None = None,
    scratch: Path | None = None,
    max_tree_bytes: int = MAX_TREE_BYTES,
) -> dict[str, Any]:
    """Admission of one task in its built image (§10.5 step 4).

    Per repeat, each step in a fresh workspace materialized from ``base_commit`` (the task's
    commit in ``bases/tb2.commits.json``) of ``base_repo`` (the tb2 base repository) exactly as a
    trial workspace is (``materialize``): (a) ``test_command`` on the unmodified workspace must
    fail; (b) ``solution_command`` must succeed and change the workspace (the patch); (c) on a
    fresh copy with that patch ``test_command`` must pass. All ``repeats`` verdicts must agree.
    The test and solution entry commands are 확인 필요 (§14 Q7): without them ``Hold
    TB2_ADMISSION``. ``run`` defaults to ``docker_runner`` (operator machines only).
    """
    if repeats < 1:
        raise Hold("TB2_ADMISSION", "repeats must be at least 1")
    if not test_command or not solution_command:
        raise Hold(
            "TB2_ADMISSION",
            f"{task.name}: test and solution entry commands are 확인 필요 (§14 Q7); pass them",
        )
    if base_repo is None or base_commit is None:
        raise Hold("TB2_ADMISSION", f"{task.name}: the tb2 base repository and commit are required")
    if task.source_dir is None:
        raise Hold("TB2_ADMISSION", f"{task.name}: the task's source directory is unknown")
    profile = load_container_profile(container_profile)
    _check_profile(profile, image)
    base_tree = resolve_base(base_repo, base_commit)
    archive = base_archive(base_repo, base_commit, max_tree_bytes=max_tree_bytes)
    runner = run or docker_runner(profile)
    tests_dir, solution_dir = task.source_dir / "tests", task.source_dir / "solution"
    owned = scratch is None
    root = Path(tempfile.mkdtemp(prefix="amplai-tb2-")) if scratch is None else Path(scratch)
    root = root.resolve()
    runs: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    patches: list[dict[str, Any]] = []

    def fresh(dest: Path, patch: dict[str, Any] | None = None) -> Path:
        return materialize(archive, base_commit, dest, patch)

    def step(
        purpose: str,
        index: int,
        argv: Sequence[str],
        ws: Path,
        inputs: dict[str, Path],
        timeout: int | None,
    ) -> int | None:
        return runner(ContainerRun(purpose, index, image, tuple(argv), ws, inputs, timeout))

    try:
        # the unmodified contents, from a copy no run ever mounts
        before = snapshot(fresh(root / "base"))
        for index in range(repeats):
            base = root / f"r{index}"
            unmodified = step(
                "tests_unmodified",
                index,
                test_command,
                fresh(base / "unmodified"),
                {TESTS_MOUNT: tests_dir},
                task.verifier_timeout_sec,
            )
            solved_ws = fresh(base / "solution")
            solved = step(
                "solution",
                index,
                solution_command,
                solved_ws,
                {SOLUTION_MOUNT: solution_dir},
                task.agent_timeout_sec,
            )
            patch = workspace_patch(before, snapshot(solved_ws))
            patches.append(patch)
            reference = step(
                "tests_reference",
                index,
                test_command,
                fresh(base / "reference", patch),
                {TESTS_MOUNT: tests_dir},
                task.verifier_timeout_sec,
            )
            codes = (unmodified, solved, reference)
            verdict = {
                "no_timeout": all(c is not None for c in codes),
                "tests_fail_unmodified": unmodified is not None and unmodified != 0,
                "solution_ok": solved == 0,
                "patch_nonempty": bool(patch["changed"] or patch["deleted"]),
                "tests_pass_reference": reference == 0,
            }
            verdicts.append(verdict)
            runs.append(
                {
                    "repeat": index,
                    "tests_unmodified_exit": unmodified,
                    "solution_exit": solved,
                    "tests_reference_exit": reference,
                    "patch_digest": patch_digest(patch),
                    "patch_files": len(patch["changed"]) + len(patch["deleted"]),
                    **verdict,
                }
            )
    finally:
        if owned:
            shutil.rmtree(root, ignore_errors=True)
    reason = _reason(verdicts)
    digests = [r["patch_digest"] for r in runs]
    return {
        "schema": "amplai.tb2-admission.v1",
        "task": task.name,
        "image": image,
        "container_profile": profile,
        "constraints": {
            "network": NETWORK,
            "uid": UID,
            "gid": GID,
            "read_only_root": True,
            "writable": [WORKSPACE],
        },
        "test_command": list(test_command),
        "solution_command": list(solution_command),
        "base_commit": base_commit,
        "base_tree": base_tree,
        "initial_digest": digest({k: digest_bytes(v) for k, v in before.items()}),
        "repeats": repeats,
        "admitted": reason == "admitted",
        "reason": reason,
        "patch_digest": digests[0],
        "patch_stable": len(set(digests)) == 1,
        "runs": runs,
    }


# -- output --------------------------------------------------------------------------------------


def _copy_files(src: Path, dest: Path) -> None:
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise RuntimeFault("TB2_SPEC", f"{path}: symbolic links are not copied")
        if path.is_file() and "__pycache__" not in path.parts:
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


def notice_text(task: Tb2Task) -> str:
    """Attribution for the copied files (Apache-2.0 §4) plus the upstream NOTICE and LICENSE."""
    assert task.source_dir is not None
    clone = task.source_dir.parent
    lines = [
        f"This directory is adapted from Terminal-Bench 2.0 ({TB2_REPO}),",
        f"commit {task.commit}, task directory {task.name}/.",
        "Licensed under the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0).",
        "",
        "Changes by AMPLAI (Work 033 S7a): tests/ and solution/ are copied unmodified;",
        "task.json and environment.json are generated; instruction.md becomes the task objective.",
    ]
    upstream = clone / "NOTICE"
    if upstream.is_file():
        lines += ["", "--- upstream NOTICE ---", upstream.read_text(errors="replace").rstrip()]
    lines += [
        "",
        "--- upstream LICENSE ---",
        (clone / "LICENSE").read_text(errors="replace").rstrip(),
    ]
    return "\n".join(lines) + "\n"


def task_json(task: Tb2Task, *, created: str) -> dict[str, Any]:
    """The task.json v2 of an admitted task (§10.2, §10.5 step 5)."""
    assert task.source_dir is not None
    objective = (task.source_dir / "instruction.md").read_text()
    if not objective.strip():
        raise RuntimeFault("TB2_SPEC", f"{task.name}: instruction.md is empty")
    return {
        "task_id": task.name,
        "version": 2,
        "domain": "terminal",
        "subdomain": task.category,
        "set": "main",
        "split": None,
        "source": {
            "kind": "tb2",
            "ref": f"{TB2_REPO}@{task.commit}:{task.name}",
            "author": TB2_REPO,
            "created": created,
        },
        "license": LICENSE_SPDX,
        "base": "tb2",
        "environment": f"tb2-{task.name}",
        "grading": "tb2_tests",
        "objective": objective,
        "acceptance": [TB2_ACCEPTANCE],
        "hidden_map": {},
        "ambiguity": None,
        "difficulty_declared": None,
    }


def write_task(
    task: Tb2Task, admitted: dict[str, Any], dest: Path, *, created: str | None = None
) -> Path:
    """``dest/<name>/`` for an admitted task: ``task.json``, ``tests/``, ``solution/``,
    ``environment.json`` (the image's container profile for qualification) and ``NOTICE``.

    ``dest`` is the corpus ``tb2/`` directory. Refuses (``TB2_ADMISSION``) a result that is not an
    admission of this task, and an existing task directory.
    """
    if admitted.get("admitted") is not True or admitted.get("task") != task.name:
        raise Hold("TB2_ADMISSION", f"{task.name}: not admitted")
    if task.source_dir is None or task.commit is None:
        raise Hold("TB2_SOURCE", f"{task.name}: write_task needs a scan of a pinned clone")
    created = created or date.today().isoformat()
    date.fromisoformat(created)
    folder = Path(dest) / task.name
    if folder.exists():
        raise RuntimeFault("TB2_SPEC", f"{folder} exists")
    meta = task_json(task, created=created)
    environment = {
        "schema": "amplai.tb2-environment.v1",
        "environment_id": meta["environment"],
        "image": admitted["image"],
        "container_profile": admitted["container_profile"],
        "task_image": task.docker_image,
        "admission": {
            k: admitted[k]
            for k in (
                "repeats",
                "reason",
                "constraints",
                "test_command",
                "solution_command",
                "base_commit",
                "base_tree",
                "initial_digest",
                "patch_digest",
                "patch_stable",
            )
        },
    }
    folder.mkdir(parents=True)
    try:
        _copy_files(task.source_dir / "tests", folder / "tests")
        _copy_files(task.source_dir / "solution", folder / "solution")
        (folder / "task.json").write_text(json.dumps(meta, indent=2) + "\n")
        (folder / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
        (folder / "NOTICE").write_text(notice_text(task))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder
