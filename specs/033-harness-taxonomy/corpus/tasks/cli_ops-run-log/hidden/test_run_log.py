from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
ITEMS = str(DATA / "items.csv")
STOCK = str(DATA / "stock.csv")
ORDERS = str(DATA / "orders.csv")


def run(
    *args: str, env: dict[str, str] | None = None, cwd: Path
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("STOCKROOM_")}
    base["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "stockroom", *args],
        cwd=cwd,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=50,
        check=False,
    )


ITEMS_CMD = ["items", "--file", ITEMS]
STOCK_CMD = ["stock", "--items", ITEMS, "--stock", STOCK]
SALES_CMD = ["sales", "--orders", ORDERS]
PRICE_CMD = ["price", "--items", ITEMS, "--sku", "MUG-001", "--quantity", "2"]
BAD_PRICE = ["price", "--items", ITEMS, "--sku", "XYZ-999", "--quantity", "1"]


def read(path: Path) -> list[str]:
    data = path.read_bytes()
    assert data == b"" or data.endswith(b"\n")
    assert b"\r" not in data
    return data.decode("utf-8").splitlines()


def error_message(done: subprocess.CompletedProcess[str]) -> str:
    assert done.returncode == 1
    lines = [ln for ln in done.stderr.splitlines() if ln.startswith("stockroom: error: ")]
    assert len(lines) == 1, done.stderr
    return lines[0][len("stockroom: error: ") :]


def test_each_command_appends_one_line(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    plain = {}
    for name, cmd in (("items", ITEMS_CMD), ("stock", STOCK_CMD), ("sales", SALES_CMD)):
        plain[name] = run(*cmd, cwd=tmp_path)
    plain["price"] = run(*PRICE_CMD, cwd=tmp_path)
    plain["bad"] = run(*BAD_PRICE, cwd=tmp_path)
    assert not log.exists()
    sequence = [
        ("items", ITEMS_CMD),
        ("stock", STOCK_CMD),
        ("sales", SALES_CMD),
        ("price", PRICE_CMD),
        ("bad", BAD_PRICE),
        ("items", ITEMS_CMD + ["--tag", "tea"]),
    ]
    for count, (name, cmd) in enumerate(sequence, start=1):
        done = run("--log-file", str(log), *cmd, cwd=tmp_path)
        if name != "items" or count == 1:
            # the commands' own output and exit codes are unchanged
            assert (done.returncode, done.stdout, done.stderr) == (
                plain[name].returncode,
                plain[name].stdout,
                plain[name].stderr,
            ), name
        assert len(read(log)) == count
    message = error_message(plain["bad"])
    assert "XYZ-999" in message
    assert read(log) == [
        "items exit=0",
        "stock exit=0",
        "sales exit=0",
        "price exit=0",
        f"price exit=1 error={message}",
        "items exit=0",
    ]
    # an existing file is appended to, not truncated
    old = tmp_path / "old.log"
    old.write_text("earlier line\nanother\n", encoding="utf-8")
    run("--log-file", str(old), *PRICE_CMD, cwd=tmp_path)
    assert read(old) == ["earlier line", "another", "price exit=0"]
    # a data error that is not a missing SKU
    odd = tmp_path / "odd.csv"
    odd.write_text("a,b\n", encoding="utf-8")
    done = run("--log-file", str(log), "items", "--file", str(odd), cwd=tmp_path)
    assert read(log)[-1] == f"items exit=1 error={error_message(done)}"
    assert "header" in read(log)[-1]
    # the option may also follow other global options
    done = run("--log-file", str(tmp_path / "b.log"), "--config", str(tmp_path / "none.ini"),
               *ITEMS_CMD, cwd=tmp_path)
    assert done.returncode == 1 and not (tmp_path / "b.log").exists()


def test_log_file_setting_and_precedence(tmp_path: Path) -> None:
    ini = tmp_path / "s.ini"
    ini.write_text(f"[stockroom]\nlog_file = {tmp_path / 'ini.log'}\n", encoding="utf-8")
    from_env = {"STOCKROOM_LOG_FILE": str(tmp_path / "env.log")}
    opt = str(tmp_path / "opt.log")

    done = run("--config", str(ini), *ITEMS_CMD, cwd=tmp_path)
    assert done.returncode == 0
    assert read(tmp_path / "ini.log") == ["items exit=0"]

    run("--config", str(ini), *PRICE_CMD, env=from_env, cwd=tmp_path)
    assert read(tmp_path / "env.log") == ["price exit=0"]
    assert read(tmp_path / "ini.log") == ["items exit=0"]

    run("--config", str(ini), "--log-file", opt, *STOCK_CMD, env=from_env, cwd=tmp_path)
    assert read(tmp_path / "opt.log") == ["stock exit=0"]
    assert read(tmp_path / "env.log") == ["price exit=0"]

    run(*SALES_CMD, env=from_env, cwd=tmp_path)
    assert read(tmp_path / "env.log") == ["price exit=0", "sales exit=0"]

    # an empty value turns logging off, whichever source asked for it
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.log")}
    run("--config", str(ini), "--log-file", "", *ITEMS_CMD, env=from_env, cwd=tmp_path)
    run("--config", str(ini), *ITEMS_CMD, env={"STOCKROOM_LOG_FILE": ""}, cwd=tmp_path)
    empty_ini = tmp_path / "empty.ini"
    empty_ini.write_text("[stockroom]\nlog_file =\n", encoding="utf-8")
    run("--config", str(empty_ini), *ITEMS_CMD, cwd=tmp_path)
    run(*ITEMS_CMD, cwd=tmp_path)
    after = {p.name: p.read_bytes() for p in tmp_path.glob("*.log")}
    assert after == before
    assert sorted(after) == ["env.log", "ini.log", "opt.log"]
    # an environment value beats the INI file even when the INI names another file
    run("--config", str(ini), *ITEMS_CMD, env=from_env, cwd=tmp_path)
    assert read(tmp_path / "env.log")[-1] == "items exit=0"
    assert read(tmp_path / "ini.log") == ["items exit=0"]


def test_runs_that_do_not_reach_a_command_are_not_logged(tmp_path: Path) -> None:
    log = tmp_path / "never.log"
    bad_ini = tmp_path / "bad.ini"
    bad_ini.write_text("[stockroom]\ncolour = blue\n", encoding="utf-8")
    attempts = [
        (["--log-file", str(log), "--version"], {}),
        (["--log-file", str(log), "items"], {}),
        (["--log-file", str(log), "frobnicate"], {}),
        (["--log-file", str(log), "price", "--items", ITEMS, "--sku", "MUG-001"], {}),
        (["--log-file", str(log), "--config", str(bad_ini), *ITEMS_CMD], {}),
        (["--log-file", str(log), "--config", str(tmp_path / "missing.ini"), *ITEMS_CMD], {}),
        (["--log-file", str(log), *ITEMS_CMD], {"STOCKROOM_TAX_BP": "lots"}),
        (["--config", str(bad_ini), *ITEMS_CMD], {"STOCKROOM_LOG_FILE": str(log)}),
    ]
    codes = []
    for args, env in attempts:
        done = run(*args, env=env, cwd=tmp_path)
        codes.append(done.returncode)
    assert codes == [0, 2, 2, 2, 1, 1, 1, 1]
    assert not log.exists()
    assert list(tmp_path.glob("*.log")) == []
    # a run that does reach its command is logged, also with an otherwise empty environment
    done = run("--log-file", str(log), *ITEMS_CMD, cwd=tmp_path)
    assert done.returncode == 0 and read(log) == ["items exit=0"]


def test_unwritable_log_gives_a_warning_only(tmp_path: Path) -> None:
    missing_dir = tmp_path / "no" / "such" / "run.log"
    plain = run(*ITEMS_CMD, cwd=tmp_path)
    done = run("--log-file", str(missing_dir), *ITEMS_CMD, cwd=tmp_path)
    assert done.returncode == 0
    assert done.stdout == plain.stdout
    lines = done.stderr.splitlines()
    assert len(lines) == 1, done.stderr
    prefix = f"stockroom: warning: cannot write log file {missing_dir}: "
    assert lines[0].startswith(prefix) and len(lines[0]) > len(prefix)
    assert not (tmp_path / "no").exists()

    folder = tmp_path / "a-folder"
    folder.mkdir()
    done = run("--log-file", str(folder), *ITEMS_CMD, cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == plain.stdout
    lines = done.stderr.splitlines()
    assert len(lines) == 1 and lines[0].startswith(f"stockroom: warning: cannot write log file {folder}: ")

    # the command's own stderr comes first; the exit code stays 1 for a data error
    bad = run(*BAD_PRICE, cwd=tmp_path)
    done = run("--log-file", str(missing_dir), *BAD_PRICE, cwd=tmp_path)
    assert done.returncode == 1 and done.stdout == ""
    lines = done.stderr.splitlines()
    assert lines[:-1] == bad.stderr.splitlines() and len(lines) == 2
    assert lines[-1].startswith(f"stockroom: warning: cannot write log file {missing_dir}: ")

    # stock keeps its own warning before the log warning
    stock = run(*STOCK_CMD, cwd=tmp_path)
    done = run("--log-file", str(folder), *STOCK_CMD, cwd=tmp_path)
    assert done.returncode == 0 and done.stdout == stock.stdout
    lines = done.stderr.splitlines()
    assert lines[0] == stock.stderr.splitlines()[0] and len(lines) == 2
    assert lines[1].startswith("stockroom: warning: cannot write log file ")

    # the same through the environment
    done = run(*ITEMS_CMD, env={"STOCKROOM_LOG_FILE": str(missing_dir)}, cwd=tmp_path)
    assert done.returncode == 0
    assert done.stderr.startswith(f"stockroom: warning: cannot write log file {missing_dir}: ")


def test_relative_path_and_settings_keys(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    run("--log-file", "rel.log", *ITEMS_CMD, cwd=work)
    run("--log-file", "rel.log", *PRICE_CMD, cwd=work)
    run("--log-file", "rel.log", *SALES_CMD, cwd=other)
    assert read(work / "rel.log") == ["items exit=0", "price exit=0"]
    assert read(other / "rel.log") == ["sales exit=0"]
    # a relative path in the INI file is relative to the current directory, not to the INI file
    ini_dir = tmp_path / "conf"
    ini_dir.mkdir()
    ini = ini_dir / "s.ini"
    ini.write_text("[stockroom]\nlog_file = from-ini.log\ncurrency_symbol = EUR\n", encoding="utf-8")
    done = run("--config", str(ini), *ITEMS_CMD, cwd=work)
    assert done.returncode == 0 and done.stdout.splitlines()[2].endswith("EUR12.50")
    assert read(work / "from-ini.log") == ["items exit=0"]
    assert not (ini_dir / "from-ini.log").exists()
    # the variable takes a relative path too, and the key is not an unknown setting
    done = run(*ITEMS_CMD, env={"STOCKROOM_LOG_FILE": "from-env.log"}, cwd=work)
    assert done.returncode == 0 and done.stderr == ""
    assert read(work / "from-env.log") == ["items exit=0"]
    # names with spaces and non-ASCII text work
    name = "log dir"
    (work / name).mkdir()
    run("--log-file", f"{name}/run log.log", *PRICE_CMD, cwd=work)
    assert read(work / name / "run log.log") == ["price exit=0"]
