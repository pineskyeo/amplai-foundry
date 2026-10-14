"""TB2 oracle admission over many tasks (Work 033 OD-15 wave 1, interfaces §10.5 step 4).

    # 1. screen: does each task's test entry start offline under V3 containment?
    .venv/bin/python scripts/tb2_oracle_wave.py screen --source CLONE --commit SHA \
        --out DIR [--task NAME ...] [--max-compressed-gb 3]

    # 2. admission of every screened task whose test runner started (scripts/tb2_adapter.py steps)
    .venv/bin/python scripts/tb2_oracle_wave.py admit --source CLONE --commit SHA --out DIR \
        --base-repo TB2_REPO --commits-file CORPUS/bases/tb2.commits.json --dest CORPUS/tb2 \
        [--task NAME ...] [--registry localhost:5000]

    # 3. the admission report table (markdown) from DIR
    .venv/bin/python scripts/tb2_oracle_wave.py report --out DIR

Operator-run, needs docker; no model is called. Every container runs with network ``none``,
uid/gid 65534, a read-only root, ``--cap-drop=ALL`` and tmpfs ``/tmp`` and ``/home/agent``
(``HOME=/home/agent``), as ``sandbox/container.py`` runs a trial.

**Screen.** One run of the task's own entry (``bash /tests/test.sh``, ``tb2_grading.entry_for``)
in the admission layout: the oracle image (below) built locally from the task image pinned by its
registry digest, the ``WorkingDir`` contents copied out of that image into a writable scratch
workspace (a git checkout opened for uid 65534, as ``tb2.materialize``) mounted at
``/workspace``, ``tests/`` read-only at ``/tests`` and a fresh ``/logs/verifier``. Every TB2
``test.sh`` runs pytest (89 of 89 at ``2fd12b8``); the screen passes when the pytest session
starts (``test session starts`` in the output). When it does not start, the unmodified-test step
of admission cannot start it either: it runs the same script in the same image layout under the
same containment, and the reference step differs only by the solution's changes to
``/workspace``. Such a task fails with reason ``test_entry_offline`` and the output tail as
evidence. An image whose compressed size exceeds ``--max-compressed-gb`` is not pulled (reason
``not_pulled_size``; the colima VM disk is the limit); an image without a registry digest is not
run (reason ``no_digest``, never the mutable tag). Images are removed after the screen unless the
runner started. A screen record is reused only for the same TB2 commit, task image tag and
schema, with a pinned digest; otherwise the task is screened again.

**Admit.** For a task whose runner started: ``extract`` the ``WorkingDir`` contents, ``base-commit``
them into the tb2 base repository, build the oracle image, push it to ``--registry`` for an
immutable digest, ``tb2_grading.admit`` (3 repeats) and, when admitted, ``tb2_grading.write_task``.
The oracle image is ``FROM`` the pinned task image with the working directory replaced by a link
to ``/workspace`` and uid 65534 (``tb2.image_spec`` without the driver layer): the admission runs
no agent, and the worker driver layer of this host is ``linux/arm64`` while every TB2 image is
``linux/amd64`` (open issue, ``runs/tb2-oracle-wave1.md``). A stale ``*.admission.json`` is
removed before the adapter runs; the result counts only when the adapter's return code (0
admitted, 1 not admitted) agrees with a result for this task and image. An ``*.admit.json`` is
reused only for the same commit and pinned task image as the current screen and the same
``--repeats``, ``--dest`` and ``--registry``. ``base-commit`` is skipped only when
``*.base.json`` records the pinned digest the recorded commit was made from; otherwise it runs
again (the same tree gives the same commit; ``tb2.commit_base`` refuses to replace a recorded base
of another tree, so the task fails at ``base-commit`` instead of being graded on the old base).

**Screen errors.** A ``docker run`` that docker itself could not start (exit 125, or a daemon,
runtime or mount error in the output) is ``screen_launch_error``, never ``test_entry_offline``,
and is screened again on the next run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amplai_foundry.meta_harness import tb2, tb2_grading, tb2_test_deps
from amplai_foundry.runtime.errors import RuntimeFault
from amplai_foundry.sandbox.git_workspace import GitWorkspaceManager

SCRATCH = Path.home() / ".amplai-sandbox-probes" / "tb2-wave1"
# v2: the screen runs in the admission layout (oracle image, writable /workspace copy).
# v3: the oracle image bakes the test runner at build time (TB2 option A, tb2_test_deps).
SCREEN_SCHEMA = "amplai.tb2-oracle-screen.v3"
SESSION_STARTED = "test session starts"
SCREEN_TIMEOUT_SEC = 900  # a screen only needs the runner to start
MAX_COMPRESSED_GB = 3.0
TAIL_LINES = 12
# docker's notice for an amd64 image on an arm64 host (emulated): not task output.
PLATFORM_WARNING = "does not match the detected host platform"
# ``docker run`` exits 125 when docker itself fails. 126/127 are also the exit of a command in the
# container (``bash`` returns the script's last status), so they count as a launch error only
# with one of docker's own messages below.
DOCKER_RUN_FAILED = 125
LAUNCH_ERRORS = (
    "docker: Error response from daemon",
    "Cannot connect to the Docker daemon",
    "OCI runtime create failed",
    "OCI runtime exec failed",
    "error while creating mount source path",
    "invalid mount config",
)
# docker did not start the container: no verdict on the task, screened again on the next run
RESCREEN = frozenset({"screen_launch_error"})
# Containment of every run, as ContainerSandbox.command (sandbox/container.py).
CONTAINMENT = (
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--user",
    f"{tb2.UID}:{tb2.GID}",
    "--network",
    tb2.NETWORK,
    "--tmpfs",
    "/tmp:rw,nosuid,nodev,size=268435456",
    "--tmpfs",
    "/home/agent:rw,nosuid,nodev,size=67108864",
    "--env",
    "HOME=/home/agent",
)
PROFILE_MEMORY, PROFILE_CPUS, PROFILE_PIDS = "2g", 2.0, 256


# -- pure helpers (tested) -----------------------------------------------------------------------


def classify_screen(output: str, code: int | None, reward: str | None) -> dict[str, Any]:
    """The screen verdict of one offline run of the test entry.

    ``runner_started`` when pytest printed its session header; otherwise ``test_entry_offline``
    with the last output lines as evidence. A timeout whose output shows no session is
    ``screen_timeout`` (not decided by the screen). A container that docker could not start
    (exit 125, or docker's own daemon, runtime or mount error in the output) is
    ``screen_launch_error``: no verdict on the task, which is screened again (``RESCREEN``).
    """
    lines = [line for line in output.splitlines() if line.strip() and PLATFORM_WARNING not in line]
    tail = lines[-TAIL_LINES:]
    if SESSION_STARTED in output:
        status = "runner_started"
    elif code is None:
        status = "screen_timeout"
    elif code == DOCKER_RUN_FAILED or any(marker in output for marker in LAUNCH_ERRORS):
        status = "screen_launch_error"
    else:
        status = "test_entry_offline"
    return {"status": status, "exit_status": code, "reward": reward, "tail": tail}


def oracle_dockerfile(
    task: tb2.Tb2Task,
    pinned_image: str,
    workdir: str,
    deps: tb2_test_deps.RunnerDeps | None = None,
    platform: str = "linux/amd64",
) -> str:
    """``tb2.image_spec``'s Dockerfile without the driver layer (the admission runs no agent).

    With ``deps`` the test runner is baked in as root right after ``USER 0:0``
    (``tb2_test_deps.bake_dockerfile_lines``, TB2 option A), before the image drops to uid 65534.
    """
    if not tb2.PINNED_IMAGE.fullmatch(pinned_image):
        raise RuntimeFault("TB2_SPEC", f"{task.name}: the task image needs an immutable digest")
    spec = tb2.image_spec(
        task,
        driver_layer_image=pinned_image,
        driver_paths=["/nonexistent-driver-path"],
        workdir=workdir,
    )
    assert spec["dockerfile"] is not None
    lines = [
        line
        for line in spec["dockerfile"].splitlines()
        if not line.startswith("COPY --from=") and not line.startswith("FROM ")
    ]
    header, rest = lines[0], lines[1:]
    if deps is not None:
        root = rest.index("USER 0:0") + 1
        rest = [*rest[:root], *tb2_test_deps.bake_dockerfile_lines(deps, platform), *rest[root:]]
    note = "# oracle admission image: no driver layer (no agent runs in admission)"
    return "\n".join([header, note, f"FROM {pinned_image}", *rest]) + "\n"


def oracle_build_context(
    task: tb2.Tb2Task, pinned_image: str, workdir: str, platform: str
) -> dict[str, str]:
    """The oracle image build context: the Dockerfile with the task's test runner baked in and
    the runner shims. ``tb2_test_deps.Unsupported`` when the task's ``test.sh`` runner cannot be
    pinned (the task is then not admitted; D-135 is not relaxed for it)."""
    assert task.source_dir is not None
    deps = tb2_test_deps.parse_test_entry((task.source_dir / "tests" / "test.sh").read_text())
    return {
        "Dockerfile": oracle_dockerfile(task, pinned_image, workdir, deps, platform),
        **tb2_test_deps.shim_files(deps),
    }


def write_build_context(build: Path, files: dict[str, str]) -> None:
    """Write ``oracle_build_context`` files under ``build``."""
    for rel, text in files.items():
        path = build / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def container_profile(image: str, task_toml: dict[str, Any]) -> dict[str, Any]:
    """The admission profile: the task's ``memory_mb``/``cpus`` capped at ``PROFILE_MEMORY``
    (the local VM has 4 GiB and 2 CPUs), uid/gid 65534, network none."""
    env = task_toml.get("environment", {}) if isinstance(task_toml, dict) else {}
    memory_mb = env.get("memory_mb") if isinstance(env, dict) else None
    cpus = env.get("cpus") if isinstance(env, dict) else None
    memory = PROFILE_MEMORY
    if isinstance(memory_mb, int | float) and 0 < memory_mb < 2048:
        memory = f"{int(memory_mb)}m"
    cpu = PROFILE_CPUS
    if isinstance(cpus, int | float) and 0 < cpus < cpu:
        cpu = float(cpus)
    return {
        "schema_version": "1.0",
        "engine": "docker",
        "image": image,
        "uid": tb2.UID,
        "gid": tb2.GID,
        "memory": memory,
        "cpus": cpu,
        "pids": PROFILE_PIDS,
        "network": tb2.NETWORK,
    }


def screen_argv(
    image: str, workspace: Path, tests: Path, verifier: Path, name: str, profile: dict[str, Any]
) -> list[str]:
    """The screen run in the admission layout: ``image`` is the oracle image (the task
    ``WorkingDir`` linked to ``/workspace``) and ``workspace`` the writable copy mounted there,
    as ``tb2_grading.docker_runner`` grades an admission test step."""
    return [
        "docker",
        "run",
        "--init",
        "--name",
        name,
        *CONTAINMENT,
        "--memory",
        str(profile["memory"]),
        "--cpus",
        str(profile["cpus"]),
        "--pids-limit",
        str(profile["pids"]),
        "--mount",
        f"type=bind,src={workspace},dst={tb2.WORKSPACE}",
        "--workdir",
        tb2.WORKSPACE,
        "--mount",
        f"type=bind,src={tests},dst={tb2_grading.TESTS_DIR},readonly",
        "--mount",
        f"type=bind,src={verifier},dst={tb2_grading.VERIFIER_DIR}",
        image,
        "bash",
        f"{tb2_grading.TESTS_DIR}/{tb2_grading.TEST_SCRIPT}",
    ]


def screen_current(record: dict[str, Any], task: tb2.Tb2Task, commit: str) -> bool:
    """Whether a screen record may be reused: same schema, TB2 commit and task image tag, a
    pinned digest of that image and a verdict (not a launch error). Anything else (an older
    layout, no digest, docker failed to start the container) is screened again."""
    pinned = record.get("pinned")
    return (
        record.get("schema") == SCREEN_SCHEMA
        and record.get("status") not in RESCREEN
        and record.get("task") == task.name
        and record.get("source_commit") == commit
        and record.get("image") == task.docker_image
        and isinstance(pinned, str)
        and tb2.PINNED_IMAGE.fullmatch(pinned) is not None
        and pinned.split("@", 1)[0] == task.docker_image.rsplit(":", 1)[0]
    )


def admit_settings(args: argparse.Namespace) -> dict[str, Any]:
    """The admit options a result depends on: repeats, corpus destination and registry."""
    return {
        "repeats": args.repeats,
        "dest": None if args.dest is None else str(args.dest),
        "registry": args.registry,
    }


def admit_current(
    record: dict[str, Any],
    screen: dict[str, Any],
    commit: str,
    settings: dict[str, Any] | None = None,
) -> bool:
    """Whether an ``*.admit.json`` belongs to the current screen (same commit and pinned image)
    and, given ``settings`` (``admit_settings``), was made with the same repeats, destination and
    registry. A one-repeat result or one made without ``--dest`` is not reused for a later run
    that asks for more."""
    return (
        record.get("task") == screen.get("task")
        and record.get("source_commit") == commit
        and record.get("pinned") == screen.get("pinned")
        and (settings is None or all(record.get(k, ...) == v for k, v in settings.items()))
    )


def base_current(
    commits: dict[str, Any], base: dict[str, Any] | None, task: str, pinned: str
) -> bool:
    """Whether the recorded base commit of ``task`` was made from ``pinned``: ``base`` (the
    ``*.base.json`` next to the admit record) names the same commit as ``tb2.commits.json`` and
    the same image digest. A commit of another image version, or one with no digest record, is
    made again."""
    commit = commits.get(task)
    return (
        isinstance(commit, str)
        and isinstance(base, dict)
        and base.get("commit") == commit
        and base.get("pinned") == pinned
    )


def admission_outcome(
    code: int, result: dict[str, Any] | None, task: str, image: str
) -> tuple[bool, str]:
    """``(admitted, reason)`` of one adapter ``admit`` run. The result counts only when it names
    this task and image and the return code agrees with it (0 admitted, 1 not admitted);
    anything else is ``admission_error``."""
    if (
        not isinstance(result, dict)
        or result.get("task") != task
        or result.get("image") != image
        or not isinstance(result.get("admitted"), bool)
        or code not in (0, 1)
        or (code == 0) != result["admitted"]
    ):
        return False, "admission_error"
    return result["admitted"], str(result.get("reason"))


def report_rows(results: list[dict[str, Any]]) -> list[str]:
    """Markdown rows ``| task | category | stage | result | reason | evidence |``."""
    rows = []
    for r in sorted(results, key=lambda x: x["task"]):
        evidence = (r.get("evidence") or "").replace("|", "\\|").replace("\n", " ")
        rows.append(
            f"| `{r['task']}` | {r.get('category', '')} | {r['stage']} | "
            f"{'admitted' if r.get('admitted') else 'failed'} | `{r['reason']}` | {evidence} |"
        )
    return rows


# -- docker steps (operator) ---------------------------------------------------------------------


def _sh(argv: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _compressed_bytes(image: str) -> int | None:
    done = _sh(["docker", "manifest", "inspect", image], timeout=120)
    if done.returncode != 0:
        return None
    manifest = json.loads(done.stdout)
    return sum(int(layer.get("size", 0)) for layer in manifest.get("layers", []))


def _repo_digest(image: str) -> str | None:
    done = _sh(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image])
    if done.returncode != 0:
        return None
    digests = json.loads(done.stdout) or []
    repo = image.rsplit(":", 1)[0]
    for d in digests:
        if d.startswith(repo + "@"):
            return str(d)
    return None


def _inspect(image: str, fmt: str) -> str:
    return _sh(["docker", "image", "inspect", "--format", fmt, image]).stdout.strip()


def _toml(task: tb2.Tb2Task) -> dict[str, Any]:
    assert task.source_dir is not None
    return tomllib.loads((task.source_dir / "task.toml").read_text())


def _tasks(args: argparse.Namespace) -> list[tb2.Tb2Task]:
    tasks = tb2.scan(args.source, commit=args.commit)
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t.name in wanted]
    return tasks


def _read_json(path: Path) -> dict[str, Any]:
    """A JSON object from ``path``; ``{}`` when it is missing, unreadable or not an object."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _screen_workspace(pinned: str, workdir: str, dest: Path) -> bool:
    """The ``WorkingDir`` contents of ``pinned`` at ``dest`` as an admission workspace: copied
    out as ``extract`` does, committed and opened for uid 65534 as ``tb2.materialize`` does."""
    for argv in tb2.extract_commands(pinned, workdir, dest):
        if _sh(argv, timeout=1800).returncode != 0:
            return False
    dest.mkdir(exist_ok=True)  # an empty WorkingDir copies nothing
    GitWorkspaceManager._commit(dest, "amplai screen " + pinned)
    GitWorkspaceManager._open(dest)
    return True


def _run_screen(argv: list[str], name: str, timeout: int) -> tuple[int | None, str]:
    code: int | None
    try:
        done = _sh(argv, timeout=timeout)
        code, output = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        code = None
        output = (
            (exc.stdout or b"").decode(errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        _sh(["docker", "stop", "-t", "5", name])
    _sh(["docker", "rm", "-f", name])
    return code, output


def screen_one(task: tb2.Tb2Task, out: Path, max_gb: float, commit: str) -> dict[str, Any]:
    image = task.docker_image
    record: dict[str, Any] = {
        "schema": SCREEN_SCHEMA,
        "task": task.name,
        "category": task.category,
        "image": image,
        "source_commit": commit,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    size = _compressed_bytes(image)
    record["compressed_bytes"] = size
    if size is None:
        return {**record, "status": "image_unavailable", "tail": ["docker manifest inspect failed"]}
    if size > max_gb * 1e9:
        return {**record, "status": "not_pulled_size", "tail": [f"{size / 1e9:.2f} GB compressed"]}
    pulled = _sh(["docker", "pull", "-q", image], timeout=3600)
    if pulled.returncode != 0:
        return {**record, "status": "image_unavailable", "tail": pulled.stderr.splitlines()[-3:]}
    pinned = _repo_digest(image)
    if pinned is None or not tb2.PINNED_IMAGE.fullmatch(pinned):
        # fail closed: the mutable tag is never run or recorded as screen evidence
        _sh(["docker", "image", "rm", image])
        return {**record, "status": "no_digest", "tail": ["no registry digest for the image"]}
    workdir = _inspect(pinned, "{{.Config.WorkingDir}}") or "/"
    record.update(
        pinned=pinned,
        workdir=workdir,
        platform=_inspect(pinned, "{{.Os}}/{{.Architecture}}"),
        image_bytes=int(_inspect(pinned, "{{.Size}}") or 0),
    )
    scratch = Path(tempfile.mkdtemp(prefix=f"screen-{task.name}-", dir=SCRATCH))
    oracle = f"amplai-tb2-screen-{task.name}:wave1"
    try:
        record.update(
            _screen_in_layout(task, out, scratch, pinned, workdir, oracle, record["platform"])
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        _sh(["docker", "image", "rm", oracle])
    if record["status"] != "runner_started":
        _sh(["docker", "image", "rm", image])
    return record


def _screen_in_layout(
    task: tb2.Tb2Task,
    out: Path,
    scratch: Path,
    pinned: str,
    workdir: str,
    oracle: str,
    platform: str,
) -> dict[str, Any]:
    """Build the oracle image locally, copy the workspace and run the entry in that layout."""
    try:
        context = oracle_build_context(task, pinned, workdir, platform)
    except tb2_test_deps.Unsupported as exc:
        return {"status": "test_deps_unsupported", "tail": [exc.message]}
    except RuntimeFault as exc:
        return {"status": "oracle_spec", "tail": [exc.message]}
    build = scratch / "build"
    build.mkdir()
    write_build_context(build, context)
    built = _sh(["docker", "build", "-q", "-t", oracle, str(build)], timeout=3600)
    if built.returncode != 0:
        return {"status": "build_failed", "tail": (built.stdout + built.stderr).splitlines()[-3:]}
    oracle_id = _inspect(oracle, "{{.Id}}")
    workspace = scratch / "workspace"
    if not oracle_id or not _screen_workspace(pinned, workdir, workspace):
        return {"status": "extract_failed", "tail": ["workspace copy or oracle image id failed"]}
    verifier = scratch / "verifier"
    verifier.mkdir()
    verifier.chmod(0o777)
    assert task.source_dir is not None
    profile = container_profile(oracle_id, _toml(task))
    name = f"amplai-tb2-screen-{uuid.uuid4().hex[:12]}"
    tests = (task.source_dir / "tests").resolve()
    argv = screen_argv(oracle_id, workspace, tests, verifier, name, profile)
    timeout = min(task.verifier_timeout_sec or 900, SCREEN_TIMEOUT_SEC)
    code, output = _run_screen(argv, name, timeout)
    reward_path = verifier / tb2_grading.REWARD_TXT
    reward = reward_path.read_text().strip()[:16] if reward_path.is_file() else None
    (out / f"{task.name}.screen.log").write_text(output)
    return {"oracle_image_id": oracle_id, **classify_screen(output, code, reward)}


def _screen(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for task in _tasks(args):
        path = args.out / f"{task.name}.screen.json"
        if path.exists() and not args.again:
            try:
                if screen_current(json.loads(path.read_text()), task, args.commit):
                    continue
            except ValueError:
                pass  # unreadable: screened again
        record = screen_one(task, args.out, args.max_compressed_gb, args.commit)
        _write(path, record)
        print(f"{task.name}: {record['status']}", flush=True)
    return 0


def _adapter(*argv: str) -> int:
    """``scripts/tb2_adapter.py`` in this process (its ``main``)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "tb2_adapter.py"
    spec = importlib.util.spec_from_file_location("tb2_adapter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.main(list(argv)))


def admit_one(
    task: tb2.Tb2Task, screen: dict[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    out: Path = args.out
    pinned, workdir = screen.get("pinned"), screen.get("workdir")
    record: dict[str, Any] = {
        "task": task.name,
        "category": task.category,
        "source_commit": args.commit,
        "pinned": pinned,
        **admit_settings(args),
    }
    if not pinned or not workdir:
        return {**record, "stage": "admit", "admitted": False, "reason": "no_pinned_image"}
    if _sh(["docker", "pull", "-q", pinned], timeout=3600).returncode != 0:
        return {**record, "stage": "admit", "admitted": False, "reason": "image_unavailable"}
    common = ["--source", str(args.source), "--commit", args.commit, "--task", task.name]
    # keyed by the pinned digest: a copy of another image version is never reused
    extracted = out / "extracted" / task.name / str(pinned).rsplit(":", 1)[-1][:16]
    if not extracted.exists():
        partial = extracted.with_name(extracted.name + ".partial")  # an interrupted copy
        shutil.rmtree(partial, ignore_errors=True)
        extracted.parent.mkdir(parents=True, exist_ok=True)
        for argv in tb2.extract_commands(pinned, workdir, partial):
            if _sh(argv, timeout=1800).returncode != 0:
                return {**record, "stage": "extract", "admitted": False, "reason": "extract_failed"}
        partial.mkdir(exist_ok=True)  # an empty WorkingDir copies nothing
        partial.rename(extracted)
    base_path = out / f"{task.name}.base.json"
    if not base_current(_read_json(args.commits_file), _read_json(base_path), task.name, pinned):
        base_path.unlink(missing_ok=True)
        code = _adapter(
            "base-commit",
            "--task",
            task.name,
            "--extracted",
            str(extracted),
            "--repo",
            str(args.base_repo),
            "--commits-file",
            str(args.commits_file),
        )
        commit = _read_json(args.commits_file).get(task.name)
        if code or not isinstance(commit, str):
            return {**record, "stage": "base-commit", "admitted": False, "reason": "base_commit"}
        _write(base_path, {"task": task.name, "pinned": pinned, "commit": commit})
    try:
        context = oracle_build_context(task, pinned, workdir, str(screen.get("platform")))
    except tb2_test_deps.Unsupported:
        return {**record, "stage": "build", "admitted": False, "reason": "test_deps_unsupported"}
    build = out / "build" / task.name
    shutil.rmtree(build, ignore_errors=True)  # no stale shim of an earlier build
    build.mkdir(parents=True)
    write_build_context(build, context)
    tag = f"{args.registry}/amplai-tb2-oracle-{task.name}:wave1"
    built = _sh(["docker", "build", "-q", "-t", tag, str(build)], timeout=3600)
    if built.returncode != 0:
        (out / f"{task.name}.build.log").write_text(built.stdout + built.stderr)
        return {**record, "stage": "build", "admitted": False, "reason": "build_failed"}
    if _sh(["docker", "push", "-q", tag], timeout=3600).returncode != 0:
        return {**record, "stage": "build", "admitted": False, "reason": "push_failed"}
    image = _repo_digest(tag)
    if image is None:
        return {**record, "stage": "build", "admitted": False, "reason": "no_digest"}
    profile_path = out / f"{task.name}.profile.json"
    _write(profile_path, container_profile(image, _toml(task)))
    result_path = out / f"{task.name}.admission.json"
    result_path.unlink(missing_ok=True)  # never read a result of an earlier run
    scratch = SCRATCH / "admit" / f"{task.name}-{uuid.uuid4().hex[:8]}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    code = _adapter(
        "admit",
        *common,
        "--image",
        image,
        "--container-profile",
        str(profile_path),
        "--base-repo",
        str(args.base_repo),
        "--commits-file",
        str(args.commits_file),
        "--solution-command",
        f"bash {tb2.SOLUTION_MOUNT}/solve.sh",
        "--repeats",
        str(args.repeats),
        "--scratch",
        str(scratch),
        "--out",
        str(result_path),
    )
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        result = json.loads(result_path.read_text()) if result_path.exists() else None
    except ValueError:
        result = None
    admitted, reason = admission_outcome(code, result, task.name, image)
    record.update(stage="admit", admitted=admitted, reason=reason, adapter_exit=code)
    write = ["write", *common, "--admission", str(result_path), "--dest", str(args.dest)]
    if admitted and args.dest is not None and _adapter(*write):
        record.update(admitted=False, reason="write_failed")
    return record


def _admit(args: argparse.Namespace) -> int:
    for task in _tasks(args):
        screen_path = args.out / f"{task.name}.screen.json"
        if not screen_path.exists():
            continue
        screen = json.loads(screen_path.read_text())
        if screen.get("status") != "runner_started":
            continue
        if not screen_current(screen, task, args.commit):
            print(f"{task.name}: stale screen record (screen it again)", flush=True)
            continue
        path = args.out / f"{task.name}.admit.json"
        if path.exists() and not args.again:
            try:
                record = json.loads(path.read_text())
                if admit_current(record, screen, args.commit, admit_settings(args)):
                    continue
            except ValueError:
                pass  # unreadable: admitted again
        record = admit_one(task, screen, args)
        _write(path, record)
        print(f"{task.name}: {record['reason']}", flush=True)
    return 0


def _evidence(screen: dict[str, Any]) -> str:
    tail = screen.get("tail") or []
    return "; ".join(str(line).strip()[:160] for line in tail[-2:])


def _report(args: argparse.Namespace) -> int:
    results = []
    for path in sorted(args.out.glob("*.screen.json")):
        screen = json.loads(path.read_text())
        admit_path = args.out / f"{screen['task']}.admit.json"
        admitted = json.loads(admit_path.read_text()) if admit_path.exists() else None
        if (
            screen["status"] == "runner_started"
            and admitted is not None
            and admit_current(admitted, screen, str(screen.get("source_commit")))
        ):
            evidence = f"screen: runner started; {admitted['reason']}"
            results.append({**admitted, "evidence": evidence})
        else:
            results.append(
                {
                    "task": screen["task"],
                    "category": screen.get("category", ""),
                    "stage": "screen",
                    "admitted": False,
                    "reason": screen["status"],
                    "evidence": _evidence(screen),
                }
            )
    print("\n".join(report_rows(results)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TB2 oracle admission wave (Work 033 OD-15).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("screen", "admit"):
        p = sub.add_parser(name)
        p.add_argument("--source", type=Path, required=True)
        p.add_argument("--commit", required=True)
        p.add_argument("--out", type=Path, required=True)
        p.add_argument("--task", action="append")
        p.add_argument("--again", action="store_true", help="redo tasks that have a record")
    screen = sub.choices["screen"]
    screen.add_argument("--max-compressed-gb", type=float, default=MAX_COMPRESSED_GB)
    admit = sub.choices["admit"]
    admit.add_argument("--base-repo", type=Path, required=True)
    admit.add_argument("--commits-file", type=Path, required=True)
    admit.add_argument("--dest", type=Path, help="the corpus tb2/ directory")
    admit.add_argument("--registry", default="localhost:5000")
    admit.add_argument("--repeats", type=int, default=tb2.ADMISSION_REPEATS)
    report = sub.add_parser("report")
    report.add_argument("--out", type=Path, required=True)
    return parser


COMMANDS = {"screen": _screen, "admit": _admit, "report": _report}


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except RuntimeFault as exc:
        print(f"tb2_oracle_wave: {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
