from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
ITEMS = shlex.quote(str(DATA / "items.csv"))
STOCK = shlex.quote(str(DATA / "stock.csv"))
ORDERS = shlex.quote(str(DATA / "orders.csv"))

OK_ITEMS = f"items --file {ITEMS} --tag tea"
OK_PRICE = f"price --items {ITEMS} --sku MUG-001 --quantity 3"
OK_STOCK = f"stock --items {ITEMS} --stock {STOCK}"
OK_SALES = f"sales --orders {ORDERS} --from 2024-02-01"
DATA_ERROR = f"price --items {ITEMS} --sku XYZ-999 --quantity 1"
USAGE_ERROR = "frobnicate --now"


def run(
    args: list[str], env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        timeout=50,
        check=False,
    )


def solo(line: str, *, config: Path | None = None, env: dict[str, str] | None = None):
    argv = shlex.split(line)
    if config is not None:
        argv = ["--config", str(config), *argv]
    return run(argv, env=env)


def expected(
    lines: list[str],
    *,
    config: Path | None = None,
    env: dict[str, str] | None = None,
    own_config: frozenset[int] = frozenset(),
) -> tuple[bytes, bytes, list[int]]:
    """Header + stdout per line, all stderr, and the status of each line, from single runs."""
    out, err, codes = b"", b"", []
    for index, line in enumerate(lines):
        use = None if index in own_config else config
        done = solo(line, config=use, env=env)
        out += b"$ " + line.strip().encode() + b"\n" + done.stdout
        err += done.stderr
        codes.append(done.returncode)
    return out, err, codes


def write_script(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_lines_run_in_order_with_headers(tmp_path: Path) -> None:
    lines = [OK_ITEMS, OK_PRICE, OK_STOCK, OK_SALES, "--version"]
    script = write_script(
        tmp_path / "all.txt",
        [
            "# nightly report",
            "",
            f"  {OK_ITEMS}   ",
            "   # an indented comment",
            OK_PRICE,
            "\t",
            f"\t{OK_STOCK}",
            OK_SALES,
            "--version",
            "# done",
        ],
    )
    out, err, codes = expected(lines)
    assert codes == [0, 0, 0, 0, 0]
    assert b"warning: 3 item(s)" in err
    done = run(["batch", str(script)])
    assert done.returncode == 0, done.stderr
    assert done.stdout == out
    assert done.stderr == err
    assert done.stdout.startswith(b"$ items --file ")
    assert b"$ --version\nstockroom 0.4.0\n" in done.stdout


def test_crlf_script_and_shell_quoting(tmp_path: Path) -> None:
    quoted = tmp_path / "my items.csv"
    quoted.write_bytes((DATA / "items.csv").read_bytes())
    lines = [
        f"items --file '{quoted}' --tag \"tea\"",
        f"items --file {shlex.quote(str(quoted))} --tag=home",
        "price --items " + shlex.quote(str(quoted)) + " --sku TEA-010 --quantity 2",
    ]
    script = tmp_path / "crlf.txt"
    script.write_bytes(("# comment\r\n" + "\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
    out, err, codes = expected(lines)
    assert codes == [0, 0, 0]
    done = run(["batch", str(script)])
    assert done.returncode == 0, done.stderr
    assert done.stdout == out and done.stderr == err
    assert b"\r" not in done.stdout
    assert done.stdout.count(b"$ ") == 3


def test_stops_at_the_first_failing_line(tmp_path: Path) -> None:
    lines = [OK_PRICE, DATA_ERROR, OK_ITEMS, USAGE_ERROR]
    script = write_script(tmp_path / "s.txt", lines)
    out, err, codes = expected(lines[:2])
    assert codes == [0, 1]
    done = run(["batch", str(script)])
    assert done.returncode == 1
    assert done.stdout == out and done.stderr == err
    assert b"--tag tea" not in done.stdout and b"frobnicate" not in done.stdout
    assert done.stderr.count(b"stockroom: error: ") == 1

    lines = [OK_PRICE, USAGE_ERROR, DATA_ERROR, OK_ITEMS]
    script = write_script(tmp_path / "u.txt", lines)
    out, err, codes = expected(lines[:2])
    assert codes == [0, 2]
    done = run(["batch", str(script)])
    assert done.returncode == 2
    assert done.stdout == out and done.stderr == err
    assert b"XYZ-999" not in done.stdout

    # a first line that fails ends everything; the exit code is that line's status
    script = write_script(tmp_path / "first.txt", [DATA_ERROR, OK_PRICE])
    done = run(["batch", str(script)])
    assert done.returncode == 1
    assert done.stdout == b"$ " + DATA_ERROR.encode() + b"\n"


def test_keep_going_runs_every_line(tmp_path: Path) -> None:
    lines = [OK_PRICE, USAGE_ERROR, DATA_ERROR, OK_ITEMS, f"price --items {ITEMS} --sku NB-200"]
    script = write_script(tmp_path / "s.txt", lines)
    out, err, codes = expected(lines)
    assert codes == [0, 2, 1, 0, 2]
    for argv in (
        ["batch", str(script), "--keep-going"],
        ["batch", "--keep-going", str(script)],
    ):
        done = run(argv)
        assert done.returncode == 2, (argv, done.stderr)
        assert done.stdout == out and done.stderr == err
        assert done.stdout.count(b"$ ") == 5

    lines = [DATA_ERROR, USAGE_ERROR, OK_ITEMS]
    script = write_script(tmp_path / "t.txt", lines)
    out, err, codes = expected(lines)
    assert codes == [1, 2, 0]
    done = run(["batch", str(script), "--keep-going"])
    assert done.returncode == 1
    assert done.stdout == out and done.stderr == err

    # all lines succeed: exit code 0 with or without the option
    script = write_script(tmp_path / "good.txt", [OK_PRICE, OK_ITEMS])
    assert run(["batch", str(script), "--keep-going"]).returncode == 0
    assert run(["batch", str(script)]).returncode == 0


def test_the_batch_config_is_the_default_for_each_line(tmp_path: Path) -> None:
    outer = tmp_path / "outer.ini"
    outer.write_text("[stockroom]\ncurrency_symbol = EUR\ntax_bp = 1000\n", encoding="utf-8")
    other = tmp_path / "other.ini"
    other.write_text("[stockroom]\ncurrency_symbol = GBP\n", encoding="utf-8")
    missing = tmp_path / "missing.ini"
    lines = [
        OK_ITEMS,
        f"--config={other} {OK_ITEMS}",
        f"--config {other} {OK_PRICE}",
        f"--conf {other} {OK_ITEMS}",
        OK_PRICE,
        f"--config {missing} {OK_ITEMS}",
        OK_PRICE,
    ]
    own = frozenset({1, 2, 3, 5})
    out, err, codes = expected(lines, config=outer, own_config=own)
    assert codes == [0, 0, 0, 0, 0, 1, 0]
    assert b"EUR" in out and b"GBP" in out and b"tax: EUR3.75" in out
    assert str(missing).encode() in err
    script = write_script(tmp_path / "s.txt", lines)
    done = run(["--config", str(outer), "batch", str(script), "--keep-going"])
    assert done.returncode == 1, done.stderr
    assert done.stdout == out and done.stderr == err
    # without --keep-going the failing line (index 5) ends the batch
    out5, err5, _ = expected(lines[:6], config=outer, own_config=own)
    done = run(["--config", str(outer), "batch", str(script)])
    assert done.returncode == 1 and done.stdout == out5 and done.stderr == err5
    # no batch-level config: the lines without their own file use the defaults
    plain, plain_err, plain_codes = expected(lines)
    done = run(["batch", str(script), "--keep-going"])
    assert done.returncode == 1 and done.stdout == plain and done.stderr == plain_err
    assert b"EUR" not in done.stdout

    # an invalid settings file given to the batch ends the command before any line runs
    done = run(["--config", str(missing), "batch", str(script)])
    assert done.returncode == 1 and done.stdout == b""
    assert len(done.stderr.decode().splitlines()) == 1
    assert done.stderr.startswith(b"stockroom: error: ")


def test_environment_settings_apply_to_every_line(tmp_path: Path) -> None:
    env = {"STOCKROOM_TAX_BP": "1000", "STOCKROOM_CURRENCY_SYMBOL": "CHF"}
    ini = tmp_path / "file.ini"
    ini.write_text("[stockroom]\ncurrency_symbol = GBP\ntax_bp = 0\n", encoding="utf-8")
    lines = [OK_PRICE, f"--config {ini} {OK_PRICE}", OK_ITEMS]
    out, err, codes = expected(lines, env=env)
    assert codes == [0, 0, 0]
    assert out.count(b"tax: CHF3.75") == 2
    script = write_script(tmp_path / "s.txt", lines)
    done = run(["batch", str(script)], env=env)
    assert done.returncode == 0
    assert done.stdout == out and done.stderr == err


def test_unbalanced_quote_is_a_line_error(tmp_path: Path) -> None:
    bad = f"items --file '{DATA / 'items.csv'}"
    lines = ["# first", OK_PRICE, "", bad, OK_ITEMS]
    script = write_script(tmp_path / "s.txt", lines)
    done = run(["batch", str(script), "--keep-going"])
    assert done.returncode == 1
    out = (
        b"$ "
        + OK_PRICE.encode()
        + b"\n"
        + solo(OK_PRICE).stdout
        + b"$ "
        + bad.encode()
        + b"\n$ "
        + OK_ITEMS.encode()
        + b"\n"
        + solo(OK_ITEMS).stdout
    )
    assert done.stdout == out
    err_lines = done.stderr.decode().splitlines()
    assert len(err_lines) == 1
    assert err_lines[0].startswith("stockroom: error: line 4: ")
    assert len(err_lines[0]) > len("stockroom: error: line 4: ")
    # without --keep-going it stops there, with status 1
    done = run(["batch", str(script)])
    assert done.returncode == 1
    assert done.stdout.endswith(b"$ " + bad.encode() + b"\n")
    assert b"--tag tea" not in done.stdout


def test_nested_batch_is_refused(tmp_path: Path) -> None:
    inner = write_script(tmp_path / "inner.txt", [OK_PRICE, OK_ITEMS])
    ini = tmp_path / "s.ini"
    ini.write_text("[stockroom]\n", encoding="utf-8")
    for nested in (f"batch {inner}", f"--config {ini} batch {inner} --keep-going"):
        script = write_script(tmp_path / "outer.txt", [OK_ITEMS, nested, OK_PRICE])
        done = run(["batch", str(script), "--keep-going"])
        assert done.returncode == 1
        assert done.stdout == (
            b"$ "
            + OK_ITEMS.encode()
            + b"\n"
            + solo(OK_ITEMS).stdout
            + b"$ "
            + nested.encode()
            + b"\n$ "
            + OK_PRICE.encode()
            + b"\n"
            + solo(OK_PRICE).stdout
        )
        err_lines = done.stderr.decode().splitlines()
        assert len(err_lines) == 1
        assert err_lines[0].startswith("stockroom: error: ") and "nested" in err_lines[0]
        done = run(["batch", str(script)])
        assert done.returncode == 1 and done.stdout.endswith(b"\n$ " + nested.encode() + b"\n")


def test_unreadable_script(tmp_path: Path) -> None:
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\xff\xfe\x80 items\n" + OK_PRICE.encode() + b"\n")
    folder = tmp_path / "folder"
    folder.mkdir()
    for target in (tmp_path / "missing.txt", folder, binary):
        for extra in ([], ["--keep-going"]):
            done = run(["batch", str(target), *extra])
            assert done.returncode == 1, (target, done.stderr)
            assert done.stdout == b""
            lines = done.stderr.decode().splitlines()
            assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
            assert str(target) in lines[0]
            assert b"Traceback" not in done.stderr


def test_script_without_commands(tmp_path: Path) -> None:
    for content in ("", "\n\n", "# only\n   # comments\n\n  \t \n"):
        script = tmp_path / "empty.txt"
        script.write_text(content, encoding="utf-8")
        for extra in ([], ["--keep-going"]):
            done = run(["batch", str(script), *extra])
            assert done.returncode == 0, done.stderr
            assert done.stdout == b"" and done.stderr == b""
    assert run(["batch"]).returncode == 2
