from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITEMS = str(ROOT / "data" / "items.csv")

ROWS = [
    ("MUG-001", "Blue Mug", "12.50", "kitchen;gift", "5"),
    ("MUG-002", "Red Mug", "12.50", "kitchen", "5"),
    ("TEA-010", "Green Tea 100g", "8.95", "food;tea", "10"),
    ("TEA-011", "Black Tea 100g", "7.40", "food;tea", "10"),
    ("PEN-100", "Fountain Pen", "34.00", "office;gift", "2"),
    ("NB-200", "Notebook A5", "6.25", "office", "20"),
    ("LMP-030", "Desk Lamp", "49.99", "office;home", "1"),
    ("CND-007", "Beeswax Candle", "9.80", "home;gift", "8"),
]
HEADER = ("sku", "name", "price", "tags", "reorder_level")


def table(rows: list[tuple[str, ...]], symbol: str = "$") -> list[str]:
    cells = [[r[0], r[1], symbol + r[2]] for r in rows]
    widths = [
        max([len(h)] + [len(c[i]) for c in cells]) for i, h in enumerate(("SKU", "Name", "Price"))
    ]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(("SKU", "Name", "Price"))).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for c in cells:
        lines.append(f"{c[0].ljust(widths[0])}  {c[1].ljust(widths[1])}  {c[2].rjust(widths[2])}")
    return lines


def as_text(lines: list[str], eol: str = "\n", bom: bool = False) -> bytes:
    return ("\ufeff" if bom else "").encode() + "".join(x + eol for x in lines).encode("utf-8")


def delimited(rows: list[tuple[str, ...]], sep: str) -> list[str]:
    return [sep.join(r) for r in [HEADER, *rows]]


TEXT = as_text(table(ROWS))
CSV = as_text(delimited(ROWS, ","))
TSV = as_text(delimited(ROWS, "\t"))


@dataclass
class Done:
    returncode: int
    stdout: bytes
    stderr: str


def run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> Done:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    done = subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        timeout=50,
        check=False,
    )
    return Done(done.returncode, done.stdout, done.stderr.decode("utf-8"))


def items(*args: str, **kwargs) -> Done:
    return run("items", "--file", ITEMS, *args, **kwargs)


def ok(done: Done) -> bytes:
    assert done.returncode == 0, (done.returncode, done.stderr)
    assert done.stderr == ""
    return done.stdout


def assert_error(done: Done, *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == b""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def listing(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def test_csv_and_tsv_listings() -> None:
    assert ok(items("--format", "csv")) == CSV
    assert ok(items("--format", "tsv")) == TSV
    assert ok(items("--format", "text")) == TEXT
    gift = [r for r in ROWS if "gift" in r[3].split(";")]
    assert ok(items("--format", "csv", "--tag", "GIFT")) == as_text(delimited(gift, ","))
    assert ok(items("--tag", "gift", "--format", "tsv")) == as_text(delimited(gift, "\t"))
    assert ok(items("--format", "csv", "--tag", "none")) == as_text([",".join(HEADER)])
    # the currency symbol belongs to the text table only
    env = {"STOCKROOM_CURRENCY_SYMBOL": "EUR "}
    assert ok(items("--format", "csv", env=env)) == CSV
    assert ok(items("--format", "tsv", env=env)) == TSV
    assert ok(items("--format", "text", env=env)) == as_text(table(ROWS, "EUR "))


def test_format_precedence_matrix(tmp_path: Path) -> None:
    by_name = {"text": TEXT, "csv": CSV, "tsv": TSV}
    tsv_ini = tmp_path / "tsv.ini"
    tsv_ini.write_text("[stockroom]\nitems_format = tsv\n", encoding="utf-8")
    settings = {
        "none": ([], {}, None),
        "env-csv": ([], {"STOCKROOM_ITEMS_FORMAT": "csv"}, "csv"),
        "ini-tsv": (["--config", str(tsv_ini)], {}, "tsv"),
        "ini-tsv-env-text": (
            ["--config", str(tsv_ini)],
            {"STOCKROOM_ITEMS_FORMAT": "text"},
            "text",
        ),
    }
    outs = {"o.tsv": "tsv", "o.CSV": "csv", "o.dat": None, "-": None, "noext": None}
    n = 0
    for option in (None, "csv", "text"):
        for out, inferred in outs.items():
            for name, (args, env, from_setting) in settings.items():
                want = option or inferred or from_setting or "text"
                folder = tmp_path / f"m{n}"
                folder.mkdir()
                n += 1
                cmd = [*args, "items", "--file", ITEMS, "--out", out]
                if option:
                    cmd += ["--format", option]
                done = run(*cmd, env=env, cwd=folder)
                assert done.returncode == 0, (option, out, name, done.stderr)
                got = done.stdout if out == "-" else (folder / out).read_bytes()
                assert got == by_name[want], (option, out, name)


def test_suffix_inference_follows_pathlib(tmp_path: Path) -> None:
    cases = {
        "a.csv": CSV,
        "b.Tsv": TSV,
        "c.TXT": TEXT,
        "d.csv.bak": TSV,
        "e.tsv.txt": TEXT,
        ".csv": TSV,
        "f.": TSV,
        "g.CsV": CSV,
        "h.json": TSV,
    }
    env = {"STOCKROOM_ITEMS_FORMAT": "tsv"}
    for name, want in cases.items():
        done = run("items", "--file", ITEMS, "--out", name, env=env, cwd=tmp_path)
        assert done.returncode == 0 and done.stdout == b"", (name, done.stderr)
        assert (tmp_path / name).read_bytes() == want, name
    sub = tmp_path / "x.tsv"
    sub.mkdir()
    done = run("items", "--file", ITEMS, "--out", "x.tsv/plain", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert (sub / "plain").read_bytes() == TEXT


def test_line_ends_and_byte_order_mark(tmp_path: Path) -> None:
    assert ok(items("--newline", "crlf")) == as_text(table(ROWS), "\r\n")
    assert ok(items("--format", "csv", "--newline", "crlf")) == as_text(
        delimited(ROWS, ","), "\r\n"
    )
    assert ok(items("--format", "tsv", "--bom")) == as_text(delimited(ROWS, "\t"), bom=True)
    assert ok(items("--bom", "--newline", "crlf", "--format", "text")) == as_text(
        table(ROWS), "\r\n", bom=True
    )
    assert ok(items("--newline", "lf")) == TEXT
    out = tmp_path / "o.csv"
    done = items("--out", str(out), "--bom", "--newline", "crlf")
    assert done.returncode == 0 and done.stdout == b""
    assert out.read_bytes() == as_text(delimited(ROWS, ","), "\r\n", bom=True)
    assert out.read_bytes().startswith(b"\xef\xbb\xbfsku,name,")
    # non-ASCII names are written as UTF-8
    catalogue = tmp_path / "uni.csv"
    catalogue.write_text(
        "sku,name,price,tags,reorder_level\nCAF-001,Café Ünal,3.10,,0\n", encoding="utf-8"
    )
    done = run("items", "--file", str(catalogue), "--format", "tsv")
    assert (
        ok(done)
        == "sku\tname\tprice\ttags\treorder_level\nCAF-001\tCafé Ünal\t3.10\t\t0\n".encode()
    )


def test_out_needs_force_to_replace_and_is_atomic(tmp_path: Path) -> None:
    out = tmp_path / "list.csv"
    done = items("--out", str(out))
    assert done.returncode == 0 and done.stdout == b"" and done.stderr == ""
    assert out.read_bytes() == CSV
    out.write_bytes(b"old content " * 1000)
    assert_error(items("--out", str(out)), str(out))
    assert_error(items("--out", str(out), "--format", "tsv"), str(out))
    assert out.read_bytes() == b"old content " * 1000
    done = items("--out", str(out), "--force", "--tag", "tea")
    assert done.returncode == 0 and done.stdout == b"", done.stderr
    assert out.read_bytes() == as_text(delimited([r for r in ROWS if "tea" in r[3]], ","))
    assert listing(tmp_path) == ["list.csv"]
    # a relative path is relative to the current directory; '-' is stdout even with --force
    done = run("items", "--file", ITEMS, "--out", "rel.tsv", cwd=tmp_path)
    assert done.returncode == 0 and (tmp_path / "rel.tsv").read_bytes() == TSV
    assert ok(items("--out", "-", "--force", "--format", "csv")) == CSV
    assert listing(tmp_path) == ["list.csv", "rel.tsv"]


def test_unsafe_outputs_are_refused(tmp_path: Path) -> None:
    catalogue = tmp_path / "items.csv"
    shutil.copyfile(ITEMS, catalogue)
    before = catalogue.read_bytes()
    for out in ("items.csv", "./items.csv", str(catalogue)):
        for force in ([], ["--force"]):
            done = run("items", "--file", "items.csv", "--out", out, *force, cwd=tmp_path)
            assert_error(done, out)
    assert catalogue.read_bytes() == before
    folder = tmp_path / "folder"
    folder.mkdir()
    assert_error(items("--out", str(folder), "--force"), str(folder))
    missing = tmp_path / "no" / "such.csv"
    assert_error(items("--out", str(missing)), str(missing))
    assert listing(tmp_path) == ["folder", "items.csv"]
    assert listing(folder) == []


def test_settings_and_usage_errors(tmp_path: Path) -> None:
    for value in ("json", "CSV", "Text"):
        assert_error(items(env={"STOCKROOM_ITEMS_FORMAT": value}), "items_format")
    bad = tmp_path / "bad.ini"
    bad.write_text("[stockroom]\nitems_format = xml\n", encoding="utf-8")
    assert_error(run("--config", str(bad), "items", "--file", ITEMS), "items_format")
    good = tmp_path / "good.ini"
    good.write_text("[stockroom]\nitems_format = csv\n", encoding="utf-8")
    assert ok(run("--config", str(good), "items", "--file", ITEMS)) == CSV
    # the setting is accepted by every command, which ignore it
    done = run(
        "--config", str(good), "price", "--items", ITEMS, "--sku", "MUG-001", "--quantity", "1"
    )
    assert (
        done.returncode == 0
        and done.stdout.decode().splitlines()[0] == "1 x MUG-001 Blue Mug: $12.50"
    )
    for args in (
        ["--format", "json"],
        ["--format", "CSV"],
        ["--newline", "cr"],
        ["--newline", "CRLF"],
    ):
        done = items(*args)
        assert done.returncode == 2 and done.stdout == b"", args


def test_default_listing_is_unchanged() -> None:
    assert ok(items()) == TEXT
    assert ok(items("--tag", "tea")) == as_text(table([r for r in ROWS if "tea" in r[3]]))
    lines = ok(items()).decode().splitlines()
    assert lines[0].split() == ["SKU", "Name", "Price"] and lines[2].endswith("$12.50")
    assert run("items").returncode == 2
