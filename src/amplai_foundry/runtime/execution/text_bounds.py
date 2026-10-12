"""Length bounds of the text agents act on (operator decision 2026-10-12, option A; D-124).

Planner, strategy and operator text that reaches a contract, a workgraph or an agent prompt is
never cut. A value over its frozen 3.0.0 wire bound Holds with a coded error that names the
field, its length and the bound, so the request is shortened or split instead of reaching an
agent without its end (pilot calibration 10: six feature tasks lost their closing rules to the
old 4000-character cut).

The bounds are the wire schemas' (``runtime/contracts/data/schemas``); text that has no wire
field of its own (``in_scope``, ``questions``, ``summary``, strategy steps, integration notes)
takes the contract's item bound.

A workgraph node objective over the node bound is named by reference with the digest of the full
text (D-124): a whole-goal node names the goal contract's objective (the immutable contract the
graph's ``contract_ref`` names, the source of truth), any other node (an integration node: goal +
notes) the plan's work item. Agents always read the full text (``full_objective``), checked
against that digest.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
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
REFERENCE_MISMATCH = "OBJECTIVE_REFERENCE_MISMATCH"
SPLIT_HINT = "Shorten the request or split it into several goals."

# the sources a node reference names (D-124)
FROM_CONTRACT = "contract.objective"
FROM_WORK_ITEM = "plan.work_item"
REFERENCE = re.compile(
    r"named by reference: source (contract\.objective|plan\.work_item), "
    r"digest (sha256:[0-9a-f]{64})\b"
)


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


def text_digest(text: str) -> str:
    """``sha256:`` of the exact text (UTF-8): the digest a node reference carries."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def node_objective(objective: str, node_id: str, *, whole_goal: bool) -> str:
    """The workgraph node's ``objective``: the work item's own text when it fits the node bound.

    A longer one is named by reference with the digest of its exact text (D-124), so two node
    definitions differ whenever their objectives do (design/06 reuse key: the node definition
    digest) and the original can be checked. A whole-goal node (a one-app goal, or a work item
    that carries the whole goal) names the goal contract's objective; any other node (an
    integration node: goal + notes) names the plan's work item. Agents get the full text
    (``full_objective``).
    """
    if len(objective) <= NODE_OBJECTIVE:
        return objective
    where = (
        f"source {FROM_CONTRACT}, digest {text_digest(objective)} (the objective of the "
        "immutable goal contract this workgraph's contract_ref names)"
        if whole_goal
        else f"source {FROM_WORK_ITEM}, digest {text_digest(objective)} (the plan's work item "
        f"for {node_id})"
    )
    return (
        f"The objective of {node_id} is {len(objective)} characters, over the workgraph node "
        f"bound of {NODE_OBJECTIVE}, so it is named by reference: {where}. Every agent of this "
        "node is given the full text."
    )


def _const(text: str) -> Callable[[], str]:
    return lambda: text


def full_objective(
    plan: dict[str, Any], node: dict[str, Any], contract_objective: Callable[[], str]
) -> str:
    """The node's full objective.

    A node within the bound holds it itself. A node named by reference (``node_objective``) is
    resolved from the plan's work item and, for a whole-goal node, falls back to
    ``contract_objective()`` (the goal contract's objective, the source of truth). The text must
    match the reference's digest; Hold OBJECTIVE_REFERENCE_MISMATCH when no source does.
    """
    own = str(node["objective"])
    named = REFERENCE.search(own)
    if named is None:
        return own
    source, expected = named.group(1), named.group(2)
    readers: list[tuple[str, Callable[[], str]]] = []
    for item in plan.get("work_items") or []:
        if (item.get("node_id") or "node-" + str(item.get("app"))) == node["node_id"]:
            text = item.get("objective")
            if isinstance(text, str):
                readers.append((FROM_WORK_ITEM, _const(text)))
            break
    if source == FROM_CONTRACT:
        readers.append((FROM_CONTRACT, contract_objective))
    found: dict[str, str] = {}
    for where, read in readers:
        text = read()
        found[where] = text_digest(text)
        if found[where] == expected:
            return text
    raise Hold(
        REFERENCE_MISMATCH,
        f"No source of {node['node_id']}'s objective matches its node reference",
        details={"node_id": node["node_id"], "source": source, "expected": expected,
                 "found": found},
    )  # fmt: skip
