#!/usr/bin/env python3
"""Validate AMPLAI task manifests.

Usage:
    python3 validate_task_manifest.py <manifest.yaml|directory> [...]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import re
import sys
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install it through the project's normal "
        "dependency process using scripts/requirements.txt.",
        file=sys.stderr,
    )
    raise SystemExit(2)

ALLOWED_STATUS = {
    "draft",
    "blocked",
    "ready",
    "in_progress",
    "verification",
    "needs_revalidation",
    "done",
    "superseded",
}
ALLOWED_RISK = {"low", "medium", "high"}
ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*-T[0-9]{3,}$")


def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def get_nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def discover_files(inputs: Iterable[str]) -> list[Path]:
    found: set[Path] = set()
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            for candidate in path.glob("*.yaml"):
                if candidate.name != "index.yaml":
                    found.add(candidate.resolve())
            for candidate in path.glob("*.yml"):
                if candidate.name != "index.yml":
                    found.add(candidate.resolve())
        elif path.is_file():
            if path.name not in {"index.yaml", "index.yml"}:
                found.add(path.resolve())
        else:
            print(f"ERROR: input does not exist: {path}", file=sys.stderr)
    return sorted(found)


def validate_manifest(path: Path, data: Any) -> list[str]:
    errors: list[str] = []
    prefix = str(path)

    if not isinstance(data, dict):
        return [f"{prefix}: root must be a mapping"]

    required_top = {
        "schema_version",
        "id",
        "title",
        "type",
        "status",
        "goal",
        "source",
        "scope",
        "dependencies",
        "implementation",
        "acceptance",
        "risk",
        "loop",
        "completion",
        "handoff",
    }
    for key in sorted(required_top - set(data)):
        errors.append(f"{prefix}: missing top-level key '{key}'")

    task_id = data.get("id")
    if not nonempty_string(task_id) or not ID_RE.fullmatch(task_id):
        errors.append(
            f"{prefix}: id must match '<FEATURE>-T001' using uppercase characters"
        )

    if not nonempty_string(data.get("title")):
        errors.append(f"{prefix}: title must be a non-empty string")

    status = data.get("status")
    if status not in ALLOWED_STATUS:
        errors.append(f"{prefix}: invalid status '{status}'")

    outcome = get_nested(data, "goal", "outcome")
    if not nonempty_string(outcome):
        errors.append(f"{prefix}: goal.outcome must be non-empty")

    requirements = as_list(get_nested(data, "source", "requirements"))
    if not requirements:
        errors.append(f"{prefix}: source.requirements must contain at least one ID")

    includes = as_list(get_nested(data, "scope", "include"))
    excludes = as_list(get_nested(data, "scope", "exclude"))
    if not includes:
        errors.append(f"{prefix}: scope.include must contain at least one item")
    if not excludes:
        errors.append(f"{prefix}: scope.exclude must contain at least one item")

    dependencies = get_nested(data, "dependencies", "tasks")
    if not isinstance(dependencies, list):
        errors.append(f"{prefix}: dependencies.tasks must be a list")
        dependencies = []
    if task_id in dependencies:
        errors.append(f"{prefix}: task cannot depend on itself")

    invariants = as_list(get_nested(data, "implementation", "invariants"))
    non_goals = as_list(get_nested(data, "implementation", "non_goals"))
    if not invariants:
        errors.append(f"{prefix}: implementation.invariants must not be empty")
    if not non_goals:
        errors.append(f"{prefix}: implementation.non_goals must not be empty")

    behaviors = as_list(get_nested(data, "acceptance", "behaviors"))
    if not behaviors:
        errors.append(f"{prefix}: acceptance.behaviors must not be empty")
    behavior_ids: set[str] = set()
    for idx, behavior in enumerate(behaviors, start=1):
        where = f"{prefix}: acceptance.behaviors[{idx}]"
        if not isinstance(behavior, dict):
            errors.append(f"{where} must be a mapping")
            continue
        for key in ("id", "given", "when", "then"):
            if not nonempty_string(behavior.get(key)):
                errors.append(f"{where}.{key} must be non-empty")
        bid = behavior.get("id")
        if nonempty_string(bid):
            if bid in behavior_ids:
                errors.append(f"{where}: duplicate behavior id '{bid}'")
            behavior_ids.add(bid)

    commands = as_list(get_nested(data, "acceptance", "commands"))
    manual_checks = as_list(get_nested(data, "acceptance", "manual_checks"))
    if not commands and not manual_checks:
        errors.append(
            f"{prefix}: acceptance requires at least one command or manual check"
        )
    command_ids: set[str] = set()
    required_gate_count = 0
    for idx, command in enumerate(commands, start=1):
        where = f"{prefix}: acceptance.commands[{idx}]"
        if not isinstance(command, dict):
            errors.append(f"{where} must be a mapping")
            continue
        for key in ("id", "kind", "run"):
            if not nonempty_string(command.get(key)):
                errors.append(f"{where}.{key} must be non-empty")
        cid = command.get("id")
        if nonempty_string(cid):
            if cid in command_ids:
                errors.append(f"{where}: duplicate command id '{cid}'")
            command_ids.add(cid)
        if command.get("required") is True:
            required_gate_count += 1
        elif command.get("required") not in (True, False):
            errors.append(f"{where}.required must be true or false")

    for idx, check in enumerate(manual_checks, start=1):
        where = f"{prefix}: acceptance.manual_checks[{idx}]"
        if not isinstance(check, dict):
            errors.append(f"{where} must be a mapping")
            continue
        for key in ("id", "observer", "action", "expected", "reason_not_automated"):
            if not nonempty_string(check.get(key)):
                errors.append(f"{where}.{key} must be non-empty")
        if check.get("required") is True:
            required_gate_count += 1

    if required_gate_count == 0:
        errors.append(f"{prefix}: at least one acceptance gate must be required")

    evidence = as_list(get_nested(data, "acceptance", "evidence"))
    if not evidence:
        errors.append(f"{prefix}: acceptance.evidence must not be empty")

    risk_level = get_nested(data, "risk", "level")
    if risk_level not in ALLOWED_RISK:
        errors.append(f"{prefix}: invalid risk.level '{risk_level}'")
    if risk_level == "high" and not as_list(get_nested(data, "risk", "approvals")):
        errors.append(f"{prefix}: high-risk task must declare risk.approvals")

    max_attempts = get_nested(data, "loop", "max_attempts")
    replan_after = get_nested(data, "loop", "replan_after_same_failure")
    if not isinstance(max_attempts, int) or max_attempts < 1:
        errors.append(f"{prefix}: loop.max_attempts must be an integer >= 1")
    if not isinstance(replan_after, int) or replan_after < 1:
        errors.append(
            f"{prefix}: loop.replan_after_same_failure must be an integer >= 1"
        )
    if (
        isinstance(max_attempts, int)
        and isinstance(replan_after, int)
        and replan_after > max_attempts
    ):
        errors.append(
            f"{prefix}: replan_after_same_failure cannot exceed max_attempts"
        )
    if not as_list(get_nested(data, "loop", "stop_conditions")):
        errors.append(f"{prefix}: loop.stop_conditions must not be empty")

    if status == "blocked":
        external = as_list(get_nested(data, "dependencies", "external"))
        if not external:
            errors.append(
                f"{prefix}: blocked task must declare dependencies.external"
            )

    if status == "done":
        if get_nested(data, "completion", "all_acceptance_passed") is not True:
            errors.append(
                f"{prefix}: done task requires completion.all_acceptance_passed=true"
            )
        if get_nested(data, "completion", "required_evidence_present") is not True:
            errors.append(
                f"{prefix}: done task requires completion.required_evidence_present=true"
            )
        if not nonempty_string(get_nested(data, "completion", "completed_at")):
            errors.append(f"{prefix}: done task requires completion.completed_at")
        if not as_list(get_nested(data, "completion", "evidence_paths")):
            errors.append(f"{prefix}: done task requires completion.evidence_paths")

    return errors


def find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in visiting:
            start = stack.index(node)
            return stack[start:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        for dep in graph.get(node, []):
            cycle = visit(dep)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in graph:
        cycle = visit(node)
        if cycle:
            return cycle
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", help="Manifest files or directories")
    args = parser.parse_args()

    paths = discover_files(args.inputs)
    if not paths:
        print("ERROR: no task manifest YAML files found", file=sys.stderr)
        return 2

    errors: list[str] = []
    manifests: dict[str, tuple[Path, dict[str, Any]]] = {}

    for path in paths:
        try:
            data = load_yaml(path)
        except Exception as exc:
            errors.append(f"{path}: YAML parse failed: {exc}")
            continue
        errors.extend(validate_manifest(path, data))
        if isinstance(data, dict) and nonempty_string(data.get("id")):
            task_id = data["id"]
            if task_id in manifests:
                errors.append(
                    f"{path}: duplicate task id '{task_id}' also used by "
                    f"{manifests[task_id][0]}"
                )
            else:
                manifests[task_id] = (path, data)

    graph: dict[str, list[str]] = {}
    for task_id, (path, data) in manifests.items():
        deps = as_list(get_nested(data, "dependencies", "tasks"))
        graph[task_id] = [dep for dep in deps if isinstance(dep, str)]
        for dep in graph[task_id]:
            if dep not in manifests:
                errors.append(f"{path}: dependency '{dep}' does not exist in task set")

    cycle = find_cycle(graph)
    if cycle:
        errors.append("dependency cycle: " + " -> ".join(cycle))

    if errors:
        print(f"FAILED: {len(errors)} validation error(s)")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"PASS: validated {len(manifests)} task manifest(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
