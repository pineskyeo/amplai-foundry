import pytest

from demo_app.formatting import truncate

ELLIPSIS = "…"


def test_text_that_fits_is_unchanged() -> None:
    assert truncate("hello", 5) == "hello"
    assert truncate("hello", 10) == "hello"
    assert truncate("", 1) == ""
    assert truncate("", 5) == ""
    assert truncate("a", 1) == "a"


def test_long_text_is_cut_with_ellipsis() -> None:
    assert truncate("hello world", 8) == "hello w" + ELLIPSIS
    assert truncate("abcdef", 4) == "abc" + ELLIPSIS
    assert truncate("abcde", 4) == "abc" + ELLIPSIS


def test_width_one_gives_only_the_ellipsis() -> None:
    assert truncate("abc", 1) == ELLIPSIS


def test_kept_part_is_not_stripped() -> None:
    assert truncate("ab cd", 4) == "ab " + ELLIPSIS


def test_result_length_is_exactly_width_for_long_text() -> None:
    for width in range(1, 12):
        result = truncate("x" * 30, width)
        assert len(result) == width
        assert result.endswith(ELLIPSIS)
        assert "..." not in result


@pytest.mark.parametrize("text,width", [("abc", 0), ("abc", -3), ("", 0), ("", -1)])
def test_width_below_one_raises(text: str, width: int) -> None:
    with pytest.raises(ValueError):
        truncate(text, width)
