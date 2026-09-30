import pytest

from stockroom.textutil import truncate

TEXTS = ["", "a", "abc", "abcdef", "hello world", "x" * 50, "Blue Mug, Large"]
ELLIPSES = ["…", "...", "", "--", "[more]", "."]


def test_result_is_never_longer_than_width() -> None:
    for text in TEXTS:
        for ellipsis in ELLIPSES:
            for width in range(1, 14):
                assert len(truncate(text, width, ellipsis)) <= width, (text, width, ellipsis)
    for text in TEXTS:
        for width in range(1, 14):
            assert len(truncate(text, width)) <= width, (text, width)


def test_text_that_fits_is_returned_unchanged() -> None:
    for text in TEXTS:
        for ellipsis in ELLIPSES:
            for width in range(max(len(text), 1), len(text) + 4):
                assert truncate(text, width, ellipsis) == text
    assert truncate("exact", 5) == "exact"
    assert truncate("abcdef", 6, "...") == "abcdef"


def test_a_cut_keeps_as_many_leading_characters_as_fit_and_ends_in_the_ellipsis() -> None:
    for text in TEXTS:
        for ellipsis in ELLIPSES:
            for width in range(max(len(ellipsis), 1), len(text)):
                result = truncate(text, width, ellipsis)
                assert result == text[: width - len(ellipsis)] + ellipsis, (text, width, ellipsis)
                assert len(result) == width


def test_examples() -> None:
    assert truncate("a long product name", 6) == "a lon…"
    assert truncate("Blue Mug, Large", 10) == "Blue Mug,…"
    assert truncate("abcdefg", 6, "...") == "abc..."
    assert truncate("abcdef", 3, "...") == "..."
    assert truncate("abcdef", 1) == "…"
    assert truncate("abcdefg", 4, "") == "abcd"
    assert truncate("abcdefg", 2, "--") == "--"


def test_width_below_one_is_refused() -> None:
    for width in (0, -1, -10):
        with pytest.raises(ValueError):
            truncate("abc", width)
        with pytest.raises(ValueError):
            truncate("", width)
