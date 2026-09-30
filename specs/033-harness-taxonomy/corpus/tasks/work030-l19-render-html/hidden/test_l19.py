from __future__ import annotations

import copy

import pytest

from demo_app.html_report import escape_html, render_html

HEAD = (
    "<!doctype html>\n"
    '<html lang="en">\n'
    '<head><meta charset="utf-8"><title>AMPLAI versions</title></head>\n'
    "<body>\n"
    "<table>\n"
    "<thead><tr><th>version</th><th>stage</th></tr></thead>\n"
    "<tbody>\n"
)
TAIL = "</tbody>\n</table>\n</body>\n</html>\n"


def row(v: str, s: str) -> str:
    return f"<tr><td>{v}</td><td>{s}</td></tr>\n"


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&#x27;"),
        ("", ""),
        ("plain text 1.2.3", "plain text 1.2.3"),
        ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
        ("a & b", "a &amp; b"),
        ("&lt;", "&amp;lt;"),
        ("&amp;", "&amp;amp;"),
        ("&&<<>>\"\"''", "&amp;&amp;&lt;&lt;&gt;&gt;&quot;&quot;&#x27;&#x27;"),
        ("it's \"x\"", "it&#x27;s &quot;x&quot;"),
        ("café ☃", "café ☃"),
        ("line1\nline2\ttab", "line1\nline2\ttab"),
        ("`=/;#%", "`=/;#%"),
    ],
)
def test_escape_html(raw: str, escaped: str) -> None:
    assert escape_html(raw) == escaped


def test_empty_list_renders_skeleton_with_empty_tbody() -> None:
    assert render_html([]) == HEAD + TAIL
    assert render_html([]) == (
        "<!doctype html>\n<html lang=\"en\">\n"
        '<head><meta charset="utf-8"><title>AMPLAI versions</title></head>\n'
        "<body>\n<table>\n<thead><tr><th>version</th><th>stage</th></tr></thead>\n"
        "<tbody>\n</tbody>\n</table>\n</body>\n</html>\n"
    )


def test_single_row() -> None:
    page = render_html([{"package_version": "3.0.0.dev3", "stage": "DEV-03"}])
    assert page == HEAD + row("3.0.0.dev3", "DEV-03") + TAIL


def test_rows_keep_input_order() -> None:
    infos = [
        {"package_version": "2.0.0", "stage": "B"},
        {"package_version": "1.0.0", "stage": "A"},
        {"package_version": "3.0.0", "stage": "C"},
    ]
    page = render_html(infos)
    assert page == HEAD + row("2.0.0", "B") + row("1.0.0", "A") + row("3.0.0", "C") + TAIL


def test_duplicate_infos_render_twice() -> None:
    info = {"package_version": "1", "stage": "S"}
    assert render_html([info, dict(info)]) == HEAD + row("1", "S") * 2 + TAIL


def test_values_are_escaped() -> None:
    page = render_html([{"package_version": "<1&2>", "stage": "it's \"x\""}])
    assert page == HEAD + row("&lt;1&amp;2&gt;", "it&#x27;s &quot;x&quot;") + TAIL
    assert "<1&2>" not in page


def test_already_escaped_text_is_escaped_again() -> None:
    page = render_html([{"package_version": "&lt;", "stage": "&amp;"}])
    assert page == HEAD + row("&amp;lt;", "&amp;amp;") + TAIL


def test_missing_stage_is_an_empty_cell() -> None:
    assert render_html([{"package_version": "1.0.0"}]) == HEAD + row("1.0.0", "") + TAIL
    assert "<td></td></tr>" in render_html([{"package_version": "1.0.0"}])


def test_empty_strings_are_empty_cells() -> None:
    assert render_html([{"package_version": "", "stage": ""}]) == HEAD + row("", "") + TAIL
    assert render_html([{"package_version": "", "stage": "S"}]) == HEAD + row("", "S") + TAIL


def test_extra_keys_are_ignored() -> None:
    info = {"package_version": "1.0.0", "stage": "RC", "python": "3.12", "extra": {"a": 1}}
    assert render_html([info]) == HEAD + row("1.0.0", "RC") + TAIL


@pytest.mark.parametrize("bad", [None, 1, 1.5, True, ["1.0.0"], b"1.0.0"])
def test_non_string_package_version_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        render_html([{"package_version": bad, "stage": "S"}])


def test_missing_package_version_raises() -> None:
    with pytest.raises(ValueError):
        render_html([{"stage": "S"}])
    with pytest.raises(ValueError):
        render_html([{}])


@pytest.mark.parametrize("bad", [None, 3, False, ["S"]])
def test_present_non_string_stage_raises(bad: object) -> None:
    with pytest.raises(ValueError):
        render_html([{"package_version": "1.0.0", "stage": bad}])


def test_bad_entry_anywhere_in_the_list_raises() -> None:
    good = {"package_version": "1.0.0", "stage": "S"}
    with pytest.raises(ValueError):
        render_html([good, good, {"stage": "S"}])
    with pytest.raises(ValueError):
        render_html([good, {"package_version": "2.0.0", "stage": None}, good])


def test_inputs_are_not_modified() -> None:
    infos = [{"package_version": "<1>", "stage": "S"}, {"package_version": "2"}]
    before = copy.deepcopy(infos)
    render_html(infos)
    assert infos == before


def test_output_ends_with_single_newline_and_is_a_str() -> None:
    page = render_html([{"package_version": "1", "stage": "S"}])
    assert isinstance(page, str)
    assert page.endswith("</html>\n")
    assert not page.endswith("\n\n")
    assert page.startswith("<!doctype html>\n<html lang=\"en\">\n")
