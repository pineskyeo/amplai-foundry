"""Length bounds of the text agents act on (operator decision 2026-10-12, option A).

Planner, strategy and operator text that reaches a contract, a workgraph or an agent prompt is
never cut. A value over its frozen 3.0.0 wire bound Holds with a coded error that names the
field, its length and the bound, so the request is shortened or split instead of reaching an
agent without its end (pilot calibration 10: six feature tasks lost their closing rules to the
old 4000-character cut).

The bounds are the wire schemas' (``runtime/contracts/data/schemas``); text that has no wire
field of its own (``in_scope``, ``questions``, ``summary``, strategy steps, integration notes)
takes the contract's item bound.
"""

from __future__ import annotations

from typing import Any

from ..errors import Hold

# goal-contract.schema.json properties.objective.maxLength
CONTRACT_OBJECTIVE = 8192
# workgraph.schema.json properties.nodes.items.properties.objective.maxLength
NODE_OBJECTIVE = 4096
# goal-contract.schema.json non_goals.items, constraints.items.statement and
# acceptance.items.statement; common.schema.json $defs.assumption.statement;
# question.schema.json question: all maxLength 4096
ITEM_TEXT = 4096
# steering-event.schema.json text and workgraph.schema.json replan_reason: maxLength 4096
STEERING_TEXT = 4096

OBJECTIVE_LENGTH = "PLANNING_OBJECTIVE_LENGTH"
TEXT_LENGTH = "PLANNING_TEXT_LENGTH"
SPLIT_HINT = "Shorten the request or split it into several goals."


def bounded(
    value: str, *, field: str, limit: int, code: str = TEXT_LENGTH, hint: str = SPLIT_HINT
) -> str:
    """``value`` trimmed; Hold ``code`` (details: field, length, limit) when it is longer than
    ``limit`` characters. Never cut."""
    text = value.strip()
    if len(text) > limit:
        raise Hold(
            code,
            f"{field} is {len(text)} characters, over its limit of {limit}. {hint}",
            details={"field": field, "length": len(text), "limit": limit},
        )
    return text


def node_objective(objective: str, node_id: str) -> str:
    """The workgraph node's ``objective``: the work item's own text when it fits the node bound.

    A longer one (a one-app goal's whole objective, up to ``CONTRACT_OBJECTIVE``, or an
    integration node's objective that embeds it) is named by reference instead: the full text
    stays in the plan's work item and reaches every agent of the node (``full_objective``).
    """
    if len(objective) <= NODE_OBJECTIVE:
        return objective
    return (
        f"The objective of {node_id} is {len(objective)} characters, over the workgraph node "
        f"bound of {NODE_OBJECTIVE}; its full text is the plan's work item for {node_id} "
        "(for a whole-goal node, the contract objective) and is given to every agent in full."
    )


def full_objective(plan: dict[str, Any], node: dict[str, Any]) -> str:
    """The node's full objective from the plan's work items (``node_objective`` may name a long
    one by reference); the graph node's own text for a plan without that item or its text."""
    for item in plan.get("work_items") or []:
        if (item.get("node_id") or "node-" + str(item.get("app"))) == node["node_id"]:
            if isinstance(item.get("objective"), str) and item["objective"]:
                return str(item["objective"])
            break
    return str(node["objective"])
