from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KEY = "STOCKROOM_CURRENCY_SYMBOL"
TAX = "STOCKROOM_TAX_BP"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    base["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        timeout=50,
        check=False,
    )


def write(tmp_path: Path, content: str | bytes, name: str = "app.env") -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return path


PRICE = ["price", "--items", "data/items.csv", "--sku", "MUG-001", "--quantity", "3"]


def price(env_file: Path, *, config: Path | None = None, env: dict[str, str] | None = None):
    args = ["--env-file", str(env_file)]
    if config is not None:
        args += ["--config", str(config)]
    return run(*args, *PRICE, env=env)


def symbol_of(tmp_path: Path, content: str) -> str:
    """The currency symbol that the env file content gives, read from the price output."""
    done = price(write(tmp_path, content))
    assert done.returncode == 0, (content, done.stderr)
    assert done.stderr == b""
    text = done.stdout.decode("utf-8")
    head = "3 x MUG-001 Blue Mug: "
    assert text.startswith(head), text
    tail = "37.50\ntax: "
    end = text.index(tail)
    symbol = text[len(head) : end]
    # the same symbol is used on every amount
    assert text == f"{head}{symbol}37.50\ntax: {symbol}0.00\ntotal: {symbol}37.50\n", text
    return symbol


def test_env_file_values_and_precedence(tmp_path: Path) -> None:
    env_file = write(tmp_path, f"{TAX}=1000\n{KEY}=GBP\n")
    done = price(env_file)
    assert done.returncode == 0 and done.stderr == b""
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: GBP37.50",
        "tax: GBP3.75",
        "total: GBP41.25",
    ]
    ini = write(tmp_path, "[stockroom]\ncurrency_symbol = EUR\ntax_bp = 825\n", "s.ini")
    # the env file beats the INI file
    done = price(env_file, config=ini)
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: GBP37.50",
        "tax: GBP3.75",
        "total: GBP41.25",
    ]
    # the INI value stays for keys the env file does not set
    only_tax = write(tmp_path, f"{TAX}=1000\n", "tax.env")
    done = price(only_tax, config=ini)
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: EUR37.50",
        "tax: EUR3.75",
        "total: EUR41.25",
    ]
    # the real environment beats both
    done = price(env_file, config=ini, env={KEY: "CHF", TAX: "500"})
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: CHF37.50",
        "tax: CHF1.88",
        "total: CHF39.38",
    ]
    done = price(env_file, config=ini, env={KEY: "CHF"})
    assert done.stdout.decode().splitlines()[1] == "tax: CHF3.75"
    # the option is global: it comes before the command, and it works for every command
    done = run("--env-file", str(env_file), "items", "--file", "data/items.csv", "--tag", "tea")
    assert done.returncode == 0
    assert done.stdout.decode().splitlines()[2].endswith("GBP8.95")


def test_line_syntax_and_ignored_lines(tmp_path: Path) -> None:
    assert symbol_of(tmp_path, f"{KEY}=EUR") == "EUR"  # no newline at the end
    assert symbol_of(tmp_path, f"# c\n\n   \n\t# indented\n  export   {KEY} =\t EUR  \n") == "EUR"
    assert symbol_of(tmp_path, f"export\t{KEY}=EUR\n") == "EUR"
    assert symbol_of(tmp_path, f"{KEY}=AAA\n{KEY}=BBB\n") == "BBB"
    assert symbol_of(tmp_path, f"  {KEY}=EUR\r\n\r\n# x\r\nOTHER=1\r\n") == "EUR"
    # only STOCKROOM_ keys count, and they are case-sensitive; well-formed foreign lines are ignored
    foreign = (
        "PATH=/usr/bin\n"
        "stockroom_currency_symbol=low\n"
        f"exportSTOCKROOM_CURRENCY_SYMBOL=glued\n"
        "MY_STOCKROOM_CURRENCY_SYMBOL=mine\n"
        "_private = 1\n"
        "TAX_BP=999\n"
    )
    assert symbol_of(tmp_path, foreign) == "$"
    assert symbol_of(tmp_path, foreign + f"{KEY}=ok\n") == "ok"
    assert symbol_of(tmp_path, "") == "$"
    assert symbol_of(tmp_path, "# only comments\n") == "$"
    # an empty value sets an empty string
    assert symbol_of(tmp_path, f"{KEY}=\n") == ""
    assert symbol_of(tmp_path, f"{KEY}=   \n") == ""
    assert symbol_of(tmp_path, f"{KEY}=''\n") == ""
    assert symbol_of(tmp_path, f'{KEY}=""\n') == ""
    # the later empty value wins over an earlier one too
    assert symbol_of(tmp_path, f"{KEY}=EUR\n{KEY}=\n") == ""


def test_unquoted_values_and_comments(tmp_path: Path) -> None:
    assert symbol_of(tmp_path, f"{KEY}=CHF # the swiss one\n") == "CHF"
    assert symbol_of(tmp_path, f"{KEY}=CHF\t#tab comment\n") == "CHF"
    assert symbol_of(tmp_path, f"{KEY}=C#F\n") == "C#F"
    assert symbol_of(tmp_path, f"{KEY}=#\n") == "#"
    assert symbol_of(tmp_path, f"{KEY}=#hash first\n") == "#hash first"
    assert symbol_of(tmp_path, f"{KEY}= # nothing\n") == ""
    assert symbol_of(tmp_path, f"{KEY}=a=b=c\n") == "a=b=c"
    assert symbol_of(tmp_path, f"{KEY}=  padded value  \n") == "padded value"
    assert symbol_of(tmp_path, f"{KEY}=two  spaces\n") == "two  spaces"
    assert symbol_of(tmp_path, f"{KEY}=kr\\x\\n\n") == "kr\\x\\n"
    assert symbol_of(tmp_path, f"{KEY}=it's\n") == "it's"
    assert symbol_of(tmp_path, f'{KEY}=say "hi"\n') == 'say "hi"'
    assert symbol_of(tmp_path, f"{KEY}=€\n") == "€"
    assert symbol_of(tmp_path, f"{KEY}=¥ # yen\n") == "¥"
    assert symbol_of(tmp_path, f"{KEY}=a #b #c\n") == "a"
    assert symbol_of(tmp_path, f"{KEY}=a#b #c\n") == "a#b"


def test_quoted_values(tmp_path: Path) -> None:
    assert symbol_of(tmp_path, f'{KEY}="EUR "  # comment\n') == "EUR "
    assert symbol_of(tmp_path, f'{KEY}=  "  spaced  "\n') == "  spaced  "
    assert symbol_of(tmp_path, f"{KEY}='a # b'\n") == "a # b"
    assert symbol_of(tmp_path, f'{KEY}="a # b"\n') == "a # b"
    assert symbol_of(tmp_path, f'{KEY}="a \\"q\\" \\\\ b"\n') == 'a "q" \\ b'
    assert symbol_of(tmp_path, f'{KEY}="line1\\nline2"\n') == "line1\nline2"
    assert symbol_of(tmp_path, f'{KEY}="x\\ty\\x\\$"\n') == "x\\ty\\x\\$"
    assert symbol_of(tmp_path, f"{KEY}='raw \\n \\\\ \\\"'\n") == 'raw \\n \\\\ \\"'
    assert symbol_of(tmp_path, f"{KEY}='say \"hi\"'\n") == 'say "hi"'
    assert symbol_of(tmp_path, f"{KEY}=\"it's\"\n") == "it's"
    assert symbol_of(tmp_path, f'{KEY}="a"#c\n') == "a"
    assert symbol_of(tmp_path, f'{KEY}="a" \t # c # d\n') == "a"
    assert symbol_of(tmp_path, f"{KEY}='a'   \n") == "a"
    assert symbol_of(tmp_path, f'{KEY}="\\\\"\n') == "\\"
    assert symbol_of(tmp_path, f'{KEY}="trailing backslash\\\\"  # c\n') == "trailing backslash\\"
    assert symbol_of(tmp_path, f'{KEY}="€ and ¥"\n') == "€ and ¥"
    assert symbol_of(tmp_path, f'export {KEY}="quoted export"\n') == "quoted export"


def assert_line_error(done: subprocess.CompletedProcess[bytes], path: Path, line: int) -> None:
    assert done.returncode == 1, (done.returncode, done.stderr)
    assert done.stdout == b""
    lines = done.stderr.decode("utf-8").splitlines()
    assert len(lines) == 1, done.stderr
    prefix = f"stockroom: error: {path}: line {line}: "
    assert lines[0].startswith(prefix), (prefix, lines[0])
    assert len(lines[0]) > len(prefix)


def test_malformed_lines_report_the_line(tmp_path: Path) -> None:
    cases = [
        ("just some words", 3),
        ("1BAD=1", 3),
        ("BAD-KEY=1", 3),
        ("two words=1", 3),
        ("=novalue", 3),
        ("export", 3),
        (f'{KEY}="never closed', 3),
        (f"{KEY}='never closed", 3),
        (f'{KEY}="one line only\\"', 3),
        (f'{KEY}="a"b', 3),
        (f"{KEY}='a'b", 3),
        (f'{KEY}="a" "b"', 3),
        ('OTHER="not closed either', 3),
        ("OTHER=a\nthis line is bad", 4),
    ]
    for body, line in cases:
        path = write(tmp_path, f"# one\n{TAX}=10\n{body}\n{KEY}=EUR\n")
        done = run("--env-file", str(path), *PRICE)
        assert_line_error(done, path, line)
    unknown = write(tmp_path, f"\n\n{KEY}=EUR\nSTOCKROOM_COLOUR=blue\n")
    done = run("--env-file", str(unknown), *PRICE)
    assert_line_error(done, unknown, 4)
    assert "STOCKROOM_COLOUR" in done.stderr.decode()
    unknown = write(tmp_path, "STOCKROOM_tax_bp=5\n")
    assert_line_error(run("--env-file", str(unknown), *PRICE), unknown, 1)
    unknown = write(tmp_path, "STOCKROOM_=5\n")
    assert_line_error(run("--env-file", str(unknown), *PRICE), unknown, 1)
    # a well-formed line after the bad one is never reached, and stdout stays empty for any command
    bad = write(tmp_path, f"{KEY}=EUR\nbroken\n")
    done = run("--env-file", str(bad), "items", "--file", "data/items.csv")
    assert_line_error(done, bad, 2)


def test_rejected_values_and_unreadable_files(tmp_path: Path) -> None:
    for content in (f"{TAX}=lots\n", f"{TAX}=-5\n", f"{TAX}=\n", f'{TAX}="1 000"\n'):
        path = write(tmp_path, content)
        done = run("--env-file", str(path), *PRICE)
        assert done.returncode == 1, (content, done.stderr)
        assert done.stdout == b""
        lines = done.stderr.decode().splitlines()
        assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    # a value that is fine
    fine = price(write(tmp_path, f"{TAX}=0825\n{KEY}=$\n"))
    assert fine.returncode == 0 and fine.stdout.decode().splitlines()[1] == "tax: $3.09"

    missing = tmp_path / "missing.env"
    folder = tmp_path / "folder.env"
    folder.mkdir()
    binary = write(tmp_path, b"STOCKROOM_CURRENCY_SYMBOL=\xff\xfe\x80\n", "binary.env")
    for target in (missing, folder, binary):
        done = run("--env-file", str(target), *PRICE)
        assert done.returncode == 1, (target, done.stderr)
        assert done.stdout == b""
        lines = done.stderr.decode().splitlines()
        assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
        assert str(target) in lines[0]
        assert b"Traceback" not in done.stderr


def test_without_an_env_file_nothing_changes(tmp_path: Path) -> None:
    done = run(*PRICE)
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: $37.50",
        "tax: $0.00",
        "total: $37.50",
    ]
    ini = write(tmp_path, "[stockroom]\ncurrency_symbol = EUR\ntax_bp = 825\n", "s.ini")
    done = run("--config", str(ini), *PRICE)
    assert done.stdout.decode().splitlines() == [
        "3 x MUG-001 Blue Mug: EUR37.50",
        "tax: EUR3.09",
        "total: EUR40.59",
    ]
    done = run("--config", str(ini), *PRICE, env={TAX: "1000", "STOCKROOM_OTHER": "x"})
    assert done.stdout.decode().splitlines()[1] == "tax: EUR3.75"
    # an env file named on the command line does not leak into the next run
    env_file = write(tmp_path, f"{KEY}=ZZZ\n")
    assert run("--env-file", str(env_file), *PRICE).stdout.startswith(b"3 x MUG-001 Blue Mug: ZZZ")
    assert run(*PRICE).stdout.startswith(b"3 x MUG-001 Blue Mug: $37.50")
    assert run("--version").stdout == b"stockroom 0.4.0\n"
    assert run("--env-file").returncode == 2
