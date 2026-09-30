from __future__ import annotations

import copy
from itertools import product

import pytest

from demo_app.upgrade import NoUpgradePath, plan_upgrade


def test_same_version_is_a_single_element_path() -> None:
    assert plan_upgrade("1", "1", {}) == ["1"]
    assert plan_upgrade("1", "1", {"1": ["2"]}) == ["1"]
    assert plan_upgrade("1", "1", {"1": ["1"], "2": ["1"]}) == ["1"]


def test_direct_edge() -> None:
    assert plan_upgrade("a", "b", {"a": ["b"]}) == ["a", "b"]


def test_shortest_path_beats_a_detour() -> None:
    steps = {"a": ["b", "d"], "b": ["c"], "c": ["d"], "d": ["e"]}
    assert plan_upgrade("a", "e", steps) == ["a", "d", "e"]
    assert plan_upgrade("a", "d", steps) == ["a", "d"]
    assert plan_upgrade("b", "e", steps) == ["b", "c", "d", "e"]


def test_shortest_beats_lexicographically_smaller_longer_path() -> None:
    steps = {"a": ["b", "z"], "b": ["c"], "c": ["t"], "z": ["t"]}
    assert plan_upgrade("a", "t", steps) == ["a", "z", "t"]


def test_lexicographic_tie_break_on_first_step() -> None:
    steps = {"s": ["y", "x", "z"], "x": ["t"], "y": ["t"], "z": ["t"]}
    assert plan_upgrade("s", "t", steps) == ["s", "x", "t"]


def test_lexicographic_tie_break_on_later_step() -> None:
    steps = {"s": ["a"], "a": ["c", "b"], "b": ["t"], "c": ["t"]}
    assert plan_upgrade("s", "t", steps) == ["s", "a", "b", "t"]


def test_tie_break_when_smaller_first_step_leads_only_to_a_longer_route() -> None:
    steps = {"s": ["a", "b"], "a": ["a2"], "a2": ["t"], "b": ["t"]}
    assert plan_upgrade("s", "t", steps) == ["s", "b", "t"]


def test_opaque_string_ordering() -> None:
    steps = {"1.0.0": ["1.9.0", "1.10.0"], "1.9.0": ["2.0.0"], "1.10.0": ["2.0.0"]}
    assert plan_upgrade("1.0.0", "2.0.0", steps) == ["1.0.0", "1.10.0", "2.0.0"]
    steps2 = {"s": ["9", "10"], "9": ["t"], "10": ["t"]}
    assert plan_upgrade("s", "t", steps2) == ["s", "10", "t"]


def test_versions_are_not_parsed() -> None:
    steps = {"v1": ["release candidate"], "release candidate": ["latest!"]}
    assert plan_upgrade("v1", "latest!", steps) == ["v1", "release candidate", "latest!"]
    assert plan_upgrade("", "x", {"": ["x"]}) == ["", "x"]


def test_cycles_do_not_break_the_search() -> None:
    steps = {"a": ["b"], "b": ["a", "c"], "c": ["a", "d"], "d": ["a"]}
    assert plan_upgrade("a", "d", steps) == ["a", "b", "c", "d"]
    assert plan_upgrade("d", "c", steps) == ["d", "a", "b", "c"]


def test_self_edges_and_duplicates_are_harmless() -> None:
    steps = {"a": ["a", "b", "b", "a"], "b": ["b", "c", "c"], "c": ["c"]}
    assert plan_upgrade("a", "c", steps) == ["a", "b", "c"]
    assert plan_upgrade("a", "b", steps) == ["a", "b"]


def test_target_that_only_appears_as_an_edge_target() -> None:
    assert plan_upgrade("a", "leaf", {"a": ["leaf"]}) == ["a", "leaf"]
    assert plan_upgrade("a", "leaf", {"a": ["m"], "m": ["leaf"]}) == ["a", "m", "leaf"]


def test_edges_are_directed() -> None:
    with pytest.raises(NoUpgradePath):
        plan_upgrade("b", "a", {"a": ["b"]})


@pytest.mark.parametrize(
    ("current", "target", "steps"),
    [
        ("a", "z", {}),
        ("a", "z", {"a": []}),
        ("a", "z", {"a": ["b"], "b": ["a"]}),
        ("a", "unknown", {"a": ["b"], "b": ["c"]}),
        ("nokey", "b", {"a": ["b"]}),
        ("a", "c", {"a": ["b"], "c": ["a"]}),
        ("a", "d", {"a": ["a"], "b": ["d"]}),
    ],
)
def test_unreachable_raises_no_upgrade_path(current: str, target: str, steps: dict) -> None:
    with pytest.raises(NoUpgradePath):
        plan_upgrade(current, target, steps)


def test_no_upgrade_path_is_a_lookup_error() -> None:
    assert issubclass(NoUpgradePath, LookupError)
    with pytest.raises(LookupError):
        plan_upgrade("a", "b", {})


def test_steps_not_modified_and_result_is_new() -> None:
    steps = {"a": ["c", "b"], "b": ["t", "a"], "c": ["t"]}
    before = copy.deepcopy(steps)
    first = plan_upgrade("a", "t", steps)
    assert steps == before
    first.append("junk")
    assert plan_upgrade("a", "t", steps) == ["a", "b", "t"]


def test_result_is_a_list_of_str() -> None:
    result = plan_upgrade("a", "b", {"a": ["b"]})
    assert type(result) is list
    assert plan_upgrade("q", "q", {}) == ["q"]
    assert type(plan_upgrade("q", "q", {})) is list


def _brute_force(current: str, target: str, steps: dict) -> list:
    """Every simple path is enumerated; the shortest, then lexicographically smallest, wins."""
    best: list = []

    def walk(path: list) -> None:
        nonlocal best
        if path[-1] == target:
            if not best or (len(path), path) < (len(best), best):
                best = list(path)
            return
        for nxt in steps.get(path[-1], []):
            if nxt not in path:
                walk([*path, nxt])

    walk([current])
    return best


def test_matches_brute_force_on_a_dense_graph_with_cycles() -> None:
    steps = {
        "1.0": ["1.1", "1.10", "2.0", "1.0"],
        "1.1": ["1.2", "1.10", "1.0"],
        "1.2": ["1.10", "2.0", "1.3"],
        "1.3": ["2.0", "1.1"],
        "1.10": ["1.2", "1.9", "3.0"],
        "1.9": ["2.0", "3.0", "1.1"],
        "2.0": ["2.1", "3.0", "1.0"],
        "2.1": ["3.0", "2.0", "1.2"],
    }
    nodes = sorted({*steps, *(n for v in steps.values() for n in v)})
    checked = 0
    for current, target in product(nodes, nodes):
        expected = _brute_force(current, target, steps)
        if expected:
            assert plan_upgrade(current, target, steps) == expected, (current, target)
            checked += 1
        else:
            with pytest.raises(NoUpgradePath):
                plan_upgrade(current, target, steps)
    assert checked >= len(nodes)


def test_matches_brute_force_on_a_layered_graph_with_many_ties() -> None:
    steps = {
        "s": ["c", "a", "b"],
        "a": ["f", "e", "d"],
        "b": ["e", "d", "f"],
        "c": ["d", "f"],
        "d": ["t", "x"],
        "e": ["t"],
        "f": ["t", "s"],
        "x": ["t"],
    }
    for current in ["s", "a", "b", "c", "d", "e", "f", "x"]:
        assert plan_upgrade(current, "t", steps) == _brute_force(current, "t", steps), current
    assert plan_upgrade("s", "t", steps) == ["s", "a", "d", "t"]
