from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import demo_app
from demo_app.cli import main

GOOD = json.dumps({"package_version": "3.0.0.dev3", "stage": "DEV-03"})


class _NoRead(io.StringIO):
    def read(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("stdin must not be read")

    def readline(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("stdin must not be read")


def run(argv: list[str], text: str):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, io.StringIO(text), out, err)
    return code, out.getvalue(), err.getvalue()


def assert_error(code: int, out: str, err: str) -> None:
    assert code == 2
    assert out == ""
    assert err.endswith("\n") and err.count("\n") == 1
    assert err.startswith("error: ")
    assert len(err) > len("error: \n")


def test_plain_summary() -> None:
    assert run([], GOOD) == (0, "amplai 3.0.0.dev3 (DEV-03)\n", "")


def test_input_with_surrounding_whitespace_and_extra_keys() -> None:
    text = '\n  {"package_version": "1.0.0", "stage": "RC", "other": [1, 2], "x": null}  \n\n'
    assert run([], text) == (0, "amplai 1.0.0 (RC)\n", "")


def test_empty_stage_and_whitespace_version_are_valid() -> None:
    assert run([], '{"package_version": "1.0.0", "stage": ""}') == (0, "amplai 1.0.0 ()\n", "")
    assert run([], '{"package_version": " ", "stage": "S"}') == (0, "amplai   (S)\n", "")


def test_upper() -> None:
    assert run(["--upper"], GOOD) == (0, "AMPLAI 3.0.0.DEV3 (DEV-03)\n", "")


def test_json() -> None:
    assert run(["--json"], GOOD) == (
        0,
        '{"summary":"amplai 3.0.0.dev3 (DEV-03)"}\n',
        "",
    )


def test_json_escapes_non_ascii_like_json_dumps() -> None:
    text = json.dumps({"package_version": "1", "stage": "é"})
    assert run(["--json"], text) == (0, '{"summary":"amplai 1 (\\u00e9)"}\n', "")


def test_json_escapes_quotes() -> None:
    text = json.dumps({"package_version": "1", "stage": 'a"b'})
    code, out, err = run(["--json"], text)
    assert (code, err) == (0, "")
    assert out == '{"summary":"amplai 1 (a\\"b)"}\n'
    assert json.loads(out) == {"summary": 'amplai 1 (a"b)'}


@pytest.mark.parametrize("argv", [["--upper", "--json"], ["--json", "--upper"]])
def test_upper_then_json_in_any_order(argv: list[str]) -> None:
    assert run(argv, GOOD) == (
        0,
        '{"summary":"AMPLAI 3.0.0.DEV3 (DEV-03)"}\n',
        "",
    )


def test_repeated_options_behave_like_single() -> None:
    assert run(["--upper", "--upper"], GOOD) == run(["--upper"], GOOD)
    assert run(["--json", "--upper", "--json"], GOOD) == run(["--upper", "--json"], GOOD)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n",
        "{not json",
        "[]",
        '["package_version", "stage"]',
        "42",
        '"text"',
        "null",
        "{}",
        '{"stage": "DEV"}',
        '{"package_version": "1.0.0"}',
        '{"package_version": "", "stage": "DEV"}',
        '{"package_version": 3, "stage": "DEV"}',
        '{"package_version": null, "stage": "DEV"}',
        '{"package_version": ["1.0.0"], "stage": "DEV"}',
        '{"package_version": "1.0.0", "stage": null}',
        '{"package_version": "1.0.0", "stage": 7}',
        '{"package_version": "1.0.0", "stage": true}',
    ],
)
def test_invalid_input_is_an_error(text: str) -> None:
    assert_error(*run([], text))


def test_invalid_input_with_options_is_still_an_error() -> None:
    assert_error(*run(["--upper", "--json"], "[]"))


@pytest.mark.parametrize(
    "argv",
    [
        ["--bogus"],
        ["extra"],
        ["--upper", "extra"],
        ["--json", "--verbose"],
        ["-h"],
        ["--upper=1"],
        [""],
        ["--UPPER"],
        ["--upper", "--json", "--upper", "x"],
    ],
)
def test_usage_errors(argv: list[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, _NoRead(GOOD), out, err)
    assert code == 2
    assert out.getvalue() == ""
    assert err.getvalue() == "error: usage\n"


def test_main_returns_plain_ints() -> None:
    assert type(run([], GOOD)[0]) is int
    assert type(run(["nope"], GOOD)[0]) is int


def _subprocess(args: list[str], text: str) -> subprocess.CompletedProcess:
    root = Path(demo_app.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "demo_app", *args],
        input=text,
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        timeout=60,
    )


def test_python_dash_m_success() -> None:
    done = _subprocess([], GOOD)
    assert done.returncode == 0
    assert done.stdout == "amplai 3.0.0.dev3 (DEV-03)\n"
    assert done.stderr == ""


def test_python_dash_m_options() -> None:
    done = _subprocess(["--upper", "--json"], GOOD)
    assert done.returncode == 0
    assert done.stdout == '{"summary":"AMPLAI 3.0.0.DEV3 (DEV-03)"}\n'


def test_python_dash_m_errors() -> None:
    bad = _subprocess([], "[]")
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert bad.stderr.startswith("error: ")
    usage = _subprocess(["--nope"], GOOD)
    assert usage.returncode == 2
    assert usage.stdout == ""
    assert usage.stderr == "error: usage\n"


def test_importing_dunder_main_has_no_side_effects() -> None:
    import importlib

    module = importlib.import_module("demo_app.__main__")
    assert module.__name__ == "demo_app.__main__"
