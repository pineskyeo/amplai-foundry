"""Deterministic WorkGraph compiler: stable metadata in, immutable DAG out."""

from __future__ import annotations

import heapq
from copy import deepcopy

from ..contracts.authority import capability_contains
from ..contracts.identity import digest, reference
from ..errors import Hold, RuntimeFault


def validate_graph(graph: dict, contract: dict, contract_ref: dict) -> list[str]:
    if reference(contract, "goal_id") != contract_ref:
        raise Hold("CONTRACT_DIGEST", "Graph must bind the exact canonical contract definition")
    if graph["scope"] != contract["scope"] or graph["contract_ref"] != contract_ref:
        raise Hold(
            "GRAPH_CONTRACT_BINDING",
            "Graph must bind the current immutable contract in the same scope",
        )
    nodes = graph["nodes"]
    by_id = {n["node_id"]: n for n in nodes}
    if len(by_id) != len(nodes) or len({n["work_id"] for n in nodes}) != len(nodes):
        raise RuntimeFault("DUPLICATE_NODE", "Node and Work identifiers must be unique")
    if not 1 <= len(nodes) <= 64:
        raise Hold("GRAPH_NODE_LIMIT", "WorkGraph must have 1–64 nodes")
    indegree = {k: 0 for k in by_id}
    children = {k: [] for k in by_id}
    targets = {digest(r) for r in contract["targets"]}
    from ..execution.strategies import StrategyRouter

    for node in nodes:
        StrategyRouter.validate_node(node, contract)
        if digest(node["target_ref"]) not in targets:
            raise Hold("GRAPH_TARGET", "Work targets an app outside the contract")
        resources = [r["resource"] for r in node["resource_claims"]]
        if len(resources) != len(set(resources)):
            raise Hold("DUPLICATE_RESOURCE", "A node must claim each resource exactly once")
        writes = any(c["effect_class"] != "pure_read" for c in node["capabilities"])
        if writes and not any(r["mode"] == "exclusive_write" for r in node["resource_claims"]):
            raise Hold("WRITE_RESOURCE", "Effectful work requires an exclusive resource claim")
        if not node["objective"].strip():
            raise Hold("NODE_OBJECTIVE", "Work needs a meaningful objective")
        deps = node["depends_on"]
        if len(set(deps)) != len(deps) or any(d not in by_id for d in deps):
            raise RuntimeFault("GRAPH_DEPENDENCY", "Unknown or duplicate dependency")
        for dep in deps:
            children[dep].append(node["node_id"])
            indegree[node["node_id"]] += 1
        if node["join"] == "any_success_read_only" and any(
            c["effect_class"] != "pure_read" for c in node["capabilities"]
        ):
            raise Hold("UNSAFE_JOIN", "Any-success joins are restricted to read-only work")
        if node["strategy"] == "discovery" and any(
            c["effect_class"] != "pure_read" for c in node["capabilities"]
        ):
            raise Hold("DISCOVERY_WRITE", "Discovery cannot perform writes")
        if any(
            not capability_contains(contract["requested_capabilities"], c)
            for c in node["capabilities"]
        ):
            raise Hold("GRAPH_CAPABILITY", "Graph may not expand requested capabilities")
        for field in (
            "max_wall_seconds",
            "max_attempts",
            "max_tokens",
            "max_parallel_works",
            "max_delegation_depth",
        ):
            if node["budget"][field] > contract["budget"][field]:
                raise Hold("NODE_BUDGET", "Node budget exceeds root: " + field)
        if node["budget"]["currency"] != contract["budget"]["currency"]:
            raise Hold("BUDGET_CURRENCY", "Budget currencies differ")
        if contract["budget"]["max_cost_microunits"] is not None and (
            node["budget"]["max_cost_microunits"] is None
            or node["budget"]["max_cost_microunits"] > contract["budget"]["max_cost_microunits"]
        ):
            raise Hold("NODE_COST", "Node cost limit exceeds root")
    ready = [k for k, v in indegree.items() if v == 0]
    heapq.heapify(ready)
    order = []
    ancestors = {}
    while ready:
        current = heapq.heappop(ready)
        order.append(current)
        ancestors[current] = set(by_id[current]["depends_on"])
        for dep in by_id[current]["depends_on"]:
            ancestors[current].update(ancestors[dep])
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(order) != len(nodes):
        raise RuntimeFault("GRAPH_CYCLE", "Execution dependencies must be acyclic")
    known = {c["id"] for c in contract["acceptance"]}
    covered = set()
    for node in nodes:
        for field in ("consumes", "produces"):
            names = [v["name"] for v in node[field]]
            if len(set(names)) != len(names):
                raise RuntimeFault("DUPLICATE_PORT", "Typed port names must be unique per node")
        for item in node["consumes"]:
            producer, external, output = (
                item["from_node"],
                item["external_ref"],
                item["output_name"],
            )
            if (producer is None) == (external is None) or (producer is None) != (output is None):
                raise RuntimeFault(
                    "PORT_XOR",
                    "An input needs either producer+output or an external reference, exclusively",
                )
            if producer:
                if producer not in ancestors[node["node_id"]]:
                    raise RuntimeFault(
                        "DATA_DEPENDENCY", "Producer must precede the consumer through dependencies"
                    )
                ports = {p["name"]: p for p in by_id[producer]["produces"]}
                if output not in ports or ports[output]["media_type"] != item["media_type"]:
                    raise RuntimeFault(
                        "PORT_TYPE", "Named producer output missing or media type differs"
                    )
        if not set(node["acceptance_ids"]) <= known:
            raise RuntimeFault("UNKNOWN_ACCEPTANCE", "Work refers to nonexistent acceptance")
        covered.update(node["acceptance_ids"])
    if not {c["id"] for c in contract["acceptance"] if c["mandatory"]} <= covered:
        raise Hold("ACCEPTANCE_COVERAGE", "The graph does not cover every mandatory result")
    return order


class GraphCompiler:
    def __init__(self, contracts):
        self.contracts = contracts

    def compile(self, draft: dict, contract: dict, contract_ref: dict) -> dict:
        self.contracts.validate("goal-contract", contract)
        graph = deepcopy(draft)
        self.contracts.validate("workgraph", graph)
        order = validate_graph(graph, contract, contract_ref)
        index = {n["node_id"]: n for n in graph["nodes"]}
        graph["nodes"] = [index[k] for k in order]
        for node in graph["nodes"]:
            node["depends_on"] = sorted(node["depends_on"])
        return graph

    def compile_adaptive(self, draft, contract, contract_ref, *, facts_by_node, available):
        """Route from server-observed facts; never silently lower contract risk.

        A raised risk floor needs a newly reviewed contract before graph admission.
        The passed facts must come from the trusted context/policy layer, not from
        an agent's self-rating or an API field containing arbitrary risk claims.
        """
        from ..execution.strategies import RISK, StrategyRouter

        graph = deepcopy(draft)
        decisions = {}
        router = StrategyRouter()
        if set(facts_by_node) != {node["node_id"] for node in graph["nodes"]}:
            raise Hold(
                "STRATEGY_FACTS", "Every node needs a pinned server-observed task description"
            )
        for node in graph["nodes"]:
            result = router.select(contract, facts_by_node[node["node_id"]], available=available)
            if RISK[result.risk_floor] > RISK[contract["risk"]]:
                raise Hold(
                    "CONTRACT_RISK_FLOOR", "Protected changes require a higher-risk contract review"
                )
            node["strategy"] = result.strategy
            decisions[node["node_id"]] = {
                "strategy": result.strategy,
                "risk_floor": result.risk_floor,
                "reasons": list(result.reasons),
                "mandatory_gates": list(result.mandatory_gates),
            }
        return self.compile(graph, contract, contract_ref), decisions
