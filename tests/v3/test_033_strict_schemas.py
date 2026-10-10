"""Every model-facing output schema satisfies the strict structured-output rules (pilot 2026-10-11).

Codex ``--output-schema`` sends the schema as a strict response format; the provider refused the
proposer's free-form ``content`` object with ``invalid_json_schema`` ("'additionalProperties' is
required to be supplied and to be false"). Strict mode needs every object to list its properties,
require all of them and forbid others; an untyped or free-form object is refused.
"""

from __future__ import annotations

from typing import Any

import pytest

from amplai_foundry.meta_harness import judges, proposer
from amplai_foundry.runtime.execution import cells, planner_codex, strategy_runner


def strict_problems(schema: Any, path: str = "$") -> list[str]:
    """Where ``schema`` breaks the strict rules (empty when it holds)."""
    found: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: not a schema object"]
    types = schema.get("type")
    kinds = set(types) if isinstance(types, list) else {types}
    if "object" in kinds:
        props = schema.get("properties")
        if not isinstance(props, dict) or not props:
            found.append(f"{path}: object without properties")
            props = {}
        if schema.get("additionalProperties") is not False:
            found.append(f"{path}: additionalProperties is not false")
        if set(schema.get("required") or []) != set(props):
            found.append(f"{path}: required is not every property")
        for name, sub in props.items():
            found += strict_problems(sub, f"{path}.{name}")
    if "array" in kinds:
        found += strict_problems(schema.get("items"), f"{path}[]")
    for key in ("anyOf", "oneOf"):
        for i, sub in enumerate(schema.get(key) or []):
            found += strict_problems(sub, f"{path}.{key}[{i}]")
    if not types and "enum" not in schema and "anyOf" not in schema:
        found.append(f"{path}: no type")
    return found


QUESTIONS = [
    judges.JudgeQuestion("q-yes", "yes_no", "Is it done?"),
    judges.JudgeQuestion("q-choice", "choice", "Which one?", choices=("a", "b")),
    judges.JudgeQuestion("q-score", "score", "How good?", scale=(0.0, 1.0)),
]

SCHEMAS = {
    "proposer.PROPOSAL_SCHEMA": proposer.PROPOSAL_SCHEMA,
    "proposer.DREAM_SCHEMA": proposer.DREAM_SCHEMA,
    "cells.PROBE_SCHEMA": cells.PROBE_SCHEMA,
    "strategy_runner.REVIEW_SCHEMA": strategy_runner.REVIEW_SCHEMA,
    "strategy_runner.FINDINGS_SCHEMA": strategy_runner.FINDINGS_SCHEMA,
    "strategy_runner.STEPS_TURN_SCHEMA": strategy_runner.STEPS_TURN_SCHEMA,
    "planner_codex.plan_schema": planner_codex.plan_schema(["unit"]),
    "planner_codex.plan_schema[steps]": planner_codex.plan_schema(["unit"], "steps"),
    "planner_codex.plan_schema[parts]": planner_codex.plan_schema(["unit"], "parts"),
    "planner_codex.multi_plan_schema": planner_codex.multi_plan_schema(
        {"a": ["unit"], "b": ["lint"]}
    ),
    "judges.answers_schema": judges.answers_schema(QUESTIONS),
}


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_a_model_facing_schema_is_strict(name: str) -> None:
    assert strict_problems(SCHEMAS[name]) == []


def test_the_checker_refuses_the_free_form_content_the_provider_refused() -> None:
    free = {
        "type": "object",
        "additionalProperties": False,
        "required": ["content"],
        "properties": {"content": {"type": "object"}},
    }
    assert strict_problems(free) == [
        "$.content: object without properties",
        "$.content: additionalProperties is not false",
    ]


def test_proposer_content_arrives_as_json_text_and_must_decode_to_an_object() -> None:
    base = {
        "kind": "prompt",
        "component_id": "prompt.x",
        "rationale": "r",
        "hypothesis": "h",
        "risk": "low",
        "predictions": {
            "improve_task_ids": [],
            "regress_task_ids": [],
            "improve_buckets": [],
            "regress_buckets": [],
            "expected_delta": 0.1,
        },
    }
    for bad in ("not json", "[1, 2]", '"text"'):
        edit, _ = proposer.parse_edit({**base, "content": bad}, development=set())
        assert edit is None


def test_a_null_bucket_field_counts_as_absent() -> None:
    assert proposer._buckets([{"domain": "bug", "task_class": None}]) == [{"domain": "bug"}]
    assert proposer._buckets([{"domain": None, "task_class": None}]) == []
