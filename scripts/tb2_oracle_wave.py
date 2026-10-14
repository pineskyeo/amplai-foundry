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
in the task image as published (pinned by its registry digest), in the image's ``WorkingDir``,
with ``tests/`` read-only at ``/tests`` and a fresh ``/logs/verifier``. Every TB2 ``test.sh`` runs
pytest (89 of 89 at ``2fd12b8``); the screen passes when the pytest session starts (``test session
starts`` in the output). When it does not start, the reference cannot pass either: the reference
step runs the same script in the same image under the same containment, and the solution changes
only ``/workspace``, while the runner bootstrap (``curl`` of ``uv``, ``uvx``/``pip`` installs,
``apt-get``) needs network or a writable root outside it. Such a task fails with reason
``test_entry_offline`` and the output tail as evidence. An image whose compressed size exceeds
``--max-compressed-gb`` is not pulled (reason ``not_pulled_size``; the colima VM disk is the
limit). Images are removed after the screen unless the runner started.

**Admit.** For a task whose runner started: ``extract`` the ``WorkingDir`` contents, ``base-commit``
them into the tb2 base repository, build the oracle image, push it to ``--registry`` for an
immutable digest, ``tb2_grading.admit`` (3 repeats) and, when admitted, ``tb2_grading.write_task``.
The oracle image is ``FROM`` the pinned task image with the working directory replaced by a link
to ``/workspace`` and uid 65534 (``tb2.image_spec`` without the driver layer): the admission runs
no agent, and the worker driver layer of this host is ``linux/arm64`` while every TB2 image is
``linux/amd64`` (open issue, ``runs/tb2-oracle-wave1.md``).
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

from amplai_foundry.meta_harness import tb2, tb2_grading
from amplai_foundry.runtime.errors import RuntimeFault

SCRATCH = Path.home() / ".amplai-sandbox-probes" / "tb2-wave1"
SCREEN_SCHEMA = "amplai.tb2-oracle-screen.v1"
SESSION_STARTED = "test session starts"
SCREEN_TIMEOUT_SEC = 900  # a screen only needs the runner to start
MAX_COMPRESSED_GB = 3.0
TAIL_LINES = 12
# docker's notice for an amd64 image on an arm64 host (emulated): not task output.
PLATFORM_WARNING = "does not match the detected host platform"
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
    ``screen_timeout`` (not decided by the screen).
    """
    lines = [line for line in output.splitlines() if line.strip() and PLATFORM_WARNING not in line]
    tail = lines[-TAIL_LINES:]
    if SESSION_STARTED in output:
        status = "runner_started"
    elif code is None:
        status = "screen_timeout"
    else:
        status = "test_entry_offline"
    return {"status": status, "exit_status": code, "reward": reward, "tail": tail}


def oracle_dockerfile(task: tb2.Tb2Task, pinned_image: str, workdir: str) -> str:
    """``tb2.image_spec``'s Dockerfile without the driver layer (the admission runs no agent)."""
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
    note = "# oracle admission image: no driver layer (no agent runs in admission)"
    return "\n".join([header, note, f"FROM {pinned_image}", *rest]) + "\n"


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
    image: str, workdir: str, tests: Path, verifier: Path, name: str, profile: dict[str, Any]
) -> list[str]:
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
        "--workdir",
        workdir,
        "--mount",
        f"type=bind,src={tests},dst={tb2_grading.TESTS_DIR},readonly",
        "--mount",
        f"type=bind,src={verifier},dst={tb2_grading.VERIFIER_DIR}",
        image,
        "bash",
        f"{tb2_grading.TESTS_DIR}/{tb2_grading.TEST_SCRIPT}",
    ]


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


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def screen_one(task: tb2.Tb2Task, out: Path, max_gb: float) -> dict[str, Any]:
    image = task.docker_image
    record: dict[str, Any] = {
        "schema": SCREEN_SCHEMA,
        "task": task.name,
        "category": task.category,
        "image": image,
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
    workdir = _inspect(image, "{{.Config.WorkingDir}}") or "/"
    record.update(
        pinned=pinned,
        workdir=workdir,
        platform=_inspect(image, "{{.Os}}/{{.Architecture}}"),
        image_bytes=int(_inspect(image, "{{.Size}}") or 0),
    )
    profile = container_profile(pinned or image, _toml(task))
    scratch = Path(tempfile.mkdtemp(prefix=f"screen-{task.name}-", dir=SCRATCH))
    verifier = scratch / "verifier"
    verifier.mkdir()
    verifier.chmod(0o777)
    assert task.source_dir is not None
    name = f"amplai-tb2-screen-{uuid.uuid4().hex[:12]}"
    argv = screen_argv(
        pinned or image, workdir, (task.source_dir / "tests").resolve(), verifier, name, profile
    )
    code: int | None
    try:
        done = _sh(argv, timeout=min(task.verifier_timeout_sec or 900, SCREEN_TIMEOUT_SEC))
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
    reward_path = verifier / tb2_grading.REWARD_TXT
    reward = reward_path.read_text().strip()[:16] if reward_path.is_file() else None
    (out / f"{task.name}.screen.log").write_text(output)
    shutil.rmtree(scratch, ignore_errors=True)
    record.update(classify_screen(output, code, reward))
    if record["status"] != "runner_started":
        _sh(["docker", "image", "rm", image])
    return record


def _screen(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for task in _tasks(args):
        path = args.out / f"{task.name}.screen.json"
        if path.exists() and not args.again:
            continue
        record = screen_one(task, args.out, args.max_compressed_gb)
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
    record: dict[str, Any] = {"task": task.name, "category": task.category}
    pinned, workdir = screen.get("pinned"), screen.get("workdir")
    if not pinned or not workdir:
        return {**record, "stage": "admit", "admitted": False, "reason": "no_pinned_image"}
    if _sh(["docker", "pull", "-q", pinned], timeout=3600).returncode != 0:
        return {**record, "stage": "admit", "admitted": False, "reason": "image_unavailable"}
    common = ["--source", str(args.source), "--commit", args.commit, "--task", task.name]
    extracted = out / "extracted" / task.name
    if not extracted.exists():
        shutil.rmtree(extracted, ignore_errors=True)
        extracted.parent.mkdir(parents=True, exist_ok=True)
        for argv in tb2.extract_commands(pinned, workdir, extracted):
            if _sh(argv, timeout=1800).returncode != 0:
                return {**record, "stage": "extract", "admitted": False, "reason": "extract_failed"}
    commits = json.loads(args.commits_file.read_text()) if args.commits_file.exists() else {}
    if task.name not in commits:
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
        if code:
            return {**record, "stage": "base-commit", "admitted": False, "reason": "base_commit"}
    build = out / "build" / task.name
    build.mkdir(parents=True, exist_ok=True)
    (build / "Dockerfile").write_text(oracle_dockerfile(task, pinned, workdir))
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
    scratch = SCRATCH / "admit" / f"{task.name}-{uuid.uuid4().hex[:8]}"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    _adapter(
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
    if not result_path.exists():
        return {**record, "stage": "admit", "admitted": False, "reason": "admission_error"}
    result = json.loads(result_path.read_text())
    record.update(stage="admit", admitted=result["admitted"], reason=result["reason"])
    write = ["write", *common, "--admission", str(result_path), "--dest", str(args.dest)]
    if result["admitted"] and args.dest is not None and _adapter(*write):
        record.update(admitted=False, reason="write_failed")
    return record


def _admit(args: argparse.Namespace) -> int:
    for task in _tasks(args):
        screen_path = args.out / f"{task.name}.screen.json"
        if not screen_path.exists():
            continue
        screen = json.loads(screen_path.read_text())
        if screen["status"] != "runner_started":
            continue
        path = args.out / f"{task.name}.admit.json"
        if path.exists() and not args.again:
            continue
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
        if screen["status"] == "runner_started" and admit_path.exists():
            admitted = json.loads(admit_path.read_text())
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
