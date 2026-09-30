import pytest

from demo_app.ranges import in_range


def test_typical_range() -> None:
    assert in_range("1.5.0", ">=1.0.0, <2.0.0") is True
    assert in_range("2.0.0", ">=1.0.0,<2.0.0") is False
    assert in_range("0.9.9", ">=1.0.0") is False
    assert in_range("1.0.0", ">=1.0.0, <2.0.0") is True
    assert in_range("1.99.99", ">=1.0.0, <2.0.0") is True


@pytest.mark.parametrize(
    "spec, lower, equal, higher",
    [
        (">=1.5.0", False, True, True),
        (">1.5.0", False, False, True),
        ("<=1.5.0", True, True, False),
        ("<1.5.0", True, False, False),
        ("==1.5.0", False, True, False),
        ("!=1.5.0", True, False, True),
    ],
)
def test_each_operator(spec: str, lower: bool, equal: bool, higher: bool) -> None:
    assert in_range("1.4.9", spec) is lower
    assert in_range("1.5.0", spec) is equal
    assert in_range("1.5.1", spec) is higher


def test_numeric_comparison() -> None:
    assert in_range("1.10.0", ">1.9.0") is True
    assert in_range("1.2.0", "<1.10.0") is True
    assert in_range("10.0.0", "<9.0.0") is False
    assert in_range("1.02.0", "==1.2.0") is True


def test_v_prefix_and_whitespace() -> None:
    assert in_range("  v1.2.3 ", ">= v1.0.0") is True
    assert in_range("v1.2.3", "  >=  1.2.3  ,  <=v1.2.3 ") is True
    assert in_range("1.2.3", "== v1.2.3") is True
    assert in_range("v2.0.0", "<1.0.0") is False


def test_all_comparisons_must_hold() -> None:
    assert in_range("1.5.0", ">=1.0.0, !=1.5.0, <2.0.0") is False
    assert in_range("1.5.1", ">=1.0.0, !=1.5.0, <2.0.0") is True
    assert in_range("1.5.0", "==1.5.0, ==1.5.0") is True


def test_empty_spec_is_true() -> None:
    assert in_range("1.2.3", "") is True
    assert in_range("1.2.3", "   ") is True


@pytest.mark.parametrize(
    "spec",
    [
        "1.0.0",
        "=1.0.0",
        "=>1.0.0",
        "<>1.0.0",
        "> =1.0.0",
        ">=",
        ">=1.0",
        ">=1.0.0,",
        ",",
        ">=1.0.0,,<2.0.0",
        ",>=1.0.0",
        ">= 1.0.0.0",
        ">=vv1.0.0",
        ">=V1.0.0",
        ">=+1.0.0",
        ">=1.-1.0",
        ">=a.b.c",
        "bad",
    ],
)
def test_malformed_spec_raises(spec: str) -> None:
    with pytest.raises(ValueError):
        in_range("1.2.3", spec)


def test_malformed_piece_raises_even_after_a_false_comparison() -> None:
    with pytest.raises(ValueError):
        in_range("0.1.0", ">=1.0.0, bad")
    with pytest.raises(ValueError):
        in_range("0.1.0", ">=1.0.0, <2")


@pytest.mark.parametrize("version", ["", "1.2", "1.2.3.4", "a.b.c", "1.-2.3", "vv1.2.3", "+1.2.3", "1..3", "V1.2.3"])
def test_unparsable_version_raises_even_with_empty_spec(version: str) -> None:
    with pytest.raises(ValueError):
        in_range(version, "")
    with pytest.raises(ValueError):
        in_range(version, ">=0.0.0")
