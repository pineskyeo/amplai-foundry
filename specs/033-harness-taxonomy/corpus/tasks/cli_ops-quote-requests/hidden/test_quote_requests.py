from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITEMS = str(ROOT / "data" / "items.csv")

REQUESTS = "# morning order\nMUG-001 2\n\nTEA-010   1\n  PEN-100\t3  \n"
REQUESTS_OUT = [
    "2: 2 x MUG-001 Blue Mug = $25.00",
    "4: 1 x TEA-010 Green Tea 100g = $8.95",
    "5: 3 x PEN-100 Fountain Pen = $102.00",
    "subtotal: $135.95",
    "tax: $0.00",
    "total: $135.95",
]
MIXED = (
    "MUG-001 2\n"
    "mug-001 1\n"
    "TEA-010 0\n"
    "TEA-010 two\n"
    "NB-200 1 extra\n"
    "PEN-100\n"
    "NB-200 +2\n"
    "NB-200 \u0663\n"
    "NB-200 3.0\n"
    "NB-200 004\n"
)
MIXED_OUT = [
    "1: 2 x MUG-001 Blue Mug = $25.00",
    "10: 4 x NB-200 Notebook A5 = $25.00",
    "subtotal: $50.00",
    "tax: $0.00",
    "total: $50.00",
]
ZERO = ["subtotal: $0.00", "tax: $0.00", "total: $0.00"]


@dataclass
class Done:
    returncode: int
    stdout: str
    stderr: str


def run(
    *args: str,
    stdin: bytes = b"",
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Done:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    done = subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        input=stdin,
        capture_output=True,
        timeout=50,
        check=False,
    )
    return Done(done.returncode, done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def quote(*args: str, **kwargs) -> Done:
    return run("quote", "--items", ITEMS, *args, **kwargs)


def put(folder: Path, name: str, text: str | bytes) -> Path:
    path = folder / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def ok(done: Done) -> list[str]:
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert done.stderr == ""
    return done.stdout.splitlines()


def assert_fatal(done: Done, *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_each_request_line_is_priced_and_totalled(tmp_path: Path) -> None:
    requests = put(tmp_path, "r.txt", REQUESTS)
    assert ok(quote(str(requests))) == REQUESTS_OUT
    assert quote(str(requests)).stdout == "".join(line + "\n" for line in REQUESTS_OUT)
    # tax is computed once, on the subtotal of all lines, half up
    two = put(tmp_path, "two.txt", "MUG-001 1\nTEA-010 1\n")
    lines = ok(quote(str(two), env={"STOCKROOM_TAX_BP": "50"}))
    assert lines[2:] == ["subtotal: $21.45", "tax: $0.11", "total: $21.56"]
    # the currency symbol comes from the settings
    lines = ok(quote(str(two), env={"STOCKROOM_CURRENCY_SYMBOL": "EUR "}))
    assert lines == [
        "1: 1 x MUG-001 Blue Mug = EUR 12.50",
        "2: 1 x TEA-010 Green Tea 100g = EUR 8.95",
        "subtotal: EUR 21.45",
        "tax: EUR 0.00",
        "total: EUR 21.45",
    ]
    # repeated SKUs stay separate lines; line numbers count every physical line
    rep = put(tmp_path, "rep.txt", "\n\n# x\nNB-200 1\n   # y\nNB-200 1\n")
    assert ok(quote(str(rep))) == [
        "4: 1 x NB-200 Notebook A5 = $6.25",
        "6: 1 x NB-200 Notebook A5 = $6.25",
        "subtotal: $12.50",
        "tax: $0.00",
        "total: $12.50",
    ]


def test_requests_come_from_standard_input_by_default(tmp_path: Path) -> None:
    assert ok(quote(stdin=REQUESTS.encode("utf-8"))) == REQUESTS_OUT
    assert ok(quote("-", stdin=REQUESTS.encode("utf-8"))) == REQUESTS_OUT
    crlf = b"MUG-001 1\r\nTEA-010 2\r\n"
    assert ok(quote(stdin=crlf)) == [
        "1: 1 x MUG-001 Blue Mug = $12.50",
        "2: 2 x TEA-010 Green Tea 100g = $17.90",
        "subtotal: $30.40",
        "tax: $0.00",
        "total: $30.40",
    ]
    assert ok(quote(str(put(tmp_path, "crlf.txt", crlf)))) == ok(quote(stdin=crlf))
    # an empty request list, or one with comments only, is a zero quote
    assert ok(quote(stdin=b"")) == ZERO
    assert ok(quote(stdin=b"# nothing\n\n   \n")) == ZERO
    # only the exact value '-' is standard input
    put(tmp_path, "-", "PEN-100 1\n")
    done = run("quote", "--items", ITEMS, "./-", stdin=b"MUG-001 1\n", cwd=tmp_path)
    assert ok(done)[0] == "1: 1 x PEN-100 Fountain Pen = $34.00"


def test_partial_failures_print_the_good_lines_and_exit_3(tmp_path: Path) -> None:
    done = quote(stdin=MIXED.encode("utf-8"))
    assert done.returncode == 3, done.stderr
    assert done.stdout.splitlines() == MIXED_OUT
    errors = done.stderr.splitlines()
    assert len(errors) == 8, done.stderr
    for number, line in zip(range(2, 10), errors, strict=True):
        assert line.startswith(f"stockroom: error: line {number}: "), line
    assert "mug-001" in errors[0]
    assert "Traceback" not in done.stderr


def test_a_quote_without_a_good_line_fails(tmp_path: Path) -> None:
    done = quote(stdin=b"XYZ-001 1\n\nMUG-001 0\n")
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.splitlines()[0].startswith("stockroom: error: line 1: ")
    assert "XYZ-001" in done.stderr.splitlines()[0]
    assert done.stderr.splitlines()[1].startswith("stockroom: error: line 3: ")
    assert len(done.stderr.splitlines()) == 2


def test_tax_rate_precedence_matrix(tmp_path: Path) -> None:
    two = put(tmp_path, "two.txt", "MUG-001 1\nTEA-010 1\n")
    settings = put(tmp_path, "s.ini", "[stockroom]\ntax_bp = 100\n")

    def tax_of(bp: int) -> int:
        return (2145 * bp + 5000) // 10000

    for use_file in (False, True):
        for env in (None, "200"):
            for option in (None, "0", "300"):
                if option is not None:
                    want = int(option)
                elif env is not None:
                    want = int(env)
                else:
                    want = 100 if use_file else 0
                args = ["--config", str(settings)] if use_file else []
                args += ["quote", "--items", ITEMS, str(two)]
                if option is not None:
                    args += ["--tax-bp", option]
                done = run(*args, env={"STOCKROOM_TAX_BP": env} if env else None)
                cents = tax_of(want)
                assert done.returncode == 0, done.stderr
                assert done.stdout.splitlines()[3] == f"tax: ${cents // 100}.{cents % 100:02d}", (
                    use_file,
                    env,
                    option,
                )


def test_out_writes_the_quote_atomically(tmp_path: Path) -> None:
    requests = put(tmp_path, "r.txt", REQUESTS)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    done = run("quote", "--items", ITEMS, str(requests), "--out", "q.txt", cwd=out_dir)
    assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    assert (out_dir / "q.txt").read_bytes() == ("\n".join(REQUESTS_OUT) + "\n").encode("utf-8")
    (out_dir / "q.txt").write_text("x" * 50000, encoding="utf-8")
    done = quote("--out", str(out_dir / "q.txt"), stdin=b"PEN-100 1\n")
    assert done.returncode == 0 and done.stdout == ""
    assert (out_dir / "q.txt").read_text(encoding="utf-8").splitlines()[0] == (
        "1: 1 x PEN-100 Fountain Pen = $34.00"
    )
    assert listing(out_dir) == ["q.txt"]
    # '-' is standard output
    assert ok(quote("--out", "-", str(requests))) == REQUESTS_OUT
    # a partial quote is written, the errors still go to stderr
    done = quote("--out", str(out_dir / "partial.txt"), stdin=MIXED.encode("utf-8"))
    assert done.returncode == 3 and done.stdout == ""
    assert len(done.stderr.splitlines()) == 8
    assert (out_dir / "partial.txt").read_text(encoding="utf-8").splitlines() == MIXED_OUT
    # a failed quote writes nothing
    done = quote("--out", str(out_dir / "q.txt"), stdin=b"XYZ-001 1\n")
    assert done.returncode == 1 and done.stdout == ""
    assert (out_dir / "q.txt").read_text(encoding="utf-8").startswith("1: 1 x PEN-100")
    done = quote("--out", str(out_dir / "never.txt"), stdin=b"XYZ-001 1\n")
    assert done.returncode == 1
    assert listing(out_dir) == ["partial.txt", "q.txt"]
    # an output path that cannot be written is reported before anything is read
    missing = out_dir / "no" / "q.txt"
    assert_fatal(quote("--out", str(missing), stdin=MIXED.encode("utf-8")), str(missing))
    assert_fatal(quote("--out", str(out_dir), stdin=b"MUG-001 1\n"), str(out_dir))
    assert listing(out_dir) == ["partial.txt", "q.txt"]


def test_the_output_never_replaces_an_input(tmp_path: Path) -> None:
    requests = put(tmp_path, "r.txt", REQUESTS)
    catalogue = tmp_path / "items.csv"
    shutil.copyfile(ITEMS, catalogue)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    for out in ("r.txt", "./r.txt", str(requests), "sub/../r.txt"):
        (tmp_path / "sub").mkdir(exist_ok=True)
        done = run("quote", "--items", str(catalogue), "r.txt", "--out", out, cwd=tmp_path)
        assert_fatal(done, out)
    for out in ("items.csv", str(catalogue)):
        done = run(
            "quote", "--items", "items.csv", "--out", out, stdin=b"MUG-001 1\n", cwd=tmp_path
        )
        assert_fatal(done, out)
    (tmp_path / "sub").rmdir()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_usage_errors_and_unreadable_inputs(tmp_path: Path) -> None:
    for value in ("-1", "abc", "", "1.5", "\u0663"):
        done = quote("--tax-bp", value, stdin=b"MUG-001 1\n")
        assert done.returncode == 2, (value, done.returncode)
        assert done.stdout == ""
    assert run("quote", stdin=b"MUG-001 1\n").returncode == 2
    assert quote("a.txt", "b.txt").returncode == 2
    missing = tmp_path / "missing.txt"
    assert_fatal(quote(str(missing)), str(missing))
    assert_fatal(quote(str(tmp_path)), str(tmp_path))
    assert_fatal(quote(str(put(tmp_path, "latin.txt", b"MUG-001 1 \xff\n"))), "latin.txt")
    assert_fatal(quote(stdin=b"MUG-001 1\n\xff\n"), "standard input")
    no_items = tmp_path / "no-items.csv"
    assert_fatal(run("quote", "--items", str(no_items), stdin=b"MUG-001 1\n"), str(no_items))
    bad_items = put(tmp_path, "bad-items.csv", "sku,price\nMUG-001,1.00\n")
    assert_fatal(run("quote", "--items", str(bad_items), stdin=b"MUG-001 1\n"))
    # other commands are unchanged
    done = run("price", "--items", ITEMS, "--sku", "MUG-001", "--quantity", "3")
    assert ok(done) == ["3 x MUG-001 Blue Mug: $37.50", "tax: $0.00", "total: $37.50"]
