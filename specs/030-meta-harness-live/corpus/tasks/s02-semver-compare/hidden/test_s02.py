import pytest

from demo_app.semver import compare_versions


def test_equal_versions_return_zero() -> None:
    assert compare_versions("1.2.3", "1.2.3") == 0
    assert compare_versions("v1.2.3", " 1.2.3 ") == 0
    assert compare_versions("01.2.3", "1.2.3") == 0


def test_older_and_newer() -> None:
    assert compare_versions("1.2.3", "1.2.4") == -1
    assert compare_versions("1.2.4", "1.2.3") == 1
    assert compare_versions("0.0.1", "0.0.0") == 1


def test_numeric_not_textual_comparison() -> None:
    assert compare_versions("1.10.0", "1.9.0") == 1
    assert compare_versions("2.0.0", "10.0.0") == -1
    assert compare_versions("1.2.10", "1.2.9") == 1


def test_major_beats_minor_beats_patch() -> None:
    assert compare_versions("2.0.0", "1.99.99") == 1
    assert compare_versions("1.2.0", "1.1.9") == 1
    assert compare_versions("1.1.9", "1.2.0") == -1


def test_result_is_exactly_minus_one_zero_or_one() -> None:
    assert compare_versions("9.0.0", "1.0.0") == 1
    assert compare_versions("1.0.0", "9.0.0") == -1


@pytest.mark.parametrize(
    "bad", ["", "1.2", "1.2.3.4", "1..3", "a.b.c", "+1.2.3", "1.-2.3", "vv1.2.3", "v", "  "]
)
def test_invalid_first_argument_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        compare_versions(bad, "1.0.0")


@pytest.mark.parametrize("bad", ["", "1.2", "1.2.3.4", "1.2.x", "1.+2.3", "vv1.2.3"])
def test_invalid_second_argument_raises(bad: str) -> None:
    with pytest.raises(ValueError):
        compare_versions("1.0.0", bad)
