"""RC02 — graph group: WorkGraph compiler correctness (test-catalog T-021..T-030).

Exercises the real GraphCompiler/validate_graph unit (INV-07 "Graph correctness",
failure GRAPH_INVALID) through ReferenceDeployment.runtime.save_graph / .graphs.compile.
No mocks of the unit under test; contracts/graphs are built from the real prepared
fixture and mutated only in the one dimension each case targets.
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from amplai_foundry.runtime.contracts.identity import digest, reference
from amplai_foundry.runtime.errors import Hold, RuntimeFault


def _contract_and_graph(d, prepared):
    contract = d.store.get(d.scope, "goal-contract", prepared["contract_ref"])
    graph = d.store.get(d.scope, "workgraph", prepared["graph_ref"])
    return contract, graph


def test_t021_cycle_rejection(deployment, prepared):
    # given: A depends B and B depends A / when: compile / expected: GRAPH_INVALID; no activation
    d = deployment
    _, graph = _contract_and_graph(d, prepared)
    template = graph["nodes"][0]
    node_a = deepcopy(template)
    node_b = deepcopy(template)
    node_a["node_id"], node_a["work_id"] = "node-cyc-a", "work-cyc-a"
    node_b["node_id"], node_b["work_id"] = "node-cyc-b", "work-cyc-b"
    node_a["depends_on"] = ["node-cyc-b"]
    node_b["depends_on"] = ["node-cyc-a"]
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t021"
    draft["nodes"] = [node_a, node_b]
    before = len(d.store.list_objects(d.scope, "workgraph"))
    with pytest.raises(RuntimeFault) as excinfo:
        d.runtime.save_graph(d.actor, draft, prepared["contract_ref"])
    assert excinfo.value.code == "GRAPH_CYCLE"
    after = len(d.store.list_objects(d.scope, "workgraph"))
    assert after == before  # no graph activation


def test_t022_stable_compiler_output(deployment, prepared):
    # given: same frozen contract/registry snapshot / when: compile twice in different
    # node iteration order / expected: same canonical DAG digest and stable topo order
    d = deployment
    contract, graph = _contract_and_graph(d, prepared)
    assert len(graph["nodes"]) >= 2
    forward = deepcopy(graph)
    reversed_draft = deepcopy(graph)
    reversed_draft["nodes"] = list(reversed(reversed_draft["nodes"]))
    compiled_forward = d.runtime.graphs.compile(forward, contract, prepared["contract_ref"])
    compiled_reversed = d.runtime.graphs.compile(reversed_draft, contract, prepared["contract_ref"])
    assert digest(compiled_forward) == digest(compiled_reversed)
    assert [n["node_id"] for n in compiled_forward["nodes"]] == [
        n["node_id"] for n in compiled_reversed["nodes"]
    ]


def test_t023_typed_artifact_mismatch(deployment, prepared):
    # given: producer outputs JSON; consumer requires binary ABI artifact / when: compile
    # expected: reject with exact edge finding (PORT_TYPE, realizing GRAPH_DATA_TYPES/SEM-07)
    d = deployment
    _, graph = _contract_and_graph(d, prepared)
    producer = deepcopy(graph["nodes"][0])
    consumer = deepcopy(graph["nodes"][0])
    producer["node_id"], producer["work_id"] = "node-producer", "work-producer"
    consumer["node_id"], consumer["work_id"] = "node-consumer", "work-consumer"
    port_name = producer["produces"][0]["name"]
    assert producer["produces"][0]["media_type"] == "application/json"
    consumer["depends_on"] = ["node-producer"]
    consumer["consumes"] = [
        {
            "name": "upstream",
            "from_node": "node-producer",
            "external_ref": None,
            "output_name": port_name,
            "media_type": "application/octet-stream",  # mismatches producer's JSON port
        }
    ]
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t023"
    draft["nodes"] = [producer, consumer]
    before = len(d.store.list_objects(d.scope, "workgraph"))
    with pytest.raises(RuntimeFault) as excinfo:
        d.runtime.save_graph(d.actor, draft, prepared["contract_ref"])
    assert excinfo.value.code == "PORT_TYPE"
    after = len(d.store.list_objects(d.scope, "workgraph"))
    assert after == before  # no graph activation


def test_t024_input_xor(deployment, prepared):
    # given: consumes supplies both from_node and external_ref / when: semantic validate
    # expected: reject; source must be exactly one
    d = deployment
    _, graph = _contract_and_graph(d, prepared)
    producer = deepcopy(graph["nodes"][0])
    consumer = deepcopy(graph["nodes"][0])
    producer["node_id"], producer["work_id"] = "node-producer2", "work-producer2"
    consumer["node_id"], consumer["work_id"] = "node-consumer2", "work-consumer2"
    port_name = producer["produces"][0]["name"]
    media_type = producer["produces"][0]["media_type"]
    consumer["depends_on"] = ["node-producer2"]
    consumer["consumes"] = [
        {
            "name": "both",
            "from_node": "node-producer2",
            "external_ref": prepared["contract_ref"],  # any well-formed ref shape
            "output_name": port_name,
            "media_type": media_type,
        }
    ]
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t024"
    draft["nodes"] = [producer, consumer]
    before = len(d.store.list_objects(d.scope, "workgraph"))
    with pytest.raises(RuntimeFault) as excinfo:
        d.runtime.save_graph(d.actor, draft, prepared["contract_ref"])
    assert excinfo.value.code == "PORT_XOR"
    after = len(d.store.list_objects(d.scope, "workgraph"))
    assert after == before  # no graph activation


def test_t025_missing_global_acceptance(deployment, prepared):
    # given: nodes cover unit tests but no cross-app integration criterion
    # when: compile/activate / expected: coverage failure (ACCEPTANCE_COVERAGE)
    d = deployment
    contract, graph = _contract_and_graph(d, prepared)
    mutated_contract = deepcopy(contract)
    verifier_ref = mutated_contract["acceptance"][0]["verifier_ref"]
    mutated_contract["acceptance"].append(
        {
            "id": "AC-cross-app-integration",
            "statement": "Cross-app integration result is verified end to end",
            "facet": "functional",
            "mandatory": True,
            "verifier_ref": verifier_ref,
            "required_evidence_types": ["json-verification"],
            "success_rule": "both app outputs are jointly verified",
            "human_acceptance_required": False,
        }
    )
    mutated_ref = reference(mutated_contract, "goal_id")
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t025"
    draft["contract_ref"] = mutated_ref
    with pytest.raises(Hold) as excinfo:
        d.runtime.graphs.compile(draft, mutated_contract, mutated_ref)
    assert excinfo.value.code == "ACCEPTANCE_COVERAGE"  # no node covers AC-cross-app-integration


def test_t026_unsafe_any_success_join(deployment, prepared):
    # given: write node joins either of two required approvals / when: compile
    # expected: unsafe join rejected (UNSAFE_JOIN)
    d = deployment
    _, graph = _contract_and_graph(d, prepared)
    node = deepcopy(graph["nodes"][0])
    node["node_id"], node["work_id"] = "node-unsafe-join", "work-unsafe-join"
    node["join"] = "any_success_read_only"
    assert any(c["effect_class"] != "pure_read" for c in node["capabilities"])
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t026"
    draft["nodes"] = [node]
    before = len(d.store.list_objects(d.scope, "workgraph"))
    with pytest.raises(RuntimeFault) as excinfo:
        d.runtime.save_graph(d.actor, draft, prepared["contract_ref"])
    assert excinfo.value.code == "UNSAFE_JOIN"
    after = len(d.store.list_objects(d.scope, "workgraph"))
    assert after == before  # no graph activation


def test_t030_graph_node_budget_limit(deployment, prepared):
    # given: draft exceeds configured max nodes / when: compile
    # expected: bounded GRAPH_INVALID/GRAPH_LIMIT; no unbounded fan-out.
    # The workgraph schema enforces this bound at maxItems=64
    # (src/amplai_foundry/runtime/contracts/data/schemas/workgraph.schema.json
    # properties.nodes.maxItems), so 65 nodes fails compile-time schema validation
    # before validate_graph's own GRAPH_NODE_LIMIT check ever runs — same GRAPH_INVALID
    # umbrella (INV-07), gate G-05.
    d = deployment
    _, graph = _contract_and_graph(d, prepared)
    template = graph["nodes"][0]
    nodes = []
    for i in range(65):  # schema/compiler caps WorkGraph at 1-64 nodes
        node = deepcopy(template)
        node["node_id"] = f"node-budget-{i}"
        node["work_id"] = f"work-budget-{i}"
        node["depends_on"] = []
        nodes.append(node)
    draft = deepcopy(graph)
    draft["graph_id"] = "graph-t030"
    draft["nodes"] = nodes
    before = len(d.store.list_objects(d.scope, "workgraph"))
    with pytest.raises(RuntimeFault) as excinfo:
        d.runtime.save_graph(d.actor, draft, prepared["contract_ref"])
    assert excinfo.value.code == "SCHEMA_INVALID"
    after = len(d.store.list_objects(d.scope, "workgraph"))
    assert after == before  # no unbounded fan-out is admitted
