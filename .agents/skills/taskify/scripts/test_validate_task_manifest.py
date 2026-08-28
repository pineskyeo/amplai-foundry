#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).with_name("validate_task_manifest.py")
SPEC = importlib.util.spec_from_file_location("validator", SCRIPT)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


def valid_manifest(task_id: str = "TEST-T001") -> dict:
    return {
        "schema_version": "1.0",
        "id": task_id,
        "title": "Complete one behavior",
        "type": "feature",
        "status": "ready",
        "goal": {"outcome": "Observable result", "user_value": "Useful result"},
        "source": {
            "feature": "TEST",
            "files": [{"path": "spec.md", "section": "REQ-001"}],
            "requirements": ["REQ-001"],
            "decisions": [],
        },
        "scope": {
            "include": ["Behavior"],
            "exclude": ["Other behavior"],
            "allowed_paths": ["src/**"],
            "forbidden_paths": ["deploy/**"],
        },
        "dependencies": {"tasks": [], "external": []},
        "implementation": {
            "guidance": [],
            "invariants": ["Invariant"],
            "non_goals": ["Non-goal"],
        },
        "acceptance": {
            "behaviors": [
                {
                    "id": "AC-01",
                    "given": "Initial state",
                    "when": "Action",
                    "then": "Result",
                }
            ],
            "commands": [
                {
                    "id": "test",
                    "kind": "test",
                    "run": "make test",
                    "required": True,
                }
            ],
            "manual_checks": [],
            "evidence": [
                {"type": "command_output", "source": "test", "required": True}
            ],
        },
        "risk": {"level": "medium", "concerns": [], "approvals": []},
        "loop": {
            "max_attempts": 5,
            "replan_after_same_failure": 3,
            "stop_conditions": ["scope_boundary_must_expand"],
        },
        "completion": {
            "all_acceptance_passed": False,
            "required_evidence_present": False,
            "completed_at": None,
            "evidence_paths": [],
        },
        "handoff": {"expected_changes": [], "notes": []},
    }


class ValidatorTests(unittest.TestCase):
    def test_valid_manifest(self) -> None:
        self.assertEqual(
            validator.validate_manifest(Path("TEST-T001.yaml"), valid_manifest()),
            [],
        )

    def test_self_dependency_is_rejected(self) -> None:
        data = valid_manifest()
        data["dependencies"]["tasks"] = ["TEST-T001"]
        errors = validator.validate_manifest(Path("TEST-T001.yaml"), data)
        self.assertTrue(any("depend on itself" in error for error in errors))

    def test_high_risk_requires_approval(self) -> None:
        data = valid_manifest()
        data["risk"]["level"] = "high"
        errors = validator.validate_manifest(Path("TEST-T001.yaml"), data)
        self.assertTrue(any("must declare risk.approvals" in error for error in errors))

    def test_done_requires_evidence(self) -> None:
        data = valid_manifest()
        data["status"] = "done"
        errors = validator.validate_manifest(Path("TEST-T001.yaml"), data)
        self.assertTrue(any("done task requires" in error for error in errors))

    def test_cycle_detection(self) -> None:
        graph = {
            "TEST-T001": ["TEST-T002"],
            "TEST-T002": ["TEST-T003"],
            "TEST-T003": ["TEST-T001"],
        }
        self.assertEqual(
            validator.find_cycle(graph),
            ["TEST-T001", "TEST-T002", "TEST-T003", "TEST-T001"],
        )


if __name__ == "__main__":
    unittest.main()
