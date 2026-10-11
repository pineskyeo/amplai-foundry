from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ITEM_HEADER = "sku,name,price,tags,reorder_level\n"
ORDER_HEADER = "order_id,customer,placed,status,sku,quantity,unit_price\n"


def run(
    *args: str, env: dict[str, str] | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd or ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


def data_set(folder: Path, prefix: str, cents: int = 100) -> Path:
    """A data directory whose files name their origin: SKU <PREFIX>-001, customer <prefix>."""
    folder.mkdir(parents=True, exist_ok=True)
    sku = f"{prefix.upper()[:4]}-001"
    price = f"{cents // 100}.{cents % 100:02d}"
    (folder / "items.csv").write_text(
        ITEM_HEADER + f"{sku},{prefix} item,{price},,5\n", encoding="utf-8"
    )
    (folder / "stock.csv").write_text(f"sku,on_hand,reserved\n{sku},1,0\n", encoding="utf-8")
    (folder / "orders.csv").write_text(
        ORDER_HEADER + f"O-1,{prefix},2024-01-02,paid,{sku},1,{price}\n", encoding="utf-8"
    )
    return folder


def listed_sku(done: subprocess.CompletedProcess[str]) -> str:
    assert done.returncode == 0, (done.returncode, done.stderr)
    return done.stdout.splitlines()[2].split()[0]


def assert_error(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 1, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith("stockroom: error: "), done.stderr
    for needle in needles:
        assert needle in lines[0], (needle, lines[0])


def assert_usage(done: subprocess.CompletedProcess[str], *needles: str) -> None:
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert done.stdout == ""
    for needle in needles:
        assert needle in done.stderr, (needle, done.stderr)


def ini(path: Path, data_dir: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "" if data_dir is None else f"data_dir = {data_dir}\n"
    path.write_text("[stockroom]\n" + body, encoding="utf-8")
    return path


def test_data_dir_option_supplies_every_default_file(tmp_path: Path) -> None:
    data_set(tmp_path / "shop", "opt", 250)
    assert listed_sku(run("--data-dir", "shop", "items", cwd=tmp_path)) == "OPT-001"
    done = run("--data-dir", str(tmp_path / "shop"), "stock")
    assert done.returncode == 0, done.stderr
    assert done.stdout == "LOW OPT-001 opt item: 1/5\n"
    done = run("--data-dir", "shop", "sales", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[:2] == ["1 order, 0 cancelled", "Revenue: $2.50"]
    done = run("--data-dir", "shop/", "price", "--sku", "OPT-001", "--quantity", "2", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == "2 x OPT-001 opt item: $5.00"


def test_explicit_files_stay_relative_to_the_current_directory(tmp_path: Path) -> None:
    data_set(tmp_path / "shop", "opt")
    data_set(tmp_path, "cwd")
    done = run("--data-dir", "shop", "items", "--file", "items.csv", cwd=tmp_path)
    assert listed_sku(done) == "CWD-001"
    # one explicit file and one default file in the same command
    (tmp_path / "shop" / "stock.csv").write_text(
        "sku,on_hand,reserved\nCWD-001,9,0\n", encoding="utf-8"
    )
    done = run("--data-dir", "shop", "stock", "--items", "items.csv", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "stock ok\n"
    done = run("--data-dir", "shop", "stock", "--stock", "stock.csv", cwd=tmp_path)
    assert done.stdout == "LOW OPT-001 opt item: 0/5\n"
    # with every input given, the data directory is not even looked at
    done = run("--data-dir", "nowhere", "items", "--file", "items.csv", cwd=tmp_path)
    assert listed_sku(done) == "CWD-001"
    env = {"STOCKROOM_DATA_DIR": "nowhere"}
    done = run("sales", "--orders", "orders.csv", env=env, cwd=tmp_path)
    assert done.returncode == 0, done.stderr


def test_environment_data_dir(tmp_path: Path) -> None:
    data_set(tmp_path / "envdir", "env")
    done = run("items", env={"STOCKROOM_DATA_DIR": "envdir"}, cwd=tmp_path)
    assert listed_sku(done) == "ENV-001"
    done = run("items", env={"STOCKROOM_DATA_DIR": str(tmp_path / "envdir")})
    assert listed_sku(done) == "ENV-001"
    # an empty variable configures nothing
    assert_usage(run("items", env={"STOCKROOM_DATA_DIR": ""}, cwd=tmp_path), "--file")


def test_settings_file_data_dir_is_relative_to_the_settings_file(tmp_path: Path) -> None:
    data_set(tmp_path / "ini", "ini")
    data_set(tmp_path / "conf" / "ini", "wrong")
    work = tmp_path / "work"
    data_set(work / "ini", "cwd")
    settings = ini(tmp_path / "conf" / "s.ini", "../ini")
    done = run("--config", "../conf/s.ini", "items", cwd=work)
    assert listed_sku(done) == "INI-001"
    done = run("--config", str(settings), "items", cwd=work)
    assert listed_sku(done) == "INI-001"
    # an absolute value is used as it is
    ini(settings, str(tmp_path / "conf" / "ini"))
    assert listed_sku(run("--config", str(settings), "items", cwd=work)) == "WRON-001"
    # a value right next to the settings file
    ini(settings, "ini")
    assert listed_sku(run("--config", "../conf/s.ini", "items", cwd=work)) == "WRON-001"
    # an empty value or no key configures nothing
    ini(settings, "")
    assert_usage(run("--config", str(settings), "items", cwd=work), "--file")
    ini(settings, None)
    assert_usage(run("--config", str(settings), "items", cwd=work), "--file")


def test_data_dir_precedence_matrix(tmp_path: Path) -> None:
    work = tmp_path / "work"
    data_set(work / "optdir", "opt")
    data_set(work / "envdir", "env")
    data_set(tmp_path / "conf" / "inidir", "ini")
    data_set(work / "inidir", "bad")
    data_set(work / "data", "dflt")
    with_key = ini(tmp_path / "conf" / "with.ini", "inidir")
    without_key = ini(tmp_path / "conf" / "without.ini", None)
    for option in (None, "optdir"):
        for env in (None, "", "envdir"):
            for settings in (None, with_key, without_key):
                args = []
                if settings is not None:
                    args += ["--config", str(settings)]
                if option is not None:
                    args += ["--data-dir", option]
                done = run(
                    *args,
                    "items",
                    env=None if env is None else {"STOCKROOM_DATA_DIR": env},
                    cwd=work,
                )
                case = (option, env, settings and settings.name)
                if option:
                    assert listed_sku(done) == "OPT-001", case
                elif env:
                    assert listed_sku(done) == "ENV-001", case
                elif settings == with_key:
                    assert listed_sku(done) == "INI-001", case
                else:
                    assert_usage(done, "--file")


def test_a_data_dir_that_is_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_text("x", encoding="utf-8")
    assert_error(run("--data-dir", "nowhere", "items", cwd=tmp_path), "nowhere")
    assert_error(run("--data-dir", "plain.txt", "sales", cwd=tmp_path), "plain.txt")
    assert_error(run("stock", env={"STOCKROOM_DATA_DIR": "gone"}, cwd=tmp_path), "gone")
    settings = ini(tmp_path / "conf" / "s.ini", "missing-dir")
    done = run("--config", str(settings), "price", "--sku", "MUG-001", "--quantity", "1")
    assert_error(done, "missing-dir")
    done = run("--data-dir", "nowhere", "stock", "--items", "x.csv", cwd=tmp_path)
    assert_error(done, "nowhere")


def test_missing_inputs_without_a_data_dir_are_usage_errors(tmp_path: Path) -> None:
    assert_usage(run("items", cwd=tmp_path), "--file")
    assert_usage(run("stock", "--items", "a.csv", cwd=tmp_path), "--stock")
    assert_usage(run("stock", "--stock", "a.csv", cwd=tmp_path), "--items")
    assert_usage(run("sales", cwd=tmp_path), "--orders")
    assert_usage(run("price", "--sku", "MUG-001", "--quantity", "1", cwd=tmp_path), "--items")
    # an empty --data-dir is a usage error, as is a missing value
    data_set(tmp_path / "shop", "opt")
    assert_usage(run("--data-dir", "", "items", cwd=tmp_path))
    assert_usage(run("items", "--data-dir", "shop", cwd=tmp_path))
    assert_usage(run("--data-dir", cwd=tmp_path))
    # the built-in default of the setting is not a data directory
    data_set(tmp_path / "data", "dflt")
    assert_usage(run("items", cwd=tmp_path), "--file")


def test_runs_with_explicit_files_are_unchanged() -> None:
    done = run("items", "--file", "data/items.csv")
    assert done.returncode == 0 and len(done.stdout.splitlines()) == 10
    done = run("stock", "--items", "data/items.csv", "--stock", "data/stock.csv")
    assert done.returncode == 0 and "3 item(s) below reorder level" in done.stderr
    done = run("sales", "--orders", "data/orders.csv")
    assert done.stdout.splitlines()[:2] == ["5 orders, 1 cancelled", "Revenue: $149.65"]
    done = run("price", "--items", "data/items.csv", "--sku", "XYZ-999", "--quantity", "1")
    assert done.returncode == 1 and done.stdout == ""
    assert run("items").returncode == 2
    assert run("--version").stdout == "stockroom 0.4.0\n"
