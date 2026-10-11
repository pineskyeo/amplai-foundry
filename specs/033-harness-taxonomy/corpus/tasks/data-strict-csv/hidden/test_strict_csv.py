from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from stockroom.errors import ParseError
from stockroom.strictcsv import CsvSyntaxError, format_csv, parse_csv

ROOT = Path(__file__).resolve().parents[3]


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=50,
        check=False,
    )


def where(text: str, **kwargs: str) -> tuple[int, int]:
    with pytest.raises(CsvSyntaxError) as caught:
        parse_csv(text, **kwargs)
    error = caught.value
    assert isinstance(error, ParseError)
    assert str(error).startswith(f"line {error.line}, column {error.column}: ")
    return error.line, error.column


def test_well_formed_files() -> None:
    assert parse_csv("a,b\r\n1,2\r\n") == [["a", "b"], ["1", "2"]]
    assert parse_csv("a,b\n1,2") == [["a", "b"], ["1", "2"]]
    assert parse_csv("a,b\r1,2\r") == [["a", "b"], ["1", "2"]]
    assert parse_csv("a,b\r\n1,2\n3,4\r5,6") == [["a", "b"], ["1", "2"], ["3", "4"], ["5", "6"]]
    assert parse_csv("") == []
    assert parse_csv("\n\r\n\r") == []
    assert parse_csv("\ufeff") == []
    assert parse_csv("\ufeffa,b\n") == [["a", "b"]]
    assert parse_csv("\ufeff\ufeffa,b\n") == [["\ufeffa", "b"]]
    assert parse_csv("a,b\n\n\r\n1,2\n\n") == [["a", "b"], ["1", "2"]]
    assert parse_csv('"a,b","c""d"\n') == [["a,b", 'c"d']]
    assert parse_csv('"line1\r\nline2","x\ry\nz"\n') == [["line1\r\nline2", "x\ry\nz"]]
    assert parse_csv('""\n') == [[""]]
    assert parse_csv('""\n""') == [[""], [""]]
    assert parse_csv('"""",""""""\n') == [['"', '""']]
    assert parse_csv("a,\n,b\n,\n") == [["a", ""], ["", "b"], ["", ""]]
    assert parse_csv(" a , b \n") == [[" a ", " b "]]
    assert parse_csv("café\xa0x,\u2028\x85\x0c\n") == [["café\xa0x", "\u2028\x85\x0c"]]
    assert parse_csv("a;b\n1;2\n", delimiter=";") == [["a", "b"], ["1", "2"]]
    assert parse_csv('a,b;"c;d"\n', delimiter=";") == [["a,b", "c;d"]]
    assert parse_csv("a\tb\n", delimiter="\t") == [["a", "b"]]
    assert parse_csv("a|b\n", delimiter="|") == [["a", "b"]]
    assert parse_csv("a,b\n", delimiter=";") == [["a,b"]]
    for bad in ("", ";;", '"', "\n", "\r", "\ufeff"):
        with pytest.raises(ValueError):
            parse_csv("a\n", delimiter=bad)


def test_syntax_errors_name_line_and_column() -> None:
    assert where('a,"b\n') == (1, 3)
    assert where('a,b\n1,"2\r\n3,4\n') == (2, 3)
    assert where('a"b,c\n') == (1, 2)
    assert where('a, "b"\n') == (1, 4)
    assert where('"a"b,c\n') == (1, 4)
    assert where('"a" ,c\n') == (1, 4)
    assert where('x,y\n"multi\r\nline"z,1\n') == (3, 6)
    assert where('x,y\n"a\rb\nc",1\n2,3"\n') == (5, 4)
    assert where('"a\r\n\r\n",b\nc,d"e\n') == (4, 4)
    assert where('\ufeffab,c"d\n') == (1, 5)
    assert where('a,b\n\n\nc,"d\n') == (4, 3)
    assert where('a\n"b""\n') == (2, 1)
    assert where('a;"b";c"\n', delimiter=";") == (1, 8)
    assert where('a;"b",c\n', delimiter=";") == (1, 6)
    assert where('"ok""",x\n"\u2028"y\n') == (2, 4)


def test_records_must_have_the_same_number_of_fields() -> None:
    assert where("a,b\n1,2,3\n") == (2, 1)
    assert where("a,b\n1\n") == (2, 1)
    assert where('a,b\n"x\ny",1\n1,2,3\n') == (4, 1)
    assert where('a\n"x\r\ny"\n\n1,2\n') == (5, 1)
    assert where('a,b\r1,2\r\r1,"2",3') == (4, 1)
    with pytest.raises(CsvSyntaxError) as caught:
        parse_csv("a,b,c\n1,2\n")
    assert "expected 3 fields, found 2" in str(caught.value)


def test_format_csv() -> None:
    assert format_csv([["a", "b"], ["1", "2"]]) == "a,b\r\n1,2\r\n"
    assert format_csv([["a", "b"]], line_end="\n") == "a,b\n"
    assert format_csv([["a", "b"]], line_end="\r") == "a,b\r"
    assert format_csv([]) == ""
    assert format_csv(iter([("a", "b")])) == "a,b\r\n"
    assert format_csv([["a,b", 'c"d', "e\nf", "g\rh", " i ", ""]]) == (
        '"a,b","c""d","e\nf","g\rh", i ,\r\n'
    )
    assert format_csv([[""]]) == '""\r\n'
    assert format_csv([[""], ["x"], [""]]) == '""\r\nx\r\n""\r\n'
    assert format_csv([["", ""]]) == ",\r\n"
    assert format_csv([["a;b", "c,d"]], delimiter=";") == '"a;b";c,d\r\n'
    assert format_csv([["a\tb", "c"]], delimiter="\t") == '"a\tb"\tc\r\n'
    assert format_csv([["\ufeffx", "\xa0", "a\ufeff"]]) == '"\ufeffx",\xa0,a\ufeff\r\n'
    for kwargs in (
        {"line_end": "\r\n\r\n"},
        {"line_end": ""},
        {"delimiter": '"'},
        {"delimiter": "ab"},
        {"delimiter": "\ufeff"},
    ):
        with pytest.raises(ValueError):
            format_csv([["a"]], **kwargs)
    with pytest.raises(ValueError):
        format_csv([["a", "b"], ["c"]])
    with pytest.raises(ValueError):
        format_csv([[]])


CHARS = [
    "a",
    "b",
    "Z",
    "1",
    " ",
    ",",
    ";",
    "\t",
    '"',
    "\r",
    "\n",
    "\r\n",
    "é",
    "\xa0",
    "\u2028",
    "\ufeff",
]


def random_table(rng: random.Random) -> list[list[str]]:
    width = rng.randint(1, 4)
    rows = []
    for _ in range(rng.randint(0, 5)):
        row = []
        for _ in range(width):
            row.append("".join(rng.choice(CHARS) for _ in range(rng.choice([0, 0, 1, 2, 5]))))
        rows.append(row)
    return rows


def test_generated_tables_round_trip() -> None:
    rng = random.Random(7031)
    for _ in range(3000):
        rows = random_table(rng)
        delimiter = rng.choice([",", ";", "\t", "|"])
        line_end = rng.choice(["\r\n", "\n", "\r"])
        text = format_csv(rows, delimiter=delimiter, line_end=line_end)
        assert parse_csv(text, delimiter=delimiter) == rows
        assert parse_csv("\ufeff" + text, delimiter=delimiter) == rows
        assert parse_csv(text.rstrip("\r\n"), delimiter=delimiter) == rows
        assert (
            format_csv(parse_csv(text, delimiter=delimiter), delimiter=delimiter, line_end=line_end)
            == text
        )


def position(text: str, index: int) -> tuple[int, int]:
    line, start, i = 1, 0, 0
    while i < index:
        if text[i] == "\r" and text.startswith("\r\n", i) and i + 1 < index:
            i += 2
            line, start = line + 1, i
            continue
        if text[i] in "\r\n":
            line, start = line + 1, i + 1
        i += 1
    return line, index - start + 1


def test_generated_errors_name_line_and_column() -> None:
    rng = random.Random(99)
    checked = 0
    while checked < 1500:
        rows = random_table(rng)
        if not rows:
            continue
        line_end = rng.choice(["\r\n", "\n", "\r"])
        text = format_csv(rows, line_end=line_end)
        # the opening quotes of quoted fields and the plain characters of unquoted fields
        openings, plain = [], []
        i, at_start, quoted = 0, True, False
        while i < len(text):
            char = text[i]
            if quoted:
                if char == '"':
                    if text.startswith('""', i):
                        i += 2
                        continue
                    quoted = False
                i += 1
                continue
            if at_start and char == '"':
                openings.append(i)
                quoted, at_start = True, False
            elif char in ",\r\n":
                at_start = True
            else:
                if not at_start:
                    plain.append(i)
                at_start = False
            i += 1
        if rng.random() < 0.5 and openings:
            # cut the text inside a quoted field: unterminated, reported at its opening quote
            start = rng.choice(openings)
            end = text.index('"', start + 1)
            while text.startswith('""', end):
                end = text.index('"', end + 2)
            cut = text[:end]
            assert where(cut) == position(cut, start), cut
        elif plain:
            # a quote inside an unquoted field (not as its first character)
            index = rng.choice(plain)
            damaged = text[:index] + '"' + text[index:]
            assert where(damaged) == position(damaged, index), damaged
        else:
            continue
        checked += 1


def test_lint_command(tmp_path: Path) -> None:
    good = '\ufeffsku,note\r\nMUG-001,"two\r\nlines"\nTEA-010,"say ""hi"""\r\n\r\nNB-200,\r'
    (tmp_path / "good.csv").write_bytes(good.encode("utf-8"))
    done = run("lint", "good.csv", "--out", "clean.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "4 records of 2 fields\n"
    assert (tmp_path / "clean.csv").read_bytes() == (
        b'sku,note\r\nMUG-001,"two\r\nlines"\r\nTEA-010,"say ""hi"""\r\nNB-200,\r\n'
    )
    (tmp_path / "one.csv").write_bytes(b'""')
    done = run("lint", "one.csv", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "1 record of 1 field\n"
    (tmp_path / "empty.csv").write_bytes(b"\r\n\n")
    done = run("lint", "empty.csv", "--out", "e.csv", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "0 records\n"
    assert (tmp_path / "e.csv").read_bytes() == b""
    (tmp_path / "semi.csv").write_bytes(b'a;b\n"x;y";z\n')
    done = run("lint", "semi.csv", "--delimiter", ";", "--out", "s.csv", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "2 records of 2 fields\n"
    assert (tmp_path / "s.csv").read_bytes() == b"a,b\r\nx;y,z\r\n"
    (tmp_path / "tab.csv").write_bytes(b"a\tb,c\n")
    done = run("lint", "tab.csv", "--delimiter", "\t", cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == "1 record of 2 fields\n"
    (tmp_path / "bad.csv").write_bytes(b'a,b\r\n"x\r\ny",1\r\n2,3"\r\n')
    done = run("lint", "bad.csv", "--out", "never.csv", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: line 4, column 4: ")
    assert not (tmp_path / "never.csv").exists()
    (tmp_path / "count.csv").write_bytes(b"a,b\n1,2,3\n")
    done = run("lint", "count.csv", cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr.startswith("stockroom: error: line 2, column 1: ")
    (tmp_path / "latin.csv").write_bytes(b"caf\xe9\n")
    done = run("lint", "latin.csv", cwd=tmp_path)
    assert (
        done.returncode == 1 and done.stdout == "" and done.stderr.startswith("stockroom: error:")
    )
    done = run("lint", "good.csv", "--delimiter", '"', cwd=tmp_path)
    assert (
        done.returncode == 1 and done.stdout == "" and done.stderr.startswith("stockroom: error:")
    )
