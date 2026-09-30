from __future__ import annotations

import copy

import pytest

from demo_app.diffing import diff_infos, format_diff


def added(key: str, new: str) -> dict:
    return {"key": key, "kind": "added", "old": None, "new": new}


def removed(key: str, old: str) -> dict:
    return {"key": key, "kind": "removed", "old": old, "new": None}


def changed(key: str, old: str, new: str) -> dict:
    return {"key": key, "kind": "changed", "old": old, "new": new}


def test_added() -> None:
    assert diff_infos({"a": "1"}, {"a": "1", "b": "2"}) == [added("b", "2")]


def test_removed() -> None:
    assert diff_infos({"a": "1", "b": "2"}, {"a": "1"}) == [removed("b", "2")]


def test_changed() -> None:
    assert diff_infos({"a": "1"}, {"a": "2"}) == [changed("a", "1", "2")]


def test_change_dicts_have_exactly_four_keys() -> None:
    result = diff_infos({"a": "1", "b": "1"}, {"b": "2", "c": "3"})
    assert len(result) == 3
    for change in result:
        assert set(change) == {"key", "kind", "old", "new"}


def test_no_changes() -> None:
    assert diff_infos({}, {}) == []
    assert diff_infos({"a": "1", "b": "2"}, {"b": "2", "a": "1"}) == []
    assert diff_infos({"a": ""}, {"a": ""}) == []


def test_mixed_changes_sorted_by_key() -> None:
    old = {"stage": "DEV", "package_version": "1.0", "gone": "x", "same": "s"}
    new = {"stage": "RC", "package_version": "1.0", "fresh": "y", "same": "s"}
    assert diff_infos(old, new) == [
        added("fresh", "y"),
        removed("gone", "x"),
        changed("stage", "DEV", "RC"),
    ]


def test_sort_is_default_string_order() -> None:
    new = {"a9": "1", "a10": "1", "B": "1", "a": "1", "_": "1"}
    keys = [c["key"] for c in diff_infos({}, new)]
    assert keys == sorted(new)
    assert keys == ["B", "_", "a", "a10", "a9"]


def test_empty_string_is_a_real_value() -> None:
    assert diff_infos({"a": ""}, {}) == [removed("a", "")]
    assert diff_infos({}, {"a": ""}) == [added("a", "")]
    assert diff_infos({"a": ""}, {"a": "x"}) == [changed("a", "", "x")]
    assert diff_infos({"a": "x"}, {"a": ""}) == [changed("a", "x", "")]


def test_comparison_is_case_and_whitespace_sensitive() -> None:
    assert diff_infos({"a": "v"}, {"a": "V"}) == [changed("a", "v", "V")]
    assert diff_infos({"a": "v"}, {"a": "v "}) == [changed("a", "v", "v ")]


@pytest.mark.parametrize("bad", [None, 1, 1.5, True, ["x"], {"k": "v"}, b"x"])
def test_non_string_value_raises_type_error(bad: object) -> None:
    with pytest.raises(TypeError):
        diff_infos({"a": bad}, {"a": "x"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        diff_infos({"a": "x"}, {"a": bad})  # type: ignore[dict-item]


def test_non_string_raises_even_when_equal_or_on_unrelated_key() -> None:
    with pytest.raises(TypeError):
        diff_infos({"a": 1}, {"a": 1})
    with pytest.raises(TypeError):
        diff_infos({"a": "1", "b": 2}, {"a": "2"})
    with pytest.raises(TypeError):
        diff_infos({"a": "1"}, {"a": "1", "z": None})  # type: ignore[dict-item]


def test_inputs_are_not_modified_and_result_is_new() -> None:
    old = {"a": "1", "b": "2"}
    new = {"b": "3", "c": "4"}
    old_copy, new_copy = copy.deepcopy(old), copy.deepcopy(new)
    first = diff_infos(old, new)
    assert old == old_copy and new == new_copy
    first.clear()
    assert len(diff_infos(old, new)) == 3


def test_format_each_kind() -> None:
    assert format_diff([added("b", "2")]) == "+ b: 2"
    assert format_diff([removed("b", "2")]) == "- b: 2"
    assert format_diff([changed("b", "1", "2")]) == "~ b: 1 -> 2"


def test_format_joins_with_newline_and_no_trailing_newline() -> None:
    text = format_diff([added("a", "1"), removed("b", "2"), changed("c", "3", "4")])
    assert text == "+ a: 1\n- b: 2\n~ c: 3 -> 4"
    assert not text.endswith("\n")


def test_format_empty_list() -> None:
    assert format_diff([]) == ""


def test_format_keeps_given_order_and_values_verbatim() -> None:
    changes = [changed("z", "1", "2"), added("a", "x y"), removed("m", "")]
    assert format_diff(changes) == "~ z: 1 -> 2\n+ a: x y\n- m: "


def test_format_empty_values() -> None:
    assert format_diff([added("a", "")]) == "+ a: "
    assert format_diff([changed("a", "", "")]) == "~ a:  -> "


def test_diff_then_format() -> None:
    old = {"package_version": "3.0.0.dev2", "stage": "DEV-02"}
    new = {"package_version": "3.0.0.dev3", "stage": "DEV-03", "build": "7"}
    assert format_diff(diff_infos(old, new)) == (
        "+ build: 7\n~ package_version: 3.0.0.dev2 -> 3.0.0.dev3\n~ stage: DEV-02 -> DEV-03"
    )
