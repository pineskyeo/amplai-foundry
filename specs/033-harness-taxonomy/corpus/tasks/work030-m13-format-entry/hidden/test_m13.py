import pytest

from demo_app.changelog import format_entry


def test_basic_entry() -> None:
    assert format_entry("1.2.0", "2026-09-30", ["add x", "fix y"]) == "## 1.2.0 - 2026-09-30\n\n- add x\n- fix y\n"


def test_single_change() -> None:
    assert format_entry("3.0.0.dev3", "2026-01-02", ["only one"]) == "## 3.0.0.dev3 - 2026-01-02\n\n- only one\n"


def test_changes_are_stripped_and_empty_ones_skipped() -> None:
    out = format_entry("1.0.0", "2026-01-01", ["  add x \t", "", "   ", "fix y\n", "\t"])
    assert out == "## 1.0.0 - 2026-01-01\n\n- add x\n- fix y\n"


def test_inner_whitespace_and_order_preserved() -> None:
    out = format_entry("1.0.0", "2026-01-01", ["b  c", "a"])
    assert out == "## 1.0.0 - 2026-01-01\n\n- b  c\n- a\n"


@pytest.mark.parametrize("changes", [[], [""], ["   ", "\t\n"], ["", ""]])
def test_no_changes_placeholder(changes: list) -> None:
    assert format_entry("1.0.0", "2026-01-01", changes) == "## 1.0.0 - 2026-01-01\n\n- no changes\n"


def test_exactly_one_trailing_newline() -> None:
    for changes in ([], ["a"], ["a", "b"], ["a\n\n"]):
        out = format_entry("1.0.0", "2026-01-01", changes)
        assert out.endswith("\n")
        assert not out.endswith("\n\n")
        assert "\n\n\n" not in out


def test_version_is_inserted_as_given() -> None:
    assert format_entry("v1.x", "2026-01-01", ["a"]).splitlines()[0] == "## v1.x - 2026-01-01"


@pytest.mark.parametrize("date", ["2024-13-45", "0000-00-00", "9999-99-99"])
def test_only_the_date_shape_is_checked(date: str) -> None:
    assert format_entry("1.0.0", date, ["a"]).startswith(f"## 1.0.0 - {date}\n")


@pytest.mark.parametrize(
    "date",
    ["2026-9-30", "26-09-30", "2026/09/30", "2026-09-30 ", " 2026-09-30", "2026-09-30\n", "", "yesterday", "20260930", "2026-09-300", "12026-09-30", "2026-09-30T00:00"],
)
def test_bad_date_raises(date: str) -> None:
    with pytest.raises(ValueError):
        format_entry("1.0.0", date, ["a"])
    with pytest.raises(ValueError):
        format_entry("1.0.0", date, [])
