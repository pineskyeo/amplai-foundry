#!/usr/bin/env python3
"""Generic verifier helpers; no product controller or third-party dependencies."""
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PYTHON_FILES = (
    "scripts/loopctl.py", "scripts/loopv2.py", ".ai-team/verifiers/run.py",
    ".ai-team/verifiers/portable.py", ".specify/scripts/taskify_to_tasks_md.py",
    ".agents/skills/taskify/scripts/validate_task_manifest.py",
)
SHELL_FILES = (
    "scripts/eval.sh", ".specify/scripts/bash/common.sh",
    ".specify/scripts/bash/check-prerequisites.sh", ".specify/scripts/bash/setup-plan.sh",
)
JSON_FILES = (
    ".ai-team/runtime/repository-profile.json", ".ai-team/runtime/policy.json",
    ".ai-team/verifiers/registry.json", ".ai-team/contracts/work-contract.schema.json",
    ".ai-team/contracts/work-contract.template.json", ".ai-team/knowledge/map.json",
    ".ai-team/knowledge/decisions.index.json", ".ai-team/policy/knowledge-readiness.json",
    ".ai-team/policy/permissions.json", ".ai-team/policy/tdd.json",
    ".ai-team/policy/quadrant.json", ".ai-team/policy/documentation.json",
    ".ai-team/policy/gardening.json",
)


def runtime():
    for rel in PYTHON_FILES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            ast.parse(handle.read(), filename=rel)
    for rel in JSON_FILES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("runtime JSON root must be an object: " + rel)
    return 0


def tasks():
    raw = os.environ.get("SPECIFY_FEATURE_DIRECTORY")
    if not raw:
        with open(os.path.join(ROOT, ".specify", "feature.json"), encoding="utf-8") as handle:
            raw = json.load(handle).get("feature_directory")
    if not isinstance(raw, str) or not raw:
        raise ValueError("selected feature is required")
    feature = os.path.realpath(os.path.join(ROOT, raw))
    if os.path.commonpath([ROOT, feature]) != ROOT or feature == ROOT:
        raise ValueError("selected feature must be inside repository")
    directory = os.path.join(feature, "task-manifests")
    return subprocess.call([
        sys.executable, "-I", "-S", "-B",
        os.path.join(ROOT, ".agents/skills/taskify/scripts/validate_task_manifest.py"), directory,
    ], cwd=ROOT)


def shell():
    for rel in SHELL_FILES:
        code = subprocess.call(["bash", "-n", os.path.join(ROOT, rel)], cwd=ROOT)
        if code:
            return code
    return 0


def main(argv):
    if len(argv) != 1 or argv[0] not in ("runtime", "tasks", "shell"):
        print("usage: portable.py runtime|tasks|shell", file=sys.stderr)
        return 2
    try:
        code = {"runtime": runtime, "tasks": tasks, "shell": shell}[argv[0]]()
    except (OSError, ValueError, SyntaxError) as exc:
        print("PORTABLE VERIFIER: UNAVAILABLE (%s)" % type(exc).__name__, file=sys.stderr)
        return 2
    print("PORTABLE VERIFIER: %s (%s)" % ("PASS" if code == 0 else "FAIL", argv[0]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
