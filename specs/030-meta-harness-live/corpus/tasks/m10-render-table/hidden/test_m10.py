import pytest

from demo_app.table import render_table


def test_basic_table() -> None:
    assert render_table(["name", "v"], [["a", "1"], ["bbb", "22"]]) == "name | v\n-----+---\na    | 1\nbbb  | 22"


def test_column_grows_to_widest_cell() -> None:
    out = render_table(["id", "label"], [["1", "x"], ["12345", "yy"]])
    assert out == "id    | label\n------+------\n1     | x\n12345 | yy"


def test_header_wider_than_cells() -> None:
    out = render_table(["version", "s"], [["1", "a"]])
    assert out == "version | s\n--------+--\n1       | a"


def test_single_column_and_no_rows() -> None:
    assert render_table(["x"], []) == "x\n-"
    assert render_table(["title"], []) == "title\n-----"
    assert render_table(["a"], [["long cell"], ["b"]]) == "a\n---------\nlong cell\nb"


def test_three_columns() -> None:
    out = render_table(["a", "bb", "ccc"], [["1", "2", "3"], ["44", "55", "66"]])
    assert out == "a  | bb | ccc\n---+----+----\n1  | 2  | 3\n44 | 55 | 66"


def test_no_trailing_spaces_and_no_trailing_newline() -> None:
    out = render_table(["k", "value"], [["a", "long value"], ["b", "v"], ["c", ""]])
    lines = out.split("\n")
    assert lines == ["k | value", "--+-----------", "a | long value", "b | v", "c |"]
    assert all(line == line.rstrip(" ") for line in lines)
    assert not out.endswith("\n")


def test_empty_header_cells_keep_alignment() -> None:
    out = render_table(["", "n"], [["abc", "1"]])
    assert out == "    | n\n----+--\nabc | 1"


@pytest.mark.parametrize(
    "rows",
    [[["a"]], [["a", "b", "c"]], [[]], [["a", "b"], ["c"]], [["a", "b"], ["c", "d", "e"]]],
)
def test_row_length_mismatch_raises(rows: list) -> None:
    with pytest.raises(ValueError):
        render_table(["h1", "h2"], rows)
