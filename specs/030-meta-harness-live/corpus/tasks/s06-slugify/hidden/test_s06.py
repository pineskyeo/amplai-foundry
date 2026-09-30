import pytest

from demo_app.slug import slugify


def test_basic_lowercasing_and_dashes() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("already-a-slug") == "already-a-slug"
    assert slugify("UPPER") == "upper"


def test_runs_of_separators_collapse() -> None:
    assert slugify("a   b") == "a-b"
    assert slugify("a - b") == "a-b"
    assert slugify("a__b!!c") == "a-b-c"


def test_leading_and_trailing_separators_are_removed() -> None:
    assert slugify("  Hello, World!  ") == "hello-world"
    assert slugify("---x---") == "x"


def test_digits_are_kept() -> None:
    assert slugify("Release 3.0 RC-2") == "release-3-0-rc-2"
    assert slugify("42") == "42"


def test_non_ascii_characters_are_separators() -> None:
    assert slugify("Café Au Lait") == "caf-au-lait"


@pytest.mark.parametrize("text", ["", "   ", "!!!", "é", "-", "_ _"])
def test_empty_result_returns_n_a(text: str) -> None:
    assert slugify(text) == "n-a"


@pytest.mark.parametrize("text", ["a  b", "-a-", "x_-_y", "  1 2 3  ", "A.B.C"])
def test_no_edge_or_double_dashes(text: str) -> None:
    result = slugify(text)
    assert not result.startswith("-")
    assert not result.endswith("-")
    assert "--" not in result
