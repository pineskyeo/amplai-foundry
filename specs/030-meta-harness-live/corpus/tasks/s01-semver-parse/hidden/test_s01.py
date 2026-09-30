import pytest

from demo_app.semver import parse_version


def test_plain_and_v_prefixed() -> None:
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("v10.0.7") == (10, 0, 7)


def test_whitespace_is_ignored() -> None:
    assert parse_version("  2.0.1 ") == (2, 0, 1)


@pytest.mark.parametrize("text", ["", "1.2", "1.2.3.4", "a.b.c", "1..3", "v", "vv1.2.3"])
def test_malformed_text_raises(text: str) -> None:
    with pytest.raises(ValueError):
        parse_version(text)


@pytest.mark.parametrize("text", ["1.-2.3", "+1.2.3", "1.2.-3", "1.+2.3"])
def test_signed_parts_raise(text: str) -> None:
    with pytest.raises(ValueError):
        parse_version(text)
