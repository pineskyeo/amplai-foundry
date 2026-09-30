from __future__ import annotations

import copy

import pytest

from demo_app.resolver import ResolveError, resolve
from demo_app.versions import parse


def test_parse_valid() -> None:
    assert parse("1.2.3") == (1, 2, 3)
    assert parse("v10.0.7") == (10, 0, 7)
    assert parse("0.0.0") == (0, 0, 0)
    assert parse("1.02.003") == (1, 2, 3)
    assert parse("100.200.300") == (100, 200, 300)


def test_parse_result_is_a_tuple_of_ints() -> None:
    result = parse("v1.2.3")
    assert type(result) is tuple
    assert all(type(part) is int for part in result)


@pytest.mark.parametrize(
    "text",
    ["", "v", "vv1.2.3", "V1.2.3", " 1.2.3", "1.2.3 ", "1.2.3\n", "1.2", "1.2.3.4", "1.2.x",
     "-1.2.3", "+1.2.3", "1..3", "a.b.c", "1.2.3-rc1", "v 1.2.3", "1,2,3", "١.2.3"],
)
def test_parse_invalid(text: str) -> None:
    with pytest.raises(ValueError):
        parse(text)


def test_parse_orders_numerically() -> None:
    assert parse("1.10.0") > parse("1.9.0")
    assert parse("2.0.0") > parse("1.99.99")


def test_highest_satisfying_version_wins() -> None:
    available = {"a": ["0.9.0", "1.5.0", "1.10.0", "2.0.0", "1.9.9"]}
    assert resolve({"a": ">=1.0.0,<2.0.0"}, available) == {"a": "1.10.0"}


def test_each_operator() -> None:
    avail = {"p": ["1.0.0", "1.5.0", "2.0.0", "3.0.0"]}
    assert resolve({"p": ">=2.0.0"}, avail) == {"p": "3.0.0"}
    assert resolve({"p": ">2.0.0"}, avail) == {"p": "3.0.0"}
    assert resolve({"p": "<=2.0.0"}, avail) == {"p": "2.0.0"}
    assert resolve({"p": "<2.0.0"}, avail) == {"p": "1.5.0"}
    assert resolve({"p": "==1.5.0"}, avail) == {"p": "1.5.0"}


def test_operator_boundaries_are_exact() -> None:
    avail = {"p": ["1.0.0", "2.0.0"]}
    assert resolve({"p": ">1.0.0"}, avail) == {"p": "2.0.0"}
    assert resolve({"p": "<2.0.0"}, avail) == {"p": "1.0.0"}
    with pytest.raises(ResolveError):
        resolve({"p": ">2.0.0"}, avail)
    with pytest.raises(ResolveError):
        resolve({"p": "<1.0.0"}, avail)
    with pytest.raises(ResolveError):
        resolve({"p": "==1.5.0"}, avail)


def test_numeric_not_lexicographic_comparison() -> None:
    avail = {"p": ["1.9.0", "1.10.0", "1.2.0"]}
    assert resolve({"p": ""}, avail) == {"p": "1.10.0"}
    assert resolve({"p": "<1.10.0"}, avail) == {"p": "1.9.0"}
    assert resolve({"p": ">=1.10.0"}, avail) == {"p": "1.10.0"}


def test_whitespace_is_allowed_in_constraints() -> None:
    avail = {"p": ["1.0.0", "1.5.0", "2.0.0"]}
    assert resolve({"p": "  >=  1.0.0  ,  <  2.0.0  "}, avail) == {"p": "1.5.0"}
    assert resolve({"p": "\t>=1.0.0\t,\t<2.0.0"}, avail) == {"p": "1.5.0"}
    assert resolve({"p": " == 2.0.0 "}, avail) == {"p": "2.0.0"}


@pytest.mark.parametrize("constraint", ["", "   ", "\t\n"])
def test_empty_constraint_means_any(constraint: str) -> None:
    assert resolve({"p": constraint}, {"p": ["0.1.0", "0.3.0", "0.2.0"]}) == {"p": "0.3.0"}


def test_v_prefix_in_available_and_constraints() -> None:
    avail = {"p": ["v1.0.0", "1.2.0", "v1.4.0", "1.3.0"]}
    assert resolve({"p": ""}, avail) == {"p": "v1.4.0"}
    assert resolve({"p": "<v1.4.0"}, avail) == {"p": "1.3.0"}
    assert resolve({"p": ">=v1.0.0,<1.2.1"}, avail) == {"p": "1.2.0"}
    assert resolve({"p": "==v1.0.0"}, avail) == {"p": "v1.0.0"}
    assert resolve({"p": "==1.4.0"}, avail) == {"p": "v1.4.0"}


def test_equal_versions_first_in_list_wins() -> None:
    assert resolve({"p": ""}, {"p": ["1.0.0", "v2.0.0", "2.0.0"]}) == {"p": "v2.0.0"}
    assert resolve({"p": ""}, {"p": ["1.0.0", "2.0.0", "v2.0.0"]}) == {"p": "2.0.0"}
    assert resolve({"p": "==1.0.0"}, {"p": ["v1.0.0", "1.0.0"]}) == {"p": "v1.0.0"}


def test_result_key_order_follows_constraints() -> None:
    constraints = {"zeta": "", "alpha": "", "mid": ""}
    available = {
        "alpha": ["1.0.0"],
        "mid": ["2.0.0"],
        "zeta": ["3.0.0"],
        "unused": ["not a version"],
    }
    result = resolve(constraints, available)
    assert list(result) == ["zeta", "alpha", "mid"]
    assert result == {"zeta": "3.0.0", "alpha": "1.0.0", "mid": "2.0.0"}


def test_empty_constraints_and_inputs_untouched() -> None:
    assert resolve({}, {"a": ["1.0.0"]}) == {}
    assert resolve({}, {}) == {}
    constraints = {"a": ">=1.0.0"}
    available = {"a": ["2.0.0", "1.0.0"]}
    before = (copy.deepcopy(constraints), copy.deepcopy(available))
    resolve(constraints, available)
    assert (constraints, available) == before


def test_missing_package_raises_resolve_error_naming_it() -> None:
    with pytest.raises(ResolveError) as info:
        resolve({"ghost": ">=1.0.0"}, {"a": ["1.0.0"]})
    assert "ghost" in str(info.value)
    assert isinstance(info.value, LookupError)
    assert not isinstance(info.value, ValueError)


def test_unsatisfiable_names_package_and_constraint() -> None:
    constraint = " >=3.0.0 , <4.0.0 "
    with pytest.raises(ResolveError) as info:
        resolve({"widget": constraint}, {"widget": ["1.0.0", "2.0.0"]})
    assert "widget" in str(info.value)
    assert constraint in str(info.value)


def test_no_versions_at_all_is_unsatisfiable() -> None:
    with pytest.raises(ResolveError) as info:
        resolve({"empty": ""}, {"empty": []})
    assert "empty" in str(info.value)
    with pytest.raises(ResolveError):
        resolve({"empty": ">=1.0.0"}, {"empty": []})


def test_conflicting_comparisons_are_unsatisfiable() -> None:
    with pytest.raises(ResolveError):
        resolve({"p": ">=2.0.0,<2.0.0"}, {"p": ["1.0.0", "2.0.0", "3.0.0"]})


@pytest.mark.parametrize(
    "constraint",
    [
        ">=1.0.0,",
        ",",
        ",>=1.0.0",
        ">=1.0.0,,<2.0.0",
        "1.2.3",
        "v1.2.3",
        ">=",
        ">= ",
        "!=1.0.0",
        "=1.0.0",
        "~=1.0.0",
        "=>1.0.0",
        "=<1.0.0",
        ">==1.0.0",
        "> =1.0.0",
        ">>1.0.0",
        ">=1.0",
        ">=1.0.0.0",
        ">=x.y.z",
        ">=1. 0.0",
        ">=V1.0.0",
        ">=1.0.0 <2.0.0",
        ">=1.0.0;<2.0.0",
        "latest",
    ],
)
def test_malformed_constraint_raises_value_error(constraint: str) -> None:
    with pytest.raises(ValueError):
        resolve({"p": constraint}, {"p": ["1.0.0", "2.0.0"]})


@pytest.mark.parametrize("bad", ["1.0", "latest", "", "1.0.0-rc1", "V1.0.0", " 1.0.0"])
def test_malformed_available_version_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        resolve({"p": ">=1.0.0"}, {"p": ["1.0.0", bad]})
    with pytest.raises(ValueError):
        resolve({"p": ""}, {"p": [bad]})


def test_malformed_available_version_raises_even_if_it_would_not_match() -> None:
    with pytest.raises(ValueError):
        resolve({"p": ">=5.0.0"}, {"p": ["1.0.0", "broken"]})


def test_error_order_for_one_package() -> None:
    # (1) missing package beats a malformed constraint
    with pytest.raises(ResolveError):
        resolve({"p": "garbage"}, {})
    # (2) malformed constraint beats a malformed available version and an unsatisfiable list
    with pytest.raises(ValueError) as info:
        resolve({"p": "garbage"}, {"p": ["broken"]})
    assert not isinstance(info.value, ResolveError)
    with pytest.raises(ValueError) as info2:
        resolve({"p": "garbage"}, {"p": []})
    assert not isinstance(info2.value, ResolveError)
    # (3) malformed available version beats an unsatisfiable constraint
    with pytest.raises(ValueError) as info3:
        resolve({"p": ">=9.0.0"}, {"p": ["1.0.0", "broken"]})
    assert not isinstance(info3.value, ResolveError)


def test_packages_are_processed_in_constraint_order() -> None:
    available = {"good": ["1.0.0"]}
    with pytest.raises(ResolveError):
        resolve({"good": "", "missing": "", "bad": "garbage"}, available)
    with pytest.raises(ValueError) as info:
        resolve({"good": "", "bad": "garbage", "missing": ""}, {**available, "bad": ["1.0.0"]})
    assert not isinstance(info.value, ResolveError)
