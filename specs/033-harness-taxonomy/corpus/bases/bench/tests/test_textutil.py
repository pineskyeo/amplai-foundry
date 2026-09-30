import pytest

from stockroom.textutil import align, plural, slugify, truncate


def test_slugify() -> None:
    assert slugify("Blue Mug, Large") == "blue-mug-large"
    assert slugify("  --Desk   Lamp--  ") == "desk-lamp"
    assert slugify("A5 notebook") == "a5-notebook"


def test_truncate_keeps_short_text() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("exact", 5) == "exact"


def test_truncate_marks_a_cut() -> None:
    assert truncate("a long product name", 6).endswith("…")
    with pytest.raises(ValueError):
        truncate("x", 0)


def test_align() -> None:
    assert align("ab", 4) == "ab  "
    assert align("ab", 4, "right") == "  ab"
    assert align("ab", 6, "center") == "  ab  "
    with pytest.raises(ValueError):
        align("ab", 4, "middle")


def test_plural() -> None:
    assert plural(1, "order") == "1 order"
    assert plural(0, "order") == "0 orders"
    assert plural(3, "item") == "3 items"
