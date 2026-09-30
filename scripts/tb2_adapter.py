"""Terminal-Bench 2.0 adapter (Work 033 S7a, interfaces §10.5): scan, select, image spec,
admission and task output. Every step that needs docker is operator-run.

    # 1-2. scan a pinned clone (Apache-2.0 LICENSE checked, gpus > 0 skipped), pick candidates
    .venv/bin/python scripts/tb2_adapter.py scan --source CLONE --commit SHA
    .venv/bin/python scripts/tb2_adapter.py select --source CLONE --commit SHA [--limit 60]

    # 3. the task image's WorkingDir (§14 Q7), its initial contents, the per-task image spec
    .venv/bin/python scripts/tb2_adapter.py workdir --source CLONE --commit SHA --task NAME
    .venv/bin/python scripts/tb2_adapter.py extract --source CLONE --commit SHA --task NAME \
        --workdir DIR --dest EXTRACTED
    .venv/bin/python scripts/tb2_adapter.py base-commit --task NAME --extracted EXTRACTED \
        --repo TB2_REPO --commits-file CORPUS/bases/tb2.commits.json
    .venv/bin/python scripts/tb2_adapter.py spec --source CLONE --commit SHA --task NAME \
        --driver-layer-image IMAGE@sha256:... --driver-path PATH [--driver-path PATH ...] \
        --workdir DIR [--out DIR]

    # 4. admission in the built image (fresh containers: network none, uid 65534, read-only root)
    .venv/bin/python scripts/tb2_adapter.py admit --source CLONE --commit SHA --task NAME \
        --image IMAGE@sha256:... --container-profile PROFILE.json --base-repo TB2_REPO \
        --commits-file CORPUS/bases/tb2.commits.json \
        --test-command '...' --solution-command '...' [--repeats 3] [--out RESULT.json]

    # 5. write tb2/<name>/ from an admission result
    .venv/bin/python scripts/tb2_adapter.py write --source CLONE --commit SHA --task NAME \
        --admission RESULT.json --dest CORPUS/tb2

The test and solution entry commands, the working directory and the driver-layer paths are
확인 필요 (§14 Q7): they are required arguments, never defaults. The commands are split with
``shlex``; the tests are mounted read-only at ``/amplai-input/tests`` and the solution at
``/amplai-input/solution`` (``tb2.TESTS_MOUNT``, ``tb2.SOLUTION_MOUNT``). The admission scratch
defaults to a directory under ``$HOME`` (colima shares ``$HOME`` only, as
``scripts/container_qualify.py``).

``--execute`` on ``workdir`` and ``extract`` runs the printed docker argvs; without it they are
only printed. ``base-commit`` writes the extracted tree into the one tb2 base repository
(``TB2_REPO``, the ``amplai-tb2`` app; created when new or empty) as a root commit under
``refs/heads/tb2/<name>`` with the fixed identity of ``scripts/corpus_base_repo.py``, keeping the
execute bit and in-tree symbolic links, and records it in ``tb2.commits.json``. ``admit``
materializes every fresh workspace from that recorded commit of ``TB2_REPO``, as a trial does.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from amplai_foundry.meta_harness import tb2
from amplai_foundry.runtime.errors import Hold, RuntimeFault

SCRATCH = Path.home() / ".amplai-sandbox-probes" / "tb2"


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _row(task: tb2.Tb2Task) -> dict[str, Any]:
    return {
        "name": task.name,
        "category": task.category,
        "difficulty": task.difficulty,
        "docker_image": task.docker_image,
        "allow_internet": task.allow_internet,
        "gpus": task.gpus,
        "agent_timeout_sec": task.agent_timeout_sec,
        "verifier_timeout_sec": task.verifier_timeout_sec,
    }


def _task(args: argparse.Namespace) -> tb2.Tb2Task:
    for task in tb2.scan(args.source, commit=args.commit):
        if task.name == args.task:
            return task
    raise RuntimeFault("TB2_SOURCE", f"no admissible task {args.task!r} in {args.source}")


def _scan(args: argparse.Namespace) -> int:
    tasks, skipped = tb2.scan_all(args.source, commit=args.commit)
    _print(
        {
            "commit": args.commit,
            "license": tb2.LICENSE_SPDX,
            "tasks": len(tasks),
            "categories": tb2.category_counts(tasks),
            "skipped": skipped,
            "rows": [_row(t) for t in tasks],
        }
    )
    return 0


def _select(args: argparse.Namespace) -> int:
    picked = tb2.select_candidates(tb2.scan(args.source, commit=args.commit), limit=args.limit)
    _print(
        {
            "limit": args.limit,
            "selected": len(picked),
            "categories": tb2.category_counts(picked),
            "names": [t.name for t in picked],
        }
    )
    return 0


def _run(argvs: list[list[str]], execute: bool) -> int:
    for argv in argvs:
        print(shlex.join(argv))
        if execute:
            done = subprocess.run(argv, check=False)
            if done.returncode:
                return done.returncode
    return 0


def _workdir(args: argparse.Namespace) -> int:
    return _run([tb2.workdir_command(_task(args).docker_image)], args.execute)


def _extract(args: argparse.Namespace) -> int:
    task = _task(args)
    args.dest.mkdir(parents=True, exist_ok=False)
    return _run(tb2.extract_commands(task.docker_image, args.workdir, args.dest), args.execute)


def _corpus_base_repo() -> Any:
    """``scripts/corpus_base_repo.py`` (its fixed identity and message), loaded by path."""
    path = Path(__file__).resolve().parent / "corpus_base_repo.py"
    spec = importlib.util.spec_from_file_location("corpus_base_repo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_commit(args: argparse.Namespace) -> int:
    base_repo = _corpus_base_repo()
    built = tb2.commit_base(
        args.extracted,
        args.repo,
        args.task,
        identity=base_repo.IDENTITY,
        message=base_repo.message_for(f"tb2/{args.task}"),
    )
    tb2.record_base_commit(args.commits_file, args.task, built["commit"])
    _print({**built, "commits_file": str(args.commits_file)})
    return 0


def _recorded_commit(commits_file: Path, name: str) -> str:
    try:
        commits = json.loads(commits_file.read_text())
    except (OSError, ValueError) as exc:
        raise Hold("TB2_ADMISSION", f"{commits_file}: unreadable JSON") from exc
    commit = commits.get(name) if isinstance(commits, dict) else None
    if not isinstance(commit, str):
        raise Hold("TB2_ADMISSION", f"{commits_file}: no base commit recorded for {name}")
    return commit


def _spec(args: argparse.Namespace) -> int:
    spec = tb2.image_spec(
        _task(args),
        driver_layer_image=args.driver_layer_image,
        driver_paths=args.driver_path,
        workdir=args.workdir,
    )
    if args.out is not None and spec["dockerfile"] is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "Dockerfile").write_text(spec["dockerfile"])
        (args.out / "image-spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    _print(spec)
    return 0 if spec["ready"] else 1


def _admit(args: argparse.Namespace) -> int:
    task = _task(args)
    commit = _recorded_commit(args.commits_file, task.name)
    scratch = (args.scratch or SCRATCH / task.name).resolve()
    scratch.mkdir(parents=True, exist_ok=False)
    result = tb2.admit(
        task,
        image=args.image,
        container_profile=args.container_profile,
        repeats=args.repeats,
        test_command=shlex.split(args.test_command),
        solution_command=shlex.split(args.solution_command),
        base_repo=args.base_repo,
        base_commit=commit,
        scratch=scratch,
    )
    if args.out is not None:
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    _print(result)
    return 0 if result["admitted"] else 1


def _write(args: argparse.Namespace) -> int:
    admitted = json.loads(args.admission.read_text())
    folder = tb2.write_task(_task(args), admitted, args.dest, created=args.created)
    _print({"written": str(folder)})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Terminal-Bench 2.0 adapter (Work 033 S7a).")
    sub = parser.add_subparsers(dest="command", required=True)

    def source(p: argparse.ArgumentParser, *, task: bool = True) -> None:
        p.add_argument("--source", type=Path, required=True, help="the local TB2 clone")
        p.add_argument("--commit", required=True, help="the pinned 40-hex commit of the clone")
        if task:
            p.add_argument("--task", required=True)

    source(sub.add_parser("scan", help="list the tasks (gpus > 0 skipped)"), task=False)
    p = sub.add_parser("select", help="candidates spread over categories")
    source(p, task=False)
    p.add_argument("--limit", type=int, default=tb2.MAX_CANDIDATES)
    p = sub.add_parser("workdir", help="print (or run) the WorkingDir inspection")
    source(p)
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("extract", help="copy the working directory out of the task image")
    source(p)
    p.add_argument("--workdir", required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--execute", action="store_true")
    p = sub.add_parser("base-commit", help="a root commit per task in the tb2 repo (git)")
    p.add_argument("--task", required=True)
    p.add_argument("--extracted", type=Path, required=True)
    p.add_argument("--repo", type=Path, required=True, help="the one tb2 base repository")
    p.add_argument("--commits-file", type=Path, required=True)
    p = sub.add_parser("spec", help="the per-task image spec and Dockerfile")
    source(p)
    p.add_argument("--driver-layer-image", required=True)
    p.add_argument("--driver-path", action="append", default=None)
    p.add_argument("--workdir")
    p.add_argument("--out", type=Path)
    p = sub.add_parser("admit", help="admission run in the built image (docker)")
    source(p)
    p.add_argument("--image", required=True)
    p.add_argument("--container-profile", type=Path, required=True)
    p.add_argument("--base-repo", type=Path, required=True, help="the tb2 base repository")
    p.add_argument("--commits-file", type=Path, required=True, help="bases/tb2.commits.json")
    p.add_argument("--test-command", required=True)
    p.add_argument("--solution-command", required=True)
    p.add_argument("--repeats", type=int, default=tb2.ADMISSION_REPEATS)
    p.add_argument("--scratch", type=Path)
    p.add_argument("--out", type=Path)
    p = sub.add_parser("write", help="write tb2/<name>/ from an admission result")
    source(p)
    p.add_argument("--admission", type=Path, required=True)
    p.add_argument("--dest", type=Path, required=True, help="the corpus tb2/ directory")
    p.add_argument("--created", help="YYYY-MM-DD (default: today)")
    return parser


COMMANDS = {
    "scan": _scan,
    "select": _select,
    "workdir": _workdir,
    "extract": _extract,
    "base-commit": _base_commit,
    "spec": _spec,
    "admit": _admit,
    "write": _write,
}


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except RuntimeFault as exc:
        print(f"tb2_adapter: {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
